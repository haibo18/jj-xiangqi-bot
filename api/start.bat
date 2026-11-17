@echo off
chcp 65001 >nul
title Pikafish Xiangqi HTTP API
cd /d "%~dp0"
echo 正在启动皮卡鱼引擎 HTTP 服务...
python pikafish_server.py --instances 2 --threads 4 --hash 128 %*
pause
