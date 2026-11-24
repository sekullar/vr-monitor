# Surum arttir, derle, etiketle, GitHub release yayinla.  Kullanim: scripts\release.ps1 -Bump patch|minor|major [-Notes "metin"] [-SkipApk]
param([ValidateSet('patch','minor','major','none')][string]$Bump='patch', [string]$Notes='', [switch]$SkipApk)
$ErrorActionPreference='Stop'
$root = Split-Path $PSScriptRoot -Parent; Set-Location $root
$v = (Get-Content VERSION).Trim().Split('.') | ForEach-Object {[int]$_}
switch ($Bump) { 'major' {$v[0]++;$v[1]=0;$v[2]=0} 'minor' {$v[1]++;$v[2]=0} 'patch' {$v[2]++} }
$ver = $v -join '.'
Set-Content VERSION $ver -NoNewline
# Android surumu
$g = Get-Content android\app\build.gradle -Raw
$code = $v[0]*10000 + $v[1]*100 + $v[2]
$g = $g -replace "versionCode \d+","versionCode $code" -replace "versionName '[^']*'","versionName '$ver'"
Set-Content android\app\build.gradle $g -NoNewline
# Exe
if (Test-Path dist\VRMonitor) { Remove-Item dist\VRMonitor -Recurse -Force }
$pc = "$root\pc"
python -m PyInstaller --noconfirm --clean --onedir --windowed --name VRMonitor --distpath "$root\dist" --workpath "$root\build" --specpath "$root\build" --icon "$root\assets\icon.ico" --add-data "$pc\ui;ui" --add-data "$pc\models;models" --add-data "$root\VERSION;." --collect-all mediapipe --collect-all av --collect-all dxcam --collect-all comtypes --collect-all windows_capture --hidden-import hid --hidden-import version --hidden-import stream --hidden-import webui --hidden-import core --hidden-import hands --hidden-import pad --hidden-import h264 --hidden-import displays --hidden-import vddctl --paths $pc --exclude-module matplotlib --exclude-module scipy --exclude-module pandas --exclude-module IPython --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide6 --exclude-module torch --exclude-module tensorflow --exclude-module sklearn --exclude-module numba --exclude-module pytest "$pc\app.py"
# Kurulum paketi (NSIS: tools\nsis\nsis-bundle ya da PATH'te makensis)
$env:NSISDIR = "$root\tools\nsis\nsis-bundle\windows"
$mk = if (Test-Path "$env:NSISDIR\makensis.exe") { "$env:NSISDIR\makensis.exe" } else { 'makensis' }
New-Item -ItemType Directory -Force release | Out-Null
$setup = "$root\release\VR.Monitor.Setup.$ver.exe"
& $mk "/DVERSION=$ver" "/DSRC=$root\dist\VRMonitor" "/DOUT=$setup" "/DICON=$root\assets\icon.ico" installer\vrmonitor.nsi
$assets = @($setup)
if (-not $SkipApk) {
  $env:JAVA_HOME='C:\Android\jdk17'; $env:ANDROID_HOME='C:\Android\sdk'
  Push-Location android; & C:\Android\gradle-8.9\bin\gradle.bat assembleDebug --no-daemon; Pop-Location
  Copy-Item android\app\build\outputs\apk\debug\app-debug.apk release\VRMonitor.apk -Force
  $assets += "$root\release\VRMonitor.apk"
}
git add -A; git commit -m "VR Monitor $ver"; git tag "v$ver"; git push; git push --tags
gh release create "v$ver" @assets --title "VR Monitor $ver" --notes ($(if ($Notes) {$Notes} else {"VR Monitor $ver"}))
"Yayinlandi: v$ver"
