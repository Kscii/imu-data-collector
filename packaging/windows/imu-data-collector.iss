#define AppName "CW12EU-T IMU 数采平台"
#ifndef AppVersion
  #define AppVersion "0.2.0-dev"
#endif
#define AppPublisher "Kscii"
#define AppExeName "imu-data-collector.exe"
#define CliExeName "imu-collector.exe"

[Setup]
AppId={{5A10D30A-7EC9-4C7B-86DE-FFEEA807957A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\IMUDataCollector
DefaultGroupName={#AppName}
OutputDir=..\..\dist-installer
OutputBaseFilename=imu-data-collector-windows-x64-{#AppVersion}-unsigned
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
UninstallDisplayIcon={app}\{#AppExeName}
ChangesEnvironment=yes

[Tasks]
Name: "addtopath"; Description: "把 imu-collector 加入当前用户 PATH"; Flags: checkedonce

[Files]
Source: "..\..\dist\imu-collector\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\启动 IMU 数采平台"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\IMU 数采平台诊断"; Filename: "{app}\{#CliExeName}"; Parameters: "doctor"; WorkingDir: "{app}"
Name: "{autodesktop}\IMU 数采平台"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath

[Code]
function NeedsAddPath(): Boolean;
var
  CurrentPath: String;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', CurrentPath) then
    CurrentPath := '';
  Result := Pos(';' + ExpandConstant('{app}') + ';', ';' + CurrentPath + ';') = 0;
end;
