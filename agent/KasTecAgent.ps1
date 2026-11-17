<#
  KasTec activity agent (version 1.0)

  Runs as the signed-in user (Group Policy starts it at logon). Every 15 seconds it notes:
    - which application is in front (its process name, e.g. outlook, excel, chrome)
    - for a web browser, the website's domain only (e.g. youtube.com), never the full address
    - whether there was keyboard or mouse input in the last minute (active) or not (idle)
  and every 5 minutes sends those minutes to KasTec.

  It does NOT record keystrokes, window titles, page contents, file names or screenshots.
  On the first run for each person it shows the company's notice (set on KasTec's Employee activity page).
  KasTec decides who is monitored (members of one AD group); for anyone else the agent records nothing.

  Download it from KasTec (Employee activity -> Settings): the server address and agent key are filled in.
#>
param(
  [string]$Server = '__KASTEC_SERVER__',
  [string]$Key = '__KASTEC_AGENT_KEY__'
)

$ErrorActionPreference = 'SilentlyContinue'
$Version = '1.0'
$Browsers = @('chrome', 'msedge', 'firefox', 'iexplore', 'brave', 'opera')
$Dir = Join-Path $env:LOCALAPPDATA 'KasTec'
$QueueFile = Join-Path $Dir 'activity-queue.json'
$AckFile = Join-Path $Dir 'notice-accepted.txt'
$LogFile = Join-Path $Dir 'agent.log'
New-Item -ItemType Directory -Force $Dir | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-AgentLog([string]$Text) {
  try {
    if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) { Move-Item $LogFile "$LogFile.old" -Force }
    Add-Content $LogFile ('{0:s} {1}' -f (Get-Date), $Text)
  } catch {}
}

# One agent per signed-in user.
$mutex = New-Object System.Threading.Mutex($false, 'Local\KasTecActivityAgent')
if (-not $mutex.WaitOne(0)) { exit }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class KasTecWin {
  [StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
  [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO info);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  public static uint IdleSeconds() {
    LASTINPUTINFO i = new LASTINPUTINFO(); i.cbSize = (uint)Marshal.SizeOf(i);
    if (!GetLastInputInfo(ref i)) return 0;
    return ((uint)Environment.TickCount - i.dwTime) / 1000;
  }
  public static uint ForegroundPid() { uint pid; GetWindowThreadProcessId(GetForegroundWindow(), out pid); return pid; }
  public static string ForegroundTitle() { StringBuilder s = new StringBuilder(512); GetWindowText(GetForegroundWindow(), s, 512); return s.ToString(); }
}
'@
try { Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes } catch {}

$headers = @{ 'X-KasTec-Agent-Key' = $Key }
$who = "$env:USERDOMAIN\$env:USERNAME"

function Get-AgentConfig {
  try {
    return Invoke-RestMethod -Uri ("$Server/api/activity/agent/config?user=" + [uri]::EscapeDataString($who)) -Headers $headers -TimeoutSec 30
  } catch {
    Write-AgentLog "Couldn't reach KasTec at ${Server}: $($_.Exception.Message)"
    return $null
  }
}

# The browser's address bar, read through Windows UI Automation; only the domain is kept.
$domainCache = @{ Key = ''; Domain = '' }
function Get-BrowserDomain {
  $hwnd = [KasTecWin]::GetForegroundWindow()
  $cacheKey = "$hwnd|" + [KasTecWin]::ForegroundTitle()   # the title is only used to notice a page change, never stored
  if ($cacheKey -eq $domainCache.Key) { return $domainCache.Domain }
  $domain = ''
  try {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    $isEdit = New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
    $edit = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $isEdit)
    if ($edit) {
      $text = ([System.Windows.Automation.ValuePattern]$edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).Current.Value
      if ($text -match '^(?:[a-z][a-z0-9+.-]*://)?([a-z0-9.-]+\.[a-z]{2,})(?:[:/?#]|$)') { $domain = $Matches[1].ToLower() -replace '^www\.', '' }
    }
  } catch {}
  $domainCache.Key = $cacheKey
  $domainCache.Domain = $domain
  return $domain
}

