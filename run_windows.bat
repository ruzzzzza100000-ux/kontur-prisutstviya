@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Контур присутствия — локальный MVP
echo Если хотите автоматический поиск, задайте BRAVE_SEARCH_API_KEY в переменных среды.
python --version >nul 2>&1
if errorlevel 1 (
  echo Python не найден. Установите Python 3.10+ и включите Add Python to PATH.
  pause
  exit /b 1
)
python server.py
pause
