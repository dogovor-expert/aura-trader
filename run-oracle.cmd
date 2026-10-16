@echo off
cd /d D:\polymarket-oracle
set ANYMODEL_API_KEY=%ANYMODEL_API_KEY%
set GROQ_API_KEY=%GROQ_API_KEY%
"D:\polymarket-oracle\venv\Scripts\python.exe" -u D:\polymarket-oracle\src\main.py --loop >> D:\polymarket-oracle\logs\loop_out.log 2>> D:\polymarket-oracle\logs\loop_err.log