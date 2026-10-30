@echo off
rem Double-click to let colleagues use KasTec from their own PCs, over HTTPS.
rem Sets up a certificate, opens the Windows Firewall port and switches KasTec to network mode.
setlocal
title KasTec Center - network access

rem Adding the firewall rule needs Administrator: ask for it.
net session >nul 2>nul
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
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
if errorlevel 1 %PY% -m pip install -r requirements.txt

%PY% -m app.tls
echo.
echo When you're done, double-click "Start KasTec" to restart KasTec with these settings.
pause
