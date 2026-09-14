; Inno Setup script for Grabline (Windows installer).
; Compiled in CI by ISCC.exe against the PyInstaller onedir bundle in
; dist\grabline\. Produces Grabline-Setup-<version>.exe.
;
; #define AppVersion is passed on the ISCC command line (/DAppVersion=1.3.5).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "GrabLine"
#define AppExe "grabline.exe"

[Setup]
AppId={{A1F0C0DE-4B3A-4E5D-9C2B-0F1A2B3C4D5E}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=GrabLine
AppPublisherURL=https://github.com/Gr33nOps/GrabLine
AppSupportURL=https://github.com/Gr33nOps/GrabLine/issues
AppUpdatesURL=https://github.com/Gr33nOps/GrabLine/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputBaseFilename=Grabline-Setup-{#AppVersion}
; Paths are relative to this .iss file (packaging\windows\), so reach up to the
; repo root where PyInstaller wrote dist\grabline\ and where CI expects the exe.
OutputDir=..\..\dist
SetupIconFile=..\grabline.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
WizardImageFile=..\grabline-wizard.bmp
WizardSmallImageFile=..\grabline-wizard-small.bmp
WizardImageStretch=no
DisableWelcomePage=no
DisableProgramGroupPage=yes
; An upgrade over a running copy: offer to close it rather than failing on
; locked files. Grabline is not restarted afterwards - the finish page offers
; that, so the user decides.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startupicon"; Description: "Start GrabLine when Windows starts"; GroupDescription: "Startup:"
Name: "magnetassoc"; Description: "Open magnet links with GrabLine"; GroupDescription: "File types:"
Name: "torrentassoc"; Description: "Open .torrent files with GrabLine"; GroupDescription: "File types:"

[Files]
Source: "..\..\dist\grabline\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Start with Windows. Written under HKCU even for an admin install - it is this
; user's login item, and it matches the value Settings reads and writes.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#AppName}"; \
    ValueData: """{app}\{#AppExe}"" --minimized"; \
    Flags: uninsdeletevalue; Tasks: startupicon

; magnet: links. HKA resolves to HKLM for an admin install, HKCU otherwise.
Root: HKA; Subkey: "Software\Classes\magnet"; ValueType: string; ValueName: ""; \
    ValueData: "URL:Magnet Link"; Flags: uninsdeletekey; Tasks: magnetassoc
Root: HKA; Subkey: "Software\Classes\magnet"; ValueType: string; ValueName: "URL Protocol"; \
    ValueData: ""; Tasks: magnetassoc
Root: HKA; Subkey: "Software\Classes\magnet\DefaultIcon"; ValueType: string; ValueName: ""; \
    ValueData: "{app}\{#AppExe},0"; Tasks: magnetassoc
Root: HKA; Subkey: "Software\Classes\magnet\shell\open\command"; ValueType: string; ValueName: ""; \
    ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: magnetassoc

; .torrent files.
Root: HKA; Subkey: "Software\Classes\.torrent"; ValueType: string; ValueName: ""; \
    ValueData: "Grabline.Torrent"; Flags: uninsdeletevalue; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\Grabline.Torrent"; ValueType: string; ValueName: ""; \
    ValueData: "BitTorrent file"; Flags: uninsdeletekey; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\Grabline.Torrent\DefaultIcon"; ValueType: string; \
    ValueName: ""; ValueData: "{app}\{#AppExe},0"; Tasks: torrentassoc
Root: HKA; Subkey: "Software\Classes\Grabline.Torrent\shell\open\command"; ValueType: string; \
    ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: torrentassoc

; Native Messaging hosts are registered by the app at runtime, not here, so
; these entries only clean up on uninstall (dontcreatekey). Without them a
; removed Grabline would leave dead host manifests behind in every browser.
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\dev.grabline.host"; \
    Flags: dontcreatekey uninsdeletekey
Root: HKCU; Subkey: "Software\Chromium\NativeMessagingHosts\dev.grabline.host"; \
    Flags: dontcreatekey uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\dev.grabline.host"; \
    Flags: dontcreatekey uninsdeletekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\dev.grabline.host"; \
    Flags: dontcreatekey uninsdeletekey
Root: HKCU; Subkey: "Software\Vivaldi\NativeMessagingHosts\dev.grabline.host"; \
    Flags: dontcreatekey uninsdeletekey
Root: HKCU; Subkey: "Software\Mozilla\NativeMessagingHosts\dev.grabline.host"; \
    Flags: dontcreatekey uninsdeletekey

[Run]
; Refresh Windows' shell icon cache so an update shows the new icon instead of
; the one it cached from a previous version.
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden skipifdoesntexist
Filename: "{app}\{#AppExe}"; Description: "Launch GrabLine"; Flags: nowait postinstall skipifsilent
