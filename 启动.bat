@echo off
cd /d "%~dp0"

rem Launch the portable app folder when present.
for %%F in ("MC*\*.exe") do (
    start "" "%%~fF"
    exit /b 0
)

if exist "*.exe" (
    for %%F in ("*.exe") do (
        start "" "%%~fF"
        exit /b 0
    )
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw -m mcmodupdater
    exit /b 0
)

echo Could not find the packaged app or pythonw.
echo Run tools\build_exe.ps1, or install Python 3.11+.
pause
exit /b 1
