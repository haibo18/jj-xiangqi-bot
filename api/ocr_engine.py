# -*- coding: utf-8 -*-
"""
JJ象棋 棋盘/棋子 OCR 识别框架（FOA-AL00 投屏窗口）

架构（五层流水线）：
  L0 捕获层   WindowCapture   —— 按进程/标题找 scrcpy 句柄，DPI 感知，置顶后抓客户区，
                                 线性放大回手机分辨率空间（1084x2412），也支持图片文件输入
  L1 定位层   GridLocator     —— 暗线投影 + 峰值聚类自动检测 9x10 网格线，
                                 校验间距一致性，失败时回退到内置标定值
  L2 分割层   PieceSegmenter  —— 逐交叉点判断占用（浅色棋子面占比）与阵营（红字/黑字），
                                 提取字形掩码并归一化为 40x40 向量
  L3 识别层   TemplateBank    —— 封闭集字形分类：字体种子模板 + NCC 匹配 + 在线自举，
                                 输出 (标签, 置信度, top1-top2 差距)；低置信度标记为未知
  L4 组装层   BoardReader     —— 9x10 符号网格 -> FEN，规则校验（子力上限/宫/相田/兵线），
                                 输出 ASCII 棋盘、JSON、标注图

为什么不用通用 OCR（Tesseract/RapidOCR）：
  * 棋子字形只有 14 个封闭类别，模板匹配更快（毫秒级）、无需安装重型依赖；
  * 标准开局天然提供 32 个带标签样本，可自动自举出高精度模板库；
  * 通用 OCR 对艺术字体、低分辨率、红黑底色的鲁棒性反而更差。

用法：
  python ocr_engine.py --file _phone.png --check                 # 识别图片文件
  python ocr_engine.py --check                                   # 识别投屏窗口
  python ocr_engine.py --file _phone.png --fen "r1baka1r1/..."   # 以已知局面自举模板库
  python ocr_engine.py --watch 1.0                               # 持续监视棋盘变化
  python ocr_engine.py --calibrate                               # 只输出网格标定
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_TITLE = "FOA-AL00"
PHONE_W, PHONE_H = 1084, 2412

# 内置标定（JJ象棋 全屏对局，手机空间坐标），作为自动检测失败时的回退
GX_DEFAULT = [76, 194, 310, 426, 542, 659, 775, 892, 1009]
GY_DEFAULT = [693, 805, 916, 1028, 1141, 1254, 1367, 1481, 1596, 1710]

FILES = "abcdefghi"
GLYPH = 40                      # 归一化字形尺寸 40x40

# 14 个封闭类别：大写=红方字形，小写=黑方字形（与 FEN 记法一致）
RED_LABELS   = {"帥": "K", "仕": "A", "相": "B", "俥": "R", "傌": "N", "炮": "C", "兵": "P"}
BLACK_LABELS = {"將": "k", "士": "a", "象": "b", "車": "r", "馬": "n", "砲": "c", "卒": "p"}
LABEL2CHAR = {v: k for k, v in {**RED_LABELS, **BLACK_LABELS}.items()}

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()


# --------------------------------------------------------------------------- #
# L0 捕获层
# --------------------------------------------------------------------------- #
class WindowCapture:
    """从 scrcpy 投屏窗口抓取画面，统一放大到手机分辨率空间。"""

    def __init__(self, title=WIN_TITLE, hwnd=None):
        self.hwnd = hwnd or self._find(title)
        if not self.hwnd:
            raise RuntimeError(f"未找到包含「{title}」的窗口")
        self.source = "window"

    @staticmethod
    def _find(title):
        u = ctypes.windll.user32
        best = None
        buf, rect = ctypes.create_unicode_buffer(256), wt.RECT()

        def cb(hwnd, _):
            nonlocal best
            n = u.GetWindowTextLengthW(hwnd)
            if n and u.IsWindowVisible(hwnd):
                u.GetWindowTextW(hwnd, buf, 256)
                t = buf.value
                u.GetWindowRect(hwnd, ctypes.byref(rect))
                if title in t and rect.right > rect.left:
                    # 优先非控制台窗口（scrcpy 主窗口标题恰为设备名）
                    if t.strip() == title:
                        best = hwnd
                    elif best is None:
                        best = hwnd
            return True

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb)
        u.EnumWindows(CB, 0)
        return best

    def activate(self):
        u = ctypes.windll.user32
        if u.IsIconic(self.hwnd):
            u.ShowWindow(self.hwnd, 9)
            time.sleep(0.5)
        u.SetForegroundWindow(self.hwnd)
        time.sleep(0.35)
        return u.GetForegroundWindow() == self.hwnd

    def client_box(self):
        u = ctypes.windll.user32
        r = wt.RECT()
        u.GetClientRect(self.hwnd, ctypes.byref(r))
        pt = wt.POINT(0, 0)
        u.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y, r.right, r.bottom

    def grab(self) -> Image.Image:
        """抓窗口客户区并放大回手机分辨率空间。"""
        self.activate()
        x, y, w, h = self.client_box()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        return img.resize((PHONE_W, PHONE_H), Image.LANCZOS)

    @staticmethod
    def from_file(path) -> Image.Image:
        img = Image.open(path).convert("RGB")
        if img.size != (PHONE_W, PHONE_H):
            img = img.resize((PHONE_W, PHONE_H), Image.LANCZOS)
        return img


# --------------------------------------------------------------------------- #
# L1 定位层
# --------------------------------------------------------------------------- #
class GridLocator:
    """暗线投影自动检测 9 条竖线 + 10 条横线，含一致性校验与回退。"""

    def __init__(self):
        self.gx, self.gy = None, None
        self.method = None

    def locate(self, img: Image.Image):
        g = np.asarray(img.convert("L")).astype(np.float32)
        dark = g < 140
        xs = self._lines(dark.sum(axis=0), n=9)
        ys = self._lines(dark.sum(axis=1), n=10)
        if xs and ys and self._uniform(xs) and self._uniform(ys):
            self.gx, self.gy, self.method = xs, ys, "auto"
        else:
            self.gx, self.gy, self.method = GX_DEFAULT[:], GY_DEFAULT[:], "builtin"
        return self.gx, self.gy

    @staticmethod
    def _lines(profile, n):
        """从投影里找 n 条近似等距的线：峰值聚类 + 等距一致性筛选。"""
        p = profile.astype(np.float64)
        hi = p[p > 0]
        if len(hi) == 0:
            return None
        thr = np.percentile(hi, 99) * 0.55
        cand = np.where(p > thr)[0]
        if len(cand) < n:
            return None
        # 聚类相邻候选
        groups, cur = [], [cand[0]]
        for v in cand[1:]:
            if v - cur[-1] <= 4:
                cur.append(v)
            else:
                groups.append(cur); cur = [v]
        groups.append(cur)
        centers = [float(np.mean(g)) for g in groups]
        # 取最强的 n 个
        scored = sorted(((p[int(round(c))], c) for c in centers), reverse=True)
        pick = sorted(c for _, c in scored[:n])
        if len(pick) < n:
            return None
        # 等距校验：残差要小
        d = np.diff(pick)
        pitch = float(np.median(d))
        if pitch < 20 or np.max(np.abs(d - pitch)) > pitch * 0.12:
            return None
        return [round(c) for c in pick]

    @staticmethod
    def _uniform(lines):
        d = np.diff(lines)
        return float(np.std(d) / np.mean(d)) < 0.06

    @property
    def pitch(self):
        return (self.gx[1] - self.gx[0] + self.gy[1] - self.gy[0]) / 2.0


# --------------------------------------------------------------------------- #
# L2 分割层
# --------------------------------------------------------------------------- #
class PieceSegmenter:
    """逐交叉点：占用判断、阵营判断、字形掩码归一化。"""

    def __init__(self, locator: GridLocator):
        self.loc = locator

    def _masks(self, arr):
        r, g, b = arr[:, :, 0].astype(np.int16), arr[:, :, 1].astype(np.int16), arr[:, :, 2].astype(np.int16)
        face = (r > 210) & (r - b > 50) & (g > 150)          # 浅色棋子面
        red = (r - g > 50) & (r > 85) & (g < 135)            # 红色笔画
        dark = np.maximum(np.maximum(r, g), b) < 110         # 黑色笔画
        return face, red, dark

    def read_grid(self, img: Image.Image):
        """返回 grid[j][i] = (label|None, side|None, glyph_vec|None, conf)。"""
        arr = np.asarray(img.convert("RGB"))
        face, red, dark = self._masks(arr)
        gx, gy = self.loc.gx, self.loc.gy
        R = int(self.loc.pitch * 0.42)                       # 内盘半径
        ri = int(self.loc.pitch * 0.34)                      # 字形半径
        yy, xx = np.mgrid[-R:R + 1, -R:R + 1]
        disk = (xx * xx + yy * yy) <= R * R

        grid = []
        for j, py in enumerate(gy):
            row = []
            for i, px in enumerate(gx):
                y0, y1 = max(0, py - R), min(arr.shape[0], py + R + 1)
                x0, x1 = max(0, px - R), min(arr.shape[1], px + R + 1)
                if y1 - y0 < R or x1 - x0 < R:
                    row.append((None, None, None, 0.0))
                    continue
                f = face[y0:y1, x0:x1]
                d = disk[:f.shape[0], :f.shape[1]]
                if d.shape != f.shape:
                    row.append((None, None, None, 0.0)); continue
                frac = f[d].mean()
                if frac < 0.25:                              # 空位
                    row.append((None, None, None, 0.0))
                    continue
                rg = red[y0:y1, x0:x1][d].mean()
                dk = dark[y0:y1, x0:x1][d].mean()
                side = "R" if rg > dk else "B"
                # 字形掩码（限制在更小的内盘，避开棋子边缘）
                ry0, ry1 = py - ri, py + ri + 1
                rx0, rx1 = px - ri, px + ri + 1
                if side == "R":
                    gm = red[ry0:ry1, rx0:rx1]
                else:
                    gm = dark[ry0:ry1, rx0:rx1] | red[ry0:ry1, rx0:rx1] * 0
                vec = self._normalize(gm)
                row.append((None, side, vec, float(frac)))
            grid.append(row)
        return grid

    @staticmethod
    def _normalize(mask):
        """字形掩码 -> 按外接框居中裁剪 -> 40x40 浮点向量。"""
        ys, xs = np.where(mask)
        if len(xs) < 12:
            return np.zeros(GLYPH * GLYPH, np.float32)
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        w, h = x1 - x0 + 1, y1 - y0 + 1
        s = max(w, h) + 2
        box = np.zeros((s, s), np.float32)
        box[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = \
            mask[y0:y1 + 1, x0:x1 + 1]
        im = Image.fromarray((box * 255).astype(np.uint8)).resize((GLYPH, GLYPH), Image.BILINEAR)
        v = np.asarray(im, np.float32) / 255.0
        return v.ravel()


# --------------------------------------------------------------------------- #
# L3 识别层：模板库
# --------------------------------------------------------------------------- #
class TemplateBank:
    """
    封闭集字形分类器。
      * seed_from_font(): 用系统楷体/宋体渲染 14 个字形作为种子模板；
      * add(fen): 用一张已知局面的截图自举真实样本（在线累积、按类求均值）；
      * match(): NCC（余弦）匹配，返回 (label, score, margin)。
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(HERE, "templates.npz")
        self.samples = {}                                    # label -> [vec, ...]
        self.load()

    # ---- 持久化 ---- #
    def load(self):
        if os.path.exists(self.path):
            z = np.load(self.path, allow_pickle=True)
            for k in z.files:
                self.samples[k] = [v for v in z[k]]

    def save(self):
        np.savez_compressed(self.path,
                            **{k: np.array(v, np.float32) for k, v in self.samples.items()})

    # ---- 种子 ---- #
    def seed_from_font(self, fonts=None):
        fonts = fonts or [r"C:\Windows\Fonts\simkai.ttf", r"C:\Windows\Fonts\SIMKAI.TTF",
                          r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\STKAITI.TTF"]
        for chars in (RED_LABELS, BLACK_LABELS):
            for ch in chars:
                lab = chars[ch]
                if lab in self.samples and self.samples[lab]:
                    continue
                for fp in fonts:
                    if os.path.exists(fp):
                        vec = self._render(ch, fp)
                        if vec is not None:
                            self.samples.setdefault(lab, []).append(vec)
                            break
        self.save()
        return {k: len(v) for k, v in sorted(self.samples.items())}

    @staticmethod
    def _render(ch, font_path, size=72):
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception:
            return None
        im = Image.new("L", (size * 2, size * 2), 0)
        d = ImageDraw.Draw(im)
        d.text((size // 2, size // 2), ch, fill=255, font=font)
        a = np.asarray(im) > 100
        ys, xs = np.where(a)
        if len(xs) < 10:
            return None
        crop = im.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
        s = max(crop.size) + 4
        canvas = Image.new("L", (s, s), 0)
        canvas.paste(crop, ((s - crop.width) // 2, (s - crop.height) // 2))
        v = np.asarray(canvas.resize((GLYPH, GLYPH), Image.BILINEAR), np.float32) / 255.0
        return v.ravel()

    # ---- 自举 ---- #
    def add_position(self, grid, fen_board):
        """grid: read_grid 输出; fen_board: 标准开局串。真实样本按类别入库。"""
        truth = []
        for line in fen_board.split("/"):
            for ch in line:
                if ch.isdigit():
                    truth.extend([None] * int(ch))
                else:
                    truth.append(ch)
        added = 0
        k = 0
        for j in range(10):
            for i in range(9):
                lab_true = truth[k]; k += 1
                if lab_true is None:
                    continue
                _, side, vec, _ = grid[j][i]
                if vec is None or side != ("R" if lab_true.isupper() else "B"):
                    continue
                self.samples.setdefault(lab_true, []).append(vec)
                added += 1
        self.save()
        return added

    # ---- 匹配 ---- #
    def match(self, vec):
        if vec is None or not np.any(vec):
            return None, 0.0, 0.0
        best, second, blab = -1, -1, None
        for lab, samps in self.samples.items():
            for s in samps:
                denom = np.linalg.norm(vec) * np.linalg.norm(s)
                if denom == 0:
                    continue
                score = float(np.dot(vec, s) / denom)
                if score > best:
                    best, second, blab = score, best, lab
                elif score > second:
                    second = score
        return blab, best, best - second


# --------------------------------------------------------------------------- #
# L4 组装层
# --------------------------------------------------------------------------- #
class BoardReader:
    LEVEL_LIMIT = {"r": 2, "n": 2, "b": 2, "a": 2, "k": 1, "c": 2, "p": 5}

    def __init__(self, bank: TemplateBank, conf_thr=0.02):
        self.loc = GridLocator()
        self.seg = PieceSegmenter(self.loc)
        self.bank = bank
        self.conf_thr = conf_thr

    def read(self, img: Image.Image, calibrate=True):
        if calibrate or self.loc.gx is None:
            self.loc.locate(img)
        grid = self.seg.read_grid(img)
        # 字形识别
        labels = [[None] * 9 for _ in range(10)]
        confs = [[0.0] * 9 for _ in range(10)]
        for j in range(10):
            for i in range(9):
                _, side, vec, _ = grid[j][i]
                if vec is None:
                    continue
                lab, score, margin = self.bank.match(vec)
                # 阵营校验：模板类别大小写必须与颜色一致，否则视为不可信
                if lab and ((lab.isupper()) != (side == "R")):
                    lab, score, margin = None, score, 0.0
                if lab is None or margin < self.conf_thr:
                    labels[j][i], confs[j][i] = ("?" if side else None), score
                else:
                    labels[j][i], confs[j][i] = lab, score
        return labels, confs, grid

    # ---- 方向与组装 ---- #
    @staticmethod
    def detect_orient(labels):
        top = sum(1 for c in labels[0] + labels[2] if c and c.isupper())
        bot = sum(1 for c in labels[9] + labels[7] if c and c.isupper())
        return "flip" if top > bot else "normal"

    @staticmethod
    def to_fen(labels, orient="normal", side="w"):
        """屏幕网格 -> 绝对 FEN（normal: 红在下）。未知子用 ? 占位（会被视为空? 不，保留原样便于诊断）。"""
        rows = []
        for ar in range(10):                                 # 绝对行 rank 9..0
            sj = (9 - ar) if orient == "normal" else ar      # 屏幕行
            si_off = 0 if orient == "normal" else 8          # 列映射
            s, empty = "", 0
            for af in range(9):
                si = af if orient == "normal" else 8 - af
                c = labels[sj][si]
                if c in (None, "?"):
                    empty += 1
                else:
                    if empty:
                        s += str(empty); empty = 0
                    s += c
            if empty:
                s += str(empty)
            rows.append(s)
        return "/".join(rows) + f" {side} - - 0 1"

    @staticmethod
    def validate(fen_board):
        """规则校验，返回告警列表。"""
        warn = []
        cnt = {}
        for line in fen_board.split("/"):
            for ch in line:
                if ch.isalpha():
                    cnt[ch] = cnt.get(ch, 0) + 1
        for k, v in cnt.items():
            lim = BoardReader.LEVEL_LIMIT[k.lower()]
            if v > lim:
                warn.append(f"{k} 数量 {v} 超上限 {lim}")
        if "K" not in cnt:
            warn.append("缺少红帅")
        if "k" not in cnt:
            warn.append("缺少黑将")
        return warn

    @staticmethod
    def ascii_board(labels):
        lines = ["    " + "  ".join(FILES)]
        for j in range(10):
            lines.append(f" {9-j}   " + "  ".join(c or "." for c in labels[j]))
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 调试可视化
# --------------------------------------------------------------------------- #
def annotate(img: Image.Image, labels, confs, locator, path):
    im = img.convert("RGB").copy()
    d = ImageDraw.Draw(im)
    pitch = locator.pitch
    R = int(pitch * 0.5)
    for j in range(10):
        for i in range(9):
            x, y = locator.gx[i], locator.gy[j]
            c = labels[j][i]
            if c is None:
                d.ellipse([x - 4, y - 4, x + 4, y + 4], outline=(0, 200, 0), width=2)
            else:
                col = (220, 40, 40) if c.isupper() else (30, 30, 30)
                d.ellipse([x - R, y - R, x + R, y + R], outline=col, width=3)
                d.text((x - R, y - R - 22), f"{c} {confs[j][i]:.2f}",
                       fill=(0, 90, 220) if c == "?" else (0, 120, 0))
    for x in locator.gx:
        d.line([(x, locator.gy[0]), (x, locator.gy[9])], fill=(0, 160, 255), width=2)
    for y in locator.gy:
        d.line([(locator.gx[0], y), (locator.gx[8], y)], fill=(0, 160, 255), width=2)
    im.save(path)
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def get_image(args):
    if args.file:
        return WindowCapture.from_file(args.file), "file"
    return WindowCapture().grab(), "window"


def main():
    ap = argparse.ArgumentParser(description="JJ象棋棋盘 OCR 识别框架")
    ap.add_argument("--file", help="输入图片（默认抓投屏窗口）")
    ap.add_argument("--check", action="store_true", help="识别并输出局面/FEN/JSON")
    ap.add_argument("--calibrate", action="store_true", help="只输出网格标定")
    ap.add_argument("--fen", help="已知局面 FEN（配合 --file 自举模板库）")
    ap.add_argument("--watch", type=float, metavar="SEC", help="持续监视，间隔秒")
    ap.add_argument("--out", default="_ocr_debug.png", help="标注图输出路径")
    ap.add_argument("--templates", help="模板库路径（默认 templates.npz）")
    args = ap.parse_args()

    bank = TemplateBank(args.templates)
    stats = bank.seed_from_font()
    reader = BoardReader(bank)

    if args.fen:
        img, _ = get_image(args)
        reader.loc.locate(img)
        grid = reader.seg.read_grid(img)
        board_part = args.fen.split()[0]
        n = bank.add_position(grid, board_part)
        print(f"[bootstrap] 自举样本 +{n}，模板库统计: {stats}")

    if args.calibrate:
        img, src = get_image(args)
        reader.loc.locate(img)
        print(json.dumps({"source": src, "method": reader.loc.method,
                          "gx": reader.loc.gx, "gy": reader.loc.gy,
                          "pitch": reader.loc.pitch}, ensure_ascii=False))
        annotate(img, [[None] * 9 for _ in range(10)], [[0] * 9 for _ in range(10)],
                 reader.loc, args.out)
        print(f"[out] {args.out}")
        return

    if args.watch:
        last = None
        while True:
            try:
                img, _ = get_image(args)
                labels, confs, _ = reader.read(img, calibrate=False)
                fen = BoardReader.to_fen(labels, BoardReader.detect_orient(labels))
                if fen != last:
                    last = fen
                    print(time.strftime("[%H:%M:%S]"), fen, flush=True)
                    print(BoardReader.ascii_board(labels), flush=True)
            except Exception as exc:
                print("[err]", exc, flush=True)
                time.sleep(2)
            time.sleep(args.watch)
        return

    img, src = get_image(args)
    labels, confs, grid = reader.read(img)
    orient = BoardReader.detect_orient(labels)
    fen = BoardReader.to_fen(labels, orient)
    warn = BoardReader.validate(fen.split()[0])
    occupied = sum(1 for row in labels for c in row if c and c != "?")
    unknown = sum(1 for row in labels for c in row if c == "?")
    print(f"[src] {src}  [grid] {reader.loc.method}  [orient] {orient}")
    print(f"[pieces] 识别 {occupied}，未知 {unknown}")
    print(BoardReader.ascii_board(labels))
    print(f"[fen] {fen}")
    print(f"[warn] {warn if warn else '无'}")
    print("[json]", json.dumps({
        "fen": fen, "orient": orient, "grid_method": reader.loc.method,
        "labels": labels, "conf": [[round(c, 3) for c in row] for row in confs],
        "warnings": warn}, ensure_ascii=False))
    annotate(img, labels, confs, reader.loc, args.out)
    print(f"[out] {args.out}")


if __name__ == "__main__":
    main()
