# Launch the desktop live monitor (Python)
#
# The first run creates a virtual environment on the F: drive and installs the
# dependencies; later runs start the app directly.

$ErrorActionPreference = 'Stop'

$venv = 'F:\Espressif\venvs\bme-host'
$py = "$venv\Scripts\python.exe"

# Clear PYTHONPATH so other interpreters on the system (ROS 2 etc.) cannot leak
# packages into this virtual environment.
$env:PYTHONPATH = ''

if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment at $venv ..."
    New-Item -ItemType Directory -Path (Split-Path $venv) -Force | Out-Null
    py -3.12 -m venv $venv
    & $py -m pip install --upgrade pip --quiet
}

$stamp = "$venv\.deps-ok"
$req = "$PSScriptRoot\python\requirements.txt"
if (-not (Test-Path $stamp) -or (Get-Item $req).LastWriteTime -gt (Get-Item $stamp).LastWriteTime) {
    Write-Host "Installing dependencies ..."
    & $py -m pip install -r $req --quiet
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
    New-Item -ItemType File -Path $stamp -Force | Out-Null
}

& $py "$PSScriptRoot\python\bme_plot.py" @args
