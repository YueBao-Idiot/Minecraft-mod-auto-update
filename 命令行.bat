@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Examples:
echo   python -m mcmodupdater plan --instance .\instance --loader fabric --game-version 1.20.1
echo   python -m mcmodupdater apply --instance .\instance --loader fabric --game-version 1.20.1 --yes
echo.
python -m mcmodupdater --help
pause
