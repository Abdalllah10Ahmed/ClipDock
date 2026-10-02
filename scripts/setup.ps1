param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venv = Join-Path $root ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create .venv. Install CPython 3.10-3.14 and try again."
    }
}
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the pinned Python dependencies."
}
& $venvPython (Join-Path $root "tools\fetch_dependencies.py")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install or verify the bundled Windows dependencies."
}
Write-Host "Setup complete. Start the app with scripts\run.ps1."
