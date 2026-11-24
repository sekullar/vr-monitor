; VR Monitor kurulum betigi. Derleme: makensis /DVERSION=0.1.0 /DSRC=<dist\VRMonitor> /DOUT=<cikti.exe> vrmonitor.nsi
Unicode true
!include "MUI2.nsh"
!ifndef VERSION
  !define VERSION "0.0.0"
!endif
Name "VR Monitor"
OutFile "${OUT}"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\VR Monitor"
SetCompressor /SOLID lzma
SetCompressorDictSize 64
!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\VRMonitor.exe"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Turkish"
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetOutPath "$INSTDIR"
  File /r "${SRC}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall VR Monitor.exe"
  CreateDirectory "$SMPROGRAMS\VR Monitor"
  CreateShortcut "$SMPROGRAMS\VR Monitor\VR Monitor.lnk" "$INSTDIR\VRMonitor.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VRMonitor" "DisplayName" "VR Monitor"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VRMonitor" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VRMonitor" "DisplayIcon" "$INSTDIR\VRMonitor.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VRMonitor" "UninstallString" '"$INSTDIR\Uninstall VR Monitor.exe"'
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\VR Monitor\VR Monitor.lnk"
  RMDir "$SMPROGRAMS\VR Monitor"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VRMonitor"
  RMDir /r "$INSTDIR"
SectionEnd
