@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto deps
python --version >nul 2>&1
if errorlevel 1 goto missing
python -m venv .venv
if errorlevel 1 goto failed
:deps
".venv\Scripts\python.exe" -c "import oci; assert oci.__version__ == '2.189.0'" >nul 2>&1
if not errorlevel 1 goto run
".venv\Scripts\python.exe" -m pip install --no-compile -r requirements.txt
if errorlevel 1 goto failed
:run
if "%~1"=="" (
  ".venv\Scripts\python.exe" grab.py --menu
) else (
  ".venv\Scripts\python.exe" grab.py %*
)
set "GRABBER_EXIT=%ERRORLEVEL%"
if not "%GRABBER_EXIT%"=="0" pause
exit /b %GRABBER_EXIT%
:missing
echo Python 3.10+ is required. Install it from python.org and enable Add to PATH.
:failed
echo Setup failed. Check Python and network access, then try again.
pause
exit /b 2
