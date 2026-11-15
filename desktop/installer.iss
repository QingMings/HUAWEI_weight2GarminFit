; 佳明体重助手 · Inno Setup 安装包脚本
; 编译：ISCC.exe desktop\installer.iss
; 输出：dist\佳明体重助手_v1.5.0_安装包.exe（免管理员，装到当前用户的 Programs 目录）

#define MyAppName "佳明体重助手"
#define MyAppVersion "1.5.0"
#define MyAppExeName "GarminWeightHelper.exe"
#define MyAppId "{{8E4B1C2A-6F3D-4A57-9A1E-2C7D5B9F3A64}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppName}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=佳明体重助手_v{#MyAppVersion}_安装包
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 安装到当前用户目录，不需要管理员权限；也可以右键「以管理员身份运行」装到 Program Files
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "..\dist\GarminWeightHelper\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\GarminWeightHelper\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
