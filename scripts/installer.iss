; ============================================================
; 像素译站 安装版 —— Inno Setup 6 脚本（中文注释）
; 用法：ISCC.exe scripts\installer.iss
; 源目录：dist\安装版\像素译站\（PyInstaller onedir，由 M6-2 / build_all.ps1 产出）
; 产物：dist\安装版\release-v1.4.3-setup.exe
; 注意：本文件以 UTF-8（带 BOM）保存，请勿改成无 BOM 以免中文乱码
; ============================================================

#define MyAppName "像素译站"
#define MyAppVersion "1.4.3"
#define MyAppPublisher "像素译站"
#define MyAppExeName "像素译站.exe"

[Setup]
; AppId 固定 GUID（{{ 是字面 { 的转义），保证升级/卸载识别为同一应用
AppId={{8A2F4C11-9D1E-4E6B-9B44-9D2F5A6B7C8D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
; x64 系统装到 64 位 Program Files（PyInstaller 产物为 64 位），32 位系统照常装 32 位目录
ArchitecturesInstallIn64BitMode=x64
; 修复（recheck）：默认目录改 **per-user 可写**的 {localappdata}\Programs（Chrome/VS Code 同款）——
; 之前默认装 {autopf}\Program Files，普通用户对 exe 旁目录只读，运行日志/config.json/cfpa 词库
; 全写不进去，desktop.py 日志初始化直接 PermissionError 启动即崩。per-user 免提权、可写。
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=..\dist\安装版
OutputBaseFilename=release-v1.4.3-setup
; 安装包图标取自 exe 自身（PyInstaller 内嵌图标）；日后有专用 .ico 可替换该行
SetupIconFile=..\assets\app-icon.ico
; 卸载器也显示应用图标
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

; -- 关于简体中文向导的说明 --
; Inno Setup 6 官方默认不带中文语言包（ChineseSimplified.isl），
; 需自行下载该文件放到 "C:\Program Files (x86)\Inno Setup 6\Languages\" 后，
; 取消下面 [Languages] 段注释，安装向导即可全中文：
; [Languages]
; Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
; 桌面快捷方式（默认不勾选，避免安装时无意污染桌面）
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 递归打包 onedir 全目录（主 exe + _internal 依赖库），保留子目录结构
Source: "..\dist\安装版\像素译站\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 开始菜单组：主程序 + 卸载入口
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
; 桌面快捷方式（仅当勾选 desktopicon 任务时创建）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装完成后可选立即启动（静默安装时自动跳过）
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
