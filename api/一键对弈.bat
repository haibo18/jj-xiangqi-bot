@echo off
setlocal
title Pikafish Auto Play
cd /d "%~dp0"
if errorlevel 1 goto :bad_dir

rem ================= configuration =================
rem PHONE_HOST : phone Wi-Fi IP. Wireless adb only works when the PC and the
rem              phone are on the SAME subnet (e.g. both 192.168.3.x).
rem              If they differ, the script falls back to the USB cable.
set "PHONE_HOST=192.168.3.57"
set "PHONE_PORT=5555"
set "SCRCPY_DIR=%~dp0scrcpy"
set "ADB=%SCRCPY_DIR%\adb.exe"
set "SCRCPY=%SCRCPY_DIR%\scrcpy.exe"
set "JJ_PKG=cn.jj.chess.huawei"
set "JJ_ACT=cn.jj.chess.huawei/cn.jj.channel.ad.activity.HuaWeiSplashActivity"
set "WIN_TITLE=FOA-AL00"
set "ADB_SERIAL="
set "SC_SERIAL="
rem Tell watch_moves.py not to block on input() after a crash: this bat owns
rem the restart loop, a "press Enter" prompt would freeze it forever.
set "WATCH_AUTORESTART=1"
set "NOLOG=0"

echo ==================================================
echo   [1/3] adb connect phone   %PHONE_HOST%:%PHONE_PORT%
echo   [2/3] start scrcpy mirror + open JJ Xiangqi
echo   [3/3] Pikafish auto play  (5-min room / auto rematch)
echo   Log : %~dp0watch.log
echo   Stop: close this window or press Ctrl+C
echo ==================================================
echo.

rem ---------- 0) hard preflight: never enter the loop with a broken setup ----------
rem The old "C:/Python315/python.exe" (forward slashes, unquoted) can make cmd
rem fail with "The system cannot find the path specified" on every restart.
rem Resolve a verified absolute path with backslashes and check dependencies.
if not exist "%~dp0watch_moves.py" goto :no_script
set "PY="
if exist "C:\Python315\python.exe" (
  "C:\Python315\python.exe" -c "import cv2,numpy,PIL" >nul 2>&1
  if not errorlevel 1 set "PY=C:\Python315\python.exe"
)
if defined PY goto :py_ready
python -c "import cv2,numpy,PIL" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :py_ready
goto :no_python

:py_ready
echo [boot] workdir = %CD%
echo [boot] python  = %PY%
echo [boot] script  = %~dp0watch_moves.py
echo.

if not exist "%ADB%"    goto :no_tools
if not exist "%SCRCPY%" goto :no_tools

rem ---------- 1) adb server + wireless connect ----------
"%ADB%" start-server >nul 2>&1

rem skip wireless when the phone does not answer ping (different subnet)
ping -n 1 -w 900 %PHONE_HOST% | findstr /i "TTL=" >nul
if errorlevel 1 (
  echo [adb] phone %PHONE_HOST% unreachable on LAN, use USB device
  goto :pick_usb_dev
)

echo [adb] phone alive, connecting wireless %PHONE_HOST%:%PHONE_PORT% ...
call :connect_wireless
if defined ADB_SERIAL goto :have_serial

echo [adb] wireless refused, enabling tcpip %PHONE_PORT% over USB ...
"%ADB%" tcpip %PHONE_PORT% >nul 2>&1
%SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul 2>&1
call :connect_wireless
if defined ADB_SERIAL goto :have_serial
echo [adb] wireless still down, use USB device

:pick_usb_dev
call :pick_usb
if defined ADB_SERIAL goto :have_serial
echo [warn] no adb device found, continue anyway
goto :start_mirror

:have_serial
echo [adb] target device = %ADB_SERIAL%
set "SC_SERIAL=-s %ADB_SERIAL%"
"%ADB%" -s %ADB_SERIAL% shell input keyevent KEYCODE_WAKEUP >nul 2>&1
"%ADB%" -s %ADB_SERIAL% shell wm dismiss-keyguard >nul 2>&1
"%ADB%" -s %ADB_SERIAL% shell svc power stayon true >nul 2>&1
echo [app] opening JJ Xiangqi (%JJ_PKG%) ...
"%ADB%" -s %ADB_SERIAL% shell am start -n %JJ_ACT% >nul 2>&1
if errorlevel 1 "%ADB%" -s %ADB_SERIAL% shell monkey -p %JJ_PKG% -c android.intent.category.LAUNCHER 1 >nul 2>&1
%SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul 2>&1

