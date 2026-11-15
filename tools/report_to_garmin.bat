@echo off
rem Huawei body-composition report -> Garmin import files (Windows, ASCII only).
rem Drag one .jpg/.png screenshot, a key-value .txt, or a report_draft.txt here.
setlocal EnableExtensions
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
rem Set SYNC_TOOL_QUIET=1 to suppress notepad/explorer/pause (for scripting & tests).
set "RC=0"
cd /d "%~dp0.."

if "%~1"=="" goto :usage

set "SRC=%~f1"
set "NAME=%~n1"
rem Dragging the edited report_draft.txt back in should write to its own folder,
rem not create a new folder literally called "report_draft".
if /i "%~nx1"=="report_draft.txt" for %%I in ("%~dp1.") do set "NAME=%%~nxI"
if not exist "%SRC%" (
  echo [ERROR] File not found: %SRC%
  set "RC=1"
  goto :end
)

rem One folder per input file so previous results are never overwritten.
set "OUT=%CD%\out\reports\%NAME%"
echo Input : %SRC%
echo Output: %OUT%
echo.
python -m sync_tool report -i "%SRC%" -o "%OUT%"
set "RC=%ERRORLEVEL%"

if "%RC%"=="0" goto :ok
if "%RC%"=="2" goto :draft
echo.
echo [FAILED] See the message above (is Python on PATH? is the input a valid report?).
goto :end

:draft
echo.
echo [NEEDS REVIEW] Windows OCR sometimes misreads digits.
echo Compare report_draft.txt with the original image, fix the numbers, save,
echo then drag report_draft.txt onto this .bat again.
echo Or paste the draft into the report-image tab of web\index.html to accumulate
echo several measurements and export ONE combined CSV / FIT at the end.
echo Draft: %OUT%\report_draft.txt
if not "%SYNC_TOOL_QUIET%"=="1" start "" notepad "%OUT%\report_draft.txt"
goto :end

:ok
echo.
echo [OK] garmin_import.csv and garmin_weight.fit are ready.
echo Next: https://connect.garmin.cn/modern/import-data - cloud icon - Import Data
echo Tip: delete any existing record for that date in Garmin first.
if not "%SYNC_TOOL_QUIET%"=="1" explorer "%OUT%"
goto :end

:usage
echo Huawei body-composition report -> Garmin import file
echo.
echo Drag ONE screenshot (.jpg/.png), a key-value .txt, or report_draft.txt
echo onto this .bat file. Command line also works:
echo   report_to_garmin.bat "C:\path\to\report.jpg"
echo.
echo Each file gets its own folder under out\reports\ (nothing is overwritten).
echo For many screenshots at once, use web\index.html instead: paste each
echo draft into the report tab, then export a single combined CSV / FIT.

:end
if "%SYNC_TOOL_QUIET%"=="1" exit /b %RC%
echo.
pause
exit /b %RC%
