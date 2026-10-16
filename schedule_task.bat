@echo off
REM Autostart sensor every 5 min via Windows Task Scheduler. Run as Admin.
set TASK=PolymarketOracle
schtasks /create /tn "%TASK%" /tr "D:\polymarket-oracle\venv\Scripts\python.exe D:\polymarket-oracle\src\main.py --once" /sc minute /mo 5 /st 00:00 /rl lowest /f
echo.
echo Task created. Check: schtasks /query /tn "PolymarketOracle"
echo Run now: schtasks /run /tn "PolymarketOracle"
echo Stop/delete: schtasks /delete /tn "PolymarketOracle" /f
pause
