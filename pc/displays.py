"""Sanal ekran yonetimi (Windows).

- Virtual Display Driver'in ayar dosyasindaki ekran sayisini degistirir ve surucuyu yeniden yukler
  (komut borusu, yonetici gerekmez; olmazsa UAC'li devcon yedegi).
- Yeni ekranlari masaustune baglar ("Uzat"), 1920x1080 yapar ve gercek ekranlarin saginda yan yana dizer.
Gercek ekranlara hicbir sekilde dokunulmaz: sadece 'Virtual Display Driver' adaptorleri ayarlanir.
"""
import ctypes
import os
import re
import subprocess
import time
from ctypes import wintypes

import vddctl

VDD_XML = r"C:\VirtualDisplayDriver\vdd_settings.xml"
DEVCON = r"C:\Android\dl\vdd-extract\Dependencies\devcon.exe"
VDD_DIR = r"C:\Android\dl\vdd-extract\SignedDrivers\x86\VDD"
MAX_VIRTUAL = 4

user32 = ctypes.WinDLL("user32", use_last_error=True)


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
                ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG), ("dmDisplayOrientation", wintypes.DWORD),
                ("dmDisplayFixedOutput", wintypes.DWORD), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                ("dmFormName", wintypes.WCHAR * 32), ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD),
                ("dmReserved2", wintypes.DWORD), ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


DM_POSITION, DM_PELSWIDTH, DM_PELSHEIGHT, DM_FREQ = 0x20, 0x80000, 0x100000, 0x400000
CDS_UPDATEREGISTRY, CDS_NORESET = 0x1, 0x10000000
ENUM_CURRENT = -1


def list_displays():
    """Tum ekran adaptorleri: ad, aciklama, bagli mi, birincil mi, sanal mi, konum ve boyut."""
    out = []
    i = 0
    while True:
        d = DISPLAY_DEVICEW()
        d.cb = ctypes.sizeof(d)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        i += 1
        attached = bool(d.StateFlags & 0x1)
        info = {"name": d.DeviceName, "desc": d.DeviceString, "attached": attached,
                "primary": bool(d.StateFlags & 0x4), "virtual": "Virtual Display" in d.DeviceString,
                "x": 0, "y": 0, "w": 0, "h": 0}
        if attached:
            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(dm)
            if user32.EnumDisplaySettingsW(d.DeviceName, ENUM_CURRENT, ctypes.byref(dm)):
                info.update(x=dm.dmPositionX, y=dm.dmPositionY, w=dm.dmPelsWidth, h=dm.dmPelsHeight)
        out.append(info)
    return out


STATE_FILE = os.path.join(os.environ.get("LOCALAPPDATA", "."), "vr-monitor", "virtual_layout.json")
SETTINGS_FILE = os.path.join(os.environ.get("LOCALAPPDATA", "."), "vr-monitor", "virtual.json")


def auto_enabled():
    """Sanal ekranlar program ile birlikte acilip kapansin mi? (varsayilan: evet)"""
    try:
        import json
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get("auto", True))
    except Exception:
        return True


def set_auto(on):
    import json
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"auto": bool(on)}, f)
    except OSError:
        pass


def _devmode(name, which=ENUM_CURRENT):
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(dm)
    ok = user32.EnumDisplaySettingsW(name, which, ctypes.byref(dm))
    return dm if ok else None


def detach_virtual(log=print):
    """Sanal ekranlari masaustunden AYIRIR (surucu yuklu kalir, yonetici gerekmez, gercek ekranlara dokunulmaz).
    Konum/cozunurluk/yenileme kaydedilir; attach_virtual() ayni degerlerle geri baglar. Oyunlar ekstra ekran gormez."""
    import json
    ds = [d for d in list_displays() if d["virtual"] and d["attached"]]
    if not ds:
        return 0
    saved = {}
    for d in ds:
        dm = _devmode(d["name"])
        saved[d["name"]] = {"x": d["x"], "y": d["y"], "w": d["w"], "h": d["h"],
                            "hz": int(dm.dmDisplayFrequency) if dm else 60}
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(saved, f)
    except OSError:
        pass
    n = 0
    for d in ds:
        dm = _devmode(d["name"])
        if dm is None:
            continue
        dm.dmPelsWidth, dm.dmPelsHeight = 0, 0
        dm.dmPositionX, dm.dmPositionY = d["x"], d["y"]
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_POSITION
        r = user32.ChangeDisplaySettingsExW(d["name"], ctypes.byref(dm), None, CDS_UPDATEREGISTRY | CDS_NORESET, None)
        log(f"{d['name']} ayrildi (sonuc {r})")
        n += 1
    user32.ChangeDisplaySettingsExW(None, None, None, 0, None)       # hepsini uygula
    time.sleep(1.0)
    return n


