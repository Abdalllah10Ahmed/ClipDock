param(
    [string]$Python = "python",
    [switch]$OneFile,
    [switch]$Slim
)

# Builds a standalone Windows executable with PyInstaller.
#
# PyInstaller is a BUILD-TIME ONLY tool.  It is deliberately not added to
# requirements.txt, so the pinned runtime dependency set stays unchanged and
# the normal source workflow keeps using only PySide6 and yt-dlp.
#
# The build installs PyInstaller into the project virtual environment on first
# use, then produces:
#   dist\ClipDock\ClipDock.exe        (one-folder, default)
#   dist\ClipDock\ClipDock.exe        (one-file, -OneFile)
#
# The folder and executable name follow the product name, which is set once in
# $appName below rather than repeated at each use.
#
# The bundled vendor\ directory (checksum-verified yt-dlp and FFmpeg) is
# copied next to the executable so the frozen app can find FFmpeg at runtime.

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$setup = Join-Path $root "scripts\setup.ps1"
& $setup -Python $Python

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "The project virtual environment is missing. Run scripts\setup.ps1 first."
}

# Install the pinned build-time dependency if it is missing.
& $venvPython -m pip install --disable-pip-version-check "pyinstaller>=6.16,<7"
if ($LASTEXITCODE -ne 0) {
    throw "Could not install PyInstaller, which is required only to build the executable."
}

# Generate the application icon so the executable looks like a normal program.
$iconPath = Join-Path $root "assets\app.ico"
& $venvPython (Join-Path $root "tools\make_icon.py") --output $iconPath
if ($LASTEXITCODE -ne 0) {
    throw "Could not generate the application icon."
}

# The product name, used for the executable, the dist folder, and the final
# message.  Stated once so those three cannot drift apart again.
$appName = "ClipDock"

$distRoot = Join-Path $root "dist"
$appDir = Join-Path $distRoot $appName
$workDir = Join-Path $root "build\pyinstaller"
$specDir = Join-Path $root "build"

foreach ($target in @($appDir, $workDir)) {
    if (Test-Path $target) {
        Remove-Item $target -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $workDir -Force | Out-Null

$arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name", $appName,
    "--distpath", $distRoot,
    "--workpath", $workDir,
    "--specpath", $specDir,
    "--paths", $root,
    "--hidden-import", "yt_dlp",
    "--collect-all", "yt_dlp",
    "--exclude-module", "tkinter",
    "--exclude-module", "PySide6.QtWebEngineCore",
    "--exclude-module", "PySide6.QtWebEngineWidgets",
    "--exclude-module", "PySide6.Qt3DCore",
    "--exclude-module", "PySide6.QtQuick",
    "--exclude-module", "PySide6.QtQml",
    "--exclude-module", "PySide6.QtMultimedia",
    "--icon", $iconPath
)
if ($OneFile) {
    $arguments += "--onefile"
} else {
    $arguments += "--onedir"
}
$arguments += (Join-Path $root "app.py")

Push-Location $root
try {
    # A native tool writing a warning to stderr must not abort the build.  Under
    # "Stop" PowerShell turns that stderr into a terminating error before
    # $LASTEXITCODE is ever consulted, so a tool that succeeds and merely talks
    # - PyInstaller does, for instance when the build runs elevated - would fail
    # the build.  The exit code is the authority here, and it is still checked.
    $ErrorActionPreference = "Continue"
    & $venvPython @arguments
    $buildExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = "Stop"
    Pop-Location
}
if ($buildExitCode -ne 0) {
    throw "PyInstaller could not build the executable."
}

# Place the checksum-verified binaries beside the executable so find_ffmpeg()
# can resolve them from a frozen application. ffprobe.exe is not in vendor/:
# the application never calls it and it is a 128 MB saving on the build.
if (Test-Path (Join-Path $appDir "vendor")) {
    Remove-Item (Join-Path $appDir "vendor") -Recurse -Force
}
if ($Slim) {
    # A slim build ships no vendor\ folder at all.  FFmpeg is 128 MB and is only
    # needed once a download has to merge or convert, so the application checks
    # the whole computer for an existing copy on first run, asks before
    # downloading one if there is none, and installs it device-wide so a second
    # user never repeats the download.  The self-check below is told to expect an
    # absent FFmpeg, since that is this variant's intended state rather than a
    # broken bundle.
    Write-Host "Slim build: vendor\ is not shipped; the program checks the computer for FFmpeg on first run."
} else {
    Copy-Item (Join-Path $root "vendor") $appDir -Recurse
}
Copy-Item (Join-Path $root "THIRD_PARTY_NOTICES.md") $appDir
Copy-Item (Join-Path $root "README.md") $appDir
# The .ico is baked into the executable already; PyInstaller's --icon option
# only sets the executable's own resource.  It also has to exist as a file at
# runtime, because that is what find_icon() hands to QIcon for the taskbar and
# Alt+Tab entries.  It is copied into assets\ to match the source layout the
# lookup expects.
Copy-Item (Join-Path $root "assets") (Join-Path $appDir "assets") -Recurse

$exePath = Join-Path $appDir "$appName.exe"
if (-not (Test-Path $exePath)) {
    throw "The build finished but the executable was not produced at $exePath."
}

# Verify the packaged program really starts and can still find its bundled
# FFmpeg next to the executable.  A non-zero exit code means the bundle is
# broken, so the build fails loudly instead of shipping an unusable program.
$env:QT_QPA_PLATFORM = "offscreen"
# Same reasoning as the PyInstaller call: a windowed executable has no console,
# so anything it writes to stderr is diagnostic noise rather than a verdict.
$ErrorActionPreference = "Continue"
# --allow-missing-ffmpeg is passed for a slim build only.  Without it the
# self-check treats an absent FFmpeg as a broken bundle, which is the correct
# reading for a full build and the wrong one for a slim build that is supposed
# to fetch it on first run.
if ($Slim) {
    & $exePath --check --allow-missing-ffmpeg
} else {
    & $exePath --check
}
$checkExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($checkExitCode -ne 0) {
    throw "The executable was built but its self-check failed (exit code $checkExitCode)."
}

$sizeMb = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
$totalBytes = (Get-ChildItem $appDir -Recurse -File | Measure-Object Length -Sum).Sum
$folderMb = [math]::Round($totalBytes / 1MB, 1)
Write-Host "Built the Windows executable at $exePath"
Write-Host "Executable size: $sizeMb MB; total folder size: $folderMb MB"
if ($Slim) {
    # The self-check deliberately tolerated a missing FFmpeg here, so claiming it
    # resolved would be reporting the opposite of what was verified.
    Write-Host "The self-check passed: the window builds. FFmpeg is absent by design, and is found on the computer or offered on first run."
} else {
    Write-Host "The self-check passed: the window builds and FFmpeg resolves from vendor\."
}
Write-Host "Copy the whole dist\$appName folder when sharing the application."
