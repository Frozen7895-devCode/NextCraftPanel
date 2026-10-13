@echo off
rem NexCraft Panel - demarrage (Windows).
setlocal enableextensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERREUR] Le panel n'est pas installe. Lancez d'abord install.bat.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m app
if errorlevel 1 (
  echo.
  echo [ERREUR] Le panel s'est arrete avec une erreur. Consultez le dossier "logs" ou le message ci-dessus.
  pause
)
endlocal
