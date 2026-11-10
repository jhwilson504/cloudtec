@echo off
rem Run this once on the computer that should host KasTec (a server or VM): right-click > Run as administrator.
rem KasTec then starts with Windows, runs in the background with nobody signed in, and colleagues open it at
rem https://<this computer's name or IP>:8443. Run it again to update and restart KasTec, or to remove the setup.
setlocal
title KasTec Center - install as a service
cd /d "%~dp0"

net session >nul 2>nul
if errorlevel 1 (
  echo Right-click "Install KasTec as a service" and choose "Run as administrator".
  pause
  exit /b 1
)

set "PY="
py --version >nul 2>nul
if not errorlevel 1 (
  set "PY=py"
) else (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python is not installed for this account.
  echo.
  echo Install it from https://www.python.org/downloads/ ^(choose "Customize installation",
  echo tick "Install Python for all users" and "Add Python to environment variables"^), then run this again.
  pause
  exit /b 1
)
if not exist "app\run.py" (
  echo KasTec's program files aren't in this folder. Double-click "Start KasTec" once first, then run this again.
  pause
  exit /b 1
)

echo Installing KasTec's components...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo Installing failed. Send a screenshot of this window for help.
  pause
  exit /b 1
)
%PY% -c "import sys; print(sys.executable)" > "%TEMP%\KasTec-python.txt"

rem Other computers need HTTPS with a certificate for THIS computer's name (a data folder copied from
rem another PC carries that PC's certificate).
%PY% -c "import socket, sys; from pathlib import Path; from app.tls import read_network, cert_summary; d = Path('data'); names = [n.lower() for n in (cert_summary(d) or {}).get('names', [])]; sys.exit(0 if read_network(d).get('enabled') and socket.gethostname().lower() in [n.split('.')[0] for n in names] else 1)" 2>nul
if errorlevel 1 (
  echo.
  echo First, set up HTTPS so other computers can reach KasTec on this computer.
  echo.
  %PY% -m app.tls
  %PY% -c "import sys; from pathlib import Path; from app.tls import read_network; sys.exit(0 if read_network(Path('data')).get('enabled') else 1)"
  if errorlevel 1 (
    echo.
    echo Network access isn't on yet, so other computers couldn't reach KasTec. Run this again once it's set up.
    pause
    exit /b 1
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = Get-Content -LiteralPath '%~f0' -Raw; Invoke-Expression $s.Substring($s.LastIndexOf('#PS' + '-START'))"
echo.
pause
exit /b 0

#PS-START
# Everything below runs in PowerShell (the batch part above never reaches it).
$ErrorActionPreference = 'Stop'
$dir = (Get-Location).Path
$data = Join-Path $dir 'data'
$taskName = 'KasTec Center'
$marker = Join-Path $dir 'KasTec runs as a service.txt'
$py = (Get-Content (Join-Path $env:TEMP 'KasTec-python.txt') -Raw).Trim()

function Stop-KasTec {
  if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $taskName }
  # The background loop (cmd running the launcher) first, so it can't start KasTec again, then KasTec itself.
  Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -match 'KasTec.*\.bat.*service' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name like 'py.exe'" | Where-Object { $_.CommandLine -match 'app\.(run|main)' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep 2
}

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
  Write-Host ''
  Write-Host 'KasTec is already set up to run in the background on this computer.'
  $c = Read-Host 'Press Enter to update and restart it, type R to remove the background setup, or Q to quit'
  if ($c -match '^[Qq]') { exit 0 }
  if ($c -match '^[Rr]') {
    Stop-KasTec
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Remove-Item $marker -ErrorAction SilentlyContinue
    Write-Host 'Removed. KasTec no longer starts with Windows; your data folder is kept. Double-click "Start KasTec" to run it by hand.'
    exit 0
  }
  Stop-KasTec
  Start-ScheduledTask -TaskName $taskName
  Write-Host 'Restarting KasTec (it checks for updates first)...'
} else {
  if ($dir -like "$env:SystemDrive\Users\*") {
    Write-Host ''
    Write-Host "KasTec is in $dir, inside a user's profile."
    Write-Host 'For a service it is better in a folder like C:\KasTec: close this window, move the whole folder'
    Write-Host '(including "data", so your connections and users come along) and run this again from there.'
    if ((Read-Host 'Continue here anyway? (y/N)') -notmatch '^[Yy]') { exit 0 }
  }

  Write-Host ''
  Write-Host 'Which Windows account should KasTec run as? It uses the passwords saved in KasTec to reach'
  Write-Host 'Active Directory, vCenter and the rest, so it needs no special rights of its own, except:'
  Write-Host 'commands on the PowerShell page run on this computer as this account.'
  Write-Host '  Press Enter   for the built-in SYSTEM account (simplest)'
  Write-Host '  DOMAIN\name   for a service account'
  $account = (Read-Host 'Account').Trim()
  $password = $null
  if ($account) {
    $secure = Read-Host "Password for $account" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
  }

  Write-Host ''
  Write-Host 'The PowerShell page normally only works in a browser on the KasTec computer itself.'
  $remoteShell = (Read-Host 'Allow it from other computers too, for KasTec administrators? (y/N)') -match '^[Yy]'

  # Settings for background mode, and the Python found here (the service account may not have it on its PATH).
  New-Item -ItemType Directory -Force $data | Out-Null
  Set-Content (Join-Path $data 'python-path.txt') ('"' + $py + '"') -Encoding ASCII
  Set-Content (Join-Path $data 'service-env.bat') ('set "KASTEC_SHELL=' + $(if ($remoteShell) { 'on' } else { 'local' }) + '"') -Encoding ASCII

  # The data folder holds the encrypted passwords and their key: only SYSTEM, Administrators and the service account.
  icacls $data /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
  if ($account) {
    icacls $data /grant "${account}:(OI)(CI)M" | Out-Null
    icacls $dir /grant "${account}:(OI)(CI)M" | Out-Null   # so updates can replace KasTec's program files
  }

  Stop-KasTec
  $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c ""' + (Join-Path $dir 'Start KasTec.bat') + '" service"') -WorkingDirectory $dir
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  $description = 'KasTec Center, started with Windows by "Install KasTec as a service". Log: data\kastec.log'
  if ($account) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description $description `
      -User $account -Password $password -RunLevel Highest -Force | Out-Null
  } else {
    $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description $description `
      -Principal $principal -Force | Out-Null
  }
  $password = $null
  Start-ScheduledTask -TaskName $taskName
  Write-Host ''
  Write-Host 'Starting KasTec in the background (the first start can take a minute)...'
}

