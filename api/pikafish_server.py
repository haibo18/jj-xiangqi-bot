#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pikafish（皮卡鱼）象棋引擎 HTTP 服务
=====================================

把 UCI 协议的 Pikafish 引擎封装成可在本地（或局域网）访问的 HTTP/JSON 接口。

特点：
  * 纯 Python 标准库实现，零第三方依赖，Python 3.8+ 可直接运行
  * 多引擎实例池，支持并发请求，每个请求独占一个进程
  * 同步接口（bestmove / analyze / perft）+ SSE 流式接口（实时推送搜索信息）
  * 内置网页控制台，打开浏览器即可下棋、看引擎分析

启动：
    python pikafish_server.py                 # 默认 http://127.0.0.1:8765
    python pikafish_server.py --port 9000 --host 0.0.0.0
    python pikafish_server.py --instances 4 --threads 4 --hash 512
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

VERSION = "1.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.dirname(HERE)          # 引擎与 nnue 权重所在目录（默认 C:\pikafish）
STATIC_DIR = os.path.join(HERE, "static")

START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"

EXE_BY_PLATFORM = {
    "win32": "Pikafish-Windows-x86-64-universal.exe",
    "darwin": "Pikafish-MacOS-universal",
    "linux": "Pikafish-Linux-x86-64-universal",
}

PLATFORM_EXE = EXE_BY_PLATFORM.get(sys.platform, "Pikafish-Linux-x86-64-universal")
DEFAULT_EXE = os.path.join(ENGINE_DIR, PLATFORM_EXE)

# 着法形如 h2e2（列 a-i，行 0-9）
MOVE_RE = re.compile(r"^[a-i][0-9][a-i][0-9]$")
FEN_ROW_PIECES = set("rnbakabnrRNBAKABNRcCpP")


# --------------------------------------------------------------------------- #
# 异常定义
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    """可以直接返回给调用方的业务错误。"""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class EngineError(RuntimeError):
    pass


class EngineTimeout(EngineError):
    pass


class _ReadTimeout(Exception):
    pass


# --------------------------------------------------------------------------- #
# UCI 输出解析
# --------------------------------------------------------------------------- #
def parse_info(line: str) -> Dict[str, Any]:
    """把一行 `info depth 12 ... pv h2e2 h7e7` 解析成字典。"""
    tokens = line.split()
    info: Dict[str, Any] = {}
    i = 1
    n = len(tokens)
    while i < n:
        key = tokens[i]
        if key == "pv":
            info["pv"] = tokens[i + 1:]
            break
        if key == "string":
            info["string"] = " ".join(tokens[i + 1:])
            break
        if key == "score":
            if i + 2 < n:
                kind = tokens[i + 1]
                try:
                    value = int(tokens[i + 2])
                except ValueError:
                    value = 0
                info["score"] = {"type": kind, "value": value}
                i += 3
                continue
            i += 1
            continue
        if key == "wdl":
            nums = []
            for t in tokens[i + 1:i + 4]:
                try:
                    nums.append(int(t))
                except ValueError:
                    break
            if len(nums) == 3:
                info["wdl"] = {"win": nums[0], "draw": nums[1], "loss": nums[2]}
                i += 4
                continue
            i += 1
            continue
        # 普通的 key <int> 形式
        if i + 1 < n and re.fullmatch(r"-?\d+", tokens[i + 1]):
            info[key] = int(tokens[i + 1])
            i += 2
            continue
        i += 1
    return info


def parse_uci_option(line: str) -> Optional[Dict[str, Any]]:
    """解析 `option name Threads type spin default 1 min 1 max 1024`。"""
    m = re.match(r"option name (.+?) type (\w+)(.*)$", line)
    if not m:
        return None
    name, opt_type, rest = m.group(1), m.group(2), m.group(3)
    opt: Dict[str, Any] = {"name": name, "type": opt_type}

    default_m = re.search(r"default (\S+)", rest)
    if default_m:
        opt["default"] = default_m.group(1)
    min_m = re.search(r"min (-?\d+)", rest)
    if min_m:
        opt["min"] = int(min_m.group(1))
    max_m = re.search(r"max (-?\d+)", rest)
    if max_m:
        opt["max"] = int(max_m.group(1))
    vars_m = re.search(r"var (.+)$", rest)
    if vars_m:
        opt["vars"] = re.findall(r"var (\S+)", "var " + vars_m.group(1)) or \
                      vars_m.group(1).split(" var ")
    return opt


