@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
pushd "%~dp0"

set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%"

echo.
echo   =====================================================
echo    RELANCE GARAGE - demonstration
echo   =====================================================
echo.

rem ---------------------------------------------------------------- Python
rem winget installs Python outside the PATH on this machine, so look for it
rem where winget puts it before falling back to the launcher or the PATH.
set "PYTHON="
for %%V in (Python313 Python312 Python311) do (
    if not defined PYTHON (
        if exist "%LOCALAPPDATA%\Programs\Python\%%V\python.exe" (
            set "PYTHON=%LOCALAPPDATA%\Programs\Python\%%V\python.exe"
        )
    )
)
if not defined PYTHON (
    py -3 --version >nul 2>&1 && set "PYTHON=py -3"
)
if not defined PYTHON (
    python --version >nul 2>&1 && set "PYTHON=python"
)
if not defined PYTHON (
    echo   [X] Python 3 est introuvable.
    echo       Installez-le avec :  winget install Python.Python.3.12
    echo.
    pause
    popd
    exit /b 1
)
echo   [1/5] Python detecte : %PYTHON%

rem ------------------------------------------------------------ environnement
set "VENV_PY=%CD%\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo   [2/5] Creation de l'environnement virtuel...
    %PYTHON% -m venv .venv
    if errorlevel 1 goto :failed
) else (
    echo   [2/5] Environnement virtuel present
)

if not exist ".venv\.deps-installed" (
    echo         Installation des dependances...
    "%VENV_PY%" -m pip install --quiet --upgrade pip
    "%VENV_PY%" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :failed
    echo ok> ".venv\.deps-installed"
)

rem -------------------------------------------------------------- donnees
if not exist "data\garage.db" (
    echo   [3/5] Generation du jeu de demonstration...
    "%VENV_PY%" -m app.seed --check
    if errorlevel 1 goto :failed
) else (
    echo   [3/5] Base de demonstration presente
    echo         Pour la regenerer : utilisez reset-demo.bat
)

rem -------------------------------------------------------------- lancement
if exist "app\server.py" (
    echo   [4/5] Preparation des relances du jour...
    "%VENV_PY%" scripts\draft_followups.py --mock >nul
    if errorlevel 1 goto :failed
    echo   [5/5] Demarrage du tableau de bord sur %URL%
    echo.
    echo         Fermez cette fenetre ou Ctrl+C pour arreter.
    echo.
    start "" /b "%VENV_PY%" scripts\open_browser.py %URL%
    "%VENV_PY%" -m uvicorn app.server:app --host 127.0.0.1 --port %PORT%
) else (
    echo   [4/5] Le tableau de bord n'est pas encore installe.
    echo.
    "%VENV_PY%" scripts\show_overdue.py
    echo.
    pause
)

popd
exit /b 0

:failed
echo.
echo   [X] Echec. Voir le message ci-dessus.
echo.
pause
popd
exit /b 1
