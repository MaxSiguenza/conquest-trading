@echo off
title Conquest Trading
echo.
echo  ⚔  CONQUEST TRADING
echo  ===================
echo.
echo  Starting Web App + Discord Bot...
echo  Web App:     http://localhost:5000
echo  Bot:         Discord server  (commands: !scan !portfolio !briefing !help)
echo.
echo  Press Ctrl+C in either window to stop.
echo.

:: Start the web app in its own window
start "Conquest Web App" cmd /k "python web_app.py"

:: Wait 2 seconds for the web app to start
timeout /t 2 /nobreak >nul

:: Start the Discord bot in its own window
start "Conquest Discord Bot" cmd /k "python discord_bot.py"

echo  Both launched. Check the two new windows.
echo.
pause
