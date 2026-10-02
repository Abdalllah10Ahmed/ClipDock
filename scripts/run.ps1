param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($Python)) {
    $localPython = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $localPython) {
        $Python = $localPython
    } else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            throw "Python 3.10-3.14 was not found. Run scripts\setup.ps1 first."
        }
        $Python = $command.Source
    }
}

& $Python -c "import PySide6, yt_dlp" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PySide6 or yt-dlp is not installed for the selected Python. Run scripts\\setup.ps1 first."
}
$env:PYTHONUTF8 = "1"
& $Python (Join-Path $root "app.py")
exit $LASTEXITCODE
