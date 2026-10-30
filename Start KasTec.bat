@echo off
rem Double-click to start KasTec Center and open it in your browser.
rem It first updates itself from GitHub when there's a newer version; the data folder is never touched.
rem To turn automatic updates off, create an empty file called "no-auto-update" in the data folder.
rem Keep the window open while you use KasTec; closing the window stops it.
setlocal

rem Run from a temporary copy, so the update can safely replace this file.
if not defined KASTEC_LAUNCHED (
  set "KASTEC_LAUNCHED=1"
  set "KASTEC_HOME=%~dp0"
  copy /y "%~f0" "%TEMP%\KasTec-launcher.bat" >nul
  "%TEMP%\KasTec-launcher.bat"
  exit /b
)
title KasTec Center

rem Find the KasTec folder, stop a KasTec that's already running, and update or repair it (PowerShell part at the end of this file).
del "%TEMP%\KasTec-home.txt" "%TEMP%\KasTec-updated.txt" >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = Get-Content -LiteralPath '%~f0' -Raw; Invoke-Expression $s.Substring($s.LastIndexOf('#PS' + '-START'))"
if errorlevel 1 (
  echo.
  echo KasTec couldn't be prepared. Send a screenshot of this window for help.
  pause
  exit /b 1
)
set /p KASTEC_HOME=<"%TEMP%\KasTec-home.txt"
cd /d "%KASTEC_HOME%"
if exist "Start CloudTec.bat" del "Start CloudTec.bat"

call :find_python || exit /b 1

%PY% -m app.run --open-browser

echo.
echo KasTec has stopped.
pause
exit /b 0

:find_python
rem Find Python: the "py" launcher first, then "python". Install KasTec's components on first run or after an update.
set "PY="
py --version >nul 2>nul
if not errorlevel 1 (
  set "PY=py"
) else (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python is not installed.
  echo.
  echo Install it from https://www.python.org/downloads/
  echo On the first screen of the installer, tick "Add python.exe to PATH".
  echo Then double-click this file again.
  echo.
  pause
  exit /b 1
)
set "NEED_PIP="
%PY% -c "import fastapi, uvicorn, ldap3, httpx, cryptography" >nul 2>nul
if errorlevel 1 set "NEED_PIP=1"
if exist "%TEMP%\KasTec-updated.txt" set "NEED_PIP=1"
if defined NEED_PIP (
  echo Installing KasTec components...
  echo.
  %PY% -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Installing failed. Send a screenshot of this window for help.
    pause
    exit /b 1
  )
  del "%TEMP%\KasTec-updated.txt" >nul 2>nul
)
exit /b 0

#PS-START
# Everything below runs in PowerShell (the batch part above never reaches it).
$ErrorActionPreference = 'Stop'
$repo = 'jhwilson504/cloudtec'
function Test-Install([string]$d) { $d -and ((Test-Path (Join-Path $d 'app\run.py')) -or (Test-Path (Join-Path $d 'data'))) }

# 1. Which folder is KasTec in? The launcher's own folder, or else Desktop\cloudtec-main
#    (for a copy of the launcher that was put somewhere else).
$dir = $env:KASTEC_HOME.TrimEnd('\')
if (-not (Test-Install $dir)) {
  $dir = Join-Path ([Environment]::GetFolderPath('Desktop')) 'cloudtec-main'
  Write-Host "Using KasTec in $dir"
}
New-Item -ItemType Directory -Force $dir | Out-Null
[IO.File]::WriteAllText((Join-Path $env:TEMP 'KasTec-home.txt'), $dir, [Text.Encoding]::Default)

# 2. Stop a KasTec that's already running (only if it really is KasTec), so its files can be updated.
if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
  $ports = @(8000)
  try { $n = Get-Content (Join-Path $dir 'data\network.json') -Raw | ConvertFrom-Json; if ($n.port) { $ports += [int]$n.port } } catch {}
  $ids = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $ports -contains $_.LocalPort } |
         Select-Object -ExpandProperty OwningProcess -Unique
  foreach ($id in $ids) {
    $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $id)
    if ($p.CommandLine -match 'app\.(run|main)') {
      Write-Host 'Restarting the KasTec that was already running...'
      Stop-Process -Id $id -Force
      Start-Sleep 2
    }
  }
}

# 3. Update from GitHub when there's a newer version (or repair a missing app folder).
$versionFile = Join-Path $dir 'data\installed-version.txt'
$installed = if (Test-Path $versionFile) { (Get-Content $versionFile -Raw).Trim() } else { '' }
$missing = -not (Test-Path (Join-Path $dir 'app\run.py'))
if ($missing -or -not (Test-Path (Join-Path $dir 'data\no-auto-update'))) {
  $latest = $null
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $latest = (Invoke-RestMethod "https://api.github.com/repos/$repo/commits/main" -TimeoutSec 15 -Headers @{ 'User-Agent' = 'KasTec' }).sha
  } catch {
    if ($missing) { Write-Host "Couldn't reach GitHub to download KasTec ($($_.Exception.Message))." }
    else { Write-Host "Couldn't check for updates ($($_.Exception.Message)). Starting the version you have." }
  }
  if ($latest -and ($missing -or $latest -ne $installed)) {
    Write-Host 'Updating KasTec to the latest version (your data is kept)...'
    $tmp = Join-Path $env:TEMP ('KasTec-update-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory $tmp | Out-Null
    try {
      $zip = Join-Path $tmp 'kastec.zip'
      Invoke-WebRequest "https://github.com/$repo/archive/$latest.zip" -OutFile $zip -UseBasicParsing -TimeoutSec 180
      Expand-Archive $zip -DestinationPath (Join-Path $tmp 'x')
      $src = Get-ChildItem (Join-Path $tmp 'x') -Directory | Select-Object -First 1
      if (-not $src -or -not (Test-Path (Join-Path $src.FullName 'app\run.py'))) { throw 'the download looks incomplete' }
      $app, $previous = (Join-Path $dir 'app'), (Join-Path $dir 'app.previous')
      if (Test-Path $app) {
        if (Test-Path $previous) { Remove-Item $previous -Recurse -Force }
        Move-Item $app $previous
      }
      foreach ($item in Get-ChildItem $src.FullName -Force | Where-Object { $_.Name -ne 'data' }) {
        $target = Join-Path $dir $item.Name
        if ($item.PSIsContainer -and (Test-Path $target)) { Remove-Item $target -Recurse -Force }
        Copy-Item $item.FullName $target -Recurse -Force
      }
      New-Item -ItemType Directory -Force (Join-Path $dir 'data') | Out-Null
      Set-Content $versionFile $latest
      Set-Content (Join-Path $env:TEMP 'KasTec-updated.txt') '1'
      Write-Host 'KasTec is up to date.'
    } catch {
      Write-Host "The update didn't work ($($_.Exception.Message))."
      $app, $previous = (Join-Path $dir 'app'), (Join-Path $dir 'app.previous')
      if (-not (Test-Path (Join-Path $app 'run.py')) -and (Test-Path (Join-Path $previous 'run.py'))) {
        if (Test-Path $app) { Remove-Item $app -Recurse -Force }
        Move-Item $previous $app
      }
      if (Test-Path (Join-Path $app 'run.py')) { Write-Host 'Starting the version you have.' }
    } finally {
      Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
}
if (-not (Test-Path (Join-Path $dir 'app\run.py'))) {
  Write-Host "KasTec's program files aren't in $dir and couldn't be downloaded."
  exit 1
}
exit 0
