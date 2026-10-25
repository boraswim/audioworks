# Vocalworks setup: installs everything the app needs into this folder and adds shortcuts.
# Run it through "Install Vocalworks.bat". Safe to run again (it repairs or updates the install).
param([switch]$NoShortcuts, [switch]$NoLaunch)  # for testing a copy
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }

Write-Host "Vocalworks setup" -ForegroundColor Green
Write-Host "Installs Python 3.11 and the app's libraries into $root\.venv (about 3 GB with an NVIDIA GPU,"
Write-Host "less without). Nothing is installed system-wide except the small 'uv' Python installer."

# 1. uv: the Python and package installer
Step "Checking for uv"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    $local = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $local) { $uv = $local }
}
if (-not $uv) {
    Write-Host "Installing uv (https://docs.astral.sh/uv/)..."
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (-not (Test-Path $uv)) { throw "uv didn't install. Install it from https://docs.astral.sh/uv/ and run this again." }
}
Write-Host "uv: $uv"

# 2. Python 3.11 in .venv (uv downloads Python itself if it's missing)
Step "Setting up Python 3.11"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $uv venv .venv --python 3.11
    if ($LASTEXITCODE -ne 0) { throw "Couldn't create the Python environment." }
}

# 3. Libraries: the GPU build of PyTorch with an NVIDIA card, the CPU build otherwise
Step "Installing the app's libraries (this takes a while the first time)"
$gpu = $null -ne (Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if ($gpu) {
    Write-Host "NVIDIA GPU found: installing the GPU build (splitting songs is much faster)."
    $index = "https://download.pytorch.org/whl/cu126"
} else {
    Write-Host "No NVIDIA GPU found: installing the CPU build (splitting a song takes several minutes)."
    $index = "https://download.pytorch.org/whl/cpu"
}
& $uv pip install --python .venv\Scripts\python.exe -r requirements.txt --extra-index-url $index --index-strategy unsafe-best-match
if ($LASTEXITCODE -ne 0) { throw "Installing the libraries failed. Check your internet connection and run this again." }

# 4. Shortcuts on the desktop and in the Start menu, with the app's icon
if (-not $NoShortcuts) {
Step "Adding shortcuts"
$shell = New-Object -ComObject WScript.Shell
$places = @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("Programs")) ""))
foreach ($dir in $places) {
    $old = Join-Path $dir "Audioworks.lnk"  # the app's old name
    if (Test-Path $old) { Remove-Item $old }
    $lnk = $shell.CreateShortcut((Join-Path $dir "Vocalworks.lnk"))
    $lnk.TargetPath = Join-Path $root ".venv\Scripts\pythonw.exe"
    $lnk.Arguments = "-m app"
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = Join-Path $root "app\assets\vocalworks.ico"
    $lnk.Description = "Vocalworks: sing covers that sound like the original"
    $lnk.Save()
}
}

Write-Host ""
Write-Host "Vocalworks is installed. Start it from the desktop or Start menu shortcut." -ForegroundColor Green
Write-Host "The vocal splitter's model (about 300 MB) downloads the first time you prepare a song."
if ($NoLaunch) { exit 0 }
$answer = Read-Host "Start Vocalworks now? (Y/n)"
if ($answer -notmatch "^[nN]") {
    Start-Process -FilePath (Join-Path $root ".venv\Scripts\pythonw.exe") -ArgumentList "-m", "app" -WorkingDirectory $root
}
