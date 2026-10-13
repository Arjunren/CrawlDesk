$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment. Install Python 3.11 or later.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the error above and try again.' }
Write-Host 'CrawlDesk is ready. Double-click launch.bat to open the app.' -ForegroundColor Green
