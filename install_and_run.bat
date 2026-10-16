@echo off
REM Установка и запуск Polymarket Oracle на диске D
cd /d D:\polymarket-oracle
if not exist venv (
  echo [1/3] Creating venv...
  python -m venv venv
)
echo [2/3] Installing deps...
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\python -m pip install -r requirements.txt
echo [3/3] Test run (one cycle)...
venv\Scripts\python src\main.py --once
echo.
echo Done. Logs: D:\polymarket-oracle\logs\signals.log
echo Loop mode: venv\Scripts\python src\main.py --loop
pause
