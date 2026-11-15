@echo off
rem Garmin Weight Helper - double click to launch. ASCII only (cmd codepage safe).
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "PYC=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"

if not exist "%PYW%" goto :setup
"%PYC%" -c "import PySide6" >nul 2>&1
if errorlevel 1 goto :setup
goto :launch

:setup
echo.
echo  First run: creating a private Python environment under desktop\.venv
echo  and installing the offline OCR components. Internet is needed ONCE.
echo  After that, all image recognition runs locally with no network.
echo  This takes a few minutes; please keep this window open.
echo.
rem If .venv already exists, just repair packages - no system Python needed.
if exist "%PYC%" goto :piponly
where python >nul 2>&1
if errorlevel 1 goto :nopython
python -m venv "%VENV%" && goto :venvok
py -3 -m venv "%VENV%" || goto :fail
:venvok
if not exist "%PYC%" goto :fail

:piponly
"%PYC%" -m pip install --disable-pip-version-check --no-input -i https://mirrors.aliyun.com/pypi/simple/ -r "%~dp0requirements.txt"
if errorlevel 1 "%PYC%" -m pip install --disable-pip-version-check --no-input -r "%~dp0requirements.txt"
if errorlevel 1 goto :fail
"%PYC%" -c "import PySide6" >nul 2>&1
if errorlevel 1 goto :fail
echo.
echo  Setup finished. Launching...
echo.

:launch
start "" "%PYW%" "%~dp0main.py"
exit /b 0

:nopython
echo [ERROR] Python 3.10+ is not on PATH.
echo Install Python from https://www.python.org/downloads/ and check "Add to PATH".
pause
exit /b 1

:fail
echo.
echo [ERROR] Setup failed. See the messages above.
echo Check desktop\logs\ and try again, or run this .bat once more.
pause
exit /b 1