# --------------------------------------------------------------------------- #
# 单个引擎进程
# --------------------------------------------------------------------------- #
class EngineWorker:
    """一个 Pikafish 进程的封装。同一时刻只允许一个线程使用。"""

    def __init__(self, exe: str, cwd: str, options: Dict[str, str], idx: int = 0):
        self.exe = exe
        self.cwd = cwd
        self.idx = idx
        self.name = f"engine-{idx}"
        self.identity: Dict[str, Any] = {}
        self.option_specs: List[Dict[str, Any]] = []
        self.current_options: Dict[str, str] = {}
        self.base_options: Dict[str, str] = dict(options)
        self.proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._start()

    # ---------------- 进程管理 ---------------- #
    def _start(self) -> None:
        if not os.path.isfile(self.exe):
            raise EngineError(f"找不到引擎可执行文件: {self.exe}")
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW，避免弹黑窗
        self.proc = subprocess.Popen(
            [self.exe],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
        self._reader = threading.Thread(target=self._reader_loop, name=self.name, daemon=True)
        self._reader.start()

        self.send("uci")
        banner = self._read_until(lambda l: l.strip() == "uciok", 20.0)
        for line in banner:
            if line.startswith("id name "):
                self.identity["name"] = line[8:].strip()
            elif line.startswith("id author "):
                self.identity["author"] = line[10:].strip()
            elif line.startswith("option name "):
                spec = parse_uci_option(line)
                if spec:
                    self.option_specs.append(spec)
                    if "default" in spec:
                        self.current_options[spec["name"]] = spec["default"]
        self.identity["path"] = self.exe
        self.apply_options(self.base_options)

    def _reader_loop(self) -> None:
        stdout = self.proc.stdout if self.proc else None
        if stdout is None:
            return
        try:
            for line in stdout:
                self._lines.put(line.rstrip("\r\n"))
        except Exception:  # 进程被关闭
            pass
        finally:
            self._lines.put(None)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        try:
            if self.alive():
                self.send("quit")
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass

    # ---------------- 读写 ---------------- #
    def send(self, cmd: str) -> None:
        if not self.alive():
            raise EngineError("引擎进程已退出")
        assert self.proc and self.proc.stdin
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise EngineError(f"向引擎写入失败: {exc}") from exc

    def _read_line(self, timeout: float) -> str:
        try:
            line = self._lines.get(timeout=max(timeout, 0.001))
        except queue.Empty:
            raise _ReadTimeout()
        if line is None:
            raise EngineError("引擎进程意外退出")
        return line

    def _read_until(self, predicate, timeout: float) -> List[str]:
        collected: List[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise EngineTimeout("等待引擎响应超时")
            collected.append(self._read_line(remain))
            if predicate(collected[-1]):
                return collected

    # ---------------- 选项 ---------------- #
    def apply_options(self, options: Dict[str, Any]) -> None:
        """仅下发发生变化的选项，然后用 isready 同步。"""
        changed = False
        for name, value in options.items():
            if value is None:
                continue
            text = str(value)
            if str(self.current_options.get(name)) == text:
                continue
            self.send(f"setoption name {name} value {text}")
            self.current_options[name] = text
            changed = True
        if changed:
            self.send("isready")
            self._read_until(lambda l: l.strip() == "readyok", 20.0)

    def restore_options(self) -> None:
        self.apply_options(self.base_options)

    # ---------------- 搜索 ---------------- #
    def analyze(
        self,
        position_cmd: str,
        go_cmd: str,
        timeout: float = 30.0,
        on_info=None,
    ) -> Dict[str, Any]:
        self.send(position_cmd)
        self.send(go_cmd)

        deadline = time.monotonic() + timeout
        stop_sent = False
        infos: Dict[int, Dict[str, Any]] = {}
        bestmove: Optional[str] = None
        ponder: Optional[str] = None
        last_raw = ""

        while True:
            remain = deadline - time.monotonic()
            if remain <= 0 and not stop_sent:
                self.send("stop")
                stop_sent = True
                deadline = time.monotonic() + 10.0
                continue
            try:
                line = self._read_line(remain if remain > 0 else 0.001)
            except _ReadTimeout:
                if stop_sent:
                    raise EngineTimeout("引擎未在限定时间内返回 bestmove")
                self.send("stop")
                stop_sent = True
                deadline = time.monotonic() + 10.0
                continue

            if line.startswith("bestmove"):
                tokens = line.split()
                if len(tokens) > 1:
                    bestmove = None if tokens[1] in ("(none)", "0000") else tokens[1]
                if "ponder" in tokens:
                    idx = tokens.index("ponder")
                    if idx + 1 < len(tokens):
                        ponder = tokens[idx + 1]
                # 收尾时引擎可能还会吐出最后一条 info，稍微清一下队列
                break

            if line.startswith("info "):
                info = parse_info(line)
                if info.get("pv"):
                    mpv = info.get("multipv", 1)
                    infos[mpv] = info
                    last_raw = line
                if on_info is not None:
                    on_info(info)

        return {
            "bestmove": bestmove,
            "ponder": ponder,
            "infos": infos,
            "last_info_raw": last_raw,
        }

    def new_game(self) -> None:
        self.send("ucinewgame")
        self.send("isready")
        self._read_until(lambda l: l.strip() == "readyok", 20.0)


# --------------------------------------------------------------------------- #
# 引擎池
# --------------------------------------------------------------------------- #
class EnginePool:
    def __init__(self, exe: str, cwd: str, size: int, options: Dict[str, str]):
        self.exe = exe
        self.cwd = cwd
        self.size = size
        self.base_options = dict(options)
        self.workers: List[EngineWorker] = []
        self._free: "queue.Queue[EngineWorker]" = queue.Queue()
        for i in range(size):
            worker = EngineWorker(exe, cwd, self.base_options, idx=i)
            self.workers.append(worker)
            self._free.put(worker)

    def acquire(self, timeout: float = 60.0) -> EngineWorker:
        try:
            worker = self._free.get(timeout=timeout)
        except queue.Empty:
            raise ApiError("所有引擎实例都在忙，请稍后重试", status=503, code="engine_busy")
        if not worker.alive():
            # 引擎进程意外退出 -> 自动重生
            worker.close()
            print(f"[pool] 引擎实例 {worker.idx} 已退出，正在重启...", flush=True)
            try:
                fresh = EngineWorker(self.exe, self.cwd, self.base_options, idx=worker.idx)
            except Exception as exc:  # noqa: BLE001
                raise ApiError(f"引擎重启失败: {exc}", status=500, code="engine_restart_failed")
            self.workers[worker.idx] = fresh
            worker = fresh
        return worker

    def release(self, worker: EngineWorker) -> None:
        self._free.put(worker)

    def set_global_option(self, name: str, value: Any) -> None:
        self.base_options[name] = str(value)
        for worker in self.workers:
            worker.base_options[name] = str(value)
            worker.apply_options({name: value})

    def new_game_all(self) -> None:
        for worker in self.workers:
            worker.new_game()

    def close(self) -> None:
        for worker in self.workers:
            worker.close()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "instances": self.size,
            "busy": self.size - self._free.qsize(),
            "idle": self._free.qsize(),
            "base_options": dict(self.base_options),
        }


class pool_guard:
    """with pool_guard(pool) as engine: ... —— 用完自动归还。"""

    def __init__(self, pool: EnginePool, timeout: float = 60.0):
        self.pool = pool
        self.timeout = timeout
        self.worker: Optional[EngineWorker] = None

    def __enter__(self) -> EngineWorker:
        self.worker = self.pool.acquire(self.timeout)
        return self.worker

    def __exit__(self, *exc) -> None:
        if self.worker is not None:
            self.pool.release(self.worker)


# --------------------------------------------------------------------------- #
# 请求参数处理
# --------------------------------------------------------------------------- #
def normalize_fen(fen: str) -> str:
    parts = fen.split()
    if len(parts) < 2:
        raise ApiError("FEN 至少需要包含「棋子布局」和「走子方」两部分")
    rows = parts[0].split("/")
    if len(rows) != 10:
        raise ApiError(f"象棋 FEN 的棋子布局必须是 10 行，当前为 {len(rows)} 行")
    for r in rows:
        width = 0
        for ch in r:
            if ch.isdigit():
                width += int(ch)
            elif ch in FEN_ROW_PIECES:
                width += 1
            else:
                raise ApiError(f"FEN 中出现无法识别的字符: {ch!r}")
        if width != 9:
            raise ApiError(f"FEN 每行必须恰好 9 列，行 {r!r} 为 {width} 列")
    side = parts[1].lower()
    if side in ("r",):
        side = "w"
    if side not in ("w", "b"):
        raise ApiError("FEN 走子方必须是 w（红方）或 b（黑方）")
    rest = parts[2:6] + ["-", "-", "0", "1"][len(parts[2:6]):]
    return " ".join([parts[0], side] + rest[:4])


def collect_moves(params: Dict[str, Any]) -> List[str]:
    raw = params.get("moves")
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,\s]+", str(raw).strip())
    moves = []
    for item in items:
        mv = item.strip().lower()
        if not mv:
            continue
        if not MOVE_RE.match(mv):
            raise ApiError(f"非法着法 {item!r}，格式应为「列行+列行」，如 h2e2")
        moves.append(mv)
    return moves