def attach_virtual(log=print):
    """Ayrilmis sanal ekranlari kaydedilmis konum/cozunurlukle geri baglar. Kayit yoksa 'Uzat' ile baglayip yan yana dizer.
    Donus: baglanan ekran sayisi."""
    import json
    ds = [d for d in list_displays() if d["virtual"]]
    todo = [d for d in ds if not d["attached"]]
    if not todo:
        return 0
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        saved = {}
    if not saved:
        arrange_virtual(log=log)
        return len(todo)
    n = 0
    for d in todo:
        s = saved.get(d["name"])
        dm = _devmode(d["name"], -2)                     # ENUM_REGISTRY_SETTINGS: kayitli son mod
        if dm is None or s is None:
            continue
        dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency = s["w"], s["h"], s["hz"]
        dm.dmPositionX, dm.dmPositionY = s["x"], s["y"]
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_FREQ | DM_POSITION
        r = user32.ChangeDisplaySettingsExW(d["name"], ctypes.byref(dm), None, CDS_UPDATEREGISTRY | CDS_NORESET, None)
        log(f"{d['name']} baglandi: {s['w']}x{s['h']} @({s['x']},{s['y']}) sonuc {r}")
        n += 1
    user32.ChangeDisplaySettingsExW(None, None, None, 0, None)
    time.sleep(1.5)
    return n


def virtual_count_present():
    """Windows'un su an gordugu sanal monitor sayisi (bagli olsun olmasin)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-PnpDevice -Class Monitor -PresentOnly -ErrorAction SilentlyContinue | "
             "Where-Object { $_.InstanceId -match 'MTT1337' }).Count"],
            capture_output=True, text=True, timeout=20)
        return int(r.stdout.strip() or 0)
    except Exception:
        return -1


def configured_count():
    try:
        m = re.search(r"<count>(\d+)</count>", open(VDD_XML, encoding="utf-8").read())
        return int(m.group(1)) if m else 1
    except OSError:
        return 1


def _write_count(n):
    txt = open(VDD_XML, encoding="utf-8").read()
    open(VDD_XML, "w", encoding="utf-8").write(re.sub(r"<count>\d+</count>", f"<count>{n}</count>", txt))


def _reload_driver(n, log):
    """Once komut borusu (yonetici gerekmez), olmazsa UAC ile devcon restart."""
    try:
        vddctl.send("RELOAD_DRIVER", reply_timeout=1.0)
    except OSError as e:
        log(f"boru kullanilamadi ({e})")
    for _ in range(14):
        time.sleep(0.7)
        if virtual_count_present() == n:
            return True
    if os.path.exists(DEVCON):
        log("Windows yonetici onayi isteyebilir (UAC)...")
        ps = (f"Start-Process -FilePath '{DEVCON}' -ArgumentList 'restart','Root\\MttVDD' "
              f"-WorkingDirectory '{VDD_DIR}' -Verb RunAs -Wait -WindowStyle Hidden")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=120)
        except Exception as e:
            log(f"devcon hatasi: {e}")
        for _ in range(14):
            time.sleep(0.7)
            if virtual_count_present() == n:
                return True
    return False


def arrange_virtual(width=1920, height=1080, hz=60, log=print):
    """Sanal ekranlari masaustune baglar, boyutlar ve gercek ekranlarin saginda yan yana dizer."""
    subprocess.run([os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "DisplaySwitch.exe"), "/extend"],
                   timeout=30)
    time.sleep(2.5)
    disp = list_displays()
    real_right = max([d["x"] + d["w"] for d in disp if d["attached"] and not d["virtual"]] or [0])
    virt = sorted([d for d in disp if d["virtual"] and d["attached"]], key=lambda d: d["x"])
    x = real_right
    for d in virt:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(dm)
        user32.EnumDisplaySettingsW(d["name"], ENUM_CURRENT, ctypes.byref(dm))
        dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency = width, height, hz
        dm.dmPositionX, dm.dmPositionY = x, 0
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_FREQ | DM_POSITION
        r = user32.ChangeDisplaySettingsExW(d["name"], ctypes.byref(dm), None, CDS_UPDATEREGISTRY | CDS_NORESET, None)
        log(f"{d['name']}: {width}x{height} @{hz} Hz, konum x={x} -> sonuc {r}")
        x += width
    user32.ChangeDisplaySettingsExW(None, None, None, 0, None)       # hepsini uygula
    time.sleep(1.5)
    return list_displays()


def set_virtual_count(n, log=print):
    """Sanal ekran sayisini n yapar (1..4). Basari durumunu ve son ekran listesini dondurur."""
    n = max(0, min(MAX_VIRTUAL, int(n)))
    _write_count(n)
    ok = True
    if virtual_count_present() != n:
        log(f"Surucu {n} sanal ekranla yeniden yukleniyor...")
        ok = _reload_driver(n, log)
    disp = arrange_virtual(log=log) if ok else list_displays()
    return ok, disp
