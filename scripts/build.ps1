param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$setup = Join-Path $root "scripts\setup.ps1"
& $setup -Python $Python

$distRoot = Join-Path $root "dist"
# The source distribution has its own folder name so it can never delete the
# packaged executable produced by build_exe.ps1 (and vice versa).
#
# It was called YouTubeDownloader-source until 2026-10-02, a name kept from
# before the product was renamed.  That decision's stated reason was that
# renaming it "would delete a build the user may still want", which is not a
# reason: a build is regenerated in seconds by re-running this script.  The
# name was the only place the old product survived in the shipped program, and
# on a public repository it read as leftover cruft in an otherwise renamed
# project.  Only the folder name changed - what the script does did not.
$appDir = Join-Path $distRoot "ClipDock-source"
if (Test-Path $appDir) {
    Remove-Item $appDir -Recurse -Force
}
New-Item -ItemType Directory -Path $appDir | Out-Null

Copy-Item (Join-Path $root "app.py") $appDir
Copy-Item (Join-Path $root "youtube_downloader") $appDir -Recurse
Copy-Item (Join-Path $root "vendor") $appDir -Recurse
Copy-Item (Join-Path $root "requirements.txt") $appDir
Copy-Item (Join-Path $root "README.md") $appDir
Copy-Item (Join-Path $root "THIRD_PARTY_NOTICES.md") $appDir
Copy-Item (Join-Path $root "scripts") $appDir -Recurse
Copy-Item (Join-Path $root "tools") $appDir -Recurse
Get-ChildItem $appDir -Directory -Filter "__pycache__" -Recurse | Remove-Item -Recurse -Force

$launcher = @"
@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1"
exit /b %ERRORLEVEL%
"@
Set-Content -Path (Join-Path $appDir "start.cmd") -Value $launcher -Encoding ASCII
Write-Host "Built runnable source distribution at $appDir"
Write-Host "The target machine needs Python 3.10-3.14; run scripts\setup.ps1 there before start.cmd if dependencies are not already installed."
