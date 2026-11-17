# -*- coding: utf-8 -*-
"""
autoplay.py —— FOA-AL00 象棋自动对弈主控（完整业务闭环）
==========================================================
把三个业务串在一起，循环至对局结束：

  1. 复用 watch_moves 的截图识别：每 350ms 抓取投屏窗口，棋盘区域指纹变化时
     才做完整 90 交点采样，解析出「哪颗棋从哪走到哪」（如 h2e2）。
  2. 将新动向以追加方式拼进 moves 序列，POST 到
     http://127.0.0.1:8765/api/bestmove （等价于
     GET /api/bestmove?fen=startpos&moves=h2e2,h9g7&movetime=1000，
     长序列下 POST 更稳），取回 bestmove 字段。
  3. 依据 象棋坐标系.txt 的交点坐标（自动标定修正累积误差后）依次点击
     起点格、终点格，完成走子，并回读画面验证走子生效。
  4. 循环往复，直至棋盘消失（对局结束/结算界面）或引擎无数可走。

性能设计：
  - 等待对手时只做 1~2ms 的降采样指纹比对，变化才触发全量采样（cv2 卷积 ~20ms）
  - 引擎思考 1000ms（可调 --movetime），点击间隔 320ms，一步全程 ~2s
  - 服务端未启动时自动拉起 pikafish_server.py（8 线程 / 512MB 哈希）

可靠性设计：
  - 每一步都与本地子力跟踪（FEN 推演）做占用一致性校验，不一致即报警/终止
  - 我方走子点击后 3 秒内验证生效，失败自动补点（最多 3 次）
  - 引擎收到非法着法序列会报错 —— 天然着法合法性校验，防跟踪失步
  - 对局中自动重新标定网格；窗口被遮挡/最小化自动置顶恢复

用法：
    python autoplay.py                       # 标准开局自动下棋（默认执屏幕下方一方）
    python autoplay.py --movetime 1500       # 每步思考 1.5 秒
    python autoplay.py --side top            # 强制执屏幕上方一方（默认自动=下方）
    python autoplay.py --fen "..." --moves h2e2,h9g7   # 从残局接入
    python autoplay.py --dump                # 只打印识别到的局面后退出（调试）
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
import urllib.request

import cv2
import numpy as np
from PIL import Image, ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from watch_moves import (CoordSystem, Sampler, board_present, calibrate,
                         chinese_notation, diff_moves, find_window, PIECE_CN)

REF_W, REF_H = 343, 763          # 象棋坐标系.txt 的图像空间
WIN_TITLE = "FOA-AL00"
API_BASE = "http://127.0.0.1:8765"
START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"
FILES = "abcdefghi"

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()


def log(msg, also=None):
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if also is not None:
        also.write(line + "\n")
        also.flush()


# --------------------------------------------------------------------------- #
# 窗口：抓图 + 置顶 + 点击（343x763 参考坐标 -> 屏幕像素）
# --------------------------------------------------------------------------- #
class Win:
    def __init__(self, title=WIN_TITLE):
        self.hwnd = find_window(title)
        self._client = None

    def client(self, refresh=False):
        if self._client is None or refresh:
            r = wt.RECT()
            user32.GetClientRect(self.hwnd, ctypes.byref(r))
            pt = wt.POINT(0, 0)
            user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
            self._client = (pt.x, pt.y, r.right, r.bottom)
        return self._client

    def activate(self) -> bool:
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 9)
            time.sleep(0.4)
        for _ in range(4):
            # ALT 键技巧：绕过 Windows 前台锁定限制
            user32.keybd_event(0x12, 0, 0, 0)               # ALT down
            user32.SetForegroundWindow(self.hwnd)
            user32.keybd_event(0x12, 0, 2, 0)               # ALT up
            time.sleep(0.15)
            if user32.GetForegroundWindow() == self.hwnd:
                return True
            try:
                user32.SwitchToThisWindow(self.hwnd, True)
            except Exception:
                pass
            time.sleep(0.25)
            if user32.GetForegroundWindow() == self.hwnd:
                return True
        return False

    def grab(self) -> Image.Image:
        x, y, w, h = self.client(refresh=True)
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        if img.size != (REF_W, REF_H):
            img = img.resize((REF_W, REF_H), Image.LANCZOS)
        return img

    def click_ref(self, gx, gy):
        """在 343x763 参考坐标系指定的点模拟一次鼠标点击（确保命中本窗口）。"""
        x, y, w, h = self.client()
        sx = int(round(x + gx * w / REF_W))
        sy = int(round(y + gy * h / REF_H))
        pt = wt.POINT(sx, sy)
        if user32.WindowFromPoint(pt) != self.hwnd:          # 被遮挡 -> 置顶后复检
            self.activate()
            time.sleep(0.15)
            if user32.WindowFromPoint(pt) != self.hwnd:
                raise RuntimeError(f"点击点({sx},{sy})被其他窗口遮挡，拒绝盲点")
        user32.SetCursorPos(sx, sy)
        time.sleep(0.06)
        user32.mouse_event(0x0002, 0, 0, 0, 0)      # LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)      # LEFTUP


# --------------------------------------------------------------------------- #
# 引擎接口
# --------------------------------------------------------------------------- #
def api_health():
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def ensure_server(spawn=True):
    """服务已在跑则返回 None，否则拉起一个实例。"""
    if api_health():
        return None
    if not spawn:
        raise SystemExit("接口服务未运行：python pikafish_server.py --port 8765")
    logf = open(os.path.join(HERE, "_autoplay_api.log"), "w", encoding="utf-8")
    flags = 0x08000000 if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "pikafish_server.py"),
         "--port", "8765", "--instances", "1", "--threads", "8",
         "--hash", "512", "--quiet"],
        stdout=logf, stderr=logf, creationflags=flags)
    for _ in range(40):
        time.sleep(0.5)
        if api_health():
            log("[api] 已自动拉起 pikafish_server.py（8 线程 / 512MB 哈希）")
            return proc
    raise SystemExit("接口服务启动失败，详见 C:\\pikafish\\api\\_autoplay_api.log")


def ask_bestmove(base_fen, moves, movetime, timeout=90):
    """POST /api/bestmove，等价 GET ?fen=...&moves=...&movetime=..."""
    body = {"fen": base_fen, "movetime": movetime,
            "threads": 8, "hash": 512, "multipv": 1}
    if moves:
        body["moves"] = ",".join(moves)
    req = urllib.request.Request(
        f"{API_BASE}/api/bestmove", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:            # 非法着法序列等 -> 500 + JSON 错误体
        try:
            data = json.loads(e.read().decode())
        except Exception:
            raise RuntimeError(f"接口 HTTP {e.code} 错误") from None
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"引擎拒绝请求: {err.get('message', err)}")
    return data


# --------------------------------------------------------------------------- #
# 坐标换算（屏幕交点 <-> ICCS，方向自适应红上/红下）
# --------------------------------------------------------------------------- #
def detect_red_top(occ):
    top = sum(1 for row in occ[:3] for c in row if c == "R")
    bot = sum(1 for row in occ[7:] for c in row if c == "R")
    return top >= bot


def fen_board_screen(fen, red_top):
    """FEN -> 屏幕交点 {(col,row): FEN字符}。red_top=True 时红在屏幕上方。"""
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


def colors_of(pieces):
    board = [[None] * 9 for _ in range(10)]
    for (c, r), ch in pieces.items():
        if ch:                                   # 允许 None/''（被吃/占位）
            board[r][c] = "R" if ch.isupper() else "B"
    return board


def fen_text(fen):
    return START_FEN if str(fen).strip().lower() in ("startpos", "start", "") else fen


def to_iccs(sq, red_top):
    c, r = sq
    return (FILES[8 - c] if red_top else FILES[c]) + (str(r) if red_top else str(9 - r))


def from_iccs(tag, red_top):
    c = FILES.index(tag[0])
    r = int(tag[1])
    return (8 - c, r) if red_top else (c, 9 - r)


def show(occ, title=""):
    if title:
        print(f"  {title}")
    print("      " + " ".join(f"{FILES[8 - i]:>2}" for i in range(9)))
    for row in range(10):
        line = " ".join("●" if occ[row][c] == "R" else "○" if occ[row][c] == "B" else "·"
                        for c in range(9))
        print(f"  {row:2d}  {line}")


# --------------------------------------------------------------------------- #
# 大厅操作：模式选择页（5分钟/10分钟/15分钟/超快 四个按钮）
# --------------------------------------------------------------------------- #
MODE_BUTTONS = {"5": (47, 572), "10": (127, 572), "15": (207, 572), "turbo": (296, 572)}


def click_mode(win, mode, logf=None):
    """点击模式按钮并验证画面确实切换（与点击前帧对比）；失败重试，最多 4 次。"""
    gx, gy = MODE_BUTTONS[mode]
    name = {"5": "5分钟", "10": "10分钟", "15": "15分钟", "turbo": "超快"}[mode]
    if not win.activate():
        log("[warn] 窗口未能置顶，模式点击可能无效", logf)
    for attempt in range(1, 5):
        before = np.asarray(win.grab().convert("L"), dtype=np.int16)
        win.click_ref(gx, gy)
        time.sleep(2.5)
        after = np.asarray(win.grab().convert("L"), dtype=np.int16)
        diff = float(np.abs(after - before).mean())
        if diff > 3.0:                              # 整屏切换（按钮数字跳动仅 ~0.5）
            log(f"[hall] 已点击「{name}」开始匹配（第 {attempt} 次尝试，diff={diff:.1f}）", logf)
            return True
        log(f"[warn] 第 {attempt} 次点击「{name}」未生效（diff={diff:.1f}），重试", logf)
    log("[err] 模式按钮点击始终无效 —— 请手动开始对局", logf)
    return False


# --------------------------------------------------------------------------- #
# 主控
# --------------------------------------------------------------------------- #
class Autoplayer:
    def __init__(self, args):
        self.args = args
        self.coords = CoordSystem()
        self.win = Win()
        self.grid = None            # (col,row) -> (x,y) 343x763 空间（标定后）
        self.sampler = None
        self.moves = [m.strip().lower() for m in (args.moves or "").split(",") if m.strip()]
        self.base_fen = args.fen or "startpos"
        self.red_top = None
        self.our_color = None       # 'r'/'b'
        self.pieces = {}            # 屏幕交点 -> FEN 字符
        self.occ = None             # 最近一次采样的颜色占用
        self.logf = open(args.log, "a", encoding="utf-8")
        self.api_proc = ensure_server(not args.no_spawn)
        self._fp = None
        self._grid_desc = None

    # ---------- 采样 ---------- #
    def set_grid(self, img):
        grid = None
        if not self.args.no_calib:
            cal = calibrate(img)
            if cal:
                GX, GY, hits = cal
                grid = {(c, r): (GX[c], GY[r]) for r in range(10) for c in range(9)}
        if grid is None:
            grid = dict(self.coords.px)
        desc = (round(grid[(0, 0)][0], 1), round(grid[(0, 0)][1], 1),
                round(grid[(8, 0)][0] - grid[(0, 0)][0], 1),
                round(grid[(0, 9)][1] - grid[(0, 0)][1], 1))
        if desc != self._grid_desc:                 # 网格变化才记录，避免日志刷屏
            self._grid_desc = desc
            if not self.args.no_calib and grid is not dict(self.coords.px):
                log(f"[calib] 标定：原点 ({desc[0]},{desc[1]})，列距 {desc[2]}px，行距 {desc[3]}px",
                    self.logf)
            else:
                log("[calib] 自动标定失败，使用 象棋坐标系.txt 原始坐标（±10px 吸附）", self.logf)
        self.grid = grid
        self.sampler = Sampler(self.coords, grid)

    def sample(self, img=None):
        """纯采样：返回当前占用矩阵，【不】修改基线 self.occ。"""
        img = img or self.win.grab()
        return self.sampler.occupy(self.sampler.sample(img))

    def fingerprint(self, img):
        """棋盘区域降采样灰度指纹（~1ms），用于廉价变化检测。"""
        a = np.asarray(img.convert("L"), dtype=np.int16)
        y0 = int(self.grid[(0, 0)][1]) - 24
        y1 = int(self.grid[(0, 9)][1]) + 24
        x0 = int(self.grid[(0, 0)][0]) - 24
        x1 = int(self.grid[(8, 0)][0]) + 24
        crop = a[max(0, y0):min(REF_H, y1), max(0, x0):min(REF_W, x1)]
        return cv2.resize(crop, (80, 90), interpolation=cv2.INTER_AREA)

    @staticmethod
    def fp_diff(a, b):
        return float(np.abs(a - b).mean())

    # ---------- 对局生命周期 ---------- #
    def start_tracking(self, occ):
        self.red_top = detect_red_top(occ)
        # 默认我方 = 屏幕下方一方（JJ象棋己方棋子总在下方）；--side 可强制指定
        self.our_color = "b" if self.red_top else "r"
        if self.args.side:
            self.our_color = "r" if self.args.side == "top" else "b"
        self.pieces = fen_board_screen(fen_text(self.base_fen), self.red_top)
        for mv in self.moves:                       # 应用种子着法
            f = from_iccs(mv[:2], self.red_top)
            t = from_iccs(mv[2:4], self.red_top)
            self.pieces[t] = self.pieces.pop(f, None) or self.pieces.get(t, "")
        pred = colors_of(self.pieces)
        n = sum(1 for row in occ for c in row if c)
        if pred != occ:
            log("[warn] 画面占用与 FEN 推演不一致（差 "
                f"{sum(1 for r in range(10) for c in range(9) if pred[r][c] != occ[r][c])} 格）"
                " —— 若非标准开局请用 --fen/--moves 指定", self.logf)
            show(occ, "屏幕识别：")
            show(pred, "FEN 推演：")
        who = "红" if self.our_color == "r" else "黑"
        pos = "上" if (self.our_color == "r") == self.red_top else "下"
        log(f"[game] 开始跟踪：屏幕 {n} 子，红在{'上' if self.red_top else '下'}方，"
            f"我方执{who}（屏幕{pos}方），已走 {len(self.moves)} 步", self.logf)

    def apply_move(self, frm, to):
        ch = self.pieces.pop(frm, None)
        self.pieces[to] = ch

    def side_to_move(self):
        return "r" if len(self.moves) % 2 == 0 else "b"

    # ---------- 等待画面进入某种状态 ---------- #
    def wait_until(self, pred, timeout, interval=0.3, quiet_fp=True):
        """pred(occ) 为真时返回 occ；指纹变化才做全量采样，每 2.5s 强制全采样兜底。"""
        t0 = time.time()
        img = self.win.grab()
        self._fp = self.fingerprint(img)
        last_full = time.time()
        while time.time() - t0 < timeout:
            time.sleep(interval)
            img = self.win.grab()
            fp = self.fingerprint(img)
            force = time.time() - last_full >= 2.5          # 指纹失灵时的兜底
            if quiet_fp and not force and self.fp_diff(fp, self._fp) < 0.8:
                continue
            self._fp = fp
            last_full = time.time()
            occ = self.sample(img)
            if pred(occ):
                return occ
        return None

    def stabilize(self, occ, rounds=3, interval=0.25):
        """连续两次采样一致才认为动画结束。"""
        for _ in range(rounds):
            time.sleep(interval)
            nxt = self.sample()
            if nxt == occ:
                return occ
            occ = nxt
        return occ

    # ---------- 我方走子 ---------- #
    def click_cell(self, sq):
        x, y = self.grid[sq]
        self.win.click_ref(x, y)

    def do_our_move(self):
        stm = self.side_to_move()
        try:
            res = ask_bestmove(self.base_fen, self.moves, self.args.movetime)
        except RuntimeError as exc:
            log(f"[err] {exc} —— 着法序列与棋盘可能失步，终止", self.logf)
            show(self.occ, "当前屏幕识别：")
            return False
        mv = (res.get("bestmove") or "").strip()
        if not mv or mv in ("(none)", "0000"):
            log("[end] 引擎判定无着可走（被将死/困毙），对局结束", self.logf)
            return False
        frm = from_iccs(mv[:2], self.red_top)
        to = from_iccs(mv[2:4], self.red_top)
        piece = self.pieces.get(frm, "?")
        cn = chinese_notation(piece, frm, to, self.red_top) if piece not in ("?", None) else ""

        # 点击前确认窗口在前台
        if not self.win.activate():
            log("[warn] 窗口未能置顶，点击可能无效", self.logf)

        expected = colors_of({**self.pieces, frm: None, to: piece})
        ok = False
        for attempt in range(1, self.args.click_retry + 1):
            self.click_cell(frm)
            time.sleep(self.args.click_gap)
            self.click_cell(to)
            got = self.wait_until(lambda o: o == expected, timeout=3.0)
            if got is not None:
                got = self.stabilize(got)
                ok = True
                break
            log(f"[warn] 第 {attempt} 次点击未见生效，重试", self.logf)
            if attempt < self.args.click_retry:      # 先点一下起点取消可能的选中态
                self.click_cell(frm)
                time.sleep(0.25)
        if not ok:
            log("[err] 我方走子点击未生效（棋盘与跟踪可能失步），终止", self.logf)
            show(self.occ, "屏幕识别：")
            show(expected, "期望局面：")
            return False

        self.moves.append(mv[:4])
        self.apply_move(frm, to)
        self.occ = got                              # 更新基线，供对手等待阶段比对
        score = res.get("score")
        sc = f"{score:+d}" if isinstance(score, int) else str(score)
        log(f"[me] {mv[:4]}  {'' if not cn else cn + '  '}"
            f"score={sc} depth={res.get('depth')} nodes={res.get('nodes')} "
            f"time={res.get('time_ms')}ms  （第 {len(self.moves)} 步）", self.logf)
        return True

    # ---------- 等待并解析对手 ---------- #
    def try_apply_opp(self, frm, to, got):
        """校验并应用一步对手走子；成功返回 True。"""
        piece = self.pieces.get(frm)
        if piece is None:
            log(f"[err] 起点交点 {to_iccs(frm, self.red_top)} 处无子（失步）", self.logf)
            return False
        expected = colors_of({**self.pieces, frm: None, to: piece})
        if expected != got:
            log(f"[err] 走子 {to_iccs(frm, self.red_top)}{to_iccs(to, self.red_top)} "
                "应用后与画面不一致（识别误差）", self.logf)
            return False
        tag = to_iccs(frm, self.red_top) + to_iccs(to, self.red_top)
        cn = chinese_notation(piece, frm, to, self.red_top)
        log(f"[op] {tag}  {cn}  {'红' if piece.isupper() else '黑'}{PIECE_CN[piece]}"
            f"  （第 {len(self.moves) + 1} 步）", self.logf)
        self.moves.append(tag)
        self.apply_move(frm, to)
        self.occ = got
        return True

    def wait_opponent(self):
        before = len(self.moves)
        opp_is_red = self.side_to_move() == "r"

        def changed(o):
            return o != self.occ and board_present(o)

        t0 = time.time()
        last_warn = 0.0
        while True:
            got = self.wait_until(changed, timeout=self.args.stall_sec, quiet_fp=True)
            if got is None:                          # 这一段时间无「检测到」的变化
                cur = self.sample()                  # 纯采样，不动基线
                if not board_present(cur):
                    log("[end] 棋盘消失 —— 对局结束", self.logf)
                    return "over"
                if cur != self.occ:                  # 对手走了但指纹没触发 -> 立即补解析
                    got = cur
                else:
                    waited = time.time() - t0
                    if waited >= self.args.stall_sec and waited - last_warn >= self.args.stall_sec:
                        last_warn = waited
                        log(f"[wait] 对手已 {waited:.0f}s 未走子（每 {self.args.stall_sec:.0f}s 提醒，"
                            f"{self.args.hard_wait}s 放弃）", self.logf)
                    if waited >= self.args.hard_wait:
                        log("[err] 对手长时间未走子且画面无变化，放弃等待", self.logf)
                        return "stall"
                    continue

            # 变化解析（带重试：容忍「将军」浮层 / 走子动画等瞬态干扰）
            for attempt in range(4):
                got = self.stabilize(got)
                if not board_present(got):
                    log("[end] 棋盘消失 —— 对局结束", self.logf)
                    return "over"
                pairs = diff_moves(self.occ, got)
                if len(pairs) == 1:
                    frm, to = pairs[0]
                    piece = self.pieces.get(frm)
                    if piece is not None and piece.isupper() == opp_is_red:
                        if self.try_apply_opp(frm, to, got):
                            return "moved"
                elif len(pairs) > 1:
                    log(f"[warn] 一次采样出现 {len(pairs)} 处变化，等待画面稳定后重试", self.logf)
                time.sleep(0.8)
                nxt = self.sample()
                if nxt == self.occ:                  # 瞬态浮层已消失，回到等待
                    break
                got = nxt
            else:
                log("[err] 对手走子解析失败（棋盘与跟踪可能失步），终止", self.logf)
                show(self.occ, "屏幕识别（走前）：")
                show(got, "屏幕识别（走后）：")
                return "desync"

    # ---------- 主循环 ---------- #
    def run(self):
        log(f"[win] hwnd={self.win.hwnd}", self.logf)
        if not self.win.activate():
            log("[warn] 窗口未能置顶，请保持投屏窗口可见", self.logf)
        if self.args.start:
            if not click_mode(self.win, self.args.start, self.logf):
                self.logf.close()
                return "no_click"

        # 等待进入对局（标准开局，或与 --fen/--moves 推演一致的占用）
        warned = False
        t_board = time.time()
        while True:
            img = self.win.grab()
            self.set_grid(img)
            occ = self.sample(img)
            if board_present(occ):
                break
            if not warned:
                log("[status] 未检测到对局棋盘（大厅/匹配中？），等待对局开始 ...", self.logf)
                warned = True
            if time.time() - t_board > self.args.board_wait:
                log("[err] 等待棋盘超时 —— 未进入对局", self.logf)
                self.logf.close()
                return "no_game"
            time.sleep(1.5)

        self.red_top = detect_red_top(occ)
        expected_start = colors_of(fen_board_screen(fen_text(self.base_fen), self.red_top))
        if occ != expected_start and not self.moves and not self.args.fen:
            log("[status] 检测到棋盘但不是标准开局 —— 等待新一局（如需残局接入用 "
                "--fen/--moves）", self.logf)
            show(occ, "当前局面：")
            t_new = time.time()
            last_note = 0.0
            while True:
                if time.time() - t_new > self.args.board_wait:
                    log("[err] 长时间未等到标准开局，退出", self.logf)
                    self.logf.close()
                    return "no_game"
                time.sleep(2.0)
                occ = self.sample()
                if not board_present(occ):
                    continue
                self.set_grid(self.win.grab())
                self.red_top = detect_red_top(occ)
                expected_start = colors_of(fen_board_screen(START_FEN, self.red_top))
                if occ == expected_start:
                    break
                if time.time() - last_note >= 30:
                    last_note = time.time()
                    log(f"[status] 仍在等待标准开局（已 {time.time()-t_new:.0f}s）", self.logf)
            log("[status] 检测到标准开局！", self.logf)

        self.start_tracking(occ)
        self.occ = occ                               # 同步基线

        # 引擎预热（加载 NNUE、分配哈希，避免第一手变慢）
        try:
            ask_bestmove("startpos", [], 200)
            log("[api] 引擎预热完成", self.logf)
        except Exception:
            pass

        result = None
        try:
            while True:
                stm = self.side_to_move()
                if stm == self.our_color:
                    if not self.do_our_move():
                        result = "finished"
                        break
                else:
                    st = self.wait_opponent()
                    if st in ("over", "stall", "desync"):
                        result = st
                        break
        except KeyboardInterrupt:
            log("\n[exit] 手动停止", self.logf)
            result = "interrupted"
        except Exception:
            log("[err] 未捕获异常：\n" + traceback.format_exc(), self.logf)
            result = "error"
        finally:
            if result in ("finished", "over"):
                log(f"[done] 对局结束，共 {len(self.moves)} 步。着法序列：\n"
                    f"       moves={','.join(self.moves)}", self.logf)
            self.logf.close()
        return result


def main():
    ap = argparse.ArgumentParser(description="FOA-AL00 象棋自动对弈主控")
    ap.add_argument("--movetime", type=int, default=1000, help="引擎每步思考毫秒，默认 1000")
    ap.add_argument("--side", choices=["top", "bottom"], default=None,
                    help="强制我方在屏幕上/下方（默认自动=下方一方）")
    ap.add_argument("--fen", default=None, help="起始局面 FEN（默认 startpos）")
    ap.add_argument("--moves", default="", help="种子着法序列，如 h2e2,h9g7")
    ap.add_argument("--click-gap", type=float, default=0.32, help="两次点击间隔秒")
    ap.add_argument("--click-retry", type=int, default=3, help="走子点击最大尝试次数")
    ap.add_argument("--stall-sec", type=float, default=20.0, help="对手无动作提醒阈值秒")
    ap.add_argument("--hard-wait", type=float, default=90.0, help="对手无动作放弃阈值秒")
    ap.add_argument("--start", choices=["5", "10", "15", "turbo"], default=None,
                    help="自动点击大厅模式按钮开始匹配（5/10/15/turbo=超快）")
    ap.add_argument("--board-wait", type=float, default=180.0,
                    help="匹配后等待棋盘出现的最长时间秒")
    ap.add_argument("--no-calib", action="store_true", help="不做自动标定，直接用 txt 坐标")
    ap.add_argument("--no-spawn", action="store_true", help="接口服务未启动时不自动拉起")
    ap.add_argument("--dump", action="store_true", help="只识别一次当前局面后退出")
    ap.add_argument("--log", default=os.path.join(HERE, "autoplay.log"))
    args = ap.parse_args()

    p = Autoplayer(args)
    if args.dump:
        p.win.activate()
        img = p.win.grab()
        p.set_grid(img)
        occ = p.sample(img)
        show(occ, "当前棋子分布（●红 ○黑）：")
        n = sum(1 for row in occ for c in row if c)
        print(f"[stat] 棋子数 {n}  红在{'上' if detect_red_top(occ) else '下'}方")
        return
    p.run()


if __name__ == "__main__":
    main()