rem ---------- 2) scrcpy mirror ----------
:start_mirror
tasklist /fi "imagename eq scrcpy.exe" 2>nul | findstr /i "scrcpy.exe" >nul
if not errorlevel 1 goto :mirror_ready
echo [scr] starting scrcpy mirror ...
start "scrcpy" /d "%SCRCPY_DIR%" "%SCRCPY%" %SC_SERIAL% --window-title "%WIN_TITLE%" --require-audio --stay-awake
echo [scr] waiting for mirror window "%WIN_TITLE%" ...
%SystemRoot%\System32\timeout.exe /t 6 /nobreak >nul 2>&1
goto :run_bot

:mirror_ready
echo [scr] scrcpy already running, reuse existing window

rem ---------- 3) auto play + guarded auto-restart ----------
:run_bot
if not exist "%~dp0watch.log" echo. > "%~dp0watch.log"
:run_loop
for %%F in ("%~dp0watch.log") do set "SZ0=%%~zF"
echo.
echo [%date% %time%] starting %PY% ...
"%PY%" "%~dp0watch_moves.py" --start 5 --rematch --movetime 1999 --log "%~dp0watch.log" %*
set "RC=%errorlevel%"
for %%F in ("%~dp0watch.log") do set "SZ1=%%~zF"
echo [%date% %time%] exited rc=%RC%  (0=clean 1=crash 2=deps 3=other instance 130=stopped)

rem Fatal setup errors must NOT be retried forever.
if "%RC%"=="130" exit /b 0
if "%RC%"=="2" goto :fatal
if "%RC%"=="3" goto :fatal
if %RC% GEQ 9000 goto :fatal
rem Three restarts in a row with no new log line = the program never really
rem started (bad path / broken env). Stop instead of spamming errors forever.
if "%SZ1%"=="%SZ0%" (set /a NOLOG+=1) else (set "NOLOG=0")
if %NOLOG% GEQ 3 goto :fatal

echo restarting in 10s ... (close this window or press Ctrl+C to stop for good)
%SystemRoot%\System32\timeout.exe /t 10 /nobreak >nul 2>&1
if errorlevel 1 ping -n 11 127.0.0.1 >nul 2>&1
goto :run_loop

:no_tools
echo [warn] adb/scrcpy not found under %SCRCPY_DIR%
echo        skip mirror startup, auto play will use the existing window
goto :run_bot

:fatal
echo.
echo [FATAL] auto-restart stopped to avoid an endless error loop.
echo         python  = %PY%
echo         workdir = %CD%
echo         script  = %~dp0watch_moves.py
echo         log     = %~dp0watch.log
echo         last rc = %RC%
echo   See the messages above and crash.log, fix the cause, then run this bat again.
echo.
pause
exit /b 1

:bad_dir
echo [FATAL] cannot enter project directory: %~dp0
pause
exit /b 1

:no_script
echo [FATAL] watch_moves.py not found in %~dp0
pause
exit /b 1

:no_python
echo [FATAL] no usable Python with cv2/numpy/PIL found.
echo         expected C:\Python315\python.exe
pause
exit /b 1

rem ---------------- helpers ----------------
:connect_wireless
"%ADB%" connect %PHONE_HOST%:%PHONE_PORT% 2>&1 | findstr /i "connected" >nul
if errorlevel 1 exit /b 0
for /f "delims=" %%S in ('"%ADB%" -s %PHONE_HOST%:%PHONE_PORT% get-state 2^>nul') do (
  if /i "%%S"=="device" set "ADB_SERIAL=%PHONE_HOST%:%PHONE_PORT%"
)
exit /b 0

:pick_usb
rem first USB serial (skip wireless ip:port entries)
"%ADB%" devices 2>nul > "%TEMP%\_adb_devs.txt"
for /f "skip=1 tokens=1" %%D in ('findstr /v ":" "%TEMP%\_adb_devs.txt"') do (
  if not "%%D"=="" if not defined ADB_SERIAL set "ADB_SERIAL=%%D"
)
if exist "%TEMP%\_adb_devs.txt" del "%TEMP%\_adb_devs.txt" >nul 2>&1
exit /b 0