# Wait for it to answer, then say where to open it.
$port = 8443
try { $n = Get-Content (Join-Path $data 'network.json') -Raw | ConvertFrom-Json; if ($n.port) { $port = [int]$n.port } } catch {}
$up = $false
foreach ($i in 1..60) {
  $t = New-Object Net.Sockets.TcpClient
  try { $t.Connect('127.0.0.1', $port); $up = $true } catch {} finally { $t.Close() }
  if ($up) { break }
  Start-Sleep 3
}
$fqdn = try { [Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName.ToLower() } catch { $env:COMPUTERNAME.ToLower() }
$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
         Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } | Select-Object -ExpandProperty IPAddress)
$urls = @("https://${fqdn}:$port") + ($ips | ForEach-Object { "https://${_}:$port" })
$text = @(
  'KasTec starts with Windows on this computer and runs in the background (Task Scheduler: "KasTec Center").'
  'Open it at:'
) + ($urls | ForEach-Object { "  $_" }) + @(
  ''
  'Its log is data\kastec.log. To update or restart it, or to remove this setup, right-click'
  '"Install KasTec as a service" and choose Run as administrator.'
)
Set-Content $marker $text
Write-Host ''
if ($up) { Write-Host 'KasTec is running.' -ForegroundColor Green }
else { Write-Host "KasTec hasn't answered yet. If it still doesn't open in a few minutes, look at data\kastec.log." -ForegroundColor Yellow }
$text | ForEach-Object { Write-Host $_ }
