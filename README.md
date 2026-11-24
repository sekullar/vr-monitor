# VR Monitor

Turn a phone in a VR headset (Cardboard-style) into virtual monitors for your Windows PC.
The PC captures your screens (real and virtual) and streams them over USB (adb reverse) or Wi-Fi;
the Android app renders them as floating, head-tracked panels in stereo. Hand tracking (webcam) and
DualShock controller input are supported.

- `pc/` – Python streamer, web control panel (http://127.0.0.1:5050), hand tracking, pad input
- `android/` – Java / OpenGL ES viewer app
- `installer/` – NSIS installer script
- `scripts/release.ps1` – bump version, build, tag and publish a GitHub release

## Install
Download `VR.Monitor.Setup.<version>.exe` and `VRMonitor.apk` from the latest release
(or install through [Sentinel Hub](https://github.com/sekullar/sentinel-hub)). Install the APK on the phone,
connect it via USB with debugging enabled, then start VR Monitor on the PC.

## Run from source
```
pip install -r requirements.txt
python pc/supervisor.py --usb --launch --stereo
```

## Versioning
`VERSION` is the single source of truth. The release tag is `v<VERSION>`; Sentinel Hub compares release tags
to offer updates. Use `scripts/release.ps1 -Bump patch|minor|major`.

License: MIT