def build_position(params: Dict[str, Any]) -> str:
    fen = params.get("fen")
    fen_text = str(fen).strip() if fen else "startpos"
    if fen_text.lower() in ("startpos", "start", "initial", "default", ""):
        cmd = "position startpos"
    else:
        cmd = "position fen " + normalize_fen(fen_text)
    moves = collect_moves(params)
    if moves:
        cmd += " moves " + " ".join(moves)
    return cmd


def param_int(params: Dict[str, Any], key: str, default=None, lo=None, hi=None) -> Optional[int]:
    value = params.get(key)
    if value is None or value == "":
        return default
    try:
        iv = int(float(value))
    except (TypeError, ValueError):
        raise ApiError(f"参数 {key} 必须是整数，收到 {value!r}")
    if lo is not None and iv < lo:
        raise ApiError(f"参数 {key} 不能小于 {lo}")
    if hi is not None and iv > hi:
        raise ApiError(f"参数 {key} 不能大于 {hi}")
    return iv


def param_float(params: Dict[str, Any], key: str, default=None, lo=None, hi=None):
    value = params.get(key)
    if value is None or value == "":
        return default
    try:
        fv = float(value)
    except (TypeError, ValueError):
        raise ApiError(f"参数 {key} 必须是数字，收到 {value!r}")
    if lo is not None and fv < lo:
        raise ApiError(f"参数 {key} 不能小于 {lo}")
    if hi is not None and fv > hi:
        raise ApiError(f"参数 {key} 不能大于 {hi}")
    return fv


