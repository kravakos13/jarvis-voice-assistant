; Джарвис — установщик (Inno Setup 6). Сборка: scripts/build_installer.sh (или ISCC installer\jarvis.iss)
; Стадия сборки: build\stage\ = исходники + python\ (embeddable Python 3.11.9 с включённым site)
#define AppName "Джарвис"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define Stage "..\build\stage"

[Setup]
; AppId must never change: upgrades install in place over the old version
AppId={{7C4D2A9E-5B1F-4E8A-9F3C-1A2B3C4D5E6F}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=kravakos13
AppPublisherURL=https://github.com/kravakos13/jarvis-voice-assistant
AppUpdatesURL=https://github.com/kravakos13/jarvis-voice-assistant/releases
DefaultDirName={localappdata}\Jarvis
DisableDirPage=auto
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build\out
OutputBaseFilename=JarvisSetup
SetupIconFile=..\assets\jarvis.ico
UninstallDisplayIcon={app}\assets\jarvis.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
UsePreviousTasks=yes
MinVersion=10.0

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
ru.Autostart=Запускать Джарвиса вместе с Windows
ru.Desktop=Ярлык на рабочем столе
ru.Deps=Скачать компоненты (Python-пакеты, голос, распознавание, Ollama и модели ИИ) — нужен интернет, несколько ГБ
ru.DepsStatus=Скачиваю компоненты Джарвиса (окно консоли покажет прогресс, это может занять 10–40 минут)...
ru.Launch=Запустить Джарвиса

[Tasks]
Name: "desktopicon"; Description: "{cm:Desktop}"
Name: "autostart"; Description: "{cm:Autostart}"
Name: "deps"; Description: "{cm:Deps}"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Excludes: "\settings.local.yaml,\aliases.yaml,\devices.yaml,\logs\*,\models\*,\cache\*,\faces\*,*.pyc,__pycache__"; Flags: ignoreversion recursesubdirs createallsubdirs
; user-editable files: never overwrite on upgrade
Source: "{#Stage}\aliases.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#Stage}\devices.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

[Dirs]
Name: "{app}\logs"
Name: "{app}\models"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m jarvis.watchdog"; WorkingDir: "{app}"; IconFilename: "{app}\assets\jarvis.ico"
Name: "{group}\{#AppName} (текстовый режим)"; Filename: "{app}\python\python.exe"; Parameters: "-m jarvis --text"; WorkingDir: "{app}"; IconFilename: "{app}\assets\jarvis.ico"
Name: "{group}\Самопроверка"; Filename: "{app}\python\python.exe"; Parameters: "-m jarvis --check"; WorkingDir: "{app}"; IconFilename: "{app}\assets\jarvis.ico"
Name: "{group}\Докачать компоненты"; Filename: "{app}\python\python.exe"; Parameters: "installer\postinstall.py"; WorkingDir: "{app}"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m jarvis.watchdog"; WorkingDir: "{app}"; IconFilename: "{app}\assets\jarvis.ico"; Tasks: desktopicon

[Registry]
; same value name/format as jarvis.platform_utils.set_autostart (so the Settings checkbox stays in sync)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Jarvis"; ValueData: """{sys}\wscript.exe"" ""{app}\Jarvis.vbs"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\python\python.exe"; Parameters: "installer\postinstall.py"; WorkingDir: "{app}"; StatusMsg: "{cm:DepsStatus}"; Tasks: deps; Check: not SkipDeps
Filename: "{app}\python\pythonw.exe"; Parameters: "-m jarvis.watchdog"; WorkingDir: "{app}"; Description: "{cm:Launch}"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /FI ""IMAGENAME eq pythonw.exe"" /FI ""WINDOWTITLE eq Джарвис*"""; Flags: runhidden; RunOnceId: "KillJarvis"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\jarvis"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\voices"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\wheels"
Type: files; Name: "{app}\settings.local.yaml.broken"

[Code]
function SkipDeps: Boolean;
begin
  { /SKIPDEPS=1 — used by tests and by updates when components are already installed }
  Result := ExpandConstant('{param:SKIPDEPS|0}') = '1';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{userdocs}\Jarvis');
    if DirExists(Data) and not UninstallSilent then
      if MsgBox('Удалить также личные данные Джарвиса (заметки, задачи, память, бэкапы) из папки' + #13#10 +
                Data + ' ?' + #13#10#13#10 + 'Нажми «Нет», чтобы сохранить их (рекомендуется).',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
    if (not UninstallSilent) and (MsgBox('Удалить также ваши настройки (settings.local.yaml, aliases.yaml, devices.yaml)' + #13#10 +
              'и своё изображение лица (папка faces)?',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
    begin
      DeleteFile(ExpandConstant('{app}\settings.local.yaml'));
      DeleteFile(ExpandConstant('{app}\aliases.yaml'));
      DeleteFile(ExpandConstant('{app}\devices.yaml'));
      DelTree(ExpandConstant('{app}\faces'), True, True, True);
    end;
    RemoveDir(ExpandConstant('{app}'));
  end;
end;
