# Launch the Web Bluetooth monitor
#
# Browsers only allow Web Bluetooth on https or localhost, so serve the folder
# locally and open the page.

$ErrorActionPreference = 'Stop'

$port = 8000
Write-Host "Opening http://localhost:$port/web/ - click 'Connect device'"
Write-Host "Press Ctrl+C to stop the server"

Start-Process "http://localhost:$port/web/"
py -3.12 -m http.server $port --directory $PSScriptRoot
