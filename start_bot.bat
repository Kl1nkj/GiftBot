@echo off
rem Runs the bot, restarts it if it crashes. Log: bot.log
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
:loop
python bot.py >> bot.log 2>&1
timeout /t 10 /nobreak >nul
goto loop
