@echo off
setlocal
rem VoiceStudio 一键启动：服务没起就自动起，然后打开网页
set "PY=D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0\runtime\python.exe"
set "DIR=D:\AHC\VoiceStudio"
set "URL=http://127.0.0.1:8756"

call :CHECK
if %ERRORLEVEL%==0 goto :OPEN

echo.
echo   正在启动 VoiceStudio 服务，大约 3~5 秒...
echo   （首次启动后这个黑色窗口会留在后台，请不要关它）
echo.
start "VoiceStudio服务" /min "%PY%" -u "%DIR%\server.py"
start "VoiceStudio ncm auto-decrypt" /min "%PY%" "%DIR%\ncm_watch.py" --start

set /a TRY=0
:WAIT
ping -n 2 127.0.0.1 >nul
call :CHECK
if %ERRORLEVEL%==0 goto :OPEN
set /a TRY+=1
if %TRY% lss 20 goto :WAIT
echo.
echo   服务启动失败了。请双击 D:\AHC\VoiceStudio\server.py 旁边的
echo   日志文件查看原因，或把下面的报错截图发给我。
echo.
pause
exit /b 1

:OPEN
start "" "%URL%"
exit /b 0

:CHECK
netstat -ano | findstr /C:":8756 " | findstr /I "LISTEN" >nul 2>&1
exit /b %ERRORLEVEL%
