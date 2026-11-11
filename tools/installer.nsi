Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
Name "Jenkins Scheduler"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\JenkinsScheduler"
InstallDirRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\JenkinsScheduler" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
VIProductVersion "2.2.0.0"
VIAddVersionKey "ProductName" "Jenkins Scheduler"
VIAddVersionKey "FileDescription" "Jenkins Scheduler Installer"
VIAddVersionKey "FileVersion" "2.2.0"
VIAddVersionKey "LegalCopyright" "Copyright (c) 2026 Riang-00 and contributors"
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE ValidateInstallDirectory
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
Var DataDir
Var Arguments
Var Result
Var ExistingInstall

Function .onInit
  SetShellVarContext current
  SetRegView 32
  ReadRegStr $ExistingInstall HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\JenkinsScheduler" "InstallLocation"
  ${IfNot} ${FileExists} "$ExistingInstall\release-manifest.json"
    StrCpy $ExistingInstall ""
  ${EndIf}
  StrCpy $DataDir "$LOCALAPPDATA\JenkinsScheduler\data"
  ${GetParameters} $Arguments
  ; Isolated installations do not participate in registered-app upgrade checks.
  ${GetOptions} $Arguments "/NO_SHORTCUTS" $0
  ${IfNot} ${Errors}
    StrCpy $ExistingInstall ""
  ${EndIf}
  ${GetOptions} $Arguments "/DATA_DIR=" $Result
  ${IfNot} ${Errors}
    StrCpy $DataDir $Result
  ${EndIf}
FunctionEnd

Function ValidateInstallDirectory
  ; Keep NSIS's directory value intact; a fresh target may not exist yet.
  ${If} $ExistingInstall != ""
    ${If} $INSTDIR != $ExistingInstall
      MessageBox MB_ICONSTOP "已安装到：$ExistingInstall$\r$\n升级请保留原安装目录。若要更换位置，请先卸载旧版，再重新安装；卸载会保留配置和历史数据。" /SD IDOK
      SetErrorLevel 1
      Abort
    ${EndIf}
  ${EndIf}
  SetErrorLevel 0
FunctionEnd

Section "Install"
  ; Silent installs must apply the same upgrade rule as the directory page.
  Call ValidateInstallDirectory
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\payload"
  File /r "${PAYLOAD}\*"
  SetOutPath "$PLUGINSDIR"
  File "${WEBVIEW}"
  ; Evergreen WebView2 is shared infrastructure. Run its signed bootstrapper
  ; only if Windows has no current-user or machine-wide runtime registration.
  ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${If} $0 == ""
    SetRegView 32
    ReadRegStr $0 HKLM "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${EndIf}
  ${If} $0 == ""
    ExecWait '"$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe" /silent /install' $0
    ${If} $0 != 0
      MessageBox MB_ICONSTOP "WebView2 安装未完成。请联网安装 Microsoft Edge WebView2 Runtime 后重试。" /SD IDOK
      SetErrorLevel 1
      Quit
    ${EndIf}
  ${EndIf}
  nsExec::ExecToStack '"$PLUGINSDIR\payload\runtime\python.exe" -E -s "$PLUGINSDIR\payload\installer.py" install --target "$INSTDIR" --data "$DataDir" --result "$PLUGINSDIR\result.txt"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    FileOpen $1 "$PLUGINSDIR\result.txt" r
    FileReadUTF16LE $1 $Result
    FileClose $1
    MessageBox MB_ICONSTOP "安装未完成：$Result" /SD IDOK
    SetErrorLevel 1
    Quit
  ${EndIf}
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  ; /NO_SHORTCUTS is useful for isolated test installations.
  ${GetOptions} $Arguments "/NO_SHORTCUTS" $0
  ${If} ${Errors}
    CreateDirectory "$SMPROGRAMS\Jenkins Scheduler"
    CreateShortcut "$SMPROGRAMS\Jenkins Scheduler\Jenkins Scheduler.lnk" "$INSTDIR\bin\JenkinsScheduler.exe"
    CreateShortcut "$DESKTOP\Jenkins Scheduler.lnk" "$INSTDIR\bin\JenkinsScheduler.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\JenkinsScheduler" "DisplayName" "Jenkins Scheduler"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\JenkinsScheduler" "DisplayVersion" "2.2.0"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\JenkinsScheduler" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\JenkinsScheduler" "InstallLocation" "$INSTDIR"
  ${EndIf}
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\payload"
  CopyFiles /SILENT "$INSTDIR\*.py" "$PLUGINSDIR\payload"
  CreateDirectory "$PLUGINSDIR\payload\runtime"
  CopyFiles /SILENT "$INSTDIR\runtime\*" "$PLUGINSDIR\payload\runtime"
  nsExec::ExecToStack '"$PLUGINSDIR\payload\runtime\python.exe" -E -s "$PLUGINSDIR\payload\installer.py" uninstall --target "$INSTDIR" --result "$PLUGINSDIR\result.txt"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    FileOpen $1 "$PLUGINSDIR\result.txt" r
    FileReadUTF16LE $1 $Result
    FileClose $1
    MessageBox MB_ICONSTOP "卸载未完成：$Result" /SD IDOK
    SetErrorLevel 1
    Quit
  ${EndIf}
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\JenkinsScheduler" "InstallLocation"
  ${If} $0 == $INSTDIR
    Delete "$DESKTOP\Jenkins Scheduler.lnk"
    Delete "$SMPROGRAMS\Jenkins Scheduler\Jenkins Scheduler.lnk"
    RMDir "$SMPROGRAMS\Jenkins Scheduler"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\JenkinsScheduler"
  ${EndIf}
  Delete "$INSTDIR\desktop.lock"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR\bin"
  RMDir "$INSTDIR\runtime"
  RMDir "$INSTDIR\docs"
  RMDir "$INSTDIR\desktop"
  RMDir "$INSTDIR"
  ; User settings, DPAPI credentials and history are deliberately retained.
SectionEnd
