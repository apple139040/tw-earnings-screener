@echo off
cd /d "%~dp0"
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI"

echo [1/4] Saving files...
git init -q -b main
git config user.name "apple139040"
git config user.email "289333141+apple139040@users.noreply.github.com"
git add -A
git commit -q -m "First version" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"

echo [2/4] Creating public GitHub repo...
gh repo create tw-earnings-screener --public --source=. --push
if errorlevel 1 goto failed

echo [3/4] Turning on the website...
gh api -X POST repos/apple139040/tw-earnings-screener/pages -f "source[branch]=main" -f "source[path]=/" --silent
if errorlevel 1 goto failed

echo [4/4] Starting the first cloud update...
timeout /t 10 /nobreak >nul
gh workflow run update.yml -R apple139040/tw-earnings-screener
if errorlevel 1 goto failed

echo.
echo DONE. Website: https://apple139040.github.io/tw-earnings-screener/
echo It may take 1-2 minutes to appear.
pause
exit /b 0

:failed
echo.
echo FAILED. Please copy the messages above and send them to Claude.
pause
exit /b 1
