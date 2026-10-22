# MirrorDesk setup (Windows 10/11, 64-bit). Run through Setup-MirrorDesk.cmd.
#   -NoFirewall   skip the firewall rules (no permission prompt)
#   -NoLaunch     do not start MirrorDesk at the end
#   -NoShortcuts  no Start menu / desktop shortcuts
param([switch]$NoFirewall, [switch]$NoLaunch, [switch]$NoShortcuts)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

if (-not [Environment]::Is64BitOperatingSystem) { throw 'MirrorDesk needs 64-bit Windows 10 or 11.' }
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'MirrorDesk needs an Intel or AMD (x64) PC; Windows on ARM is not supported yet.' }
if ($Root.Length -gt 120) { Write-Warning "This folder path is long ($($Root.Length) characters); if setup fails, move the MirrorDesk folder somewhere shorter, for example C:\MirrorDesk." }

# Files unpacked from a downloaded zip are marked "from the internet"; clear the mark for this folder only.
Get-ChildItem -LiteralPath $Root -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

$UvVersion = '0.12.12'
$UvSha256 = '3d54912924c36e862c14f427d04f2ed70a99e8001d1c30caa101f6d5711626d5'
$UvDir = Join-Path $Root 'tools\uv'
$Uv = Join-Path $UvDir 'uv.exe'
Write-Host '[setup] Python installer (uv)...'
if (-not (Test-Path -LiteralPath $Uv)) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $zip = Join-Path $env:TEMP "mirrordesk-uv-$UvVersion-$PID.zip"
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
    $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $UvSha256) { Remove-Item -LiteralPath $zip -Force; throw "The uv download failed its checksum check ($hash). Nothing was installed." }
    New-Item -ItemType Directory -Force -Path $UvDir | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $UvDir -Force
    Remove-Item -LiteralPath $zip -Force
}

Write-Host '[setup] Python 3.13 and MirrorDesk libraries (the first time takes a few minutes)...'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root 'runtime\python'      # Python lives in this folder
$env:UV_PYTHON_PREFERENCE = 'only-managed'                         # never use another Python on this PC
$env:UV_PROJECT_ENVIRONMENT = Join-Path $Root '.venv'
$env:UV_CACHE_DIR = Join-Path $env:TEMP 'mirrordesk-uv-cache'      # downloads; removed below
$env:UV_NO_PROGRESS = '1'
# Native programs report progress on stderr; Windows PowerShell 5.1 would turn that into
# terminating errors under 'Stop'. Judge them by their exit code instead.
$ErrorActionPreference = 'Continue'
& $Uv sync --locked --no-dev --python 3.13 --project $Root
if ($LASTEXITCODE -ne 0) { Write-Host 'Installing Python and its libraries failed (see the messages above). Check the internet connection and run setup again.'; exit 1 }
& $Uv --quiet cache clean | Out-Null

$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Arguments = @('-m', 'mirrordesk.winsetup', 'install')
if ($NoFirewall) { $Arguments += '--no-firewall' }
if ($NoLaunch) { $Arguments += '--no-launch' }
if ($NoShortcuts) { $Arguments += '--no-shortcuts' }
& $Python @Arguments
exit $LASTEXITCODE
