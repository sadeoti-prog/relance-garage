@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"

set "VENV_PY=%CD%\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo   [X] Environnement non installe. Lancez start.bat une premiere fois.
    echo.
    pause
    popd
    exit /b 1
)

"%VENV_PY%" scripts\reset_demo.py
if errorlevel 1 (
    echo.
    echo   [X] La remise a zero a echoue. Ne pas faire la demo en l'etat.
)
echo.
pause
popd
