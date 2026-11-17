# -*- coding: utf-8 -*-
"""
JJ象棋 自动对弈（Pillow 实现）：截图识别 + 模拟点击，着法由本地 Pikafish HTTP 接口给出。

用法：
    python play.py --check              # 只读取并显示当前局面
    python play.py --moves 3            # 替底方走 3 步
    python play.py --loop               # 下到终局
    python play.py --loop --movetime 2500

要点：
    * Pillow ImageGrab 抓 FOA-AL00 窗口客户区，放大到手机分辨率后识别
    * 自动判断棋盘方向（红上或红下），坐标换算到标准 UCI（a-i / 0-9）
    * 棋子种类靠「标准开局 + 前后帧差」增量跟踪，无需字形 OCR
    * 点击用 ctypes SetCursorPos + mouse_event 在窗口内完成
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_TITLE = "FOA-AL00"

PHONE_W, PHONE_H = 1084, 2412
GX = [76, 194, 310, 426, 542, 659, 775, 892, 1009]
GY = [693, 805, 916, 1028, 1141, 1254, 1367, 1481, 1596, 1710]
FILES = "abcdefghi"

STANDARD_START = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"

API_PORT = 8766
API_BASE = f"http://127.0.0.1:{API_PORT}"

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()


# --------------------------------------------------------------------------- #
# 窗口与点击
# --------------------------------------------------------------------------- #
class Window:
    def __init__(self, title=WIN_TITLE):
        self.hwnd = self._find(title)
        if not self.hwnd:
            raise RuntimeError(f"未找到窗口 {title}")

    @staticmethod
    def _find(title):
        found = []

        def cb(hwnd, _):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                rect = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if title in buf.value and rect.right > rect.left:
                    found.append((hwnd, buf.value))
            return True

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb)
        user32.EnumWindows(CB, 0)
        for hwnd, txt in found:
            if txt.strip() == title:
                return hwnd
        return found[0][0] if found else None

    def client_rect(self):
        r = wt.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(r))
        pt = wt.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y, r.right, r.bottom

    def activate(self):
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)
        user32.SetForegroundWindow(self.hwnd)
        time.sleep(0.25)
        return user32.GetForegroundWindow() == self.hwnd

    def grab(self) -> Image.Image:
        x, y, w, h = self.client_rect()
        return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)


def click(x, y, delay=0.05):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(delay)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.04)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


# --------------------------------------------------------------------------- #
# 棋盘识别（全部在「手机像素空间」进行）
# --------------------------------------------------------------------------- #
class Board:
    UPSCALE = 3

    def __init__(self, win: Window):
        self.win = win
        self.cx, self.cy, self.cw, self.ch = win.client_rect()
        self.sx = self.cw / PHONE_W
        self.sy = self.ch / PHONE_H
        self.orient = "normal"      # normal: 红在下; flip: 红在上
        # 放大后的手机空间尺寸
        self.PW = PHONE_W
        self.PH = PHONE_H

    # ---- 屏幕像素 <-> 手机空间 ---- #
    def phone_to_screen(self, px, py):
        return self.cx + px * self.sx, self.cy + py * self.sy

    def grab_phone(self) -> Image.Image:
        """抓窗口并放大回手机分辨率（Pillow LANCZOS）。"""
        img = self.win.grab()
        return img.resize((self.PW, self.PH), Image.LANCZOS)

    # ---- 采样识别 ---- #
    def read_phone(self, img: Image.Image):
        arr = np.asarray(img.convert("RGB")).astype(np.int16)
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        face = (r > 210) & (r - b > 50) & (g > 150)
        red_glyph = (r - g > 50) & (r > 85) & (g < 135)
        dark = np.maximum(np.maximum(r, g), b) < 95

        rad = 26
        yy, xx = np.mgrid[-rad:rad + 1, -rad:rad + 1]
        disk = (xx * xx + yy * yy) <= rad * rad
        grid = []
        for py in GY:
            row = []
            for px in GX:
                f = face[py - rad:py + rad + 1, px - rad:px + rad + 1][disk].mean()
                if f < 0.28:
                    row.append(None)
                    continue
                rg = red_glyph[py - rad:py + rad + 1, px - rad:px + rad + 1][disk].mean()
                dk = dark[py - rad:py + rad + 1, px - rad:px + rad + 1][disk].mean()
                row.append("R" if rg > dk else "B")
            grid.append(row)
        return grid

    # ---- 方向 ---- #
    def detect_orient(self, grid):
        top_red = sum(1 for c in grid[0] if c == "R") + \
                  sum(1 for c in grid[2] if c == "R")
        bot_red = sum(1 for c in grid[9] if c == "R") + \
                  sum(1 for c in grid[7] if c == "R")
        self.orient = "flip" if top_red > bot_red else "normal"
        return self.orient

    def to_abs(self, i, j):
        """屏幕格 (i列, j行, j=0为屏幕顶) -> 绝对 (file, rank)"""
        if self.orient == "normal":
            return i, 9 - j
        return 8 - i, j

    def from_abs(self, file, rank):
        if self.orient == "normal":
            return file, 9 - rank
        return 8 - file, rank

    def abs_grid(self, grid):
        out = [[None] * 9 for _ in range(10)]
        for j in range(10):
            for i in range(9):
                f, r = self.to_abs(i, j)
                out[9 - r][f] = grid[j][i]
        return out

    def screen_xy(self, file, rank):
        i, j = self.from_abs(file, rank)
        return self.phone_to_screen(GX[i], GY[j])

    def overlay(self, img: Image.Image, path: str):
        im = img.convert("RGB").copy()
        d = ImageDraw.Draw(im)
        for j in range(10):
            for i in range(9):
                x, y = GX[i], GY[j]
                d.ellipse([x - 26, y - 26, x + 26, y + 26], outline=(0, 255, 0), width=3)
        for i in range(9):
            d.line([(GX[i], GY[0]), (GX[i], GY[9])], fill=(0, 160, 255), width=2)
        for j in range(10):
            d.line([(GX[0], GY[j]), (GX[8], GY[j])], fill=(0, 160, 255), width=2)
        im.save(path)
        return path


# --------------------------------------------------------------------------- #
# 局面
# --------------------------------------------------------------------------- #
def parse_fen_board(fen_rows: str):
    state = []
    for line in fen_rows.split("/"):
        row = []
        for ch in line:
            row.extend([None] * int(ch) if ch.isdigit() else [ch])
        state.append(row)
    return state


def to_fen(state, side):
    rows = []
    for r in state:
        s, empty = "", 0
        for p in r:
            if p is None:
                empty += 1
            else:
                if empty:
                    s += str(empty); empty = 0
                s += p
        if empty:
            s += str(empty)
        rows.append(s)
    return "/".join(rows) + f" {side} - - 0 1"


def colors_of(state):
    return [[None if p is None else ("R" if p.isupper() else "B") for p in row] for row in state]


def apply_move(state, mv):
    f1, r1, f2, r2 = int(FILES.index(mv[0])), int(mv[1]), int(FILES.index(mv[2])), int(mv[3])
    piece = state[9 - r1][f1]
    state[9 - r2][f2] = piece
    state[9 - r1][f1] = None
    return piece


def diff_move(state, abs_colors):
    """已知局面 vs 新颜色帧 -> (uci, piece) 或 None"""
    old = colors_of(state)
    vac, fil = [], []
    for j in range(10):
        for i in range(9):
            o, n = old[j][i], abs_colors[j][i]
            if o and n is None:
                vac.append((i, j))
            elif n and o != n:
                fil.append((i, j))
    if len(vac) == 1 and len(fil) == 1:
        (fi, fj), (ti, tj) = vac[0], fil[0]
        return FILES[fi] + str(9 - fj) + FILES[ti] + str(9 - tj), state[fj][fi]
    return None


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
def ensure_server():
    try:
        urllib.request.urlopen(f"{API_BASE}/api/health", timeout=2)
        return None
    except Exception:
        pass
    log = open(os.path.join(HERE, "_api_play.log"), "w", encoding="utf-8")
    flags = 0x08000000 if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "pikafish_server.py"),
         "--port", str(API_PORT), "--instances", "1", "--threads", "8",
         "--hash", "512", "--quiet"],
        stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    for _ in range(75):
        time.sleep(0.4)
        try:
            urllib.request.urlopen(f"{API_BASE}/api/health", timeout=2)
            return proc
        except Exception:
            continue
    raise RuntimeError("引擎接口启动失败，见 _api_play.log")


def ask_bestmove(fen, movetime):
    payload = json.dumps({"fen": fen, "movetime": movetime,
                          "threads": 8, "hash": 512, "multipv": 1}).encode()
    req = urllib.request.Request(f"{API_BASE}/api/bestmove", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    if "error" in data:
        raise RuntimeError(data["error"]["message"])
    return data


def warm_server():
    """确保接口可用并完成 NNUE 预热（先做一次 300ms 搜索）。"""
    proc = ensure_server()
    ask_bestmove(to_fen(parse_fen_board(STANDARD_START), "w"), 300)
    print("[warm] 引擎接口已预热")
    return proc


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def wait_board_change(board, ref_abs_colors, timeout=50.0, poll=0.8):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(poll)
        grid = board.read_phone(board.grab_phone())
        cur = board.abs_grid(grid)
        if cur != ref_abs_colors:
            time.sleep(1.0)                  # 等走子动画结束
            grid = board.read_phone(board.grab_phone())
            return board.abs_grid(grid)
    return None


def show(colors_abs):
    print("      " + "  ".join(FILES))
    for j in range(10):
        print(f" {9-j}    " + "  ".join((c or ".") for c in colors_abs[j]))


def wait_for_board(board, timeout=90):
    """等待棋盘出现在窗口中（匹配/动画/广告结束后）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        grid = board.read_phone(board.grab_phone())
        n = sum(1 for row in grid for c in row if c)
        if n >= 20:
            return grid
        time.sleep(1.0)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--moves", type=int, default=1)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--movetime", type=int, default=2500)
    ap.add_argument("--rematch", action="store_true",
                    help="先预热引擎，再点击「再来一局」开始新对局")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    win = Window()
    board = Board(win)
    print(f"[win] hwnd={win.hwnd} client={board.cx},{board.cy} "
          f"{board.cw}x{board.ch} scale={board.sx:.3f}")
    win.activate()

    proc = None
    if args.rematch:
        print("[warm] 预热引擎接口 ...")
        proc = warm_server()
        x = board.cx + int(board.cw * 0.5)
        y = board.cy + int(board.ch * 0.89)
        print(f"[click] 再来一局 ({x},{y})")
        click(x, y)
        time.sleep(2)

    print("[wait] 等待标准开局出现 ...")
    t0 = time.time()
    start_colors = colors_of(parse_fen_board(STANDARD_START))
    while time.time() - t0 < 120:
        img = board.grab_phone()
        grid = board.read_phone(img)
        board.detect_orient(grid)
        if board.abs_grid(grid) == start_colors:
            break
        time.sleep(1.0)
    else:
        print("[end] 120 秒内未出现标准开局")
        return
    print("[ok] 新对局开始")
    if args.debug:
        board.overlay(img, os.path.join(HERE, "_board_grid.png"))
        print("[debug] 标注图 _board_grid.png")

    grid = board.read_phone(img)
    orient = board.detect_orient(grid)
    print(f"[orient] {orient}（{'红上黑下' if orient=='flip' else '红下黑上'}）")
    abs_colors = board.abs_grid(grid)

    state = parse_fen_board(STANDARD_START)
    mism = [(i, j) for j in range(10) for i in range(9)
            if colors_of(state)[j][i] != abs_colors[j][i]]
    if mism:
        print(f"[diff] 与标准开局不一致 {len(mism)} 处: " +
              ", ".join(f"{FILES[i]}{9-j}" for i, j in mism[:12]))
        show(abs_colors)
        if not args.check:
            return
    else:
        print("[ok] 当前为标准开局")

    # 我方 = 底方颜色
    bottom = sum(1 for c in abs_colors[9] if c)
    my_color = "R" if abs_colors[9].count("R") >= abs_colors[9].count("B") else "B"
    print(f"[side] 我方执 {'红' if my_color=='R' else '黑'}（屏幕下方，共{bottom}子）")
    my_side = "w" if my_color == "R" else "b"

    if args.check:
        show(abs_colors)
        return

    fresh = not mism
    if proc is None:
        proc = ensure_server()
    try:
        done = 0
        limit = 10 ** 9 if args.loop else args.moves
        while done < limit:
            # ---------- 等对手（若当前局面=起始且我执黑，红先走） ----------
            if fresh and my_color == "B":
                print("[wait] 等待红方（对手）先行 ...")
                cur = wait_board_change(board, colors_of(state), timeout=90)
                if cur is None:
                    print("[end] 90 秒无变化，退出")
                    return
                mv = diff_move(state, cur)
                if mv:
                    print(f"[op] 对手走: {mv[0]} ({mv[1]})")
                    apply_move(state, mv[0])
                fresh = False
            elif fresh and my_color == "R":
                fresh = False

            # ---------- 我方思考 ----------
            fen = to_fen(state, my_side)
            res = None
            for attempt in range(3):
                try:
                    res = ask_bestmove(fen, args.movetime)
                    break
                except Exception as exc:
                    print(f"[api] 调用失败({exc})，重启引擎接口重试 {attempt+1}/3")
                    try:
                        if proc:
                            proc.terminate()
                    except Exception:
                        pass
                    proc = ensure_server()
            if res is None:
                print("[end] 接口连续失败，停止")
                return
            mv = res["bestmove"]
            if not mv:
                print("[end] 无着法可走（终局）")
                return
            print(f"[me] {mv}  depth={res['depth']} score={res['score']} "
                  f"nodes={res['nodes']} time={res['time_ms']}ms")

            # ---------- 点击走子 ----------
            f1, r1, f2, r2 = int(FILES.index(mv[0])), int(mv[1]), \
                             int(FILES.index(mv[2])), int(mv[3])
            before = board.abs_grid(board.read_phone(board.grab_phone()))
            ok = False
            for attempt in range(3):
                sx, sy = board.screen_xy(f1, r1)
                tx, ty = board.screen_xy(f2, r2)
                win.activate()
                click(sx, sy)
                time.sleep(0.5)
                click(tx, ty)
                time.sleep(1.3)
                after = board.abs_grid(board.read_phone(board.grab_phone()))
                if after != before:
                    ok = True
                    break
                print(f"[retry] 第 {attempt+1} 次点击未生效，重试")
                time.sleep(1.5)
            if not ok:
                print("[end] 点击走子失败，停止")
                show(board.abs_grid(board.read_phone(board.grab_phone())))
                return

            piece = apply_move(state, mv)
            print(f"[cnt] 我方第 {done+1} 步完成 ({piece})")
            done += 1

            # ---------- 等对手回应 ----------
            cur = wait_board_change(board, colors_of(state), timeout=60)
            if cur is None:
                print("[end] 对手长时间未走（可能终局/认输）")
                return
            mv2 = diff_move(state, cur)
            if mv2:
                print(f"[op] 对手走: {mv2[0]} ({mv2[1]})")
                apply_move(state, mv2[0])
            else:
                print("[end] 对手着法解析失败，停止")
                show(cur)
                return
    finally:
        if proc:
            proc.terminate()
            print("[api] 引擎接口已关闭")


if __name__ == "__main__":
    main()
