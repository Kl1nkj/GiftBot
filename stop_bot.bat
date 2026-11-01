@echo off
rem Stop the bot running in background
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*start_bot.bat*') -or ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*bot.py*') -or $_.Name -eq 'cloudflared.exe' -or ($_.Name -eq 'ssh.exe' -and $_.CommandLine -like '*localhost.run*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo.
echo Bot stopped. / Bot ostanovlen.
pause
