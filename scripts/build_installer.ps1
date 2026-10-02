param(
    [string]$Python = "python",
    [switch]$Full,
    [switch]$SkipExe
)

# Builds the shareable installer: dist\ClipDock-Setup.exe
#
# Two steps, in this order, and the order is the whole point:
#
#   1. scripts\build_exe.ps1  freezes the application into dist\ClipDock\ and
#      verifies that the frozen program actually starts.
#   2. ISCC (Inno Setup)     packages that folder into a single wizard
#      installer.
#
# The installer script does not build anything; it packages whatever is in
# dist\ClipDock.  Running it on its own would therefore ship whatever happened
# to be in that folder, including a stale build from an earlier day.  Doing both
# steps here is what stops that.
#
# By default this builds the SLIM executable - no bundled FFmpeg, 134 MB - and
# the installer around it, which is the artefact meant for other people's
# machines.  -Full builds the self-contained variant instead, which is 279 MB
# before compression and needs no first-run download on the recipient's side.
#
# Inno Setup is a build-time-only tool, in the same sense PyInstaller is: it is
# not in requirements.txt and the application's runtime dependencies are
# unchanged by it.  Install it with:
#   winget install JRSoftware.InnoSetup

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$appName = "ClipDock"

# --- Locate the Inno Setup compiler -----------------------------------------
# winget installs Inno Setup per-user, so the Program Files path is not the
# only one worth trying and asserting on it would make this script fail on a
# machine where the compiler is present and working.  Both are checked, and a
# clear instruction is printed if neither has it.
$isccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    throw "The Inno Setup compiler (ISCC.exe) was not found. Install it with: winget install JRSoftware.InnoSetup"
}

# --- Step 1: the executable --------------------------------------------------
if ($SkipExe) {
    Write-Host "Skipping the executable build; packaging the existing dist\$appName folder as it stands."
} else {
    # A HASHTABLE splat, not an array splat, and that is not a style preference.
    # @array passes its elements POSITIONALLY to a script's param() block, so
    # @("-Python", "python", "-Slim") binds "-Python" to $Python, "python" to
    # $OneFile, and silently drops -Slim altogether - the build then produces
    # the full 279 MB variant while this script reports a slim one, and nothing
    # anywhere fails.  @hashtable passes by name, which is the only form that
    # binds a switch correctly.
    $exeArgs = @{ Python = $Python }
    if (-not $Full) { $exeArgs["Slim"] = $true }
    & (Join-Path $PSScriptRoot "build_exe.ps1") @exeArgs
}

$appDir = Join-Path $root "dist\$appName"
$appExe = Join-Path $appDir "$appName.exe"
if (-not (Test-Path $appExe)) {
    # The single most likely cause is -SkipExe against a folder that was never
    # built or was deleted.  Saying so beats an ISCC error about no source files.
    throw "Nothing to package: $appExe does not exist. Run without -SkipExe, or build the executable first."
}

# Check the FOLDER, not the flag.  This is here because the first version of
# this script passed -Slim by array-splatting, which PowerShell bound
# positionally and dropped without a word: the build produced the full 279 MB
# variant, the installer came out at 103 MB, and the message at the end still
# called it a slim build.  Nothing failed, which is the whole problem.  A claim
# about which variant this is can only be trusted if it is read off the artefact
# that is about to be packaged.
$vendorDir = Join-Path $appDir "vendor"
$hasVendor = Test-Path $vendorDir
if ($Full -and -not $hasVendor) {
    throw "-Full was requested but dist\$appName\vendor is absent, so this would package a slim build. Rerun without -SkipExe."
}
if (-not $Full -and $hasVendor) {
    throw "This should be a slim build, but dist\$appName\vendor exists, so FFmpeg is about to be packaged too. Rerun without -SkipExe. Use -Full if a self-contained installer is what you actually want."
}

# --- Step 2: the installer ---------------------------------------------------
& $iscc (Join-Path $PSScriptRoot "clipdock.iss")
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup could not build the installer (exit code $LASTEXITCODE)."
}

$setupPath = Join-Path $root "dist\$appName-Setup.exe"
if (-not (Test-Path $setupPath)) {
    throw "The installer was not produced at $setupPath."
}

# --- Report ------------------------------------------------------------------
# The SHA-256 is printed rather than written to a file on purpose.  It is only
# useful if it is sent separately from the installer, which is the whole point
# of it: it lets a recipient confirm they received the file that was actually
# sent, which is the one guarantee an unsigned installer cannot give on its own.
$payloadMb = [math]::Round((Get-ChildItem $appDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
$setupMb = [math]::Round((Get-Item $setupPath).Length / 1MB, 1)
$hash = (Get-FileHash $setupPath -Algorithm SHA256).Hash

Write-Host ""
Write-Host "Installer  : $setupPath"
Write-Host "Size       : $setupMb MB (from a $payloadMb MB application folder)"
Write-Host "SHA-256    : $hash"
Write-Host ""
Write-Host "The installer is unsigned, so Windows may show a blue warning before the"
Write-Host "wizard opens. installer\info-before.txt explains this on the first page"
Write-Host "of the wizard itself, and its text is worth reading once here so you can"
Write-Host "describe it to the person you are sending it to."
if ($Full) {
    Write-Host ""
    Write-Host "This is the FULL build: FFmpeg is bundled, so the recipient is never"
    Write-Host "asked to download anything, at the cost of a much larger download."
} else {
    Write-Host ""
    Write-Host "This is the SLIM build: FFmpeg is not bundled. On first run the program"
    Write-Host "checks the computer for an existing copy and only asks to download one"
    Write-Host "if there is none."
}
