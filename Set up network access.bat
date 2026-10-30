@echo off
rem Double-click to let colleagues use KasTec from their own PCs, over HTTPS.
rem Sets up a certificate, opens the Windows Firewall port and switches KasTec to network mode.
rem It runs as you (so it uses your Python); Windows asks for permission only to add the firewall rule.
setlocal
title KasTec Center - network access
cd /d "%~dp0"

set "PY="
py --version >nul 2>nul
if not errorlevel 1 (
  set "PY=py"
) else (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python is not installed. Run "Start KasTec" first, it explains how to install it.
  pause
  exit /b 1
)
%PY% -c "import cryptography" >nul 2>nul
if errorlevel 1 %PY% -m pip install --disable-pip-version-check -q -r requirements.txt

%PY% -m app.tls
echo.
echo When you're done, double-click "Start KasTec" (or your KasTec shortcut) to restart KasTec with these settings.
pause
