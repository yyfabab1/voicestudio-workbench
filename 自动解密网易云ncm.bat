@echo off
setlocal
rem ---------------------------------------------------------------
rem  NetEase .ncm auto-decrypt watcher (ASCII-only file on purpose:
rem  the path to the GPT-SoVITS runtime contains Chinese, and a UTF-8
rem  bat would garble it under the GBK console. So this bat only uses
rem  ASCII interpreters and lets ncm_watch.py do the real work.)
rem
rem  Safe to run repeatedly: ncm_watch.py --start is single-instance.
rem ---------------------------------------------------------------
set "PY=C:\Users\yyfabab\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "DIR=D:\AHC\VoiceStudio"

if not exist "%PY%" set "PY=D:\RVC\Retrieval-based-Voice-Conversion-WebUI\runtime\python.exe"
if not exist "%PY%" (
  echo No python found. Edit PY in this bat.
  pause
  exit /b 1
)

pushd "%DIR%"
"%PY%" ncm_watch.py --start
"%PY%" ncm_watch.py --status
echo.
echo Log: %DIR%\data\ncm_watch.log
echo.
pause
