# -*- coding: utf-8 -*-
"""
pikafish_local.py —— Pikafish 引擎本地直连封装（进程内 UCI，替代 HTTP 接口）

与 HTTP 接口功能等价：
    bestmove(moves=["h2e2","h9g7"], movetime=1000)
        == GET http://127.0.0.1:8765/api/bestmove?fen=startpos&moves=h2e2,h9g7&movetime=1000

性能优势：
  - 无 HTTP/JSON 序列化开销（每步省 ~5-15ms 及服务端调度）
  - 引擎常驻单实例，独占全部分配线程，Hash 更大 -> 同样 movetime 下搜得更深
  - 进程崩溃自动重生（带一次重试）
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time

ENGINE_EXE = r"C:\pikafish\Pikafish-Windows-x86-64-universal.exe"
ENGINE_DIR = r"C:\pikafish"
NNUE_FILE = r"C:\pikafish\pikafish.nnue"

START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"
_FILES = "abcdefghi"


class EngineDead(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 着法合法性预校验：Pikafish 收到非法着法会直接崩溃退出，必须先过滤
# --------------------------------------------------------------------------- #
def parse_fen(fen: str) -> dict:
    """FEN -> {(file 0..8=a..i, rank 0..9)}，rank 0 = 红方底线（与 UCI 一致）。"""
    board = {}
    for i, line in enumerate(fen.split()[0].split("/")):
        c = 0
        for ch in line:
            if ch.isdigit():
                c += int(ch)
            else:
                board[(c, 9 - i)] = ch
                c += 1
    return board


def _legal(board: dict, frm: tuple, to: tuple) -> bool:
    p = board.get(frm)
    if not p:
        return False
    t = board.get(to)
    if t and t.isupper() == p.isupper():
        return False                                  # 不能吃己方子
    red = p.isupper()
    fc, fr = frm
    tc, tr = to
    if not (0 <= tc <= 8 and 0 <= tr <= 9) or frm == to:
        return False
    dc, dr = tc - fc, tr - fr
    P = p.upper()

    def palace(c, r):
        return 3 <= c <= 5 and ((0 <= r <= 2) if red else (7 <= r <= 9))

    def own(r):
        return (r <= 4) if red else (r >= 5)

    if P in ("R", "K", "C"):                          # 直线子
        if dc != 0 and dr != 0:
            return False
        if P == "K":
            return (abs(dc) + abs(dr) == 1
                    and palace(fc, fr) and palace(tc, tr))
        screens = 0
        if dc == 0:
            step = 1 if tr > fr else -1
            for r in range(fr + step, tr, step):
                if board.get((fc, r)):
                    screens += 1
        else:
            step = 1 if tc > fc else -1
            for c in range(fc + step, tc, step):
                if board.get((c, fr)):
                    screens += 1
        if P == "R":
            return screens == 0
        return screens == (1 if t else 0)             # 炮：走无架、吃一架
    if P == "N":
        if (abs(dc), abs(dr)) not in ((1, 2), (2, 1)):
            return False
        leg = (fc + dc // 2, fr) if abs(dc) == 2 else (fc, fr + dr // 2)
        return not board.get(leg)
    if P == "B":
        if abs(dc) != 2 or abs(dr) != 2:
            return False
        if board.get(((fc + tc) // 2, (fr + tr) // 2)):
            return False
        return own(tr)
    if P == "A":
        return (abs(dc) == 1 and abs(dr) == 1
                and palace(fc, fr) and palace(tc, tr))
    if P == "P":
        fwd = 1 if red else -1
        if dc == 0 and dr == fwd:
            return True
        return dr == 0 and abs(dc) == 1 and not own(fr)   # 过河后可横走
    return False


def _attacked(board, target_sq, by_upper):
    """target_sq 上的棋子是否被 by_upper 一方攻击（借助着法规则反查）。"""
    return any(_legal(board, sq, target_sq)
               for sq, v in board.items() if v.isupper() == by_upper)


def fen_sane(fen: str) -> bool:
    """FEN 基础体检。Pikafish 收到非法局面（双将/将帅对脸/士象越界/
    兵在身后/未走子方被将军/行列结构错等）不会拒绝而是直接崩溃退出，
    所以发给引擎前必须过滤。实测崩溃项：对方正被将军、行列宽错、子力超限。"""
    parts = str(fen).split()
    if not parts:
        return False
    rows = parts[0].split("/")
    if len(rows) != 10:
        return False
    for row in rows:
        n = 0
        for ch in row:
            if ch.isdigit():
                if ch == "0":
                    return False
                n += int(ch)
            elif ch in "rnbaekcpRNBAEKCP":
                n += 1
            else:
                return False
        if n != 9:
            return False
    try:
        board = parse_fen(str(fen))
    except Exception:
        return False
    if not board:
        return False
    vals = list(board.values())
    if any(v not in "RNBAKCP" and v not in "rnbakcp" for v in vals):
        return False                              # 出现了不存在的棋子字符
    if vals.count("K") != 1 or vals.count("k") != 1:
        return False
    for up, lim in (("R", 2), ("N", 2), ("C", 2), ("A", 2), ("B", 2), ("P", 5)):
        if sum(1 for v in vals if v.upper() == up and v.isupper()) > lim:
            return False
        if sum(1 for v in vals if v.upper() == up and not v.isupper()) > lim:
            return False
    try:
        rk = next(pos for pos, v in board.items() if v == "K")
        bk = next(pos for pos, v in board.items() if v == "k")
    except StopIteration:
        return False
    for (c, r), v in board.items():
        if v in ("K", "k") and not (3 <= c <= 5
                                    and ((7 <= r <= 9) if v == "k"
                                         else (0 <= r <= 2))):
            return False                          # 帅/将不在九宫
        if v in ("A", "a") and not (3 <= c <= 5
                                    and ((0 <= r <= 2) if v.isupper()
                                         else (7 <= r <= 9))):
            return False                          # 仕（红，下宫）/士（黑，上宫）
        if v in ("B", "b"):
            if v == "B" and r > 4:
                return False                      # 相不能过河
            if v == "b" and r < 5:
                return False                      # 象不能过河
        if v == "P" and r <= 2:
            return False                          # 兵不可能在自己底线两排
        if v == "p" and r >= 7:
            return False
    if rk[0] == bk[0] and all((rk[0], y) not in board
                              for y in range(rk[1] + 1, bk[1])):
        return False                              # 将帅对脸
    stm = parts[1] if len(parts) > 1 else "w"
    if stm not in ("w", "b"):
        return False
    # 未走子方不能正被将军（轮红走时黑不能被将，反之亦然）——实测会让引擎退出
    if stm == "w":
        if _attacked(board, bk, True):
            return False
    else:
        if _attacked(board, rk, False):
            return False
    return True


def validate_sequence(fen: str, moves) -> None:
    """逐着校验着法序列；非法时抛 ValueError（避免引擎崩溃）。
    含送王检查：一步走完后自家王不能正被将军（内部失步时可能记入
    这种着法，重放出的局面会让引擎直接崩溃退出）。"""
    board = parse_fen(START_FEN if str(fen).strip().lower() in ("startpos", "start", "")
                      else fen)
    for k, mv in enumerate(moves):
        mv = mv.strip().lower()
        if len(mv) < 4 or mv[0] not in _FILES or mv[2] not in _FILES \
                or not mv[1].isdigit() or not mv[3].isdigit():
            raise ValueError(f"第 {k + 1} 步 {mv!r} 格式非法")
        frm = (_FILES.index(mv[0]), int(mv[1]))
        to = (_FILES.index(mv[2]), int(mv[3]))
        if not _legal(board, frm, to):
            raise ValueError(f"第 {k + 1} 步 {mv} 在当前局面下不合法")
        piece = board.pop(frm)
        board[to] = piece
        # 走子方王的位置：可能就是被吃的目标（吃王不可能出现，防御性检查）
        red = piece.isupper()
        ksq = None
        for sq, v in board.items():
            if v == ("K" if red else "k"):
                ksq = sq
                break
        if ksq is None:
            raise ValueError(f"第 {k + 1} 步 {mv} 后走子方王不存在")
        if _attacked(board, ksq, not red):
            raise ValueError(f"第 {k + 1} 步 {mv} 送王（走后自家王被将军）")


def _default_threads() -> int:
    cpu = os.cpu_count() or 4
    return max(1, min(8, cpu - 1))


class LocalEngine:
    """单个 Pikafish 常驻进程；bestmove() 线程安全（内部加锁）。"""

    def __init__(self, exe: str = ENGINE_EXE, cwd: str = ENGINE_DIR,
                 threads: int | None = None, hash_mb: int = 256,
                 nnue: str = NNUE_FILE):
        self.exe = exe
        self.cwd = cwd
        self.threads = threads or _default_threads()
        self.hash_mb = hash_mb
        self.nnue = nnue if os.path.isfile(nnue) else None
        self._lock = threading.Lock()
        self.proc = None
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._spawn()

    # ---------------- 进程管理 ---------------- #
    def _spawn(self) -> None:
        if not os.path.isfile(self.exe):
            raise FileNotFoundError(f"找不到引擎: {self.exe}")
        flags = 0x08000000 if os.name == "nt" else 0      # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [self.exe], cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags)
        self._lines = queue.Queue()
        threading.Thread(target=self._reader_loop, daemon=True).start()
        self._send("uci")
        self._read_until(lambda l: l.strip() == "uciok", 20.0)
        self._send(f"setoption name Threads value {self.threads}")
        self._send(f"setoption name Hash value {self.hash_mb}")
        if self.nnue:
            self._send(f"setoption name EvalFile value {self.nnue}")
        self._send("isready")
        self._read_until(lambda l: l.strip() == "readyok", 20.0)

    def _reader_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                self._lines.put(line.rstrip("\r\n"))
        except Exception:
            pass
        self._lines.put(None)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        try:
            if self.alive():
                self._send("quit")
                self.proc.wait(timeout=3)
        except Exception:
            pass

    # ---------------- 读写 ---------------- #
    def _send(self, cmd: str) -> None:
        if not self.alive():
            raise EngineDead("引擎进程已退出")
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise EngineDead(f"向引擎写入失败: {exc}") from exc

    def _read_line(self, timeout: float) -> str:
        try:
            line = self._lines.get(timeout=max(timeout, 0.001))
        except queue.Empty:
            raise TimeoutError()
        if line is None:
            raise EngineDead("引擎进程意外退出")
        return line

    def _read_until(self, predicate, timeout: float) -> list[str]:
        collected = []
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TimeoutError("等待引擎响应超时")
            collected.append(self._read_line(remain))
            if predicate(collected[-1]):
                return collected

    # ---------------- 搜索 ---------------- #
    @staticmethod
    def _parse_info(line: str) -> dict:
        out = {}
        t = line.split()
        for i, tok in enumerate(t):
            if tok in ("depth", "nodes", "nps", "multipv", "seldepth", "hashfull") \
                    and i + 1 < len(t):
                try:
                    out[tok] = int(t[i + 1])
                except ValueError:
                    pass
            elif tok == "score" and i + 2 < len(t):
                kind, val = t[i + 1], t[i + 2]
                if kind in ("cp", "mate") and (val.lstrip("-").isdigit()):
                    out["score"] = {"type": kind, "value": int(val)}
            elif tok == "pv" and i + 1 < len(t):
                out["pv"] = t[i + 1:]
        return out

    def bestmove(self, moves=(), movetime: int = 1000, fen: str = "startpos") -> dict:
        """position {fen} moves {..} + go movetime {ms} -> 结果 dict。
        与 HTTP /api/bestmove 返回字段兼容：
        bestmove/ponder/score{type,value}/depth/time_ms/nodes/nps/pv/game_over"""
        fen_in = str(fen).strip()
        if fen_in.lower() not in ("startpos", "start", "") \
                and not fen_sane(fen_in):
            raise ValueError(f"FEN 体检未过，拒发引擎: {fen_in}")
        moves = [m.strip().lower() for m in moves if m and m.strip()]
        validate_sequence(fen_in, moves)              # 非法着法 -> ValueError
        # 注意：FEN 中红方棋子是大写字母，绝不能 lower()（会把局面变成
        # 非法双黑而让引擎崩溃）——只对用于比较的关键字做小写
        if fen_in.lower() in ("startpos", "start", ""):
            pos = "position startpos"
        else:
            pos = "position fen " + fen_in
        if moves:
            pos += " moves " + " ".join(moves)
        return self._search(pos, movetime)

    def _search(self, pos_cmd: str, movetime: int, _retried: bool = False) -> dict:
        with self._lock:
            try:
                return self._search_once(pos_cmd, movetime)
            except (EngineDead, TimeoutError):
                if _retried:
                    raise RuntimeError("引擎两次搜索均失败（着法序列可能非法）") from None
                # 引擎崩溃/超时：重生进程后重试一次
                try:
                    self.proc.kill()
                except Exception:
                    pass
                self._spawn()
                return self._search_once(pos_cmd, movetime)

    def _search_once(self, pos_cmd: str, movetime: int) -> dict:
        t0 = time.monotonic()
        self._send(pos_cmd)
        self._send(f"go movetime {movetime}")
        deadline = time.monotonic() + movetime / 1000.0 + 25.0
        stop_sent = False
        info = {}
        bestmove = None
        ponder = None
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0 and not stop_sent:
                self._send("stop")
                stop_sent = True
                deadline = time.monotonic() + 10.0
                continue
            try:
                line = self._read_line(remain if remain > 0 else 0.001)
            except TimeoutError:
                if stop_sent:
                    raise TimeoutError("引擎未在限定时间内返回 bestmove")
                self._send("stop")
                stop_sent = True
                deadline = time.monotonic() + 10.0
                continue
            if line.startswith("bestmove"):
                tok = line.split()
                if len(tok) > 1 and tok[1] not in ("(none)", "0000"):
                    bestmove = tok[1]
                if "ponder" in tok:
                    k = tok.index("ponder")
                    if k + 1 < len(tok):
                        ponder = tok[k + 1]
                break
            if line.startswith("info ") and " pv " in line:
                info = self._parse_info(line)
        elapsed = int((time.monotonic() - t0) * 1000)
        return {
            "bestmove": bestmove,
            "ponder": ponder,
            "game_over": bestmove is None,
            "score": info.get("score"),
            "depth": info.get("depth"),
            "nodes": info.get("nodes"),
            "nps": info.get("nps"),
            "pv": " ".join(info.get("pv", [])[:6]) or None,
            "time_ms": elapsed,
        }

    def warmup(self, movetime: int = 200) -> None:
        """开局搜索一次，提前加载 NNUE / 预热哈希。"""
        self._search("position startpos", movetime)


if __name__ == "__main__":
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(description="Pikafish 本地命令行（替代 HTTP 接口）")
    ap.add_argument("--fen", default="startpos", help="起始局面 FEN 或 startpos")
    ap.add_argument("--moves", default="", help="逗号分隔着法序列，如 h2e2,h9g7")
    ap.add_argument("--movetime", type=int, default=1000, help="思考毫秒数")
    ap.add_argument("--threads", type=int, default=0, help="引擎线程数（0=自动）")
    ap.add_argument("--hash", type=int, default=512, help="哈希 MB")
    a = ap.parse_args()

    eng = LocalEngine(threads=a.threads or None, hash_mb=a.hash)
    try:
        r = eng.bestmove(fen=a.fen, movetime=a.movetime,
                         moves=[m for m in a.moves.split(",") if m.strip()])
        print(_json.dumps(r, ensure_ascii=False))
    except ValueError as exc:
        print(_json.dumps({"error": {"code": "illegal_move", "message": str(exc)}},
                          ensure_ascii=False))
    finally:
        eng.close()
