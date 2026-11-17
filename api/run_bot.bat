@echo off
title Pikafish Auto Play
cd /d "%~dp0"
echo ==================================================
echo   Xiangqi Auto Play  (10-min room / auto rematch)
echo   Log : %~dp0watch.log
echo   Stop: press Ctrl+C in this window
echo ==================================================
echo.
C:\Python315\python.exe watch_moves.py --start 10 --rematch --movetime 1000 --log watch.log %*
echo.
echo Program exited. Press any key to close this window...
pause >nul
