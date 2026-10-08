; Inno Setup script for ClipDock.
;
; Turns the already-built application folder into a single wizard installer:
;   dist\ClipDock\  ->  dist\ClipDock-Setup.exe
;
; The wizard is the plain Next / Next / choose-folder sequence, with a
; destination field the user can change.  Everything else is arranged so that
; installing ClipDock never makes Windows ask for administrator rights, which
; is the same principle the FFmpeg installer in core\dependencies.py follows.
;
; Build order matters: run scripts\build_exe.ps1 -Slim FIRST.  This script
; packages whatever is in dist\ClipDock and does not build anything itself, so
; running it against a stale folder would quietly ship the stale build.
;
; Build with:
;   & "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" scripts\clipdock.iss

; --- Product identity -------------------------------------------------------
; These four values are the only ones that name the product.  They are stated
; once here and used by the wizard, the Apps and Features entry, the version
; resource on the installer, and the output filename, so they cannot drift
; apart.  AppPublisher shows in Apps and Features and on the installer's file
; properties; change it if you want a real name there.
#define ProductName "ClipDock"
#define ProductVersion "0.3.0"
#define ProductPublisher "ClipDock"
#define ProductExeName "ClipDock.exe"

; A fixed identifier, not a generated one.  Inno keys "is this already
; installed, and if so where" off this value, so it must not change between
; releases or a new version installs beside the old one instead of upgrading
; it.  The doubled brace is Inno's own escaping, not a typo.
#define ProductAppId "{{00AB9811-ADF8-4A3C-AC93-3CDACB060EF1}"