def build_go(params: Dict[str, Any]) -> Dict[str, Any]:
    """把请求参数翻译成 UCI 的 go 命令。"""
    depth = param_int(params, "depth", None, 1, 60)
    movetime = param_int(params, "movetime", None, 1, 600000)
    nodes = param_int(params, "nodes", None, 1, None)
    wtime = param_int(params, "wtime", None, 1, None)
    btime = param_int(params, "btime", None, 1, None)
    winc = param_int(params, "winc", 0, 0, None)
    binc = param_int(params, "binc", 0, 0, None)
    movestogo = param_int(params, "movestogo", None, 1, None)
    infinite = bool(params.get("infinite"))

    if wtime or btime:
        cmd = f"go wtime {wtime or 60000} btime {btime or 60000} winc {winc} binc {binc}"
        if movestogo:
            cmd += f" movestogo {movestogo}"
        estimated = max((wtime or 60000), (btime or 60000)) / 1000.0
        return {"cmd": cmd, "budget": estimated, "kind": "clock"}
    if depth:
        return {"cmd": f"go depth {depth}", "budget": depth * 0.6, "kind": "depth"}
    if movetime:
        return {"cmd": f"go movetime {movetime}", "budget": movetime / 1000.0, "kind": "movetime"}
    if nodes:
        return {"cmd": f"go nodes {nodes}", "budget": 30.0, "kind": "nodes"}
    if infinite:
        return {"cmd": "go infinite", "budget": 30.0, "kind": "infinite"}
    default_ms = param_int(params, "default_movetime", 1500, 1, 600000)
    return {"cmd": f"go movetime {default_ms}", "budget": default_ms / 1000.0, "kind": "movetime"}


