@echo off
setlocal
rem VoiceStudio 一键启动：服务没起就自动起，然后打开网页
set "PY=D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0\runtime\python.exe"
set "DIR=D:\AHC\VoiceStudio"
set "URL=http://127.0.0.1:8756"
set "HEALTH=%URL%/api/health"
set "SYS=%SystemRoot%\System32"
set "LOG=%DIR%\_startup.log"

rem ---- 每次启动都留一份现场：起不来时不用猜，用户截图即可 ----
> "%LOG%" echo ==== %date% %time% 启动 ====
>> "%LOG%" echo PY=%PY%
>> "%LOG%" echo SYS=%SYS%
if exist "%PY%" (
  >> "%LOG%" echo [ok] python 存在
) else (
  >> "%LOG%" echo [!!] python 不存在：GPT-SoVITS 目录可能被改名/移动了
)
if exist "%SYS%\netstat.exe" (
  >> "%LOG%" echo [ok] netstat 可用
) else (
  >> "%LOG%" echo [!!] netstat 找不到：SystemRoot 或 PATH 有问题
)

call :CHECK
if not errorlevel 1 goto :OPEN
rem 端口有人在听、但不是我们的服务（健康检查不应答）-> 别再起一个，起了也端口冲突
if errorlevel 2 goto :BUSY

echo.
echo   正在启动 VoiceStudio 服务，大约 3~5 秒...
echo   （首次启动后这个黑色窗口会留在后台，请不要关它）
echo.
start "VoiceStudio服务" /min "%PY%" -u "%DIR%\server.py"

set /a TRY=0
:WAIT
timeout /t 1 /nobreak >nul 2>&1
if errorlevel 1 "%SYS%\ping.exe" -n 2 127.0.0.1 >nul 2>&1
call :CHECK
if not errorlevel 1 goto :OPEN
set /a TRY+=1
if %TRY% lss 40 goto :WAIT

rem ---- 失败：把现场打出来，别再让人去找不存在的日志 ----
>> "%LOG%" echo ---- 失败现场 ----
>> "%LOG%" echo 探测轮次=%TRY%
( "%SYS%\netstat.exe" -ano | "%SYS%\findstr.exe" /C:":8756 " ) >> "%LOG%" 2>&1
>> "%LOG%" echo ---- 结束 ----
echo.
echo   服务在 40 秒内没有就绪。这次的现场已经写进：
echo     %LOG%
echo   下面是它的内容（可直接截图发我）：
echo   ------------------------------------------------------------
type "%LOG%"
echo   ------------------------------------------------------------
echo.
echo   排查提示：
echo     · 任务栏里那个最小化的「VoiceStudio服务」窗口，里面就是真实报错
echo     · 也可能服务其实已经起来了：先在浏览器打开 %URL% 试试
echo.
pause
exit /b 1

:OPEN
start "" "%URL%"
>> "%LOG%" echo [ok] 服务已就绪，已打开 %URL%
exit /b 0

:BUSY
>> "%LOG%" echo [!!] 8756 有人在听，但健康检查不应答（别的程序占了？）
echo.
echo   端口 8756 已经有人在用，但它不像是 VoiceStudio（健康检查不应答）。
echo   我没有再起一个服务 —— 起了也会因为端口冲突直接失败。
echo   请先在任务管理器里结束掉旧的 python.exe，再双击本文件。
echo.
pause
exit /b 2

:CHECK
rem 快判据：端口有没有人在听。系统命令走全路径（本机 PATH 有被改坏的历史）
"%SYS%\netstat.exe" -ano | "%SYS%\findstr.exe" /C:":8756 " | "%SYS%\findstr.exe" /I "LISTEN" >nul 2>&1
if errorlevel 1 exit /b 1
rem 硬判据：必须真的是这家服务（问健康接口），排除端口被别的程序占了
rem   --noproxy "*" 是本机必需的：设了 HTTP_PROXY=127.0.0.1:56990，否则本机请求会被拦成 502
"%SYS%\curl.exe" -f -s -m 5 --noproxy "*" -o NUL "%HEALTH%" >nul 2>&1
if errorlevel 1 exit /b 2
exit /b 0
