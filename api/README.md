# Pikafish（皮卡鱼）象棋引擎 HTTP 接口

把 `C:\pikafish` 下的皮卡鱼 UCI 引擎封装为本地 HTTP/JSON 网络接口，附带一个可点击走子的网页控制台。

- **零依赖**：纯 Python 标准库（3.8+），无需安装任何第三方包
- **并发**：默认 2 个引擎实例组成进程池，多请求自动排队
- **引擎**：Pikafish 2026-09-25 + pikafish.nnue 权重，自动加载
- **地址**：默认 `http://127.0.0.1:8765`

## 快速开始

```bat
:: 方式一：双击启动
C:\pikafish\api\start.bat

:: 方式二：命令行
python C:\pikafish\api\pikafish_server.py

:: 局域网访问（手机/其他电脑可访问）
python C:\pikafish\api\pikafish_server.py --host 0.0.0.0 --port 8765
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | 127.0.0.1 | 监听地址，`0.0.0.0` 表示开放局域网 |
| `--port` | 8765 | 监听端口 |
| `--instances` | 2 | 引擎进程实例数（并发能力） |
| `--threads` | 自动 | 每个实例的搜索线程数 |
| `--hash` | 128 | 每个实例的哈希表大小（MB） |
| `--engine` | 自动按平台 | 引擎可执行文件路径 |

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 网页控制台（棋盘可视化） |
| GET | `/api/health` | 健康检查 / 引擎信息 |
| GET·POST | `/api/bestmove` | 求最佳着法（MultiPV=1） |
| GET·POST | `/api/analyze` | 局面分析（默认输出前 3 条线路） |
| GET | `/api/analyze/stream` | SSE 流式分析，实时推送搜索信息 |
| GET | `/api/perft` | 走法生成校验（节点统计） |
| GET | `/api/options` | 列出引擎全部 UCI 选项 |
| POST | `/api/options` | 修改全局选项，如 `{"Threads": 8}` |
| POST | `/api/newgame` | 清空哈希表，开始新对局 |
| GET | `/openapi.json` | 接口摘要 |

## 核心参数

| 参数 | 说明 |
|---|---|
| `fen` | `startpos` 或完整象棋 FEN，如 `rnbakabnr/.../RNBAKABNR w - - 0 1` |
| `moves` | 走子序列（UCI 坐标：列 a-i、行 0-9），如 `h2e2,h9g7` |
| `depth` | 搜索深度（1-60） |
| `movetime` | 思考时间（毫秒） |
| `nodes` | 节点数上限 |
| `wtime`/`btime`/`winc`/`binc`/`movestogo` | 按对局时钟搜索 |
| `multipv` | 输出前 N 条最佳线路（1-128） |
| `threads` / `hash` | 仅本次请求生效，结束后自动还原 |
| `skill` | 棋力 0-20；或 `elo` 1280-3133（限棋力） |
| `rule` | 循环子力规则：`AsianRule`/`ChineseRule` 等 8 种 |
| `timeout` | 服务端保护性超时（秒） |

不传 `depth`/`movetime` 等时默认按 1500ms 思考。

## 调用示例

**curl — 最佳着法：**

```bash
curl "http://127.0.0.1:8765/api/bestmove?fen=startpos&depth=15"
curl "http://127.0.0.1:8765/api/bestmove?fen=startpos&moves=h2e2,h9g7&movetime=1000"
```

**Python：**

```python
import json, urllib.request

req = urllib.request.Request(
    "http://127.0.0.1:8765/api/analyze",
    data=json.dumps({
        "fen": "startpos",
        "moves": ["h2e2", "h9g7"],
        "depth": 20,
        "multipv": 3,
        "threads": 8,
    }).encode(),
    headers={"Content-Type": "application/json"},
)
result = json.load(urllib.request.urlopen(req))
print(result["bestmove"])        # h0g2
print(result["score"])           # {"type": "cp", "value": 5}
for line in result["lines"]:
    print(line["rank"], line["score"], line["pv_text"])
```

**JavaScript（浏览器）：**

```js
const r = await fetch("http://127.0.0.1:8765/api/bestmove", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({fen: "startpos", depth: 15}),
});
const {bestmove, score} = await r.json();
```

## 返回结构（bestmove / analyze）

```jsonc
{
  "bestmove": "h2e2",          // 最佳着法；无棋可走时为 null
  "ponder": "h9g7",            // 引擎预估的对手应手
  "game_over": false,
  "score": {"type": "cp", "value": 6},   // cp=百分之一兵值；type=mate 时 value 为绝杀步数
  "depth": 14, "nodes": 276782, "nps": 1040533, "time_ms": 266,
  "lines": [                   // MultiPV 多线路，rank 1 为最佳
    {"rank": 1, "depth": 14, "cp": 6, "mate": null,
     "wdl": {"win": 20, "draw": 979, "loss": 1},
     "pv": ["h2e2","h9g7","g3g4","c6c5","b0a2"], "pv_text": "h2e2 h9g7 ..."}
  ],
  "engine": {"name": "Pikafish 2026-09-25", "threads": 4, "hash": 128, "multipv": 1},
  "request": {"position": "position startpos", "go": "go depth 14"}
}
```

错误统一返回 `{"error": {"code": "...", "message": "..."}}`，FEN 与着法在服务端先做校验。

## 坐标约定

UCI 着法为 4 字符：起点列行 + 终点行列。列 `a-i` 从红方左侧数起，行 `0-9` 从红方底线数起。
例如红方开局「炮二平五」= `h2e2`，「马八进七」= `b0c2`。

## 文件结构

```
C:\pikafish\
├── Pikafish-Windows-x86-64-universal.exe   # 引擎（原样保留）
├── pikafish.nnue                           # NNUE 权重（原样保留）
└── api\
    ├── pikafish_server.py    # HTTP 服务（唯一源码文件）
    ├── start.bat             # 双击启动
    └── static\index.html     # 网页控制台
```

## 网页控制台功能

- SVG 棋盘，点击选子/走子，悔棋、重置、应用任意 FEN
- 「开始分析」展示 MultiPV 多线路评分与 PV；「让引擎走一步」自动落子
- 评分条直观显示局面倾向（红优/黑优/均势）
- 走子后可自动重新分析；底部附当前请求的 curl 示例
