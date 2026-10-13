@echo off
rem NexCraft Panel - installation (Windows).
rem Cree un environnement virtuel local (.venv), installe les dependances et prepare le fichier .env.
rem Ce script ne modifie ni le pare-feu, ni l'antivirus, ni aucun reglage systeme.
setlocal enableextensions
cd /d "%~dp0"
echo == NexCraft Panel : installation ==

set "PY="
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
  )
)
if not defined PY (
  echo.
  echo [ERREUR] Python 3.11 ou superieur est introuvable.
  echo Installez-le depuis https://www.python.org/downloads/ en cochant "Add python.exe to PATH",
  echo puis relancez install.bat.
  pause
  exit /b 1
)
echo Python detecte : %PY%

if not exist ".venv\Scripts\python.exe" (
  echo Creation de l'environnement virtuel ^(.venv^)...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo.
    echo [ERREUR] Impossible de creer l'environnement virtuel dans "%CD%\.venv".
    pause
    exit /b 1
  )
)

echo Installation des dependances ^(connexion Internet necessaire^)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet --upgrade pip
if errorlevel 1 (
  echo.
  echo [ERREUR] Mise a jour de pip impossible : verifiez votre connexion Internet ou votre proxy.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERREUR] Echec de l'installation des dependances. Verifiez votre connexion Internet ^(acces a pypi.org^).
  pause
  exit /b 1
)

if not exist data mkdir data
if not exist server mkdir server
if not exist backups mkdir backups
if not exist logs mkdir logs
if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo Fichier .env cree a partir de .env.example.
)

where java >nul 2>&1
if errorlevel 1 (
  echo [Avertissement] Java est introuvable dans le PATH. Installez un JDK/JRE ^(ex. Temurin 21^) avant de demarrer
  echo                 un serveur Minecraft, ou indiquez son chemin complet dans "Parametres du serveur".
) else (
  echo Java detecte.
)

echo.
echo Installation terminee.
echo   1. Lancez le panel : start.bat
echo   2. Ouvrez l'adresse affichee ^(par defaut http://localhost:8080^) et creez votre compte administrateur.
pause
endlocal