def build_engine_overrides(params: Dict[str, Any]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    multipv = param_int(params, "multipv", None, 1, 128)
    if multipv:
        overrides["MultiPV"] = str(multipv)
    threads = param_int(params, "threads", None, 1, 512)
    if threads:
        overrides["Threads"] = str(threads)
    hash_mb = param_int(params, "hash", None, 1, 32768)
    if hash_mb:
        overrides["Hash"] = str(hash_mb)
    skill = param_int(params, "skill", None, 0, 20)
    if skill is not None:
        overrides["Skill Level"] = str(skill)
    elo = param_int(params, "elo", None, 1280, 3133)
    if elo:
        overrides["UCI_LimitStrength"] = "true"
        overrides["UCI_Elo"] = str(elo)
    rule = params.get("rule")
    if rule:
        overrides["Repetition Rule"] = str(rule)
    return overrides


def format_result(engine: EngineWorker, raw: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    infos: Dict[int, Dict[str, Any]] = raw["infos"]
    lines = []
    for rank, info in sorted(infos.items()):
        score = info.get("score") or {}
        pv = info.get("pv") or []
        lines.append({
            "rank": rank,
            "depth": info.get("depth"),
            "seldepth": info.get("seldepth"),
            "score": score or None,
            "cp": score.get("value") if score.get("type") == "cp" else None,
            "mate": score.get("value") if score.get("type") == "mate" else None,
            "wdl": info.get("wdl"),
            "nodes": info.get("nodes"),
            "nps": info.get("nps"),
            "time_ms": info.get("time"),
            "pv": pv,
            "pv_text": " ".join(pv),
        })
    primary = lines[0] if lines else None
    bestmove = raw["bestmove"]
    return {
        "bestmove": bestmove,
        "bestmove_text": bestmove,
        "ponder": raw["ponder"],
        "game_over": bestmove is None,
        "score": primary["score"] if primary else None,
        "depth": primary["depth"] if primary else None,
        "nodes": primary["nodes"] if primary else None,
        "nps": primary["nps"] if primary else None,
        "time_ms": primary["time_ms"] if primary else None,
        "lines": lines,
        "engine": {
            "name": engine.identity.get("name"),
            "threads": int(engine.current_options.get("Threads", 1)),
            "hash": int(engine.current_options.get("Hash", 16)),
            "multipv": int(engine.current_options.get("MultiPV", 1)),
        },
        "request": meta,
    }


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #
POOL: Optional[EnginePool] = None
CONFIG: Dict[str, Any] = {}
START_TS = time.time()


def json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class PikafishHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"PikafishAPI/{VERSION}"
    sys_version = ""

    # ---------------- 基础设施 ---------------- #
    def log_message(self, fmt: str, *args) -> None:
        if not CONFIG.get("quiet"):
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json_bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 400, code: str = "bad_request") -> None:
        self._send_json({"error": {"code": code, "message": message}}, status=status)

    def _collect_params(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        for key, values in query.items():
            params[key] = values[0] if len(values) == 1 else values
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                raw = self.rfile.read(length)
                ctype = (self.headers.get("Content-Type") or "").lower()
                if "json" in ctype:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ApiError(f"请求体不是合法 JSON: {exc}")
                    if not isinstance(body, dict):
                        raise ApiError("请求体必须是一个 JSON 对象")
                    params.update(body)
                elif "form-urlencoded" in ctype:
                    form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                    for key, values in form.items():
                        params[key] = values[0] if len(values) == 1 else values
        return params

    # ---------------- 路由 ---------------- #
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(unquote(path[len("/static/"):]))
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/api/health":
                return self._handle_health()
            if path == "/api/options":
                if method == "POST":
                    return self._handle_set_options()
                return self._handle_get_options()
            if path == "/api/newgame":
                return self._handle_newgame()
            if path in ("/api/bestmove", "/api/analyze", "/api/move"):
                return self._handle_search(path)
            if path == "/api/analyze/stream":
                return self._handle_stream()
            if path == "/api/perft":
                return self._handle_perft()
            if path == "/openapi.json":
                return self._handle_openapi()
            raise ApiError(f"未找到接口 {path}", status=404, code="not_found")
        except ApiError as exc:
            self._send_error_json(exc.message, exc.status, exc.code)
        except EngineTimeout as exc:
            self._send_error_json(f"引擎超时: {exc}", 504, "engine_timeout")
        except EngineError as exc:
            self._send_error_json(f"引擎错误: {exc}", 500, "engine_error")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_error_json(f"服务器内部错误: {exc}", 500, "internal_error")

    # ---------------- 静态文件 ---------------- #
    def _serve_static(self, rel_path: str) -> None:
        rel_path = rel_path.replace("\\", "/").lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        if not full.startswith(os.path.normpath(STATIC_DIR)) or not os.path.isfile(full):
            raise ApiError(f"文件不存在: {rel_path}", status=404, code="not_found")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
        }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ---------------- 各接口实现 ---------------- #
    def _handle_health(self) -> None:
        assert POOL is not None
        first = POOL.workers[0]
        self._send_json({
            "status": "ok",
            "version": VERSION,
            "uptime_seconds": round(time.time() - START_TS, 1),
            "engine": {
                "name": first.identity.get("name"),
                "author": first.identity.get("author"),
                "path": first.exe,
                "nnue": os.path.join(ENGINE_DIR, "pikafish.nnue"),
                "options_count": len(first.option_specs),
            },
            "pool": POOL.snapshot(),
            "protocol": "UCI",
            "game": "xiangqi (中国象棋)",
        })

    def _handle_get_options(self) -> None:
        assert POOL is not None
        worker = POOL.workers[0]
        self._send_json({
            "options": worker.option_specs,
            "current": dict(worker.current_options),
        })

    def _handle_set_options(self) -> None:
        assert POOL is not None
        params = self._collect_params()
        name = params.get("name") or params.get("option")
        value = params.get("value")
        if name and name not in ("name", "option"):
            pairs = {str(name): value}
        else:
            pairs = {k: v for k, v in params.items()
                     if k not in ("name", "option", "value") and isinstance(v, (str, int, float, bool))}
        if not pairs:
            raise ApiError("请在请求体中提供要设置的选项，例如 {\"Threads\": 4} 或 {\"name\":\"Hash\",\"value\":256}")
        applied = {}
        for opt_name, opt_value in pairs.items():
            if opt_value is True or opt_value is False:
                opt_value = "true" if opt_value else "false"
            POOL.set_global_option(str(opt_name), opt_value)
            applied[str(opt_name)] = str(opt_value)
        self._send_json({"ok": True, "applied": applied, "current": POOL.workers[0].current_options})

    def _handle_newgame(self) -> None:
        assert POOL is not None
        POOL.new_game_all()
        self._send_json({"ok": True, "message": "已重置所有引擎的哈希表与搜索状态"})

    def _run_search(self, params: Dict[str, Any], default_multipv: int) -> Dict[str, Any]:
        assert POOL is not None
        position = build_position(params)
        go = build_go(params)
        overrides = build_engine_overrides(params)
        overrides.setdefault("MultiPV", str(default_multipv))

        budget = param_float(params, "timeout", None, 0.1, 600)
        if budget is None:
            budget = min(120.0, max(10.0, go["budget"] + 15.0))

        with pool_guard(POOL) as engine:
            engine.apply_options(overrides)
            try:
                raw = engine.analyze(position, go["cmd"], timeout=budget)
            finally:
                engine.restore_options()

        meta = {
            "position": position,
            "go": go["cmd"],
            "limit": go["kind"],
            "timeout_seconds": budget,
        }
        return format_result(engine, raw, meta)

    def _handle_search(self, path: str) -> None:
        params = self._collect_params()
        default_multipv = 3 if path == "/api/analyze" else 1
        self._send_json(self._run_search(params, default_multipv))

    def _handle_perft(self) -> None:
        assert POOL is not None
        params = self._collect_params()
        position = build_position(params)
        depth = param_int(params, "depth", 3, 1, 8)
        with pool_guard(POOL) as engine:
            engine.send(position)
            engine.send(f"go perft {depth}")
            moves: Dict[str, int] = {}
            total = None
            deadline = time.monotonic() + 120.0
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    raise EngineTimeout("perft 计算超时")
                line = engine._read_line(remain)
                if line.startswith("Nodes searched"):
                    total = int(line.split(":")[-1].strip())
                    break
                m = re.match(r"^([a-i][0-9][a-i][0-9]):\s*(\d+)$", line.strip())
                if m:
                    moves[m.group(1)] = int(m.group(2))
                elif line.strip().lower().startswith("perft") or "isready" in line:
                    continue
            if total is None:
                total = sum(moves.values())
            engine.send("isready")
            engine._read_until(lambda l: l.strip() == "readyok", 20.0)
        self._send_json({
            "position": position,
            "depth": depth,
            "nodes": total,
            "moves": dict(sorted(moves.items(), key=lambda kv: -kv[1])),
        })

    def _handle_stream(self) -> None:
        """SSE 流式分析：实时推送 info 行，最后推送 bestmove。"""
        assert POOL is not None
        try:
            params = self._collect_params()
            position = build_position(params)
            go = build_go(params)
            overrides = build_engine_overrides(params)
            overrides.setdefault("MultiPV", "1")
            budget = param_float(params, "timeout", None, 0.1, 600) or min(
                120.0, max(10.0, go["budget"] + 15.0))
        except ApiError as exc:
            return self._send_error_json(exc.message, exc.status, exc.code)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()

        def chunk(payload: bytes) -> None:
            if payload:
                self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            else:
                self.wfile.write(b"%x\r\n\r\n" % 0)
            self.wfile.flush()

        def emit(event: str, data: Any) -> None:
            chunk(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))

        engine = None
        try:
            emit("start", {"position": position, "go": go["cmd"], "timeout_seconds": budget})
            engine = POOL.acquire()
            engine.apply_options(overrides)

            def on_info(info: Dict[str, Any]) -> None:
                if info.get("pv") or info.get("string"):
                    emit("info", info)

            raw = engine.analyze(position, go["cmd"], timeout=budget, on_info=on_info)
            result = format_result(engine, raw, {"position": position, "go": go["cmd"]})
            emit("bestmove", result)
            emit("done", {"ok": True})
            chunk(b"")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                emit("error", {"message": str(exc)})
                chunk(b"")
            except Exception:
                pass
        finally:
            if engine is not None:
                try:
                    engine.restore_options()
                finally:
                    POOL.release(engine)

    def _handle_openapi(self) -> None:
        self._send_json({
            "openapi": "3.0.0",
            "info": {"title": "Pikafish 象棋引擎 API", "version": VERSION},
            "paths": {
                "/api/health": {"get": {"summary": "健康检查"}},
                "/api/bestmove": {"get": {"summary": "最佳着法（MultiPV=1）"},
                                  "post": {"summary": "最佳着法（MultiPV=1）"}},
                "/api/analyze": {"get": {"summary": "局面分析（默认 MultiPV=3）"},
                                 "post": {"summary": "局面分析（默认 MultiPV=3）"}},
                "/api/analyze/stream": {"get": {"summary": "SSE 流式分析"}},
                "/api/perft": {"get": {"summary": "perft 节点统计"}},
                "/api/options": {"get": {"summary": "列出引擎选项"},
                                 "post": {"summary": "修改引擎选项"}},
                "/api/newgame": {"post": {"summary": "清空哈希表，开始新对局"}},
            },
            "commonParams": {
                "fen": "startpos 或 完整象棋 FEN",
                "moves": "走子序列，如 h2e2,h7e7",
                "depth": "搜索深度 1-60",
                "movetime": "思考时间（毫秒）",
                "nodes": "搜索节点数上限",
                "multipv": "输出前 N 条最佳线路 1-128",
                "threads": "本次搜索使用的线程数",
                "hash": "哈希表大小（MB）",
                "skill": "棋力等级 0-20",
                "timeout": "服务端保护性超时（秒）",
            },
        })


class PikafishHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------- #
# 启动入口
# --------------------------------------------------------------------------- #
def guess_threads(instances: int) -> int:
    cpu = os.cpu_count() or 4
    return max(1, min(4, cpu // max(1, instances)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pikafish 象棋引擎 HTTP 服务",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，局域网访问用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--engine", default=DEFAULT_EXE, help="引擎可执行文件路径")
    parser.add_argument("--engine-dir", default=ENGINE_DIR, help="引擎工作目录（用于查找 nnue 权重）")
    parser.add_argument("--instances", type=int, default=2, help="引擎进程实例数")
    parser.add_argument("--threads", type=int, default=0, help="每个实例的搜索线程数，0 表示自动")
    parser.add_argument("--hash", type=int, default=128, help="每个实例的哈希表大小（MB）")
    parser.add_argument("--quiet", action="store_true", help="不打印访问日志")
    args = parser.parse_args()

    global POOL, CONFIG
    CONFIG = {"quiet": args.quiet}

    threads = args.threads or guess_threads(args.instances)
    nnue = os.path.join(args.engine_dir, "pikafish.nnue")
    options = {"Threads": str(threads), "Hash": str(args.hash)}
    if os.path.isfile(nnue):
        options["EvalFile"] = nnue

    print("=" * 68)
    print("  Pikafish 象棋引擎 HTTP 服务  v" + VERSION)
    print("=" * 68)
    print(f"  引擎文件 : {args.engine}")
    print(f"  工作目录 : {args.engine_dir}")
    print(f"  NNUE 权重: {nnue if os.path.isfile(nnue) else '未找到（使用引擎默认）'}")
    print(f"  实例/线程: {args.instances} 个实例 × {threads} 线程，Hash {args.hash}MB")
    sys.stdout.flush()

    try:
        POOL = EnginePool(args.engine, args.engine_dir, args.instances, options)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[错误] 引擎启动失败: {exc}")
        print("请检查 --engine 指向的路径是否正确，以及 nnue 权重是否与引擎同目录。")
        return 1

    first = POOL.workers[0]
    print(f"  引擎标识 : {first.identity.get('name')}")
    print(f"  引擎选项 : {len(first.option_specs)} 项")
    print("-" * 68)
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"  网页控制台: http://{display_host}:{args.port}/")
    print(f"  健康检查  : http://{display_host}:{args.port}/api/health")
    print(f"  接口示例  : http://{display_host}:{args.port}/api/bestmove?fen=startpos&depth=15")
    if args.host in ("0.0.0.0", "::"):
        print(f"  局域网访问: http://<本机IP>:{args.port}/")
    print("-" * 68)
    print("  按 Ctrl+C 停止服务")
    sys.stdout.flush()

    server = PikafishHTTPServer((args.host, args.port), PikafishHandler)

    def shutdown(signum, frame):  # noqa: ARG001
        print("\n正在停止服务...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
    except (ValueError, AttributeError):
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if POOL:
            POOL.close()
        print("服务已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
