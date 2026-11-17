# jj-xiangqi-bot

基于 [Pikafish（皮卡鱼）](https://github.com/official-pikafish/Pikafish) 中国象棋引擎的自动化对弈项目：引擎 HTTP 接口 + 棋盘识别（OCR/模板匹配）+ 自动落子脚本，配合 scrcpy/ADB 操控安卓设备上的象棋应用。

> 仅供学习研究。请遵守目标平台规则与法律法规，使用者自负其责。

## 目录结构

```
jj-xiangqi-bot/
├── pikafish.nnue                         # NNUE 神经网络权重
├── Pikafish-Windows-x86-64-universal.exe # Windows 引擎
├── Pikafish-Linux-x86-64-universal       # Linux 引擎
├── Pikafish-MacOS-universal              # macOS 引擎
├── Pikafish-Android-arm64-universal       # Android 引擎
├── api/                                   # 自动化与服务代码
│   ├── pikafish_server.py                 # 引擎 HTTP/JSON 服务（含网页控制台）
│   ├── pikafish_local.py                  # 本地引擎封装
│   ├── autoplay.py                        # 自动对弈主流程
│   ├── watch_moves.py                     # 监听走子 / 局面识别
│   ├── play.py / shot.py / board_scan.py
│   ├── ocr_engine.py / jjboard.py
│   ├── templates.npz / board_coords.json / 象棋坐标系.json
│   ├── static/index.html                  # 网页控制台
│   ├── scrcpy/                            # scrcpy + adb 运行时
│   └── *.bat                              # 一键启动脚本
└── docs/                                  # 引擎说明、协议与贡献者名单（见根目录 txt）
```

## 快速开始

```bat
:: 启动引擎 HTTP 服务（默认 http://127.0.0.1:8765）
python api\pikafish_server.py

:: 一键自动对弈（先在设备上打开象棋应用并连上 ADB）
api\一键对弈.bat
```

接口与参数详见 [`api/README.md`](api/README.md)。

## 版本管理约定

- 主干分支：`main`
- 语义化版本：`vMAJOR.MINOR.PATCH`，每个可发布版本打 Git 标签并在 GitHub Release 附上二进制附件
- 第三方应用安装包（`.apk`）不进入 Git 仓库，统一作为 Release 附件分发
- 运行日志、调试截图、临时缓存、`__pycache__` 等均由 `.gitignore` 排除，不纳入版本历史

## 许可与归属

- 引擎与 NNUE 权重的版权、协议见根目录 `NNUE权重协议`、`作者&算力贡献`、`更新日志` 等文本文件
- 本仓库内自动化脚本部分按现状（AS-IS）提供
