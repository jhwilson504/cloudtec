# Install-KasTecOnVM.ps1: run on the VM (smitvmv) in an Administrator PowerShell window.
#   Installs Python if needed, downloads KasTec to C:\KasTec, optionally copies your data from your PC,
#   then runs "Install KasTec as a service".
param(
  [string]$Folder = 'C:\KasTec',
  [string]$CopyDataFrom = ''   # e.g. '\\YOURPC\C$\Users\1873352\Desktop\cloudtec-main\data'
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Open PowerShell with "Run as administrator" and run this again.'
}

# 1. Python, installed for all users so the KasTec service can use it.
function Find-Python {
  foreach ($c in 'py', 'python') {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
      $exe = & $c -c "import sys; print(sys.executable)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $exe -and $exe -notmatch 'WindowsApps') { return $exe.Trim() }
    }
  }
}
$python = Find-Python
if (-not $python) {
  Write-Host 'Installing Python 3.12 for all users...'
  $setup = Join-Path $env:TEMP 'python-3.12.7-amd64.exe'
  Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile $setup -UseBasicParsing
  $p = Start-Process $setup -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0' -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "The Python installer failed (exit code $($p.ExitCode))." }
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
  $python = Find-Python
  if (-not $python) { throw 'Python was installed but not found. Close this window, open a new Administrator PowerShell and run the script again.' }
}
Write-Host "Python: $python" -ForegroundColor Green

# 2. KasTec's program files (the data folder is never touched).
$repo = 'jhwilson504/cloudtec'
$sha = (Invoke-RestMethod "https://api.github.com/repos/$repo/commits/main" -Headers @{ 'User-Agent' = 'KasTec' } -TimeoutSec 30).sha
Write-Host "Downloading KasTec ($($sha.Substring(0, 7))) to $Folder..."
$tmp = Join-Path $env:TEMP ('KasTec-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $tmp, $Folder -Force | Out-Null
try {
  $zip = Join-Path $tmp 'kastec.zip'
  Invoke-WebRequest "https://github.com/$repo/archive/$sha.zip" -OutFile $zip -UseBasicParsing -TimeoutSec 300
  Expand-Archive $zip -DestinationPath (Join-Path $tmp 'x')
  $src = Get-ChildItem (Join-Path $tmp 'x') -Directory | Select-Object -First 1
  foreach ($item in Get-ChildItem $src.FullName -Force | Where-Object { $_.Name -ne 'data' }) {
    $target = Join-Path $Folder $item.Name
    if ($item.PSIsContainer -and (Test-Path $target)) { Remove-Item $target -Recurse -Force }
    Copy-Item $item.FullName $target -Recurse -Force
  }
} finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }

# 3. Your connections, users and passwords from your PC (stop KasTec there first).
$data = Join-Path $Folder 'data'
if ($CopyDataFrom) {
  if (Test-Path (Join-Path $data 'cloudtec.db')) {
    Write-Host "$data already has KasTec data, so nothing was copied (delete it first to copy again)." -ForegroundColor Yellow
  } else {
    if (-not (Test-Path (Join-Path $CopyDataFrom 'cloudtec.db'))) { throw "No KasTec data (cloudtec.db) in $CopyDataFrom." }
    Write-Host "Copying your KasTec data from $CopyDataFrom..."
    New-Item -ItemType Directory $data -Force | Out-Null
    # Not the old certificate/network settings (they're for your PC's name) or the old log.
    Get-ChildItem $CopyDataFrom -Force | Where-Object { $_.Name -notin 'tls', 'network.json', 'kastec.log', 'python-path.txt', 'service-env.bat' } |
      ForEach-Object { Copy-Item $_.FullName (Join-Path $data $_.Name) -Recurse -Force }
  }
}
New-Item -ItemType Directory $data -Force | Out-Null
Set-Content (Join-Path $data 'installed-version.txt') $sha

# 4. Make it start with Windows (asks a few questions and sets up HTTPS for this VM).
Write-Host ''
Write-Host 'Setting KasTec up to run in the background...' -ForegroundColor Cyan
Set-Location $Folder
& (Join-Path $Folder 'Install KasTec as a service.bat')
