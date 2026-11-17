# -*- coding: utf-8 -*-
"""
watch_moves.py —— FOA-AL00 象棋全自动对弈主控（单文件完整闭环）
================================================================
业务闭环（默认模式）：

  1. PrintWindow 抓取投屏窗口（即使被遮挡/部分覆盖也能拿到完整画面），
     每 --interval 秒轮询棋盘区域指纹，变化时做 90 交点全量采样，
     解析出「哪颗棋从哪走到哪」（如 h2e2）。
  2. 每识别到一步着法就累加进 moves 列表，拼成接口参数：
        http://127.0.0.1:8765/api/bestmove?fen=startpos&moves=h2e2,h9g7&movetime=1000
     取回 bestmove。
  3. 按引擎返回的着法换算屏幕交点坐标，先点起点、隔 --click-gap 秒再点终点
     （间隔防止点不到），回读画面验证走子生效。
  4. 回到第 1 步不断循环，直至「再来一局」结算画面出现（棋局结束）。

调试模式：
    python watch_moves.py --dump            # 只识别打印当前局面后退出
    python watch_moves.py --file x.png      # 用图片调试单帧
    python watch_moves.py --no-click        # 被动监听：只打印着法，不动子

常用参数：
    python watch_moves.py                   # 全自动（默认我方=屏幕下方一方）
    python watch_moves.py --movetime 1500   # 引擎每步思考 1.5 秒
    python watch_moves.py --side top        # 强制我方=屏幕上方一方（默认 auto）
    python watch_moves.py --rematch         # 终局后自动点「再来一局」连续下
    python watch_moves.py --start 15        # 先点大厅「10分钟」按钮开始匹配
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

try:
    import cv2
    import numpy as np
    from PIL import Image, ImageGrab
except Exception as _imp_err:            # 解释器选错/缺依赖时给出可读提示
    print(f"[err] 依赖导入失败：{_imp_err}")
    print(f"[err] 当前解释器：{sys.executable}")
    print(r"[hint] 请使用装有依赖的解释器："
          r"C:\Python315\python.exe watch_moves.py --start 10 --rematch")
    try:
        input("按回车键退出 ...")
    except Exception:
        pass
    raise SystemExit(2)

try:
    from pikafish_local import LocalEngine
    from pikafish_local import fen_sane
except Exception:
    LocalEngine = None
    fen_sane = None

# 控制台编码兜底：默认 cp936 控制台下，个别字符（✓ 等）会让 print 抛
# UnicodeEncodeError 直接把进程带崩，这里统一改成 UTF-8 + 替换不可编码字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
GRID_FILE = r"C:\pikafish\api\象棋坐标系.json"
WIN_TITLE = "FOA-AL00"
REF_W, REF_H = 343, 763
API = "http://127.0.0.1:8765"
START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"
FILES = "abcdefghi"

CN = "一二三四五六七八九"
PIECE_CN = {"K": "帥", "A": "仕", "B": "相", "R": "俥", "N": "傌", "C": "炮", "P": "兵",
            "k": "將", "a": "士", "b": "象", "r": "車", "n": "馬", "c": "砲", "p": "卒"}

MODE_BUTTONS = {"5": (53, 603), "10": (131, 603), "15": (210, 603), "turbo": (288, 603)}
REMATCH_BTN = (167, 671)                 # 「再来一局」按钮中心（343x763 客户区参考系）
REMATCH_BOX = (107, 649, 227, 694)       # 按钮检测区域 x0,y0,x1,y1
POPUP_DISMISS = (171, 145)               # 关闭奖励弹窗的安全空白点（头像下方）
REWARD_CLOSE = (320, 271)                # 「快速升级/段位保护」领奖浮层右上角 ×
PROMO_CLOSE = (299, 201)                 # App 启动推广弹窗右上角 ×（343x763 参考系）
REWARD_CLOSE_BOX = (0.892, 0.338, 0.980, 0.378)   # × 检测区域（w,h 比例）
REWARD_GOLD_BOX = (0.350, 0.544, 0.659, 0.611)    # 金色「领取」按钮检测区域
CONFIRM_BTN = (171, 413)                 # 「比赛已经结束」弹窗的绿色「确定」按钮
CONFIRM_BTN_BOX = (0.379, 0.522, 0.627, 0.564)    # 确定按钮检测区域（w,h 比例）
CONFIRM_CARD_BOX = (0.117, 0.406, 0.875, 0.603)   # 米色对话框卡片检测区域
RANKUP_BACK = (31, 730)                  # 「段位提升」全屏页左下角弯曲箭头返回
RANKUP_BACK_BOX = (0.0, 0.917, 0.233, 1.0)        # 箭头暖色点检测区域
RANKUP_BG_BOX = (0.087, 0.498, 0.904, 0.786)      # 页面亮蓝紫背景检测区域
ADB_CANDIDATES = [
    r"C:\pikafish\api\scrcpy\adb.exe",
    r"C:\Users\17601\Desktop\scrcpy-win64-v5.0.1\adb.exe",
]
ADB_EXE = next((p for p in ADB_CANDIDATES if os.path.isfile(p)), ADB_CANDIDATES[-1])
JJ_PACKAGE = "cn.jj.chess.huawei"               # JJ 象棋包名（App 重启自愈用）

# 实测棋盘几何常量（多局截图回归一致）：343x763 参考系下
# 原点 (22.5,218.5)，列距 37.05px，行距 35.9px。用于校验/兜底标定结果。
NOM_X0, NOM_Y0, NOM_DX, NOM_DY = 22.5, 218.5, 37.05, 35.9


def grid_nominal():
    """实测棋盘几何生成的 90 交点网格 {(col,row): (x,y)}。"""
    return {(c, r): (NOM_X0 + c * NOM_DX, NOM_Y0 + r * NOM_DY)
            for r in range(10) for c in range(9)}

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()


def log(msg, also=None):
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if also is not None:
        also.write(line + "\n")
        also.flush()


# --------------------------------------------------------------------------- #
# 坐标系：读 txt，得到 (col,row) -> {x,y,iccs,...}
# --------------------------------------------------------------------------- #
class CoordSystem:
    def __init__(self, path=GRID_FILE):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.meta = data["meta"]
        self.cells = {}
        for row in data["matrix"]:
            for c in row:
                self.cells[(c["col"], c["row"])] = c
        self.px = {(c["col"], c["row"]): (c["x"], c["y"]) for c in self.cells.values()}
        self.iccs = {(c["col"], c["row"]): c["iccs"] for c in self.cells.values()}

    def name(self, col, row):
        return self.iccs[(col, row)]


# --------------------------------------------------------------------------- #
# 窗口查找 + PrintWindow 抓图（防遮挡）
# --------------------------------------------------------------------------- #
def find_window(title=WIN_TITLE):
    u = ctypes.windll.user32
    hits = []
    buf, rect = ctypes.create_unicode_buffer(300), wt.RECT()

    def cb(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n and u.IsWindowVisible(hwnd):
            u.GetWindowTextW(hwnd, buf, 300)
            if buf.value.strip() == title:
                u.GetWindowRect(hwnd, ctypes.byref(rect))
                hits.append((hwnd, rect.right - rect.left, rect.bottom - rect.top))
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb)
    u.EnumWindows(CB, 0)
    if not hits:
        raise RuntimeError(f"未找到窗口 {title}（scrcpy 投屏是否在运行？）")
    return max(hits, key=lambda t: t[2])[0]


def wait_window(title=WIN_TITLE, timeout=90.0):
    """等 scrcpy 窗口出现（启动脚本刚拉起投屏时窗口有延迟）。"""
    end = time.time() + timeout
    while True:
        try:
            return find_window(title)
        except RuntimeError:
            if time.time() >= end:
                raise
            time.sleep(1.0)


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def grab_window(hwnd) -> Image.Image | None:
    """PrintWindow 抓窗口内容并裁出真正的客户区（被遮挡也能拿到内容）。
    注意：PrintWindow 渲染的是整个窗口（含标题栏/边框），必须按
    GetWindowRect 尺寸取图后再按客户区偏移裁剪，否则整体偏移一个标题栏。
    """
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)
        time.sleep(0.4)
    cr = wt.RECT()
    u.GetClientRect(hwnd, ctypes.byref(cr))
    cw, ch = cr.right, cr.bottom
    if cw <= 0 or ch <= 0:
        return None
    wr = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(wr))
    pt = wt.POINT(0, 0)
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    off_x, off_y = pt.x - wr.left, pt.y - wr.top      # 客户区在窗口内的偏移
    ww, wh = wr.right - wr.left, wr.bottom - wr.top
    img = None
    hdc = u.GetWindowDC(hwnd)
    if hdc:
        mem = g.CreateCompatibleDC(hdc)
        bmp = g.CreateCompatibleBitmap(hdc, ww, wh)
        g.SelectObject(mem, bmp)
        if u.PrintWindow(hwnd, mem, 2):                # PW_RENDERFULLCONTENT
            buf = ctypes.create_string_buffer(ww * wh * 4)
            bi = _BMIH(ctypes.sizeof(_BMIH), ww, -wh, 1, 32, 0, 0, 0, 0, 0, 0)
            if g.GetDIBits(mem, bmp, 0, wh, buf, ctypes.byref(bi), 0):
                full = Image.frombuffer("RGB", (ww, wh), buf.raw, "raw", "BGRX", 0, 1)
                x2, y2 = min(off_x + cw, ww), min(off_y + ch, wh)
                if 0 <= off_x < x2 and 0 <= off_y < y2:
                    img = full.crop((off_x, off_y, x2, y2))
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(hwnd, hdc)
    if img is None:                                    # 兜底：屏幕抓取（怕遮挡）
        img = ImageGrab.grab(bbox=(pt.x, pt.y, pt.x + cw, pt.y + ch), all_screens=True)
    if img.size != (REF_W, REF_H):
        img = img.resize((REF_W, REF_H), Image.LANCZOS)
    return img


# --------------------------------------------------------------------------- #
# 自动标定：圆盘卷积找棋子中心 -> 拟合 9x10 等距网格
# --------------------------------------------------------------------------- #
def calibrate(img: Image.Image):
    arr = np.asarray(img.convert("RGB")).astype(int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    face = ((r > 175) & (r - b > 45) & (g > 105) & (r < 248)).astype(np.float32)
    R = 17
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1]
    kern = ((xx * xx + yy * yy) <= R * R).astype(np.float32)
    kern /= kern.sum()
    score = cv2.filter2D(face, -1, kern)
    peak = (score == cv2.dilate(score, np.ones((17, 17), np.uint8))) & (score > 0.2)
    ys, xs = np.where(peak)
    pts = list(zip(xs.astype(float), ys.astype(float)))
    if len(pts) < 16:
        return None

    centers = []
    for x, y in sorted(pts):
        if all((x - cx) ** 2 + (y - cy) ** 2 > 9 for cx, cy in centers):
            centers.append((x, y))
    if len(centers) < 16:
        return None

    def best(vals, n, lo, hi, plo, phi):
        bestv = (0, None, None)
        v = np.array(vals)
        for pitch in np.arange(plo, phi, 0.25):
            for off in np.arange(lo, hi, 0.5):
                ks = np.round((v - off) / pitch)
                d = np.abs(v - (off + ks * pitch))
                hit = ((d < 5) & (ks >= 0) & (ks <= n - 1)).sum()
                if hit > bestv[0]:
                    bestv = (hit, off, pitch)
        hit, off, pitch = bestv
        ks = np.round((v - off) / pitch)
        m = (np.abs(v - (off + ks * pitch)) < 5) & (ks >= 0) & (ks <= n - 1)
        if m.sum() < 6:
            return None, 0
        A = np.vstack([ks[m], np.ones(m.sum())]).T
        (a, bv), *_ = np.linalg.lstsq(A, v[m], rcond=None)
        return [a * k + bv for k in range(n)], hit

    GX, hx = best([p[0] for p in centers], 9, 12, 40, 30, 42)
    GY, hy = best([p[1] for p in centers], 10, 200, 235, 30, 42)
    if GX is None or GY is None or hx < 18 or hy < 18:
        return None
    for axis in (np.diff(GX), np.diff(GY)):
        if axis.min() < 33 or axis.max() > 41:
            return None
    # 关键防线：拟合结果必须与实测棋盘几何一致，否则一律拒绝。
    # 曾把棋盘外的 UI 圆盘拟进行距 40.5px 的垃圾网格，导致所有点击偏移 30+px。
    if (abs(GX[0] - NOM_X0) > 5 or abs(GY[0] - NOM_Y0) > 5
            or abs((GX[8] - GX[0]) - 8 * NOM_DX) > 8 * 0.8
            or abs((GY[9] - GY[0]) - 9 * NOM_DY) > 9 * 0.8):
        return None
    return GX, GY, hx + hy


# --------------------------------------------------------------------------- #
# 逐交点采样：占用 / 阵营
# --------------------------------------------------------------------------- #
class Sampler:
    """逐交点采样：以交点为期望位置，±SNAP 像素内吸附真实棋子中心。"""

    SNAP = 10
    R = 14

    def __init__(self, coords: CoordSystem, grid=None):
        self.co = coords
        self.grid = grid or coords.px

    def sample(self, img: Image.Image):
        arr = np.asarray(img.convert("RGB")).astype(int)
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        face = ((r > 175) & (r - b > 45) & (g > 105) & (r < 248)).astype(np.float32)
        red = (r - g > 50) & (g < 115) & (r > 85)
        dark = np.maximum(np.maximum(r, g), b) < 110

        yy, xx = np.mgrid[-self.R:self.R + 1, -self.R:self.R + 1]
        kern = ((xx * xx + yy * yy) <= self.R * self.R).astype(np.float32)
        kern /= kern.sum()
        score = cv2.filter2D(face, -1, kern)

        rad = 11
        yy, xx = np.mgrid[-rad:rad + 1, -rad:rad + 1]
        disk = (xx * xx + yy * yy) <= rad * rad

        board = [[None] * 9 for _ in range(10)]
        found = []                                    # (score, col, row, x, y)
        for (col, row), (x0, y0) in self.grid.items():
            x0, y0 = int(round(x0)), int(round(y0))
            ys0, ys1 = max(0, y0 - self.SNAP), min(score.shape[0], y0 + self.SNAP + 1)
            xs0, xs1 = max(0, x0 - self.SNAP), min(score.shape[1], x0 + self.SNAP + 1)
            if ys1 <= ys0 or xs1 <= xs0:
                continue
            win = score[ys0:ys1, xs0:xs1]
            j, i = np.unravel_index(int(np.argmax(win)), win.shape)
            if win[j, i] < 0.30:
                continue
            x, y = xs0 + i, ys0 + j
            # 裁剪窗必须 clamp 到图像内：标定漂移时交点可能贴边，
            # 负索引回绕会让切片尺寸与 disk 掩码不匹配 -> IndexError 崩溃
            y1, x1 = min(score.shape[0], y + rad + 1), min(score.shape[1], x + rad + 1)
            y2, x2 = max(0, y - rad), max(0, x - rad)
            disk_cut = disk[:y1 - y2, :x1 - x2]
            rf = red[y2:y1, x2:x1][disk_cut].mean()
            df = dark[y2:y1, x2:x1][disk_cut].mean()
            found.append((float(win[j, i]), col, row, x, y,
                          ("R" if rf >= df else "B")))
        # 非极大值抑制：同一颗棋子真实中心落在两格点之间时会被重复检测，
        # 按检测中心去重（<22px 视为同一颗），保留响应更高者
        found.sort(key=lambda t: -t[0])
        kept_centers = []
        for sc, col, row, x, y, color in found:
            if any((x - kx) ** 2 + (y - ky) ** 2 < 22 * 22 for kx, ky in kept_centers):
                continue
            kept_centers.append((x, y))
            board[row][col] = (color, 0, 0)
        return board

    def occupy(self, board):
        return [[(c[0] if c else None) for c in row] for row in board]


# --------------------------------------------------------------------------- #
# 差分：上一帧 -> 当前帧（带颜色配对与自走噪声过滤）
# --------------------------------------------------------------------------- #
def diff_moves(prev, cur):
    """返回 [(from(col,row), to(col,row))]；无法唯一确定时返回 []。"""
    vac, fil = [], []
    for row in range(10):
        for col in range(9):
            p, c = prev[row][col], cur[row][col]
            if p and not c:
                vac.append((col, row))
            elif c and (not p or p != c):
                fil.append((col, row))
    if not vac and not fil:
        return []
    if len(vac) != len(fil):
        return []                                   # 数量不匹配 = 识别噪声/瞬态
    if len(vac) == 1:
        v, f = vac[0], fil[0]
        if v == f:                                  # 同格闪烁 = 噪声
            return []
        if cur[f[1]][f[0]] != prev[v[1]][v[0]]:     # 移动子颜色必须一致
            return []
        return [(v, f)]
    return []                                       # 其余交由带合法性校验的候选解析
    # 多处变化：按「颜色一致 + 距离最近」配对
    moves, used = [], set()
    for v in vac:
        vc = prev[v[1]][v[0]]
        cand = [(abs(v[0] - f[0]) + abs(v[1] - f[1]), k) for k, f in enumerate(fil)
                if k not in used and cur[f[1]][f[0]] == vc and f != v]
        if not cand:
            return []
        _, k = min(cand)
        used.add(k)
        moves.append((v, fil[k]))
    return moves


# --------------------------------------------------------------------------- #
# 中文记法 / FEN 换算 / ICCS 换算
# --------------------------------------------------------------------------- #
def chinese_notation(piece, frm, to, red_at_top=True):
    """piece: FEN 字符；frm/to: (col,row) 屏幕坐标。返回中文着法或 None。"""
    if not piece:
        return None
    is_red = piece.isupper()
    fc, fr = frm
    tc, tr = to

    def file_no(col):
        if red_at_top:
            return (col + 1) if is_red else (9 - col)
        return (9 - col) if is_red else (col + 1)

    def num(n):
        return CN[n - 1] if is_red else str(n)

    f_from, f_to = file_no(fc), file_no(tc)
    ch = PIECE_CN[piece]
    if fr == tr:
        return f"{ch}{num(f_from)}平{num(f_to)}"
    if red_at_top:
        forward = (tr > fr) if is_red else (tr < fr)
    else:
        forward = (tr < fr) if is_red else (tr > fr)
    verb = "进" if forward else "退"
    if piece.upper() in "RCPK":
        return f"{ch}{num(f_from)}{verb}{num(abs(tr - fr))}"
    return f"{ch}{num(f_from)}{verb}{num(f_to)}"


def detect_red_top(occ):
    top = sum(1 for row in occ[:3] for c in row if c == "R")
    bot = sum(1 for row in occ[7:] for c in row if c == "R")
    return top >= bot


def to_iccs(sq, red_top):
    """屏幕交点 -> ICCS（UCI：a0=红方左侧角，红先，与 startpos 一致）。"""
    c, r = sq
    return (FILES[8 - c] if red_top else FILES[c]) + (str(r) if red_top else str(9 - r))


def from_iccs(tag, red_top):
    c = FILES.index(tag[0])
    r = int(tag[1])
    return (8 - c, r) if red_top else (c, 9 - r)


def fen_board_screen(fen, red_top):
    """FEN -> 屏幕交点 {(col,row): FEN字符}。
    红在上方（我方执黑，屏幕是从黑方视角看的）：file a 在屏幕右 -> col=8-c，
    rank k 在屏幕第 k 行 -> row=9-i（FEN 第 i 行 = rank 9-i）。
    红在下方（我方执红）：file a 在屏幕左 -> col=c，rank k 在屏幕 9-k 行 -> row=i。
    """
    out = {}
    for i, line in enumerate(fen.split()[0].split("/")):
        c = 0
        for ch in line:
            if ch.isdigit():
                c += int(ch)
            else:
                sc = (8 - c) if red_top else c
                sr = (9 - i) if red_top else i
                out[(sc, sr)] = ch
                c += 1
    return out


def fen_from_screen(pieces, red_top, stm="r"):
    """屏幕坐标棋子表 -> 完整 FEN（含走子方）。用于以画面为准重建局面。"""
    grid = [[None] * 9 for _ in range(10)]        # 按 FEN 行序（0 = 黑底线）
    for (c, r), ch in pieces.items():
        if not ch:
            continue
        if red_top:                                # 屏幕是从黑方视角看的
            grid[9 - r][8 - c] = ch
        else:
            grid[r][c] = ch
    rows = []
    for i in range(10):
        line, empty = [], 0
        for ch in grid[i]:
            if ch:
                if empty:
                    line.append(str(empty))
                    empty = 0
                line.append(ch)
            else:
                empty += 1
        if empty:
            line.append(str(empty))
        rows.append("".join(line))
    return "/".join(rows) + (" w" if stm == "r" else " b") + " - - 0 1"


def colors_of(pieces):
    board = [[None] * 9 for _ in range(10)]
    for (c, r), ch in pieces.items():
        if ch:
            board[r][c] = "R" if ch.isupper() else "B"
    return board


def fen_text(fen):
    return START_FEN if str(fen).strip().lower() in ("startpos", "start", "") else fen


def board_present(occ):
    n = sum(1 for row in occ for c in row if c)
    r = sum(1 for row in occ for c in row if c == "R")
    return 10 <= n <= 32 and r >= 3 and (n - r) >= 3


def move_legal(pieces, frm, to, red_top):
    """轻量象棋走法校验（pieces: 屏幕交点->FEN字符，走子前局面）。
    用于过滤识别瞬态产生的伪着法（如車斜走）。"""
    fc, fr = frm
    tc, tr = to
    if frm == to or not (0 <= tc <= 8 and 0 <= tr <= 9):
        return False
    piece = pieces.get(frm)
    if not piece:
        return False
    target = pieces.get(to)
    if target and target.isupper() == piece.isupper():
        return False                                  # 不能吃己方子
    dc, dr = tc - fc, tr - fr
    red = piece.isupper()
    p = piece.upper()

    def in_palace(c, r):
        # 九宫取决于棋子颜色：红方九宫在 red_top 一侧，黑方在另一侧。
        # 此前只判红方九宫，黑方的士/将在九宫内正常走子会被误判非法，
        # 导致对手支士/出帅时整段解析失败（触发无谓的重标定/重建）
        top = (red == red_top)
        return 3 <= c <= 5 and ((0 <= r <= 2) if top else (7 <= r <= 9))

    def own_side(r):
        if red:
            return (r <= 4) if red_top else (r >= 5)
        return (r >= 5) if red_top else (r <= 4)

    if p in ("R", "K", "C"):                          # 直线子
        if dc != 0 and dr != 0:
            return False
        if p == "K":
            return (abs(dc) + abs(dr) == 1
                    and in_palace(fc, fr) and in_palace(tc, tr))
        screens = 0
        if dc == 0:
            step = 1 if tr > fr else -1
            for r in range(fr + step, tr, step):
                if pieces.get((fc, r)):
                    screens += 1
        else:
            step = 1 if tc > fc else -1
            for c in range(fc + step, tc, step):
                if pieces.get((c, fr)):
                    screens += 1
        if p == "R":
            return screens == 0
        return screens == (1 if target else 0)        # 炮：移动无架、吃子一架
    if p == "N":
        if (abs(dc), abs(dr)) not in ((1, 2), (2, 1)):
            return False
        leg = (fc + dc // 2, fr) if abs(dc) == 2 else (fc, fr + dr // 2)
        return not pieces.get(leg)
    if p == "B":
        if abs(dc) != 2 or abs(dr) != 2:
            return False
        if pieces.get(((fc + tc) // 2, (fr + tr) // 2)):
            return False                              # 塞象眼
        return own_side(tr)                           # 象不过河
    if p == "A":
        return (abs(dc) == 1 and abs(dr) == 1
                and in_palace(fc, fr) and in_palace(tc, tr))
    if p == "P":
        fwd = (1 if red_top else -1) if red else (-1 if red_top else 1)
        if dc == 0 and dr == fwd:
            return True
        return (dr == 0 and abs(dc) == 1 and not own_side(fr))  # 过河后可横走
    return False


def move_shape_ok(pieces, frm, to, red_top):
    """宽松校验：只验证走法「形状」（不校验阻挡与己方占位）。
    用于基线可能已失步的放宽解析，至少挡住形状上根本不可能的变化。"""
    piece = pieces.get(frm)
    if not piece or frm == to:
        return False
    fc, fr = frm
    tc, tr = to
    dc, dr = tc - fc, tr - fr
    red = piece.isupper()
    p = piece.upper()

    def in_palace(c, r):
        # 同 move_legal：九宫按棋子颜色区分（红 red_top 一侧，黑另一侧）
        top = (red == red_top)
        return 3 <= c <= 5 and ((0 <= r <= 2) if top else (7 <= r <= 9))

    def own_side(r):
        if red:
            return (r <= 4) if red_top else (r >= 5)
        return (r >= 5) if red_top else (r <= 4)

    if p in ("R", "C"):
        return dc == 0 or dr == 0
    if p == "K":
        return (abs(dc) + abs(dr) == 1
                and in_palace(fc, fr) and in_palace(tc, tr))
    if p == "N":
        return (abs(dc), abs(dr)) in ((1, 2), (2, 1))
    if p == "B":
        return abs(dc) == 2 and abs(dr) == 2 and own_side(tr)
    if p == "A":
        return (abs(dc) == 1 and abs(dr) == 1
                and in_palace(fc, fr) and in_palace(tc, tr))
    if p == "P":
        fwd = (1 if red_top else -1) if red else (-1 if red_top else 1)
        if dc == 0 and dr == fwd:
            return True
        return dr == 0 and abs(dc) == 1 and not own_side(fr)
    return False


def remap_occ(occ):
    """整盘旋转 180°（列 c->8-c，行 r->9-r）。"""
    return [[occ[9 - r][8 - c] for c in range(9)] for r in range(10)]


def mirror_move(mv):
    """着法 180° 旋转：a1b2 -> i8h7。"""
    return (FILES[8 - FILES.index(mv[0])] + str(9 - int(mv[1]))
            + FILES[8 - FILES.index(mv[2])] + str(9 - int(mv[3])))


def show(occ, title=""):
    if title:
        print(f"  {title}")
    print("      " + " ".join(f"{FILES[8 - i]:>2}" for i in range(9)))
    for row in range(10):
        line = " ".join("●" if occ[row][c] == "R" else "○" if occ[row][c] == "B" else "·"
                        for c in range(9))
        print(f"  {row:2d}  {line}")


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
def api_health(api=API):
    try:
        with urllib.request.urlopen(f"{api}/api/health", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def ensure_server(api=API, spawn=True):
    if api_health(api):
        return None
    if not spawn:
        raise SystemExit("接口服务未运行：python pikafish_server.py --port 8765")
    logf = open(os.path.join(HERE, "_watch_api.log"), "w", encoding="utf-8")
    flags = 0x08000000 if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "pikafish_server.py"),
         "--port", "8765", "--instances", "1", "--threads", "16",
         "--hash", "2048", "--quiet"],
        stdout=logf, stderr=logf, creationflags=flags)
    for _ in range(40):
        time.sleep(0.5)
        if api_health(api):
            log("[api] 已自动拉起 pikafish_server.py（16 线程 / 2048MB 哈希）")
            return proc
    raise SystemExit("接口服务启动失败，详见 C:\\pikafish\\api\\_watch_api.log")


def get_json(url, timeout=120):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode())
            err = data.get("error", {})
            raise RuntimeError(f"接口 HTTP {e.code}: {err.get('message', data)}") from None
        except json.JSONDecodeError:
            raise RuntimeError(f"接口 HTTP {e.code}") from None


# --------------------------------------------------------------------------- #
# 主控
# --------------------------------------------------------------------------- #
class Bot:
    def __init__(self, args):
        self.args = args
        self.api = args.api
        self.coords = CoordSystem()
        self.hwnd = wait_window()
        self.moves = [m.strip().lower() for m in (args.moves or "").split(",") if m.strip()]
        self.base_fen = args.fen or "startpos"
        self.red_top = None
        self.our_color = None          # 'r' / 'b'
        self.pieces = {}               # 屏幕交点 -> FEN 字符
        self.base = None               # 基线占用（最近一次确认的局面）
        self.grid = None
        self.sampler = None
        self._fp = None
        self._last_full = 0.0
        self._seen_game = False
        self._board_lum = None
        self._last_popup_click = 0.0
        self._round = 0
        self._grid_desc = None
        # 失步自检（连吃场景）：连续两帧看到同一组差异格就判真失步
        self._gap_prev = None
        self._gap_hits = 0
        self._gap_try_ts = 0.0
        self._gap_gaveup = None
        self._resync_tries = 0         # 本局重同步次数（每局重置）
        self._clean_moves = 0          # 连续干净着法数（用于衰减重同步配额）
        self._rebuilds = 0             # 本局以屏幕重建局面的次数
        self._parity_off = 0           # 重建局面后着法奇偶偏移（维护走子方）
        self._stalled = 0              # 连续拿不到共识帧的次数（防无限干等）
        self._adb_ok = None
        self._adb_serial = None
        self._adb_devs = []
        self._adb_wh = (1084, 2412)
        self.engine = None
        self.logf = open(args.log, "a", encoding="utf-8")
        if args.engine == "local" and LocalEngine is not None:
            try:
                self.engine = LocalEngine(threads=args.threads or None, hash_mb=args.hash)
                self.engine.warmup(200)
                log(f"[engine] 本地直连 Pikafish（{self.engine.threads} 线程 / "
                    f"{args.hash}MB 哈希），无需 HTTP 接口", self.logf)
            except Exception as exc:
                log(f"[warn] 本地引擎启动失败（{exc}），退回 HTTP 接口", self.logf)
                self.engine = None
        if self.engine is None:
            self.api_proc = ensure_server(self.api, not args.no_spawn)
        if not self.adb_setup():
            log("[warn] adb 不可用，走子重试将只使用鼠标点击", self.logf)

    # ---------- 基础 ---------- #
    def grab(self):
        return grab_window(self.hwnd)

    def sample(self, img=None):
        img = img or self.grab()
        if img is None:
            return None
        return self.sampler.occupy(self.sampler.sample(img))

    def set_grid(self, img):
        grid = None
        src = "标定"
        if not self.args.no_calib:
            cal = calibrate(img)
            if cal:
                GX, GY, hits = cal
                grid = {(c, r): (GX[c], GY[r]) for r in range(10) for c in range(9)}
        if grid is None:
            prev = getattr(self, "grid", None)
            if prev is not None:                   # 沿用上一次的有效网格
                grid, src = prev, "沿用上次"
            else:                                  # 首次且标定失败：实测几何兜底
                grid, src = grid_nominal(), "实测几何"
        desc = (round(grid[(0, 0)][0], 1), round(grid[(0, 0)][1], 1),
                round(grid[(8, 0)][0] - grid[(0, 0)][0], 1),
                round(grid[(0, 9)][1] - grid[(0, 0)][1], 1))
        if desc != self._grid_desc:
            self._grid_desc = desc
            log(f"[calib] 网格（{src}）：原点 ({desc[0]},{desc[1]})，"
                f"列距 {desc[2] / 8:.2f}px，行距 {desc[3] / 9:.2f}px", self.logf)
        self.grid = grid
        self.sampler = Sampler(self.coords, grid)

    def fingerprint(self, img):
        """棋盘区域降采样指纹（~1ms），字节级精确比较。"""
        a = np.asarray(img.convert("L"), dtype=np.int16)
        y0 = int(self.grid[(0, 0)][1]) - 24
        y1 = int(self.grid[(0, 9)][1]) + 24
        x0 = int(self.grid[(0, 0)][0]) - 24
        x1 = int(self.grid[(8, 0)][0]) + 24
        crop = a[max(0, y0):min(REF_H, y1), max(0, x0):min(REF_W, x1)]
        return cv2.resize(crop, (72, 96), interpolation=cv2.INTER_AREA).tobytes()

    def stabilize(self, occ, rounds=4, gap=0.3):
        """连续两次采样一致才认为动画结束。"""
        for _ in range(rounds):
            time.sleep(gap)
            nxt = self.sample()
            if nxt is None:
                continue
            if nxt == occ:
                return occ
            occ = nxt
        return occ

    def settled_occ(self, need=3, gap=0.35, rounds=12):
        """多帧共识采样：连续 need 次全盘识别完全一致才返回该局面。
        连吃动画进行中的帧会缺子（棋子在滑出/淡出，起点终点都采不到），
        这类瞬态帧在 1 秒窗口内不会重复出现，永远凑不满 need 次共识。
        严格共识拿不到时退回「逐格多数表决」：某一格被选中高亮圈/轨迹
        染色反复抖动时，整盘永远不可能连续 3 帧完全相同，旧实现会一直
        返回 None，最后把还能下的棋判成 desync 弃局。逐格表决能容忍单格
        闪烁，而真动画期间格子在移动、投票必然分散，仍然返回 None。"""
        last, hits = None, 0
        frames = []
        for _ in range(rounds):
            occ = self.sample()
            if occ is not None and board_present(occ):
                frames.append(occ)
                if occ == last:
                    hits += 1
                else:
                    last, hits = occ, 1
                if hits >= need:
                    return last
            else:
                hits = 0
                last = None                 # 无效帧后不能沿用旧基线凑次数
            time.sleep(gap)
        return self._vote_occ(frames)

    @staticmethod
    def _vote_occ(frames):
        """逐格多数表决生成共识局面。只有 90 个格子**全部**拿到 >=60%
        的多数值才采信；任何一格摇摆不定（真动画/大面积浮层）就返回 None，
        绝不拿拼接出来的坏帧去修复局面。"""
        n = len(frames)
        if n < 5:
            return None
        need = (n * 3 + 4) // 5             # ceil(0.6 * n)
        out = [[None] * 9 for _ in range(10)]
        for r in range(10):
            for c in range(9):
                cnt = {}
                for f in frames:
                    v = f[r][c]
                    cnt[v] = cnt.get(v, 0) + 1
                val, k = max(cnt.items(), key=lambda kv: kv[1])
                if k < need:
                    return None
                out[r][c] = val
        return out if board_present(out) else None

    # ---------- 终局检测：「再来一局」按钮 ---------- #
    def _button_visible(self, img):
        """「再来一局」按钮检测（亮度自适应）。
        结算时 App 会把画面压暗约一半，固定阈值会失灵，
        故只用颜色比例关系（绿-蓝、红-蓝差值）。"""
        a = np.asarray(img.convert("RGB")).astype(int)
        h, w, _ = a.shape
        band = a[int(h * 0.845):int(h * 0.925), int(w * 0.2):int(w * 0.8)]
        r, g, b = band[:, :, 0], band[:, :, 1], band[:, :, 2]
        m = (g - b > 30) & (r - b > 25) & (g > 50) & (g >= r * 0.8)
        return m.mean() > 0.12

    def settle_popup_visible(self, img):
        """「结算奖励/争分夺秒」弹窗：棋盘中部被压暗 + 底部有绿色按钮。
        （正常对局/大厅中部亮度 ~140-160，弹窗压暗后 ~55-65）"""
        a = np.asarray(img.convert("RGB")).astype(int)
        h, w, _ = a.shape
        lum = a[int(h * 0.38):int(h * 0.55),
                int(w * 0.15):int(w * 0.85)].mean()
        return lum < 100 and self._button_visible(img)

    def reward_popup_visible(self, img):
        """「快速升级/段位保护」领奖浮层：右上角暖白 × + 中部金色「领取」按钮。
        两特征同时成立才判定（正常对局/结算画面/大厅均不同时满足）。"""
        a = np.asarray(img.convert("RGB")).astype(int)
        h, w, _ = a.shape
        x0, y0, x1, y1 = REWARD_CLOSE_BOX
        box = a[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        r, g, b = box[:, :, 0], box[:, :, 1], box[:, :, 2]
        close_n = int(((r > 170) & (g > 150) & (b > 110) & (r - b < 80)).sum())
        x0, y0, x1, y1 = REWARD_GOLD_BOX
        box = a[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        r, g, b = box[:, :, 0], box[:, :, 1], box[:, :, 2]
        gold = float(((r > 170) & (g > 110) & (b < 150) & (r > g) & (g >= b)).mean())
        if close_n < 20 or gold < 0.25:
            return False
        # 反向排除：浮层卡片会盖住棋盘中部，棋子面占比必然低（<0.07）；
        # 结算画面棋盘裸露、28 颗棋子全可见（占比 ~0.093），其橙黄棋子
        # 同样会凑齐 × 和金色两特征，不排除就会误判成"浮层未消失"无限点 ×
        band = a[int(h * 0.13):int(h * 0.73), int(w * 0.03):int(w * 0.98)]
        r, g, b = band[:, :, 0], band[:, :, 1], band[:, :, 2]
        face = float(((r > 175) & (r - b > 45) & (g > 105) & (r < 248)).mean())
        return face < 0.07

    def close_reward_popup(self):
        """点击领奖浮层右上角 ×，并等待浮层消失。"""
        try:
            if not self.activate():
                log("[warn] 窗口未能置顶", self.logf)
            self.tap_point(*REWARD_CLOSE)
        except RuntimeError as exc:
            log(f"[warn] {exc}", self.logf)
        for _ in range(8):
            time.sleep(0.8)
            img = self.grab()
            if img is None or not self.reward_popup_visible(img):
                log("[fix] 领奖浮层已通过右上角 × 关闭", self.logf)
                return True
        log("[warn] 点击 × 后领奖浮层仍未消失", self.logf)
        return False

    def promo_popup_visible(self, img):
        """App 冷启动/重启后的运营推广弹窗：深色背景 + 中部橙色「立即查看」。
        实测：弹窗 dark=0.85/orange=0.28；大厅 0.37/0.00、对局 0.31/0.01。"""
        a = np.asarray(img.convert("RGB")).astype(int)
        h, w, _ = a.shape
        r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        if float((lum < 70).mean()) < 0.55:
            return False
        band = a[int(h * 0.60):int(h * 0.68), int(w * 0.30):int(w * 0.70)]
        br, bg, bb = band[:, :, 0], band[:, :, 1], band[:, :, 2]
        orange = ((br > 190) & (bg > 90) & (bg < 190)
                  & (bb < 130) & (br - bb > 90))
        return float(orange.mean()) > 0.20

    def close_promo_popup(self):
        """点击启动推广弹窗右上角 ×，并等待弹窗消失。"""
        try:
            if not self.activate():
                log("[warn] 窗口未能置顶", self.logf)
            self.tap_point(*PROMO_CLOSE)
        except RuntimeError as exc:
            log(f"[warn] {exc}", self.logf)
        for _ in range(6):
            time.sleep(0.8)
            img = self.grab()
            if img is None or not self.promo_popup_visible(img):
                log("[fix] 启动推广弹窗已通过右上角 × 关闭", self.logf)
                return True
        log("[warn] 点击 × 后启动推广弹窗仍未消失", self.logf)
        return False

    def confirm_popup_visible(self, img):
        """「比赛已经结束，点击确认返回大厅」弹窗：
        中部大片米色卡片 + 卡片下沿绿色「确定」按钮，两特征同时成立。
        实测：弹窗 green=850/beige=0.58；对局/大厅/领奖浮层均 <30/<0.05。"""
        a = np.asarray(img.convert("RGB")).astype(int)
        h, w, _ = a.shape
        x0, y0, x1, y1 = CONFIRM_BTN_BOX
        box = a[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        r, g, b = box[:, :, 0], box[:, :, 1], box[:, :, 2]
        green = int(((g - b > 30) & (g - r > 15) & (g > 90) & (g < 200)).sum())
        x0, y0, x1, y1 = CONFIRM_CARD_BOX
        box = a[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        r, g, b = box[:, :, 0], box[:, :, 1], box[:, :, 2]
        beige = float(((r > 195) & (g > 180) & (b > 140)
                       & (abs(r - g) < 35) & (r - b > 25) & (r - b < 90)).mean())
        return green >= 300 and beige >= 0.35

    def close_confirm_popup(self):
        """点击「确定」关闭比赛结束弹窗，并等待其消失。"""
        try:
            if not self.activate():
                log("[warn] 窗口未能置顶", self.logf)
            self.tap_point(*CONFIRM_BTN)
        except RuntimeError as exc:
            log(f"[warn] {exc}", self.logf)
        for _ in range(8):
            time.sleep(0.8)
            img = self.grab()
            if img is None or not self.confirm_popup_visible(img):
                log("[fix] 比赛结束弹窗已通过「确定」关闭", self.logf)
                return True
        log("[warn] 点击「确定」后弹窗仍未消失", self.logf)
        return False

    def rankup_page_visible(self, img):
        """「段位提升」全屏展示页：上部大片亮蓝紫云雾背景 + 左下角暖色箭头。
        实测：该页 bg=0.86/arrow=168；大厅 bg=0.44、对局 bg=0.48、
        结算/弹窗 bg=0.00，双条件互不误触。"""
        a = np.asarray(img.convert("RGB")).astype(int)
        h, w, _ = a.shape
        x0, y0, x1, y1 = RANKUP_BACK_BOX
        box = a[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        r, g, b = box[:, :, 0], box[:, :, 1], box[:, :, 2]
        arrow = int(((r > 110) & (r - b > 18) & (g > 85) & (g < b + 90)).sum())
        x0, y0, x1, y1 = RANKUP_BG_BOX
        box = a[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
        r, g, b = box[:, :, 0], box[:, :, 1], box[:, :, 2]
        bg = float(((b > 140) & (b >= r + 10) & (g > 110)).mean())
        return arrow >= 100 and bg >= 0.70

    def close_rankup_page(self):
        """点击左下角弯曲箭头返回，退出段位提升页。"""
        try:
            if not self.activate():
                log("[warn] 窗口未能置顶", self.logf)
            self.tap_point(*RANKUP_BACK)
        except RuntimeError as exc:
            log(f"[warn] {exc}", self.logf)
        for _ in range(8):
            time.sleep(0.8)
            img = self.grab()
            if img is None or not self.rankup_page_visible(img):
                log("[fix] 段位提升页已通过左下角返回箭头退出", self.logf)
                return True
        log("[warn] 点击返回箭头后段位提升页仍未消失", self.logf)
        return False

    def tap_point(self, gx, gy):
        """点击参考系坐标：鼠标优先，被其他窗口遮挡时 adb 直点。"""
        try:
            self.click_ref(gx, gy)
        except RuntimeError as exc:
            if not self.adb_setup():
                raise
            log(f"[fix] {exc} —— 改用 adb 直点", self.logf)
            w, h = self._adb_wh
            x = int(round(gx * w / REF_W))
            y = int(round(gy * h / REF_H))
            flags = 0x08000000 if os.name == "nt" else 0
            subprocess.run([ADB_EXE, "-s", self._adb_serial, "shell", "input",
                            "tap", str(x), str(y)],
                           timeout=15, creationflags=flags)

    def dismiss_and_rematch(self):
        """关掉结算奖励弹窗（点任意空白处），再点「再来一局」。
        若领奖浮层叠在结算画面上，先点其右上角 ×。"""
        try:
            img = self.grab()
            if img is not None and self.reward_popup_visible(img):
                self.close_reward_popup()
            if not self.activate():
                log("[warn] 窗口未能置顶", self.logf)
            self.tap_point(*POPUP_DISMISS)     # 弹窗点任意处即关闭
            time.sleep(1.2)
            self.tap_point(*REMATCH_BTN)
        except RuntimeError as exc:
            log(f"[warn] {exc}", self.logf)
        time.sleep(2.0)

    def over_confirm(self):
        for _ in range(2):
            img = self.grab()
            if img is None or not self._button_visible(img):
                return False
            time.sleep(1.2)
        return True

    def board_gone_confirm(self, need=4, gap=0.6):
        """「棋盘消失」必须连续多帧都确认才算对局结束。
        吃子/将军/结算过渡动画、淡出遮罩都会让单帧识别不到棋盘，
        单帧判死会把正在进行的对局误判成结束（11:31:02 那次就是）。
        只有结算按钮真的出现，或连续 need 帧（约 2 秒）都无棋盘才认。"""
        for k in range(need):
            img = self.grab()
            if img is None:
                return False
            if self._button_visible(img):
                return True
            occ = self.sample(img)
            if occ is not None and board_present(occ):
                return False
            if k + 1 < need:
                time.sleep(gap)
        return True

    # ---------- 点击 ---------- #
    def activate(self):
        u = ctypes.windll.user32
        if u.IsIconic(self.hwnd):
            u.ShowWindow(self.hwnd, 9)
            time.sleep(0.4)
        for _ in range(4):
            u.keybd_event(0x12, 0, 0, 0)
            u.SetForegroundWindow(self.hwnd)
            u.keybd_event(0x12, 0, 2, 0)
            time.sleep(0.15)
            if u.GetForegroundWindow() == self.hwnd:
                return True
            try:
                u.SwitchToThisWindow(self.hwnd, True)
            except Exception:
                pass
            time.sleep(0.25)
            if u.GetForegroundWindow() == self.hwnd:
                return True
        return False

    def click_ref(self, gx, gy):
        x, y, w, h = self.client()
        sx = int(round(x + gx * w / REF_W))
        sy = int(round(y + gy * h / REF_H))
        pt = wt.POINT(sx, sy)
        u = ctypes.windll.user32
        top = u.WindowFromPoint(pt)
        if top:
            top = u.GetAncestor(top, 2)                # GA_ROOT
        if top != self.hwnd:
            self.activate()
            time.sleep(0.15)
            top = u.WindowFromPoint(pt)
            top = u.GetAncestor(top, 2) if top else None
            if top != self.hwnd:
                raise RuntimeError(f"点击点({sx},{sy})被其他窗口遮挡，拒绝盲点")
        u.SetCursorPos(sx, sy)
        time.sleep(0.06)
        u.mouse_event(0x0002, 0, 0, 0, 0)              # LEFTDOWN
        time.sleep(0.05)
        u.mouse_event(0x0004, 0, 0, 0, 0)              # LEFTUP

    def client(self):
        r = wt.RECT()
        u = ctypes.windll.user32
        u.GetClientRect(self.hwnd, ctypes.byref(r))
        pt = wt.POINT(0, 0)
        u.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y, r.right, r.bottom

    def click_cell(self, sq):
        # 贴边列（a/i）鼠标点击常落在棋盘边框上被 App 忽略：adb 直点更可靠
        if sq[0] in (0, 8) and self.adb_setup():
            self.adb_cell(sq)
            return
        try:
            self.click_ref(*self.grid[sq])
        except RuntimeError as exc:
            # 鼠标点击被其他窗口遮挡：adb 直点不受窗口遮挡影响
            if self.adb_setup():
                log(f"[fix] {exc} —— 改用 adb 直点", self.logf)
                self.adb_cell(sq)
            else:
                raise

    # ---------- adb 直点（绕过窗口焦点/边缘误触问题） ---------- #
    def _adb_list(self):
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            out = subprocess.run([ADB_EXE, "devices"], capture_output=True,
                                 text=True, timeout=15,
                                 creationflags=flags).stdout
        except Exception:
            return []
        return [l.split("\t")[0] for l in out.splitlines()
                if "\t" in l and l.strip().endswith("device")]

    def adb_setup(self):
        if self._adb_ok is not None:
            return self._adb_ok
        self._adb_ok = False
        if not os.path.isfile(ADB_EXE):
            return False
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            devs = self._adb_list()
            if not devs:
                return False
            # 同时存在 USB 与无线两个条目时，挑真正能应答的那个（防止死连接）
            self._adb_devs = devs
            picked = None
            for d in devs:
                try:
                    r = subprocess.run([ADB_EXE, "-s", d, "shell", "echo", "ok"],
                                       capture_output=True, text=True, timeout=8,
                                       creationflags=flags)
                    if r.returncode == 0 and "ok" in (r.stdout or ""):
                        picked = d
                        break
                except Exception:
                    continue
            self._adb_serial = picked or devs[0]
            out = subprocess.run([ADB_EXE, "-s", self._adb_serial,
                                  "shell", "wm", "size"], capture_output=True,
                                 text=True, timeout=15, creationflags=flags).stdout
            for line in out.splitlines():
                if "size" in line.lower() and ":" in line:
                    w, h = line.split(":")[-1].strip().split("x")
                    self._adb_wh = (int(w), int(h))
                    break
            self._adb_ok = True
            log(f"[adb] 直点就绪 serial={self._adb_serial} 分辨率={self._adb_wh}"
                f" 设备={devs}", self.logf)
        except Exception as exc:
            log(f"[warn] adb 初始化失败: {exc}", self.logf)
        return self._adb_ok

    def restart_jj_app(self):
        """通过 adb 强制重启 JJ 象棋 App：卡死/残局棋盘/异常界面的最终自愈手段。"""
        if not self.adb_setup() or not self._adb_serial:
            log("[warn] adb 不可用，无法重启 App 自愈", self.logf)
            return False
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            subprocess.run([ADB_EXE, "-s", self._adb_serial, "shell",
                            "am", "force-stop", JJ_PACKAGE],
                           capture_output=True, timeout=20, creationflags=flags)
            time.sleep(2.0)
            subprocess.run([ADB_EXE, "-s", self._adb_serial, "shell",
                            "monkey", "-p", JJ_PACKAGE,
                            "-c", "android.intent.category.LAUNCHER", "1"],
                           capture_output=True, timeout=20, creationflags=flags)
            log("[fix] JJ 象棋已强制重启，等待回到大厅 ...", self.logf)
            time.sleep(8.0)
            return True
        except Exception as exc:
            log(f"[warn] 重启 JJ 象棋失败: {exc}", self.logf)
            return False

    def adb_cell(self, sq, jitter=False):
        gx, gy = self.grid[sq]
        jx, jy = (3, -2) if jitter else (0, 0)
        w, h = self._adb_wh
        x = int(round((gx + jx) * w / REF_W))
        y = int(round((gy + jy) * h / REF_H))
        flags = 0x08000000 if os.name == "nt" else 0
        cmd = [ADB_EXE, "-s", self._adb_serial, "shell", "input",
               "tap", str(x), str(y)]
        try:
            r = subprocess.run(cmd, timeout=15, creationflags=flags,
                               capture_output=True)
            if r.returncode == 0:
                return
        except Exception:
            pass
        # 无线/USB 中某一个掉线：换一个仍在线且能应答的 serial 重试一次
        for d in self._adb_list():
            if d == self._adb_serial:
                continue
            try:
                r = subprocess.run([ADB_EXE, "-s", d, "shell", "input", "tap",
                                    str(x), str(y)], timeout=15,
                                   creationflags=flags, capture_output=True)
                if r.returncode == 0:
                    log(f"[adb] serial 切换 {self._adb_serial} -> {d}", self.logf)
                    self._adb_serial = d
                    return
            except Exception:
                continue

    # ---------- 点击走子：先确认选中，再点终点 ---------- #
    def square_changed(self, img_a, img_b, sq, half=18, thresh=6.0):
        """棋盘上 sq 交点附近区域是否发生变化（选中高亮/棋子增减）。"""
        if img_a is None or img_b is None:
            return False
        ax, ay = int(round(self.grid[sq][0])), int(round(self.grid[sq][1]))
        ya0, ya1 = max(0, ay - half), ay + half
        xa0, xa1 = max(0, ax - half), ax + half
        ca = np.asarray(img_a.convert("L"), dtype=np.int16)[ya0:ya1, xa0:xa1]
        cb = np.asarray(img_b.convert("L"), dtype=np.int16)[ya0:ya1, xa0:xa1]
        if ca.size == 0 or ca.shape != cb.shape:
            return False
        return float(np.abs(ca - cb).mean()) > thresh

    def select_source(self, sq, attempt):
        """点击起点并确认出现选中高亮。返回所用方式 'mouse'/'adb'，失败 None。"""
        edge = sq[0] in (0, 8)          # a/i 贴边列：鼠标点击常落空，adb 优先
        if edge or attempt > 1:
            ways = [("adb", False), ("adb", True), ("mouse", False)]
        else:
            ways = [("mouse", False), ("adb", False), ("adb", True)]
        base_img = self.grab()
        for way, jit in ways:
            if way == "adb" and not self.adb_setup():
                continue
            try:
                if way == "mouse":
                    self.click_cell(sq)
                else:
                    self.adb_cell(sq, jitter=jit)
            except Exception as exc:
                log(f"[warn] 点击起点失败（{way}）: {exc}", self.logf)
                continue
            time.sleep(0.45)
            img = self.grab()
            if self.square_changed(base_img, img, sq):
                return way
        return None

    def _try_move(self, pre, frm, to, expected, window=6.0):
        """抢救用完整走子：清选中态 -> 选起点 -> 点终点 -> 等生效。
        返回 (accepted, occ, pairs)。"""
        # 上一次尝试可能把起点留在"已选中"状态：先点空白处取消，
        # 否则下一次点起点会变成"取消选中"，点终点自然无效
        try:
            self.tap_point(*POPUP_DISMISS)
        except RuntimeError:
            pass
        time.sleep(0.6)
        for attempt in (1, 2):
            way = self.select_source(frm, attempt)
            if way is None:
                time.sleep(0.4)
                continue
            try:
                if way == "mouse":
                    self.click_cell(to)
                else:
                    self.adb_cell(to, jitter=attempt > 1)
            except Exception:
                continue
            occ, pairs = self.wait_move_applied(pre, frm, to, expected,
                                                window=window)
            if occ is not None:
                return True, occ, pairs
            # 起点可能仍处于选中态，下一轮前再清一次
            try:
                self.tap_point(*POPUP_DISMISS)
            except RuntimeError:
                pass
            time.sleep(0.6)
        return False, None, []

    def _rescue_move(self, pre, frm, to, expected, mv):
        """走子点击未生效后的顽强恢复：清浮层/对话框 -> 重标定 -> 重试。
        返回 (accepted, final_occ, pairs_all)；对局已结束返回 ("over",...)。"""
        for rescue in range(1, 4):
            img = self.grab()
            if img is not None:
                if self.reward_popup_visible(img):
                    log(f"[fix] 走子无效时发现领奖浮层，先关闭（抢救 {rescue}/3）",
                        self.logf)
                    self.close_reward_popup()
                    continue
                if self.confirm_popup_visible(img):
                    log(f"[fix] 走子无效时发现比赛结束弹窗，点「确定」"
                        f"（抢救 {rescue}/3）", self.logf)
                    self.close_confirm_popup()
                    continue
                if self.over_confirm():
                    log("[end] 检测到结算画面 —— 对局结束", self.logf)
                    return "over", None, []
                if self.settle_popup_visible(img):
                    log("[fix] 走子无效时发现结算弹窗 —— 视为对局结束", self.logf)
                    self.dismiss_and_rematch()
                    return "over", None, []
            # 网格可能漂移：重新标定，并检查走子是否其实已生效
            img = self.grab()
            if img is not None:
                self.set_grid(img)
                frm = from_iccs(mv[:2], self.red_top)
                to = from_iccs(mv[2:4], self.red_top)
                occ = self.sample(img)
                if occ is not None and occ == expected:
                    log("[fix] 抢救时发现走子实际已生效", self.logf)
                    return True, occ, [(frm, to)]
            ok, occ, pairs = self._try_move(pre, frm, to, expected, window=10.0)
            if ok:
                log(f"[fix] 抢救第 {rescue} 轮走子成功", self.logf)
                return True, occ, pairs
        # 抢救失败：留一张现场截图方便事后诊断
        try:
            stamp = datetime.datetime.now().strftime("%H%M%S")
            fail_img = self.grab()
            if fail_img is not None:
                fail_img.save(os.path.join(HERE, f"_fail_{stamp}.png"))
                log(f"[warn] 已保存现场截图 _fail_{stamp}.png", self.logf)
        except Exception:
            pass
        return False, None, []

    def wait_move_applied(self, pre, frm, to, expected, window=6.0):
        """等待走子在画面上生效；返回 (局面, 差分对) 或 (None, None)。"""
        deadline = time.time() + window
        while time.time() < deadline:
            occ = self.sample()
            if occ is None:
                continue
            if occ == expected:
                return occ, [(frm, to)]
            # 走子已执行的直接证据：起点变空 + 终点变成走子方的子。
            # 吃子高亮/选中残影常让个别棋子颜色被误读，diff_moves 会因
            # vac/fil 数量不匹配整体放弃 —— 之前就在这里把已生效的走子
            # 误判成失败，白白重试、触发重建（点击"未生效"假象的根源）
            if (occ[frm[1]][frm[0]] is None
                    and occ[to[1]][to[0]] == expected[to[1]][to[0]]):
                occ = self.stabilize(occ)
                if (occ[frm[1]][frm[0]] is None
                        and occ[to[1]][to[0]] == expected[to[1]][to[0]]):
                    return occ, [(frm, to)]
            # 兜底证据：起点变空 + 终点有子（任意颜色）—— 我方回合内对方
            # 棋子不可能移动，静止的我方棋子面检测也不会无端读成空，所以
            # 起点读空只能是走子已生效。注意终点可能是任何状态：我方刚落
            # 的子（可能被轨迹高亮染成对方颜色）、甚至对手在验证窗口内
            # 已经完成的反吃（如我方吃車后对手立刻用傌反吃）——这些都不
            # 改变「起点已空 = 走子已生效」的事实；对手的应手着法会在
            # 下一轮 opp_turn 由「起点变空 + 合法走法」正常解析吸收
            if (occ[frm[1]][frm[0]] is None
                    and occ[to[1]][to[0]] is not None):
                occ = self.stabilize(occ)
                if (occ[frm[1]][frm[0]] is None
                        and occ[to[1]][to[0]] is not None):
                    log("[fix] 起点已空（对手可能已应手），认定我方走子生效",
                        self.logf)
                    return occ, [(frm, to)]
            if occ != pre:
                occ = self.stabilize(occ)
                pairs = diff_moves(pre, occ)
                if pairs and (frm, to) in pairs:
                    return occ, pairs
            time.sleep(0.15)
        return None, None

    def migrate_state(self):
        """画面整盘旋转 180° 后，把跟踪世界一起旋转，保持与屏幕一致。"""
        self._flips = getattr(self, "_flips", 0) + 1
        self.red_top = not self.red_top
        self.moves = [mirror_move(m) for m in self.moves]
        self.pieces = {(8 - c, 9 - r): p for (c, r), p in self.pieces.items()}
        if self.base:
            self.base = remap_occ(self.base)
        self._fp = None

    # ---------- 引擎 ---------- #
    def ask_engine(self, timeout=None):
        """默认本地直连引擎；--engine http 时走 GET /api/bestmove。"""
        if self.engine is not None:
            try:
                return self.engine.bestmove(moves=self.moves,
                                            movetime=self.args.movetime,
                                            fen=self.base_fen)
            except ValueError as exc:
                raise RuntimeError(f"着法序列非法（与棋盘失步）: {exc}") from None
        url = (f"{self.api}/api/bestmove?fen="
               + urllib.parse.quote(self.base_fen))
        if self.moves:
            url += "&moves=" + urllib.parse.quote(",".join(self.moves), safe=",")
        url += f"&movetime={self.args.movetime}"
        to = timeout or (self.args.movetime / 1000.0 + 30)
        data = get_json(url, timeout=to)
        if "error" in data:
            err = data["error"]
            raise RuntimeError(f"引擎拒绝请求: {err.get('message', err)}")
        return data

    # ---------- 对局准备 ---------- #
    def wait_game(self):
        """等一局可下的棋（标准开局或与 --fen/--moves 一致），返回 (img, occ)。"""
        t0 = time.time()
        self._wg_since = time.time()
        # 每局重新计时：_nostd_since 是跨局保留的实例属性，只靠「棋盘消失」
        # 清零是不够的。上一局结束后结算页仍显示残局棋盘，若不清零，本局
        # 一开始就会被误判成「非标准开局已卡 120s」而强杀 App（实战中每局
        # 结束后都白重启一次，白白损失一分多钟）。
        self._nostd_since = 0
        warned = False
        # 有 --rematch 且无种子着法时：可能停在被放弃的死局上，
        # 要等到 App 超时判负弹出结算，等待上限放宽到 700s
        limit = self.args.board_wait
        if self.args.rematch and not (self.moves or self.args.fen):
            limit = max(limit, 700.0)
        while True:
            img = self.grab()
            if img is None:
                time.sleep(1.0)
                continue
            if self.sampler is None:
                self.set_grid(img)
            occ = self.sample(img)
            # 总兜底：进入等待超过 180s 仍未开局 —— 无论卡在哪种画面
            # （按钮循环/非标准棋盘/未知界面/检测误报），重启 App 强制
            # 恢复。重启后可能弹出推广浮层，由下面的分支处理
            if time.time() - getattr(self, "_wg_since", t0) > 180:
                log("[fix] 等待开局超过 180s，强制重启 JJ 象棋自愈", self.logf)
                self.restart_jj_app()
                self._wg_since = time.time()
                self._nostd_since = 0
                t0 = time.time()
                warned = False
                self._last_start_click = 0
                continue
            # 「快速升级/段位保护」领奖浮层：点右上角 × 关闭（优先于结算弹窗处理）
            if (self.args.rematch and self.reward_popup_visible(img)
                    and time.time() - self._last_popup_click > 4):
                self._last_popup_click = time.time()
                log("[hall] 领奖浮层出现：点击右上角 × 关闭", self.logf)
                self.close_reward_popup()
                continue
            if (self.args.rematch and self.confirm_popup_visible(img)
                    and time.time() - self._last_popup_click > 4):
                self._last_popup_click = time.time()
                log("[hall] 比赛结束弹窗：点击「确定」返回", self.logf)
                self.close_confirm_popup()
                continue
            if (self.args.rematch and self.rankup_page_visible(img)
                    and time.time() - self._last_popup_click > 4):
                self._last_popup_click = time.time()
                log("[hall] 段位提升页出现：点击左下角返回箭头", self.logf)
                self.close_rankup_page()
                continue
            # App 冷启动/重启自愈后的运营推广弹窗（不限 rematch 模式：
            # 重启自愈本身就可能发生在任何模式下）
            if (self.promo_popup_visible(img)
                    and time.time() - self._last_popup_click > 4):
                self._last_popup_click = time.time()
                log("[hall] 启动推广弹窗：点击右上角 × 关闭", self.logf)
                self.close_promo_popup()
                continue
            # 结算奖励弹窗（棋盘中部被压暗+按钮可见）：点空白关闭后再开新局
            # 冷启动也可触发：弹窗检测要求棋盘区被压暗，大厅是亮屏不会误触
            if (self.args.rematch and self.settle_popup_visible(img)
                    and time.time() - self._last_popup_click > 4):
                self._last_popup_click = time.time()
                log("[hall] 结算奖励弹窗：点空白处关闭后再点「再来一局」", self.logf)
                self.dismiss_and_rematch()
                continue
            # 结算页有两种版式：全尺寸棋盘（网格采样有效）与缩小棋盘
            # （采样网格对不上、board_present 必为 False）。按钮检测本身
            # 只看底部绿色横带，已经足够特异，不能再叠加 board_present
            # 条件，否则缩小棋盘版式永远点不到「再来一局」。
            if (self.args.rematch and self._button_visible(img)
                    and (self._seen_game or self._round == 0)):
                log("[hall] 检测到「再来一局」，自动点击", self.logf)
                try:
                    if not self.activate():
                        log("[warn] 窗口未能置顶", self.logf)
                    self.click_ref(*REMATCH_BTN)
                except RuntimeError as exc:
                    log(f"[warn] {exc}", self.logf)
                time.sleep(2.5)
                continue
            if not (occ and board_present(occ)):
                self._nostd_since = 0              # 棋盘消失=计时作废，重新开始算
            if occ and board_present(occ):
                self.red_top = detect_red_top(occ)
                expected = colors_of(self.expected_pieces())
                if self.moves or self.args.fen:
                    # 种子模式：两种视角都试，取与画面匹配最好者；
                    # 差 >4 格说明是换边/重连动画等瞬态，等待画面稳定
                    bd, brt = 99, self.red_top
                    for rt in (True, False):
                        self.red_top = rt
                        pred = colors_of(self.expected_pieces())
                        d = sum(1 for r in range(10) for c in range(9)
                                if pred[r][c] != occ[r][c])
                        if d < bd:
                            bd, brt = d, rt
                    self.red_top = brt
                    if bd <= 4:
                        return img, occ
                    if not warned:
                        warned = True
                        log(f"[warn] 画面与 --fen/--moves 推演差 {bd} 格，"
                            "等待画面稳定 ...", self.logf)
                        show(occ, "当前局面：")
                elif occ == expected:
                    return img, occ
                else:
                    if not warned:
                        warned = True
                        log("[status] 已检测到棋盘但不是标准开局，等待新一局 ...",
                            self.logf)
                        show(occ, "当前局面：")
                    # 重启自愈：非标准开局棋盘（App 断线恢复的残局/异常
                    # 局面）卡超过 120s 且没有按钮可点 —— App 重启后
                    # 强杀回大厅，由后面的兜底重新匹配，保证不永久卡死
                    ns = getattr(self, "_nostd_since", 0)
                    if not ns:
                        self._nostd_since = time.time()
                    elif time.time() - ns > 120:
                        log("[fix] 非标准开局棋盘卡超过 120s，"
                            "强制重启 JJ 象棋自愈", self.logf)
                        self.restart_jj_app()
                        self._nostd_since = 0
                        self._last_start_click = 0
                        t0 = time.time()
                        warned = False
                # 卡在结算画面太久：关弹窗后点「再来一局」打破僵局
                if (self.args.rematch and self._button_visible(img)
                        and time.time() - t0 > 45):
                    log("[hall] 长时间停在结算画面，点击「再来一局」", self.logf)
                    if self.settle_popup_visible(img):
                        self.dismiss_and_rematch()
                    else:
                        try:
                            if not self.activate():
                                log("[warn] 窗口未能置顶", self.logf)
                            self.tap_point(*REMATCH_BTN)
                        except RuntimeError as exc:
                            log(f"[warn] {exc}", self.logf)
                        time.sleep(2.5)
            elif (self.args.rematch and self._button_visible(img)
                  and time.time() - t0 > 45):
                # 缩小棋盘结算页走不到上面的 board_present 分支，
                # 45 秒兜底在这里补一份（上面的自动点击若一直没生效也靠它）
                log("[hall] 长时间停在结算画面（缩小棋盘版式），点击「再来一局」",
                    self.logf)
                try:
                    if not self.activate():
                        log("[warn] 窗口未能置顶", self.logf)
                    self.tap_point(*REMATCH_BTN)
                except RuntimeError as exc:
                    log(f"[warn] {exc}", self.logf)
                time.sleep(2.5)
            if time.time() - t0 > limit:
                return None, None
            # 兜底：等待超过 90s 仍没进入对局（大厅/误识别画面都算），
            # 重新点模式按钮开始匹配。旧逻辑写在「有棋盘」分支内，
            # 大厅画面（board_present=False）永远走不到，只能干等 300s 超时。
            if (self.args.start and time.time() - t0 > 90
                    and time.time() - getattr(self, "_last_start_click", 0) > 45):
                self._last_start_click = time.time()
                log("[hall] 等待超过 90s 无进展，重新点击模式按钮开始匹配", self.logf)
                self.click_start()
                t0 = time.time()
            time.sleep(1.5)

    def expected_pieces(self):
        pieces = fen_board_screen(fen_text(self.base_fen), self.red_top or False)
        for mv in self.moves:
            f = from_iccs(mv[:2], self.red_top)
            t = from_iccs(mv[2:4], self.red_top)
            pieces[t] = pieces.pop(f, None) or pieces.get(t, "")
        return pieces

    # ---------- 失步重同步 ---------- #
    MAX_RESYNC = 8                     # 每局重同步配额（连吃多的对局用得上）

    def _enumerate_tails(self, board, side, depth, target=None):
        """从 board 局面出发，枚举后续 depth 步（由 side 先走，双方交替）
        的合法着法组合，返回 [(tail, pieces)]。
        给出 target（画面占用）时做剪枝：每步必须动到「当前推演与画面不一致」
        的格子 —— 连吃失步的错着法一定涉及这些格子，深度 3 也只需枚举几百种。"""
        results = []
        cells = [(c, r) for r in range(10) for c in range(9)]

        def mism(bd):
            pred = colors_of(bd)
            return {cells[i] for i in range(90)
                    if pred[cells[i][1]][cells[i][0]] != target[cells[i][1]][cells[i][0]]}

        def rec(bd, side, tail, d, mm):
            if d == depth:
                if target is None or not mm:
                    results.append((tail[:], dict(bd)))
                return
            want_red = (side == "r")
            for frm, p in list(bd.items()):
                if not p or p.isupper() != want_red:
                    continue
                for to in cells:
                    if to == frm:
                        continue
                    if target is not None and frm not in mm and to not in mm:
                        continue               # 与画面差异无关的着法：不可能是错着
                    if not move_legal(bd, frm, to, self.red_top):
                        continue
                    cap = bd.get(to)
                    bd[to] = bd.pop(frm)
                    tail.append(to_iccs(frm, self.red_top)
                                + to_iccs(to, self.red_top))
                    rec(bd, "b" if want_red else "r", tail, d + 1,
                        mism(bd) if target is not None else set())
                    tail.pop()
                    bd[frm] = bd.pop(to)
                    if cap is not None:
                        bd[to] = cap

        rec(board, side, [], 0,
            mism(board) if target is not None else set())
        return results

    def _replay_head(self, moves):
        """从 base_fen 重放着法，返回屏幕坐标棋子表；非法序列返回 None。"""
        board = fen_board_screen(fen_text(self.base_fen), self.red_top or False)
        try:
            for mv in moves:
                f = from_iccs(mv[:2], self.red_top)
                t = from_iccs(mv[2:4], self.red_top)
                if f not in board:
                    return None
                board[t] = board.pop(f)
        except Exception:
            return None
        return board

    def resync_moves(self, max_back=2, occ=None, force=False):
        """连吃/恢复解析会累积识别伪差，导致内部局面与真实棋盘失步，
        引擎着法被 App 判非法（点起点选不中/点终点不走）。
        这里枚举替换最近 max_back 步着法，找出能**严格**解释当前画面的
        组合（多解时不猜），成功则修正 moves/pieces/base。"""
        if not force and self._resync_tries >= self.MAX_RESYNC:
            return False
        if self._resync_tries >= self.MAX_RESYNC + 4:
            return False                            # 连 force 也不能无限重试
        if occ is None:
            # 屏幕真相必须来自共识画面：单帧可能正处连吃动画缺子，
            # 拿它枚举会凑出带错误吃子的假历史（失步的另一种来源）
            occ = self.settled_occ()
            if occ is None:
                log("[warn] 重同步放弃：画面迟迟无法稳定（动画/浮层进行中）",
                    self.logf)
                return False
        if occ is None or not board_present(occ):
            return False
        n = len(self.moves)
        if n == 0:
            return False
        best = None                                 # (changed, tail, pieces)
        for k in range(1, max_back + 1):
            if k > n:
                break
            old_tail = self.moves[n - k:]
            board0 = self._replay_head(self.moves[:n - k])
            if board0 is None:
                return False
            found = []
            # 先剪枝搜索（快），无解再退回全枚举（深 2 以内才做，代价可接受）
            side = self._side_after(n - k)
            for target in (occ, None):
                if target is None and k > 2:
                    break
                found = self._enumerate_tails(board0, side, k, target)
                if found:
                    break
            for tail, pieces in found:
                pred = colors_of(pieces)
                diff = sum(1 for r in range(10) for c in range(9)
                           if pred[r][c] != occ[r][c])
                if diff != 0:
                    continue
                changed = sum(1 for a, b in zip(tail, old_tail) if a != b)
                cand = (changed, tail, pieces)
                if best is None or cand[:2] < best[:2]:
                    best = cand
            if best is not None:
                break                               # 浅层已有完美解
        if best is None:
            return False
        changed, tail, pieces = best
        old_tail = self.moves[n - len(tail):]
        if tail == old_tail:
            log("[fix] 重同步：着法无误、画面与推演一致 —— 失步另有原因",
                self.logf)
            return False
        self._resync_tries += 1
        log(f"[fix] 重同步成功（本局第 {self._resync_tries}/{self.MAX_RESYNC} 次，"
            f"回溯 {len(tail)} 步）：{','.join(old_tail)}"
            f" -> {','.join(tail)}", self.logf)
        self.moves = self.moves[:n - len(tail)] + tail
        self.pieces = pieces
        self.base = occ
        self._fp = None
        self._gap_prev, self._gap_hits = None, 0
        return True

    # ---------- 连吃失步自检 ---------- #
    def _sync_gap(self, occ):
        """内部推演 vs 画面：返回不一致的格子集合。"""
        pred = colors_of(self.pieces)
        return {(c, r) for r in range(10) for c in range(9)
                if pred[r][c] != occ[r][c]}

    def _sync_watchdog(self, occ=None):
        """连吃之后主动体检：内部局面和真实棋盘是否已经悄悄分叉。
        连续两次（且画面确实静止）都看到同一组差异格 -> 判定真失步，
        立刻以画面为准回溯修正，避免引擎着法被 App 判非法后整局报废。"""
        if occ is None:
            occ = self.sample()
        if occ is None or not board_present(occ):
            return False
        diffs = self._sync_gap(occ)
        if not diffs:
            self._gap_prev, self._gap_hits = None, 0
            self._gap_gaveup = None
            return False
        if diffs == self._gap_gaveup:
            return False                           # 同一组差异已确认救不回来（识别噪声）
        if diffs == self._gap_prev:
            self._gap_hits += 1
        else:
            self._gap_prev, self._gap_hits = diffs, 1
        if self._gap_hits < 2:
            return False                           # 第一次看到：可能是动画/瞬态
        if time.time() - self._gap_try_ts < 6.0:
            return False                           # 冷却，避免反复空搜
        # 终检：差异必须扛过两帧、间隔 0.4s 的复核 —— 连吃动画中途的
        # 缺子帧与内部推演也"差 N 格"，但它撑不过二次复核
        time.sleep(0.4)
        occ2 = self.sample()
        if occ2 is None or occ2 != occ:
            self._gap_prev, self._gap_hits = None, 0
            return False                           # 画面还在变：等它稳住再说
        time.sleep(0.4)
        occ3 = self.sample()
        if occ3 is None or occ3 != occ:
            self._gap_prev, self._gap_hits = None, 0
            return False
        self._gap_try_ts = time.time()
        # 纯「原地变色」差异（每个差异格两侧都有子、只有颜色不同）：
        # 象棋规则里棋子颜色绝不改变，这类差异不可能由任何着法序列产生，
        # 只能是 App 走子轨迹/选中高亮圈把棋子染色造成的识别伪差。
        # 回溯和重建都救不了它（重建反而会把真子换成幻影子），
        # 正确做法是忽略并记住，等高亮圈随下一着消失后差分自然归零。
        if all(self.pieces.get(sq) and occ[sq[1]][sq[0]] for sq in diffs):
            log(f"[warn] 自检差异全是原地变色（高亮圈伪差，"
                f"{'/'.join(to_iccs(s, self.red_top) for s in sorted(diffs))}），忽略",
                self.logf)
            self._gap_gaveup = diffs
            self._gap_prev, self._gap_hits = None, 0
            return False
        log(f"[fix] 自检：内部局面与画面差 {len(diffs)} 格"
            f"（{'/'.join(to_iccs(s, self.red_top) for s in sorted(diffs))}），"
            f"以画面为准重同步 ...", self.logf)
        if self.resync_moves(max_back=2, occ=occ, force=True):
            self._clean_moves = 0
            return True
        # 差异格少时多半是单点识别噪声，交给正常解析；格数多则值得深挖一步
        if len(diffs) >= 3 and self.resync_moves(max_back=3, occ=occ, force=True):
            self._clean_moves = 0
            return True
        log("[warn] 自检未找到能解释画面的着法组合，改用屏幕重建局面 ...",
            self.logf)
        self._gap_gaveup = diffs
        self._gap_prev, self._gap_hits = None, 0
        # 差异已扛过 3 帧共识，说明内部局面确实错了（不是瞬态噪声）；
        # 此时绝不能再按错误棋盘走子（会去点空格选不中，整局报废），
        # 直接以屏幕为真相重建 FEN，让引擎在正确局面上继续下。
        if self.rebuild_from_screen(occ, self.side_to_move(), reason="自检失步"):
            self._clean_moves = 0
            return True
        return False

    # ---------- 终极恢复：以屏幕为准重建局面 ---------- #
    def rebuild_from_screen(self, occ, stm, reason=""):
        """深于回溯上限的失步（误记着法太早、棋子数都对不上）的最后手段：
        直接承认屏幕是真相，把内部棋子按「同色就近配对」迁移到画面占位上，
        配不上的内部子视为已被吃，屏幕多出的格子继承剩余内部类型。
        之后引擎基于重建的 FEN 继续下，不再纠结历史着法对不对。"""
        if self._rebuilds >= 3:
            return False                            # 每局限 3 次，防识别劣化死循环
        if not board_present(occ):
            return False
        # 屏幕真相稳定性二次验证。旧逻辑要求「连续两帧与传入 occ 逐格
        # 完全相同」，只要有一格被高亮圈/轨迹染色周期性抖动就永远拒绝，
        # 最后把还能下的棋判成 desync 弃局。改为用多帧共识复核：共识拿不到
        # （真动画）仍拒绝；共识与依据差 <=2 格才采信，并以共识为准重建。
        chk = self.settled_occ(need=3, gap=0.3, rounds=9)
        if chk is None:
            log("[wait] 重建放弃：画面迟迟无法稳定（可能仍在动画中），再等等",
                self.logf)
            return False
        d = sum(1 for r in range(10) for c in range(9)
                if chk[r][c] != occ[r][c])
        if d > 2:
            log(f"[wait] 重建放弃：复核共识与依据差 {d} 格，等稳定后重试",
                self.logf)
            return False
        occ = chk                                   # 以共识画面为最终真相
        scr = {"R": [], "B": []}
        for r in range(10):
            for c in range(9):
                if occ[r][c]:
                    scr[occ[r][c]].append((c, r))
        if len(scr["R"]) < 2 or len(scr["B"]) < 2 or not board_present(occ):
            return False
        new_pieces, lost, migrated = {}, [], 0
        for color in ("R", "B"):
            squares = scr[color]
            inner = [(sq, ch) for sq, ch in self.pieces.items()
                     if ch and (ch.isupper() == (color == "R"))]
            # 全配对按曼哈顿距离贪心就近迁移（棋子不会瞬移，就近即最可信）
            cand = []
            for a, ch in inner:
                for b in squares:
                    cand.append((abs(a[0] - b[0]) + abs(a[1] - b[1]), a, b, ch))
            cand.sort(key=lambda t: t[0])
            used_a, used_b = set(), set()
            for d, a, b, ch in cand:
                if a in used_a or b in used_b:
                    continue
                used_a.add(a)
                used_b.add(b)
                new_pieces[b] = ch
            for a, ch in inner:
                if a not in used_a:
                    lost.append(ch)
            spare = [ch for a, ch in inner if a not in used_a]
            for b in squares:                        # 屏幕多出的格子：继承剩余类型
                if b not in used_b:
                    new_pieces[b] = spare.pop() if spare else \
                        ("P" if color == "R" else "p")
            migrated += len(used_a)
        fen = fen_from_screen(new_pieces, self.red_top or False, stm)
        if fen_sane is not None and not fen_sane(fen):
            # 重建结果本身非法（画面误读造成双将/对脸等）：发给引擎会把它
            # 带崩，宁可放弃重建（让上层走 err 收局），也不能污染引擎
            log("[fix] 重建出的 FEN 体检未过（画面误读），放弃重建", self.logf)
            return False
        self._rebuilds += 1
        log(f"[fix] 以屏幕为准重建局面（本局第 {self._rebuilds}/3 次，{reason}）："
            f"迁移 {migrated} 子、按画面弃 {len(lost)} 子"
            f"（{''.join(PIECE_CN.get(x, x) for x in lost) or '无'}），"
            f"轮到{'红' if stm == 'r' else '黑'}", self.logf)
        show(occ, "重建依据（屏幕）：")
        self.base_fen = fen
        self.moves = []
        self._parity_off = 0 if stm == "r" else 1
        self.pieces = new_pieces
        self.base = occ
        self._fp = None
        self._gap_prev = self._gap_hits = self._gap_gaveup = None
        self._gap_try_ts = 0.0
        return True

    def setup(self, img, occ):
        self.set_grid(img)
        if not (self.moves or self.args.fen) or self.red_top is None:
            self.red_top = detect_red_top(occ)
        if self.args.side == "top":
            self.our_color = "r" if self.red_top else "b"
        elif self.args.side == "bottom":
            self.our_color = "b" if self.red_top else "r"
        else:                                          # auto：默认屏幕下方一方
            self.our_color = "b" if self.red_top else "r"
        self.pieces = self.expected_pieces()
        self._seen_game = True
        self._resync_tries = 0                      # 每局重置重同步次数
        self._clean_moves = 0
        self._rebuilds = 0
        self._parity_off = 0
        self._stalled = 0
        self._gap_prev, self._gap_hits = None, 0
        self._gap_try_ts = 0.0
        self._gap_gaveup = None
        pred = colors_of(self.pieces)
        diff = sum(1 for r in range(10) for c in range(9) if pred[r][c] != occ[r][c])
        if diff and (self.moves or self.args.fen):
            log(f"[warn] FEN 推演与画面差 {diff} 格", self.logf)
            show(occ, "屏幕识别：")
            show(pred, "FEN 推演：")
        self.base = occ
        self._fp = self.fingerprint(self.grab())
        self._last_full = time.time()
        self._board_lum = self._board_luma(img)
        who = "红" if self.our_color == "r" else "黑"
        pos = "上" if (self.our_color == "r") == self.red_top else "下"
        log(f"[game] 开始跟踪：红在{'上' if self.red_top else '下'}方，"
            f"我方执{who}（屏幕{pos}方），已走 {len(self.moves)} 步", self.logf)

    # ---------- 走子方 ---------- #
    def side_to_move(self):
        """当前轮到谁（重建局面后用奇偶偏移维护，正常对局等价于步数奇偶）。"""
        return "r" if (len(self.moves) + self._parity_off) % 2 == 0 else "b"

    def _side_after(self, n):
        """走完 n 步后轮到谁。"""
        return "r" if (n + self._parity_off) % 2 == 0 else "b"

    # ---------- 我方走子 ---------- #
    def our_turn(self):
        stm = self.side_to_move()
        # 连吃之后先体检一次：内部局面若已与真实棋盘分叉，
        # 引擎给的着法会被 App 判非法（点了没反应），必须在问引擎前修正
        if self._sync_watchdog():
            log("[fix] 已按画面修正局面，重新思考走子", self.logf)
            return True
        res = None
        for attempt in (1, 2):                         # 引擎偶发崩溃自动重试一次
            try:
                res = self.ask_engine()
                break
            except RuntimeError as exc:
                if attempt == 1 and ("500" in str(exc) or "503" in str(exc)
                                     or "引擎" in str(exc)):
                    log(f"[warn] 接口临时错误（{exc}），3s 后重试", self.logf)
                    time.sleep(3.0)
                    continue
                # 引擎嫌着法序列非法：几乎都是连吃造成的内部失步，
                # 以画面为准修正后重来，而不是直接报废这一局
                log(f"[warn] {exc} —— 尝试以画面为准重同步后重下", self.logf)
                if self.resync_moves(max_back=2, force=True) \
                        or self.resync_moves(max_back=3, force=True):
                    log("[fix] 局面已重同步，重新思考走子", self.logf)
                    return True
                # 重同步救不回来（多半是内部局面坏到引擎直接崩）：
                # 最后再给一次屏幕重建的机会，而不是直接报废这一局
                occ = self.sample()
                if occ is not None and self.rebuild_from_screen(
                        occ, stm, "引擎进程异常退出"):
                    return True
                log(f"[err] {exc} —— 着法序列与棋盘失步，无法恢复", self.logf)
                return "err"
            except ValueError as exc:
                # FEN 体检未过：内部局面/重建结果非法，以屏幕为准重建
                log(f"[warn] {exc}", self.logf)
                occ = self.sample()
                if occ is not None and self.rebuild_from_screen(
                        occ, stm, "FEN 体检未过"):
                    return True
                return "err"
            except Exception as exc:
                log(f"[err] 接口请求失败: {exc}", self.logf)
                return "err"
        mv = (res.get("bestmove") or "").strip()
        if not mv or res.get("game_over"):
            log("[end] 引擎判定无着可走（被将死/困毙），对局结束", self.logf)
            return "over"
        frm = from_iccs(mv[:2], self.red_top)
        to = from_iccs(mv[2:4], self.red_top)
        piece = self.pieces.get(frm)
        if not piece or piece.isupper() != (stm == "r"):
            log(f"[err] 引擎着法 {mv} 起点无我方棋子（内部局面已失步），"
                f"尝试恢复 ...", self.logf)
            if self.resync_moves(max_back=2, force=True) \
                    or self.resync_moves(max_back=3, force=True):
                log("[fix] 局面已重同步，重新思考走子", self.logf)
                return True
            # 回溯救不回来（失步太深）：以屏幕为准重建局面
            occ = self.sample()
            if occ is not None and self.rebuild_from_screen(
                    occ, stm, "引擎起点无我方棋子"):
                return True
            show(self.base, "当前屏幕识别：")
            return "desync"
        cn = chinese_notation(piece, frm, to, self.red_top)
        exp = dict(self.pieces)
        exp.pop(frm, None)
        exp[to] = piece
        expected = colors_of(exp)
        pre = self.base

        if not self.activate():
            log("[warn] 窗口未能置顶，点击可能无效", self.logf)
        sc = res.get("score") or {}
        sct = f"{sc.get('type','')} {sc.get('value','')}".strip() if sc else "?"
        log(f"[go] 引擎着法 {mv}  {cn or ''}  score={sct} depth={res.get('depth')}", self.logf)

        accepted = False
        final_occ = None
        pairs_all = []
        last_way = "mouse"
        for attempt in range(1, self.args.click_retry + 1):
            if attempt > 1:
                # 上次失败后起点多半还处于"已选中"态：再点起点=取消选中，
                # 点终点自然无效 —— 先点空白处清除选中态再重来
                try:
                    self.tap_point(*POPUP_DISMISS)
                except RuntimeError:
                    pass
                time.sleep(0.5)
            way = self.select_source(frm, attempt)
            if way is None:
                log(f"[warn] 第 {attempt} 次点击起点未能选中棋子，重试", self.logf)
                time.sleep(0.4)
                continue
            last_way = way
            log(f"[go] 已选中起点（{way}），点击终点", self.logf)
            try:
                if way == "mouse":
                    self.click_cell(to)
                else:
                    self.adb_cell(to, jitter=attempt > 1)
            except Exception as exc:
                log(f"[warn] 点击终点失败: {exc}", self.logf)
                time.sleep(0.3)
                continue
            final_occ, pairs_all = self.wait_move_applied(pre, frm, to, expected)
            if final_occ is not None:
                accepted = True
                break
            log(f"[warn] 第 {attempt} 次点击走子未见生效，重试", self.logf)
            time.sleep(0.3)

        if not accepted and last_way == "mouse" and self.adb_setup():
            # 鼠标点击全部失败（边缘列/焦点问题）：改用 adb 直点整套流程
            log("[fix] 鼠标点击无效，改用 adb 直点", self.logf)
            try:                                   # 先清掉可能的选中态
                self.tap_point(*POPUP_DISMISS)
            except RuntimeError:
                pass
            time.sleep(0.5)
            way = self.select_source(frm, 2)
            if way:
                self.adb_cell(to, jitter=True)
                final_occ, pairs_all = self.wait_move_applied(pre, frm, to, expected)
                accepted = final_occ is not None

        if not accepted:
            # 最终恢复检查：走子可能实际已生效（只是验证窗口内没等到）
            time.sleep(1.0)
            occ = self.sample()
            if occ is not None and occ != pre:
                if occ == remap_occ(pre):
                    # 画面整盘旋转（对手断线重连等触发换边渲染）
                    self.migrate_state()
                    self.base = occ
                    if getattr(self, "_flips", 0) <= 6:
                        log("[fix] 检测到视图旋转 180°，已迁移跟踪，重试走子", self.logf)
                        return "flip"
                occ = self.stabilize(occ)
                pairs = diff_moves(pre, occ)
                if len(pairs) == 1 and pairs[0] == (frm, to):
                    accepted, final_occ, pairs_all = True, occ, pairs
                    log("[fix] 迟到确认：走子实际已生效", self.logf)
        if not accepted:
            # 恢复轮：可能是网格漂移导致点击偏移 —— 重新标定后整体重试
            img = self.grab()
            if img is not None:
                self.set_grid(img)
                frm = from_iccs(mv[:2], self.red_top)
                to = from_iccs(mv[2:4], self.red_top)
                occ = self.sample(img)
                if occ is not None and occ == expected:
                    accepted, final_occ, pairs_all = True, occ, [(frm, to)]
                    log("[fix] 重新标定后发现走子实际已生效", self.logf)
                else:
                    log("[fix] 重新标定完成，整体重试走子", self.logf)
                    # 此前失败的点击可能把起点留在选中态：先点空白清除，
                    # 再做完整的 选起点 -> 点终点（盲点终点在无选中时毫无意义）
                    try:
                        self.tap_point(*POPUP_DISMISS)
                    except RuntimeError:
                        pass
                    time.sleep(0.5)
                    if not accepted:
                        for attempt in (1, 2):
                            way = self.select_source(frm, attempt)
                            if way is None:
                                continue
                            if way == "mouse":
                                self.click_cell(to)
                            else:
                                self.adb_cell(to, jitter=(attempt > 1))
                            final_occ, pairs_all = self.wait_move_applied(
                                pre, frm, to, expected)
                            if final_occ is not None:
                                accepted = True
                                break
        if not accepted:
            # 最后抢救：清浮层/对话框/选中态 + 重标定 + 多轮重试，
            # 而不是一遇到点击未生效就放弃整局
            st, final_occ, pairs_all = self._rescue_move(pre, frm, to, expected, mv)
            if st == "over":
                return "over"
            if st is True:
                accepted = True
        if not accepted:
            log("[err] 我方走子点击未生效（已尽力抢救），尝试重同步局面 ...",
                self.logf)
            show(self.base, "走前局面：")
            show(expected, "期望局面：")
            # 连吃后内部跟踪可能与真实棋盘失步（引擎着法被 App 判非法）。
            # 以屏幕为准修正最近 1-2 步，成功则重新思考走子而不是放弃整局。
            if self.resync_moves() or self.resync_moves(max_back=3):
                log("[fix] 局面已重同步，重新思考走子", self.logf)
                return True
            # 深度失步（误记的着法太早、棋子数都对不上）：以屏幕为准重建
            occ = self.sample()
            if occ is not None and self.rebuild_from_screen(
                    occ, self.side_to_move(), "走子反复未生效"):
                log("[fix] 局面已按屏幕重建，重新思考走子", self.logf)
                return True
            return "fail"

        self.moves.append(mv)
        self.pieces = exp
        extra = [p for p in pairs_all if p != (frm, to)]
        opp_is_red = self.our_color != "r"
        # 连吃时验证窗口里常混进多个伪差对（棋子淡出/残影被当成走了子）。
        # 这里逐对做走子合法性校验：只有**整批都合法**才当对手着法采纳，
        # 否则宁可丢弃、让 opp_turn 从干净基线重新解析 —— 把伪差写进历史
        # 正是连吃之后内部局面失步的源头。
        fast, probe, bad = [], dict(self.pieces), None
        for f, t in extra:
            pp = probe.get(f)
            if (not pp or pp.isupper() != opp_is_red
                    or not move_legal(probe, f, t, self.red_top)):
                bad = (f, t)
                break
            probe.pop(f, None)
            probe[t] = pp
            fast.append((f, t))
        if bad is not None:
            log(f"[warn] 验证窗口内 {len(extra)} 个疑似对手着法，其中 "
                f"{to_iccs(bad[0], self.red_top)}{to_iccs(bad[1], self.red_top)} "
                f"不合法 —— 判为识别伪差，整批丢弃后重新解析", self.logf)
            fast = []
        for f, t in fast:
            pp = self.pieces.get(f)
            tag = to_iccs(f, self.red_top) + to_iccs(t, self.red_top)
            log(f"[op] {tag}  {chinese_notation(pp, f, t, self.red_top)}  （快速应手）",
                self.logf)
            self.moves.append(tag)
            self.pieces.pop(f, None)
            self.pieces[t] = pp
        # 基线始终由内部棋子表推得（而不是屏幕上那一帧），
        # 这样内部数据自洽；残余的识别噪声交给自检/watchdog 处理
        self.base = colors_of(self.pieces)
        self._fp = None                                # 强制下轮全量采样
        self._clean_moves += 1
        if self._clean_moves >= 4 and self._resync_tries:
            self._clean_moves = 0
            self._resync_tries = max(0, self._resync_tries - 1)
        sc = res.get("score") or {}
        sct = f"{sc.get('type','')} {sc.get('value','')}".strip() if sc else "?"
        log(f"[me] {mv}  {cn}  score={sct} depth={res.get('depth')} "
            f"time={res.get('time_ms')}ms  （第 {len(self.moves)} 步）", self.logf)
        return True

    # ---------- 变化解析（带合法性与候选消歧） ---------- #
    def _parse_change(self, prev, cur, mover):
        """从两帧占用差解析着法；返回 (frm,to) 或 None。
        单起点多终点时（一步真着法 + 识别瞬态），用走子合法性逐一筛选。"""
        mover_red = (mover == "r")
        pairs = diff_moves(prev, cur)
        if len(pairs) == 1:
            frm, to = pairs[0]
            p = self.pieces.get(frm)
            if p and p.isupper() == mover_red and move_legal(self.pieces, frm, to, self.red_top):
                return frm, to
            return None
        vac, fil = [], []
        for row in range(10):
            for col in range(9):
                a, b = prev[row][col], cur[row][col]
                if a and not b:
                    vac.append((col, row))
                elif b and (not a or a != b):
                    fil.append((col, row))
        if len(vac) == 1 and fil:
            v = vac[0]
            p = self.pieces.get(v)
            mc = "R" if mover_red else "B"
            cands = [f for f in fil if f != v and cur[f[1]][f[0]] == mc]
            legal = [(v, f) for f in cands if move_legal(self.pieces, v, f, self.red_top)]
            if len(legal) == 1:
                return legal[0]
            if len(legal) > 1:
                # 多个候选都「合法」：典型场景是 App 的走子轨迹红圈把终局
                # 旁格的我方棋子染成红色（如 俥四平二 时轨迹圈住相邻黑卒），
                # 画面多出一格伪差。用「画面解释力」择优：把候选应用到内部
                # 局面后，剩余差分越少越好；平手时优先剩余差分全是「原地
                # 变色」（两侧都有子、只颜色不同）的候选 —— 变色是高亮圈
                # 伪差的典型形态，出现/消失格则意味着真实子力对不上。
                best, best_key = None, None
                for mv in legal:
                    sim = dict(self.pieces)
                    pc = sim.pop(mv[0], None)
                    if pc:
                        sim[mv[1]] = pc
                    flips = appears = 0
                    for row in range(10):
                        for col in range(9):
                            sc = cur[row][col]
                            ic = sim.get((col, row))
                            ic = ("R" if ic.isupper() else "B") if ic else None
                            if ic == sc:
                                continue
                            if ic and sc:
                                flips += 1               # 原地变色（伪差）
                            else:
                                appears += 1             # 出现/消失（真异常）
                    key = (appears, flips)
                    if best_key is None or key < best_key:
                        best, best_key = mv, key
                return best
            return None
        if len(vac) == 2 and len(fil) == 2:
            # 一步真着法 + 一个「变色伪差」：真着法正常配对，伪差格起点==终点
            sols = []
            for i in range(2):
                v1, f1 = vac[i], fil[i]
                v2, f2 = vac[1 - i], fil[1 - i]
                if v1 == f1:                       # v1 格是伪差（原地变色）
                    p2 = self.pieces.get(v2)
                    if (p2 and p2.isupper() == mover_red and f2 != v2
                            and cur[f2[1]][f2[0]] == ("R" if mover_red else "B")
                            and move_legal(self.pieces, v2, f2, self.red_top)):
                        sols.append((v2, f2))
            if len(sols) == 1:
                return sols[0]
        # 对手吃子 + 画面污染：真实吃子会让「起点」和「被吃格」都变空
        # （两格进 vac），而落点的新子可能被吃子动画/轨迹圈误读成空、
        # 旁格被染成走子方颜色（多出的 fil 格全是伪差）。仅在
        # 「起点=走子方子、终点=敌方子、走法合法」且配对唯一时采纳；
        # 伪差格不写进内部表，等高亮消失后由自检的变色忽略自动吸收。
        if len(vac) == 2 and len(fil) <= 2:
            sols = []
            for o in vac:
                for d in vac:
                    if o == d:
                        continue
                    p = self.pieces.get(o)
                    q = self.pieces.get(d)
                    if (p and p.isupper() == mover_red and q
                            and q.isupper() != mover_red
                            and move_legal(self.pieces, o, d, self.red_top)):
                        sols.append((o, d))
            if len(sols) == 1:
                return sols[0]
        return None

    # ---------- 失步自动恢复 ---------- #
    SQUARES = [(c, r) for r in range(10) for c in range(9)]

    def _direct_accept(self, occ, mover):
        """恢复0：一空一满的干净单步着法，但内部表记错颜色/身份时的直采。
        只纠颜色（同类型翻面）或补身份（画面上消失的唯一同色子），
        走法形状仍然要过 move_legal，杜绝把假着法写进历史。"""
        pairs = diff_moves(self.base, occ)
        if len(pairs) != 1:
            return False
        frm, to = pairs[0]
        want_red = (mover == "r")
        scr = occ[to[1]][to[0]]
        if scr is None or (scr == "R") != want_red:
            return False                            # 画面颜色与轮次不符：交给阶梯
        piece = self.pieces.get(frm)
        fixed = ""
        if piece and piece.isupper() == want_red:
            pass                                    # 颜色本来就对，只可能是形状问题
        elif piece:
            piece = piece.upper() if want_red else piece.lower()
            fixed = "颜色纠正"
        else:
            cands = [c for sq, c in self.pieces.items()
                     if c and c.isupper() == want_red and sq != frm
                     and occ[sq[1]][sq[0]] is None]
            if len(cands) != 1:
                return False
            piece = cands[0]
            fixed = "身份补齐"
        if not move_legal({**self.pieces, frm: piece}, frm, to, self.red_top):
            return False
        tag = to_iccs(frm, self.red_top) + to_iccs(to, self.red_top)
        cn = chinese_notation(piece, frm, to, self.red_top)
        side = "红" if piece.isupper() else "黑"
        log(f"[fix] 差分直采（{fixed}）{tag}  {cn}  {side}{PIECE_CN[piece]}"
            f"  （第 {len(self.moves) + 1} 步）", self.logf)
        self.pieces[frm] = piece
        self.moves.append(tag)
        self.pieces.pop(frm, None)
        self.pieces[to] = piece
        self.base = occ
        self._fp = None
        return True

    @staticmethod
    def _board_luma(img):
        """棋盘区域平均亮度（用于检测弹窗/浮层导致的整体变暗）。"""
        a = np.asarray(img.convert("L").crop((20, 210, 310, 560)), dtype=np.float32)
        return float(a.mean())

    def _match_by_legal(self, occ, mover, max_diff=2):
        """全盘匹配恢复：枚举走子方所有合法着法，与 occ 全盘比对。
        完全匹配，或唯一接近最优（伪差 ≤ max_diff、差异格只落在起终点、
        且明显优于次优）才采纳。
        差异格必须只在 起点/终点 —— 连吃残影只会出现在这两处；若差异跑到
        别处，说明是内部局面本身已经错了，不能靠瞎猜一步来"圆过去"。
        返回 (frm, to, expected_grid) 或 None。"""
        mover_red = (mover == "r")
        mc = "R" if mover_red else "B"
        cands = []
        for frm, p in list(self.pieces.items()):
            if p.isupper() != mover_red:
                continue
            for to in self.SQUARES:
                if to == frm or not move_legal(self.pieces, frm, to, self.red_top):
                    continue
                exp = [row[:] for row in self.base]
                exp[to[1]][to[0]] = mc
                exp[frm[1]][frm[0]] = None
                diffs = [(c, r) for r in range(10) for c in range(9)
                         if exp[r][c] != occ[r][c]]
                cands.append((len(diffs), frm, to, exp, diffs))
        if not cands:
            return None
        cands.sort(key=lambda t: t[0])
        d0, frm, to, exp, diffs = cands[0]
        if d0 == 0:
            return frm, to, exp
        if (d0 <= max_diff and (len(cands) == 1 or d0 + 1 <= cands[1][0])
                and all(sq in (frm, to) for sq in diffs)):
            return frm, to, exp
        return None

    def _accept_recovered(self, frm, to, exp):
        """采纳恢复解析出的对手着法；用干净期望盘做新基线。"""
        piece = self.pieces.get(frm)
        tag = to_iccs(frm, self.red_top) + to_iccs(to, self.red_top)
        cn = chinese_notation(piece, frm, to, self.red_top) if piece else "?"
        side = "红" if (piece or "").isupper() else "黑"
        name = PIECE_CN.get(piece, "?") if piece else "?"
        log(f"[op] {tag}  {cn}  {side}{name}"
            f"  （第 {len(self.moves) + 1} 步，恢复解析）", self.logf)
        self.moves.append(tag)
        self.pieces.pop(frm, None)
        self.pieces[to] = piece
        self.base = exp
        self._fp = None

    def _relaxed_parse(self, prev, cur, mover):
        """放宽解析（长稳画面兜底）：1 起点对 1 终点，只校验颜色与走法形状。"""
        mover_red = (mover == "r")
        vac, fil = [], []
        for row in range(10):
            for col in range(9):
                a, b = prev[row][col], cur[row][col]
                if a and not b:
                    vac.append((col, row))
                elif b and (not a or a != b):
                    fil.append((col, row))
        if len(vac) == 1 and len(fil) == 1 and fil[0] != vac[0]:
            p = self.pieces.get(vac[0])
            mc = "R" if mover_red else "B"
            if (p and p.isupper() == mover_red and cur[fil[0][1]][fil[0][0]] == mc
                    and move_shape_ok(self.pieces, vac[0], fil[0], self.red_top)):
                return vac[0], fil[0]
        return None

    def _confirm_occ(self, occ, tries=3, gap=0.45):
        """容忍型解析前的二次确认：画面必须已静止在同一局面。
        连吃时残影/淡出会让单帧差分凭空多出几格，直接采纳就等于把伪差
        写进着法历史（失步的起点）。"""
        for _ in range(tries):
            time.sleep(gap)
            nxt = self.sample()
            if nxt is None:
                continue
            if nxt == occ:
                return True
            occ = nxt
        return False

    def _dump_state(self, why, occ=None):
        """失步现场转储：棋盘文本写入日志 + 原图存 PNG 供排查。"""
        log(f"[dump] ===== 失步现场：{why} =====", self.logf)
        for title, occx in (("基线(内部)", self.base), ("画面识别", occ)):
            if occx is None:
                continue
            buf = ["  " + " ".join(f"{FILES[8 - i]:>2}" for i in range(9))]
            for row in range(10):
                buf.append(f"  {row:2d} " + " ".join(
                    "●" if occx[row][c] == "R" else "○" if occx[row][c] == "B" else "·"
                    for c in range(9)))
            log(f"[dump] {title}\n" + "\n".join(buf), self.logf)
        try:
            img = self.grab()
            if img is not None:
                name = time.strftime("_desync_%m%d_%H%M%S.png")
                img.save(name)
                log(f"[dump] 画面已存 {name}", self.logf)
        except Exception:
            pass

    # ---------- 等待对手 ---------- #
    def opp_turn(self):
        # 上一手之后内部局面若已与真实棋盘分叉（连吃常见），先修正再等对手，
        # 否则后面的差分全部建立在错误基线上，越拖越偏
        self._sync_watchdog()
        t0 = time.time()
        last_warn = 0.0
        noise = 0
        multi = 0
        veto = 0
        colorfix = 0
        while True:
            img = self.grab()
            if img is None:
                time.sleep(0.5)
                continue
            if self._button_visible(img) and self.over_confirm():
                log("[end] 检测到「再来一局」—— 对局结束", self.logf)
                return "over"
            if self.reward_popup_visible(img):
                log("[end] 领奖浮层（快速升级/段位保护）出现 —— 点击右上角 × 关闭",
                    self.logf)
                self.close_reward_popup()
                continue
            if self.confirm_popup_visible(img):
                log("[end] 比赛结束弹窗出现 —— 点击「确定」返回", self.logf)
                self.close_confirm_popup()
                continue
            if self.rankup_page_visible(img):
                log("[end] 段位提升页出现 —— 点击左下角返回箭头", self.logf)
                self.close_rankup_page()
                continue
            if self.settle_popup_visible(img):
                log("[end] 结算奖励弹窗出现 —— 对局结束，关闭弹窗开新局", self.logf)
                self.dismiss_and_rematch()
                return "over"
            now = time.time()
            fp = self.fingerprint(img)
            force = now - self._last_full >= 2.0       # 指纹失灵时的兜底全采样
            if fp == self._fp and not force:
                waited = now - t0
                if len(self.moves) == 0 and waited > self.args.first_wait:
                    # 开局一直没人走子 -> 我方大概率执另一边，自动纠正
                    self.our_color = "b" if self.our_color == "r" else "r"
                    who = "红" if self.our_color == "r" else "黑"
                    log(f"[auto] 开局 {self.args.first_wait:.0f}s 无人走子，"
                        f"自动纠正为我方执{who}", self.logf)
                    return "flip"
                if waited >= self.args.stall_sec and waited - last_warn >= self.args.stall_sec:
                    last_warn = waited
                    log(f"[wait] 对手已 {waited:.0f}s 未走子"
                        f"（{self.args.hard_wait:.0f}s 放弃）", self.logf)
                if waited >= self.args.hard_wait:
                    log("[err] 对手长时间未走子，放弃等待", self.logf)
                    return "stall"
                time.sleep(self.args.interval)
                continue

            self._fp = fp
            self._last_full = now
            occ = self.sample(img)
            if occ is None:
                continue
            if not board_present(occ):
                if self.board_gone_confirm():
                    log("[end] 棋盘消失（连续多帧确认）—— 对局结束", self.logf)
                    return "over"
                self._fp = None
                continue
            if occ == self.base:
                noise = 0                              # 浮层散去、画面复原
                multi = 0
                continue
            occ = self.stabilize(occ)
            if not board_present(occ):
                if self.board_gone_confirm():
                    log("[end] 棋盘消失（连续多帧确认）—— 对局结束", self.logf)
                    return "over"
                self._fp = None
                continue
            if occ == remap_occ(self.base) and any(
                    any(row) for row in self.base):
                # 对手断线重连可能触发换边渲染：整盘旋转而无子移动
                self.migrate_state()
                self.base = occ
                if getattr(self, "_flips", 0) <= 6:
                    log("[fix] 检测到视图旋转 180°，已迁移跟踪", self.logf)
                    continue
            # 浮层/弹窗/掉线遮罩会让整盘变暗：耐心等它消失，不计入噪声
            if self._board_lum:
                lum = self._board_luma(img)
                if abs(lum - self._board_lum) > 12:
                    waited = now - t0
                    if waited > 30 and now - last_warn > 15:
                        last_warn = now
                        log(f"[wait] 棋盘被浮层/弹窗遮挡（亮度 {lum:.0f} vs 正常 "
                            f"{self._board_lum:.0f}），等待恢复 ...", self.logf)
                    if waited > max(self.args.hard_wait, 120):
                        self._dump_state("浮层长时间不消失", occ)
                        return "desync"
                    time.sleep(1.5)
                    continue
            if self._button_visible(self.grab()) and self.over_confirm():
                log("[end] 检测到「再来一局」—— 对局结束", self.logf)
                return "over"

            mover = self.side_to_move()
            # -------- 差分可信度门控（位置判断不准的根因就在这里） --------
            # 任何真实着法都会让「起点变空」。若两帧差异里没有任何格子变空，
            # 说明只是颜色/高光识别抖动，绝不可能是着法；若差异 ≥3 格，
            # 说明画面里还留着吃子动画的残影或缺子帧（双方快速连续吃子时
            # 特别常见），这类帧拿去解析就会把假位置写进着法历史。
            # 两种情况都先用多帧共识等画面真正静止，再重新解析。
            diff_now, vac_now = [], 0
            for _r in range(10):
                for _c in range(9):
                    _a, _b = self.base[_r][_c], occ[_r][_c]
                    if _a != _b:
                        diff_now.append((_c, _r, _a, _b))
                        if _a and not _b:
                            vac_now += 1
            if len(diff_now) >= 3 or vac_now == 0:
                settled = self.settled_occ(need=3, gap=0.4, rounds=10)
                if settled is None or settled == self.base:
                    self._fp = None
                    time.sleep(0.2)
                    continue                       # 画面未定/已复原：不解析
                occ = settled
                if not any(self.base[r][c] and not occ[r][c]
                           for r in range(10) for c in range(9)):
                    colorfix += 1
                    if colorfix <= 3:
                        if colorfix == 1:
                            log("[wait] 画面颜色抖动（无子力增减），等待恢复 ...",
                                self.logf)
                        self._fp = None
                        time.sleep(0.3)
                        continue
                    # 抖动持续：交给下面的常规恢复阶梯（重同步/重建）
            parsed = self._parse_change(self.base, occ, mover)
            if parsed:
                frm, to = parsed
                # 落子确认：采样间隔内的瞬态帧（吃子动画/残影）可能拼出
                # "假着法"——0.35s 后复检，起点必须仍空、终点必须仍有子，
                # 任一被推翻就说明这帧不可信，丢弃后按最新画面重新解析
                time.sleep(0.35)
                occ2 = self.sample()
                if (occ2 is not None and board_present(occ2) and occ2 != occ
                        and (occ2[frm[1]][frm[0]] is not None
                             or occ2[to[1]][to[0]] is None)):
                    veto += 1
                    if veto <= 3:
                        log("[wait] 差分未通过落子确认（瞬态帧），按最新画面重判",
                            self.logf)
                        self._fp = None
                        continue
                veto = 0
                piece = self.pieces.get(frm)
                tag = to_iccs(frm, self.red_top) + to_iccs(to, self.red_top)
                cn = chinese_notation(piece, frm, to, self.red_top)
                side = "红" if piece.isupper() else "黑"
                log(f"[op] {tag}  {cn}  {side}{PIECE_CN[piece]}"
                    f"  （第 {len(self.moves) + 1} 步）", self.logf)
                self.moves.append(tag)
                self.pieces.pop(frm, None)
                self.pieces[to] = piece
                self.base = occ
                self._fp = None
                return True
            # 恢复0：画面是「干净的单步着法」，只是内部表颜色/身份记偏了。
            # 这类帧以前会被整条恢复阶梯反复拒绝，最后白重建一次还把局面搞坏；
            # 棋子不会变色，画面给出的一空一满就是真相，按画面纠正后直接采纳。
            if self._direct_accept(occ, mover):
                return True
            # 解析失败：先走恢复阶梯，最后才考虑放弃
            changed_squares = sum(1 for r in range(10) for c in range(9)
                                  if self.base[r][c] != occ[r][c])
            if changed_squares >= 2:
                noise += 1
                if noise == 1:
                    # 首次失败：记录变化格细节（哪格从什么颜色变成什么），
                    # 便于事后判断是着法、动画还是结算过渡
                    det = []
                    for r in range(10):
                        for c in range(9):
                            a, b = self.base[r][c], occ[r][c]
                            if a != b:
                                sq = to_iccs((c, r), self.red_top)
                                det.append(f"{sq}:{a or '空'}->{b or '空'}")
                    log(f"[warn] 变化 {changed_squares} 格无法解析为合法着法"
                        f"（第 1 次）[{'; '.join(det)}]", self.logf)
                    # 单一着法却被拒：把拒绝原因查清楚（内部表缺子/颜色不符/
                    # 走法形状不合法），这是判断“内部失步”还是“识别误读”的关键
                    _pairs = diff_moves(self.base, occ)
                    if len(_pairs) == 1:
                        _f, _t = _pairs[0]
                        _pc = self.pieces.get(_f)
                        if not _pc:
                            _why = "内部表该格无子"
                        elif _pc.isupper() != (mover == "r"):
                            _why = f"内部表记为{PIECE_CN.get(_pc, _pc)}但画面是" \
                                   f"{'红' if occ[_t[1]][_t[0]] == 'R' else '黑'}子"
                        elif not move_legal(self.pieces, _f, _t, self.red_top):
                            _why = (f"内部表{PIECE_CN.get(_pc, _pc)}走"
                                    f"{to_iccs(_f, self.red_top)}->"
                                    f"{to_iccs(_t, self.red_top)}形状不合法")
                        else:
                            _why = "未明"
                        log(f"[warn] 该帧是单一着法但被拒：{_why}", self.logf)
                else:
                    log(f"[warn] 变化 {changed_squares} 格无法解析为合法着法"
                        f"（第 {noise} 次）", self.logf)
                # 结算过渡的早期信号：「再来一局」按钮已渲染出来。
                # 对局实际已结束，剩下的多格变化都是结算动画，
                # 不要再解析/重标定/重建（白白消耗重建配额、拖 100 秒）
                if noise >= 4 and self._button_visible(img) and self.over_confirm():
                    log("[end] 对手回合出现结算画面 —— 对局结束", self.logf)
                    return "over"
                # 恢复1：全盘合法着法模糊匹配（容忍 ≤2 格识别伪差）
                rec = self._match_by_legal(occ, mover)
                if rec:
                    frm, to, exp = rec
                    d = sum(1 for r in range(10) for c in range(9)
                            if exp[r][c] != occ[r][c])
                    # 有伪差时先二次确认画面已静止：连吃残影造成的"假着法"
                    # 一旦写入历史，后面就和真实棋盘对不上了
                    if d and not self._confirm_occ(occ):
                        log("[wait] 画面仍在变化（吃子动画/残影），等稳定后重判",
                            self.logf)
                        self._fp = None
                        continue
                    self._accept_recovered(frm, to, exp)
                    return True
                # 恢复2：标定可能漂移，重新标定后重采样
                if noise % 4 == 2:
                    log("[fix] 尝试重新标定棋盘 ...", self.logf)
                    try:
                        img2 = self.grab()
                        if img2 is not None:
                            self.set_grid(img2)
                    except Exception:
                        pass
                    self._fp = None
                    time.sleep(0.5)
                    continue
                # 恢复3：画面长稳后放宽为「1起点1终点，仅验颜色+走法形状」
                if noise >= 6:
                    rec = self._relaxed_parse(self.base, occ, mover)
                    if rec and self._confirm_occ(occ):
                        frm, to = rec
                        exp = [row[:] for row in self.base]
                        exp[to[1]][to[0]] = exp[frm[1]][frm[0]]
                        exp[frm[1]][frm[0]] = None
                        self._accept_recovered(frm, to, exp)
                        return True
                if noise >= 12:
                    # 反复失败往往因为帧不稳（吃子动画长达数十秒）：
                    # 先取多帧共识画面，拿不到就继续等，绝不拿坏帧修复
                    stable = self.settled_occ(need=3, gap=0.4)
                    if stable is None:
                        self._stalled += 1
                        log("[wait] 画面持续不稳定，继续等待共识帧 ...",
                            self.logf)
                        if self._stalled >= 6:
                            # 有界兜底：单格识别抖动可能永远凑不满全盘共识，
                            # 旧逻辑 noise-=3 会在这里无限打转（真卡死）。
                            # 与其按错误棋盘继续走子，不如结束本局重开一局。
                            log("[fix] 共识帧长期不可得，结束本局重新开始",
                                self.logf)
                            self._dump_state("画面长期无法稳定", occ)
                            return "desync"
                        noise -= 3                  # 不让 noise 冲到弃局
                        continue
                    self._stalled = 0
                    occ = stable
                    # 连吃后可能内部局面已失步：先尝试以画面为准重同步
                    if self.resync_moves():
                        log("[fix] 对手着法解析失败，但局面已重同步 —— 继续对局",
                            self.logf)
                        noise = 0
                        continue
                    # 回溯救不回来：以屏幕为准重建，重新等对手走子
                    if self.rebuild_from_screen(occ, mover, "对手着法反复无法解析"):
                        noise = 0
                        continue
                    self._dump_state("变化反复无法解析", occ)
                    return "desync"
                time.sleep(1.2)
                continue
            noise += 1
            if noise >= 12:
                # 单点变化长时间不消失：也可能是漏记的对手着法导致失步
                if self.resync_moves() or self.resync_moves(max_back=3) \
                        or self.rebuild_from_screen(occ, mover, "单点差异长期不消失"):
                    noise = 0
                    continue
                self._dump_state("单点变化长时间不消失", occ)
                return "desync"
            time.sleep(1.2)                            # 瞬态浮层/动画：耐心等它散去

    # ---------- 大厅按钮 ---------- #
    def click_start(self):
        gx, gy = MODE_BUTTONS[self.args.start]
        name = {"5": "5分钟", "10": "10分钟", "15": "15分钟", "turbo": "超快"}[self.args.start]
        if not self.activate():
            log("[warn] 窗口未能置顶，模式点击可能无效", self.logf)
        for attempt in range(1, 5):
            before = self.fingerprint(self.grab())
            try:
                self.tap_point(gx, gy)
            except RuntimeError as exc:
                # 窗口被遮挡且 adb 不可用：绝不退出进程，等遮挡解除后重试
                log(f"[warn] {exc} —— 等待遮挡解除后重试", self.logf)
                time.sleep(3.0)
                continue
            time.sleep(2.5)
            after = self.fingerprint(self.grab())
            if before != after:
                log(f"[hall] 已点击「{name}」开始匹配（第 {attempt} 次尝试）", self.logf)
                return True
            log(f"[warn] 第 {attempt} 次点击「{name}」未生效，重试", self.logf)
        log("[err] 模式按钮点击始终无效 —— 请手动开始对局", self.logf)
        return False

    # ---------- 清理 ---------- #
    def close(self):
        try:
            if self.engine is not None:
                self.engine.close()
        except Exception:
            pass
        try:
            self.logf.close()
        except Exception:
            pass

    # ---------- 主循环 ---------- #
    def run(self):
        log(f"[win] hwnd={self.hwnd}  接口 {self.api}", self.logf)
        init = self.grab()
        if init is not None:
            self.set_grid(init)                       # 指纹/点击需要网格先就位
        if self.args.start:
            if not self.click_start():
                self.logf.close()
                return "no_click"

        while True:                                    # 每局一轮；--rematch 时连下
            img, occ = self.wait_game()
            if occ is None:
                if not self.args.rematch:
                    log("[err] 等待棋盘超时 —— 未进入对局", self.logf)
                    return "no_game"
                log("[hall] 未等到棋盘，15 秒后重试 ...", self.logf)
                if self.args.start:
                    self.click_start()
                time.sleep(15.0)
                continue
            log("[status] 检测到标准开局！", self.logf)
            self.setup(img, occ)
            self._round += 1

            # 引擎预热（HTTP 模式下加载 NNUE、分配哈希；本地引擎已在启动时预热）
            if self.engine is None:
                try:
                    get_json(f"{self.api}/api/bestmove?fen=startpos&movetime=200",
                             timeout=30)
                    log("[api] 引擎预热完成", self.logf)
                except Exception:
                    pass

            result = None
            try:
                while True:
                    stm = self.side_to_move()
                    if stm == self.our_color:
                        st = self.our_turn()
                    else:
                        st = self.opp_turn()
                    if st is True or st == "flip":
                        continue
                    result = st
                    break
            except KeyboardInterrupt:
                log("\n[exit] 手动停止", self.logf)
                result = "interrupted"
            except Exception:
                log("[err] 未捕获异常：\n" + traceback.format_exc(), self.logf)
                result = "error"

            log(f"[done] 本局结束（{result}），共 {len(self.moves)} 步。\n"
                f"       moves={','.join(self.moves)}", self.logf)
            if not self.args.rematch or result == "interrupted":
                self.logf.close()
                return result
            log("[hall] 5 秒后进入下一局 ...", self.logf)
            time.sleep(5.0)
            self.moves = []
            self.base_fen = self.args.fen or "startpos"


# --------------------------------------------------------------------------- #
# 被动监听（--no-click）：只识别打印着法
# --------------------------------------------------------------------------- #
def passive_run(args):
    coords = CoordSystem()
    hwnd = wait_window(timeout=30)
    print(f"[win] hwnd={hwnd} 窗口标题 {WIN_TITLE}")
    if args.file:
        img = Image.open(args.file).convert("RGB")
        if img.size != (REF_W, REF_H):
            img = img.resize((REF_W, REF_H), Image.LANCZOS)
        hwnd = None
    else:
        img = grab_window(hwnd)
    if img is None:
        print("[err] 无法截取窗口画面")
        return
    grid = None
    if not args.no_calib:
        cal = calibrate(img)
        if cal:
            GX, GY, hits = cal
            grid = {(c, r): (GX[c], GY[r]) for r in range(10) for c in range(9)}
            print(f"[calib] 自动标定成功（命中 {hits}）")
    if grid is None:
        print("[calib] 使用 象棋坐标系.txt 的坐标（含 ±10px 自动吸附）")
    sampler = Sampler(coords, grid)
    red_top = None
    pieces = fen_board_screen(fen_text(args.fen), True) if args.fen else {}

    base = sampler.occupy(sampler.sample(img))
    if args.dump or args.file:
        show(base, "当前棋子分布（●红 ○黑）")
        print(f"[stat] 棋子数 {sum(1 for row in base for c in row if c)}")
        red_top = detect_red_top(base)
        print(f"[stat] 红在{'上' if red_top else '下'}方")
    if args.file:
        return
    in_game = board_present(base)
    if not in_game:
        print("[status] 当前画面不是对局棋盘，等待进入对局 ...")
    print(f"[run] 每 {args.interval:.1f}s 采样一次（被动监听，不动子），Ctrl+C 结束\n")
    logf = open(args.log, "a", encoding="utf-8")
    try:
        while True:
            time.sleep(args.interval)
            img = grab_window(hwnd)
            if img is None:
                continue
            cur = sampler.occupy(sampler.sample(img))
            if cur == base:
                continue
            cur = _settle(hwnd, sampler, cur)
            if cur is None:
                continue
            if not board_present(cur):
                if in_game:
                    print("[status] 已离开对局界面，等待下一局")
                    in_game = False
                base = cur
                continue
            if not in_game:
                n = sum(1 for row in cur for c in row if c)
                print(f"[status] 检测到对局棋盘（{n} 子），建立新基线")
                in_game = True
                base = cur
                continue
            red_top = detect_red_top(cur)
            pairs = diff_moves(base, cur)
            if not pairs:
                base = cur                              # 噪声：直接重同步
                continue
            for frm, to in pairs:
                tag = to_iccs(frm, red_top) + to_iccs(to, red_top)
                stamp = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"[{stamp}] {tag}", flush=True)
                logf.write(f"{stamp} {tag}\n")
                logf.flush()
                if pieces:
                    p = pieces.get(frm)
                    pieces.pop(frm, None)
                    pieces[to] = p
            base = cur
    except KeyboardInterrupt:
        print("\n[exit] 已停止")
    finally:
        logf.close()


def _settle(hwnd, sampler, occ, rounds=3, gap=0.7):
    for _ in range(rounds):
        time.sleep(gap)
        img = grab_window(hwnd)
        if img is None:
            continue
        nxt = sampler.occupy(sampler.sample(img))
        if nxt == occ:
            return occ
        occ = nxt
    return occ


# --------------------------------------------------------------------------- #
_LOCK_FH = None


def _preflight():
    """启动自检：解释器/依赖不对时立刻说清楚，而不是抛一堆 ModuleNotFoundError。
    典型踩坑：命令行里的 python 指向别的解释器（如 WorkBuddy 自带 3.13），
    没有 numpy/opencv/requests，一启动就崩。"""
    missing = []
    for mod, pkg in (("numpy", "numpy"), ("cv2", "opencv-python"), ("PIL", "pillow")):
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    if not missing:
        return True
    print(f"[err] 当前解释器缺少依赖：{', '.join(missing)}")
    print(f"[err] 正在使用的解释器：{sys.executable}")
    print(r"[hint] 请用装有依赖的解释器启动："
          r"C:\Python315\python.exe watch_moves.py --start 10 --rematch")
    print(r"[hint] 或先安装依赖："
          rf"C:\Python315\python.exe -m pip install {' '.join(missing)}")
    return False


def _single_instance():
    """同一时间只允许一个对弈进程（两个实例会互抢点击/adb，表现为莫名出错）。"""
    global _LOCK_FH
    try:
        import msvcrt
    except Exception:
        return True
    try:
        fh = open(os.path.join(HERE, "_watch.lock"), "a+")
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write("1")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        _LOCK_FH = fh
        return True
    except OSError:
        print("[err] 已有另一个对弈实例在运行 —— 请先关掉它再启动（否则两边抢点击）")
        return False
    except Exception:
        return True


def _hold(msg="按回车键退出 ..."):
    """出错时别让窗口一闪而过，留下可读的错误信息。
    由「一键对弈.bat」拉起时（WATCH_AUTORESTART=1）必须跳过：重启循环由
    bat 负责，这里的 input() 会一直等回车，把自动重启彻底卡死。"""
    if os.environ.get("WATCH_AUTORESTART") == "1":
        return
    try:
        input(msg)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="FOA-AL00 象棋全自动对弈主控")
    ap.add_argument("--movetime", type=int, default=1000, help="引擎每步思考毫秒，默认 1000")
    ap.add_argument("--side", choices=["auto", "top", "bottom"], default="auto",
                    help="我方位置（auto=屏幕下方一方，开局无人走子会自动纠正）")
    ap.add_argument("--fen", default=None, help="起始局面 FEN（默认 startpos）")
    ap.add_argument("--moves", default="", help="种子着法序列，如 h2e2,h9g7")
    ap.add_argument("--interval", type=float, default=0.3, help="轮询间隔秒，默认 0.3")
    ap.add_argument("--click-gap", type=float, default=0.45, help="起点/终点两次点击间隔秒")
    ap.add_argument("--click-retry", type=int, default=3, help="走子点击最大尝试次数")
    ap.add_argument("--first-wait", type=float, default=25.0,
                    help="开局等待对手走子的自动判边阈值秒")
    ap.add_argument("--stall-sec", type=float, default=25.0, help="对手无动作提醒阈值秒")
    ap.add_argument("--hard-wait", type=float, default=120.0, help="对手无动作放弃阈值秒")
    ap.add_argument("--start", choices=["5", "10", "15", "turbo"], default=None,
                    help="先点击大厅模式按钮开始匹配（5/10/15/turbo=超快）")
    ap.add_argument("--rematch", action="store_true", help="终局后自动点「再来一局」连续对弈")
    ap.add_argument("--board-wait", type=float, default=300.0, help="等待棋盘出现最长时间秒")
    ap.add_argument("--api", default=API, help="引擎接口地址（--engine http 时使用）")
    ap.add_argument("--engine", choices=["local", "http"], default="local",
                    help="local=进程内直连 Pikafish（默认，更快）；http=走 8765 接口")
    ap.add_argument("--threads", type=int, default=20, help="引擎线程数（0=自动）")
    ap.add_argument("--hash", type=int, default=4096, help="引擎哈希 MB")
    ap.add_argument("--no-calib", action="store_true", help="不做自动标定，直接用 txt 坐标")
    ap.add_argument("--no-spawn", action="store_true", help="接口服务未启动时不自动拉起")
    ap.add_argument("--no-click", action="store_true", help="被动监听模式：只打印着法")
    ap.add_argument("--file", help="用图片调试（单次识别后退出）")
    ap.add_argument("--dump", action="store_true", help="只打印当前棋子分布后退出")
    ap.add_argument("--log", default=os.path.join(HERE, "watch.log"))
    args = ap.parse_args()

    # 启动自检：环境不对就立即给出可读提示（窗口不闪退）
    print(f"[boot] 解释器 {sys.executable}  Python {sys.version.split()[0]}  "
          f"cwd={os.getcwd()}  stdout={sys.stdout.encoding}")
    if not _preflight():
        _hold()
        return 2
    if not _single_instance():
        _hold()
        return 3

    try:
        if args.no_click or args.file:
            passive_run(args)
            return
        bot = Bot(args)
        try:
            if args.dump:
                bot.activate()
                img = bot.grab()
                bot.set_grid(img)
                occ = bot.sample(img)
                show(occ, "当前棋子分布（●红 ○黑）：")
                n = sum(1 for row in occ for c in row if c)
                red_top = detect_red_top(occ)
                print(f"[stat] 棋子数 {n}  红在{'上' if red_top else '下'}方  "
                      f"我方默认执{'黑' if red_top else '红'}")
                return
            result = bot.run()
            # run() 自己吞掉 KeyboardInterrupt 并以 "interrupted" 返回，
            # 若不转成专用退出码，进程会以 0 退出，bat 会误当正常结束而重启
            if result == "interrupted":
                return 130
        finally:
            bot.close()
    except KeyboardInterrupt:
        print("\n[exit] 手动停止")
        return 130                       # 让 bat 识别「用户主动停止」，不再重启
    except Exception:
        tb = traceback.format_exc()
        print("[err] 未捕获异常，进程即将退出：\n" + tb)
        try:
            with open(os.path.join(HERE, "crash.log"), "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n{tb}")
        except Exception:
            pass
        _hold("错误已记录到 crash.log，按回车键退出 ...")
        return 1


if __name__ == "__main__":
    # 必须把 main() 的返回值交给 sys.exit：旧写法丢弃返回值，进程永远以
    # 0 退出，bat 无法区分「正常结束」和「依赖缺失/已有实例/崩溃」。
    sys.exit(main())