function Get-Queue {
  try { return @(Get-Content $QueueFile -Raw | ConvertFrom-Json) } catch { return @() }
}

function Send-Queue([array]$Ready) {
  $queue = @(Get-Queue) + @($Ready)
  if ($queue.Count -gt 10000) { $queue = $queue[-10000..-1] }   # about a week offline; older minutes are dropped
  if (-not $queue.Count) { return $true }
  $sent = $true
  for ($i = 0; $i -lt $queue.Count; $i += 2000) {
    $batch = @($queue[$i..([math]::Min($i + 1999, $queue.Count - 1))])
    $body = @{ user = $who; computer = $env:COMPUTERNAME; agent = $Version; samples = $batch } | ConvertTo-Json -Depth 4 -Compress
    try {
      $r = Invoke-RestMethod -Method Post -Uri "$Server/api/activity/agent/report" -Headers $headers -Body $body `
             -ContentType 'application/json; charset=utf-8' -TimeoutSec 60
      if ($r.monitor -eq $false) { $script:monitor = $false }
    } catch {
      Write-AgentLog "Upload failed, will retry: $($_.Exception.Message)"
      $queue = @($queue[$i..($queue.Count - 1)])
      $sent = $false
      break
    }
  }
  if ($sent) { Remove-Item $QueueFile -ErrorAction SilentlyContinue }
  else { $queue | ConvertTo-Json -Depth 3 -Compress | Set-Content $QueueFile -Encoding UTF8 }
  return $sent
}

# --- main loop --------------------------------------------------------------------------------
Write-AgentLog "Agent $Version started for $who on $env:COMPUTERNAME"
$monitor = $false
$nextConfig = Get-Date
$nextUpload = (Get-Date).AddMinutes(5)
$minutes = @{}   # "2026-11-17T13:05:00Z|app|domain" -> sample
$mySession = [System.Diagnostics.Process]::GetCurrentProcess().SessionId

while ($true) {
  if ((Get-Date) -ge $nextConfig) {
    $config = Get-AgentConfig
    if ($config) {
      $monitor = [bool]$config.monitor
      if ($monitor -and -not (Test-Path $AckFile)) {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show($config.notice, 'Activity monitoring', 'OK', 'Information')
        Set-Content $AckFile ('Notice shown {0:s}' -f (Get-Date))
      }
    }
    $nextConfig = (Get-Date).AddHours(1)
  }

  if ($monitor) {
    $now = (Get-Date).ToUniversalTime()
    $minute = $now.ToString('yyyy-MM-ddTHH:mm:00Z')
    $idle = [KasTecWin]::IdleSeconds()
    $app = 'unknown'; $domain = ''
    # The lock screen (LogonUI) in this person's own session; other sessions on a shared server don't count.
    $locked = [bool](Get-Process -Name LogonUI -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $mySession })
    if ($locked) { $app = 'locked' }
    else {
      $p = Get-Process -Id ([KasTecWin]::ForegroundPid()) -ErrorAction SilentlyContinue
      if ($p) { $app = $p.ProcessName.ToLower() }
      if ($Browsers -contains $app) { $domain = Get-BrowserDomain }
    }
    $key = "$minute|$app|$domain"
    if (-not $minutes.ContainsKey($key)) { $minutes[$key] = [ordered]@{ t = $minute; app = $app; domain = $domain; active = 0; idle = 0 } }
    if (-not $locked -and $idle -lt 60) { $minutes[$key].active += 15 } else { $minutes[$key].idle += 15 }

    if ((Get-Date) -ge $nextUpload) {
      $ready = @($minutes.Keys | Where-Object { $_ -notlike "$minute|*" })
      $samples = @($ready | ForEach-Object { $minutes[$_] })
      foreach ($k in $ready) { $minutes.Remove($k) }
      [void](Send-Queue $samples)
      $nextUpload = (Get-Date).AddMinutes(5)
    }
  }
  Start-Sleep -Seconds 15
}