[Setup]
AppId={#ProductAppId}
AppName={#ProductName}
AppVersion={#ProductVersion}
AppVerName={#ProductName} {#ProductVersion}
AppPublisher={#ProductPublisher}
VersionInfoVersion={#ProductVersion}
VersionInfoProductVersion={#ProductVersion}
VersionInfoCompany={#ProductPublisher}
VersionInfoDescription={#ProductName} Setup
VersionInfoProductName={#ProductName} Setup

; Per-user install into the conventional per-user Programs folder.  With
; PrivilegesRequired=lowest below, this is what a right-click Run as
; administrator still gets: the same folder for the same user, never a
; machine-wide copy, and never a UAC prompt.  To offer a machine-wide
; alternative instead, set DefaultDirName={autopf}\{#ProductName} and set
; PrivilegesRequired=admin.
DefaultDirName={localappdata}\Programs\{#ProductName}
DefaultGroupName={#ProductName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest

; The frozen build is 64-bit only: _internal\ holds x64 Python and Qt binaries.
; Refusing here produces a clear message, whereas installing anyway produces a
; program that dies at startup with no explanation.  x64compatible covers both
; real x64 and ARM64 running under x64 emulation.
ArchitecturesAllowed=x64compatible

OutputDir=..\dist
OutputBaseFilename={#ProductName}-Setup

; Compression.  The default LZMA2 setting already takes the 134 MB application
; folder to about 47 MB, which is small enough that the extra minutes solid
; compression costs per build were not worth paying for a file that gets
; produced a handful of times.  If the installer ever needs to be as small as
; possible - a size-capped download, say - changing this to lzma2/max with
; SolidCompression=yes is a one-line change that buys a smaller file in
; exchange for a slower build.
Compression=lzma2
SolidCompression=no

SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#ProductExeName}
WizardStyle=modern

; Read by the first wizard page, before the user has committed to anything.  It
; explains what the program does on first run and why Windows may warn about an
; unsigned installer, which is the single most useful thing to tell someone
; before they install rather than after.
InfoBeforeFile=installer\info-before.txt

; The application holds its files open while running, so a reinstall or an
; upgrade over a running copy would otherwise fail with a sharing violation.
CloseApplications=yes
RestartApplications=no

; A log beside the installed files.  If a friend reports that the wizard
; failed, this is the first thing worth asking them for.
SetupLogging=yes

; No SignTool line on purpose: no code-signing certificate is available for a
; build shared privately with a few people, and the cost of one is
; disproportionate to that.  The result is an unsigned installer, which Windows
; may warn about before the wizard opens.  That is acknowledged up front in
; installer\info-before.txt rather than left for the recipient to discover as a
; surprise blue screen.  If a certificate is ever added, SignTool is where it
; goes, and info-before.txt is the text that then needs deleting.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; The desktop shortcut is the only optional thing, and it rides on the existing
; "additional icons" task rather than adding a page of its own.  GroupDescription
; is the label above the checkbox, so it has to be set for the box to read as
; more than a bare "Create a desktop shortcut".
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

; Start with Windows.  A [Tasks] entry is checked unless `unchecked` says
; otherwise, so the absent flag below is the whole of "checked by default" -
; and there is deliberately no `checkedonce` either, because that flag *un*ticks
; the box whenever a previous version is found, which would silently switch the
; option off for everyone the first time they upgrade.  Inno's UsePreviousTasks
; (its default) is what remembers a choice the user actually made.
;
; The description carries the consent question, because a ticked box here is
; consent to launch a GUI application on every sign-in and "Start with Windows"
; does not tell anyone that a frameless ClipDock window will then appear on its
; own.  It sits on the same Select Additional Tasks page as the desktop icon, so
; a person who does not want it unticks it here rather than hunting through
; Windows' startup settings afterwards.
;
; What the box writes is in [Registry], including the removal on uninstall that
; the checked default makes the common case rather than a corner case.
Name: "autostart"; Description: "Start ClipDock when Windows starts - a ClipDock window then opens by itself, with no prompt"; GroupDescription: "Start with Windows:"

[Files]
; The whole frozen application: the executable, _internal\ (Python, PySide6 and
; yt-dlp), assets\, and the licence notices.  ignoreversion keeps Qt's and
; Python's own file versions from being compared against what is already
; installed, which would otherwise prompt on every upgrade.
Source: "..\dist\{#ProductName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#ProductName}"; Filename: "{app}\{#ProductExeName}"
Name: "{group}\Uninstall {#ProductName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\{#ProductExeName}"; Tasks: desktopicon

[Registry]
; The autostart entry the `autostart` task above writes, and the half that is
; easy to forget: taking it away again.
;
; HKCU, not HKLM.  PrivilegesRequired=lowest means Setup never elevates, so a
; machine-wide Run key is not reachable - and it would not be right either:
; this starts ClipDock for the person who installed it, not for everyone on the
; machine.  No administrator rights are needed for any of it, which is the same
; principle the FFmpeg install in core\dependencies.py follows.
;
; The data is quoted because {localappdata}\Programs\{#ProductName}\{#ProductExeName}
; contains a space, and Windows reads an unquoted Run value as the first token
; plus everything after it - the classic unquoted-image-path hijack, where a
; directory planted before "ClipDock.exe" wins.  So this is a security property
; and not a formatting nicety.
;
; uninsdeletevalue is the part that gets forgotten.  The task is checked by
; default, so on most machines this value will exist, and an uninstall without
; this flag leaves a sign-in entry pointing at an executable that has been
; deleted - once every sign-in, forever, with nothing left installed that could
; remove it.
;
; The second entry is the other direction of the same problem: Inno only
; *creates* what is selected, it never retracts what an earlier install
; created, so upgrading with the box unticked would otherwise leave the value
; in place and the user's untick would not have taken effect.  `not` is Inno's
; own boolean operator on a Tasks parameter, so this says exactly "when the
; person did not ask for this, make sure it is not there".
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#ProductName}"; ValueData: """{app}\{#ProductExeName}"""; Flags: uninsdeletevalue; Tasks: autostart
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#ProductName}"; Flags: deletevalue; Tasks: not autostart

[Run]
; postinstall draws a checked "Run {#ProductName}" box on the final page.
; skipifsilent keeps a scripted install from popping a window in someone's face.
Filename: "{app}\{#ProductExeName}"; Description: "Start {#ProductName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Deliberately empty.
;
; The FFmpeg that ClipDock may have installed lives in %ProgramData%, outside
; {app}, so it is untouched by design and not by luck: other programs on the
; machine may depend on it and a downloader has no business deleting it.
;
; The user's settings in %LOCALAPPDATA% are also left alone.  Deleting someone's
; preferences because they removed the program would be the wrong default, and
; it would delete download history paths they may still want.
;
; Anything that genuinely does need cleaning up has to be listed here
; explicitly, and this section is where that decision gets made.
