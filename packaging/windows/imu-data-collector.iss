#define AppName "CW12EU-T IMU Data Collector"
#ifndef AppVersion
  #define AppVersion "0.3.0-dev"
#endif
#define AppPublisher "Kscii"
#define AppExeName "imu-data-collector.exe"
#define CliExeName "imu-collector.exe"

[Setup]
AppId={{5A10D30A-7EC9-4C7B-86DE-FFEEA807957A}
AppName={code:UiText|AppName}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\IMUDataCollector
DefaultGroupName={code:UiText|AppName}
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
Name: "addtopath"; Description: "{code:UiText|AddToPath}"; Flags: checkedonce

[Files]
Source: "..\..\dist\imu-collector\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{code:UiText|Launch}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\{code:UiText|Diagnostics}"; Filename: "{app}\{#CliExeName}"; Parameters: "doctor"; WorkingDir: "{app}"
Name: "{autodesktop}\{code:UiText|Desktop}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath

[Code]
function UiText(Key: String): String;
var
  Chinese: Boolean;
begin
  // Custom labels follow Windows UI language; the built-in wizard stays unchanged.
  // https://jrsoftware.org/ishelp/topic_isxfunc_getuilanguage.htm
  Chinese := (GetUILanguage and $3FF) = $04;
  Result := Key;
  if Key = 'AppName' then begin
    if Chinese then Result := 'CW12EU-T IMU 数采平台'
    else Result := '{#AppName}';
  end else if Key = 'AddToPath' then begin
    if Chinese then Result := '把 imu-collector 加入当前用户 PATH'
    else Result := 'Add imu-collector to the current user PATH';
  end else if Key = 'Launch' then begin
    if Chinese then Result := '启动 IMU 数采平台'
    else Result := 'Start IMU Data Collector';
  end else if Key = 'Diagnostics' then begin
    if Chinese then Result := 'IMU 数采平台诊断'
    else Result := 'IMU Data Collector Diagnostics';
  end else if Key = 'Desktop' then begin
    if Chinese then Result := 'IMU 数采平台'
    else Result := 'IMU Data Collector';
  end;
end;

function NeedsAddPath(): Boolean;
var
  CurrentPath: String;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', CurrentPath) then
    CurrentPath := '';
  Result := Pos(';' + ExpandConstant('{app}') + ';', ';' + CurrentPath + ';') = 0;
end;
