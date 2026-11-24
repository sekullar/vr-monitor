#!/usr/bin/env python3
"""VR Monitor - PC tarafi.

Yakalama: dxcam (GPU / DXGI Desktop Duplication), olmazsa mss.
Kodlama: donanim H.264 (PyAV: NVENC / AMF) varsayilan; olmazsa veya istenirse OpenCV JPEG.
Her gercek monitor icin bir yakalama thread'i, her panel icin bir kodlama thread'i; aralarinda
tek yuvalik "en yeni kare" kutusu var (yavassa eski kareler atilir, gecikme birikmez).
Telefon her karenin damgasini geri yollar (ack): PC kendi saatiyle uctan uca gecikmeyi olcer.
Ayrica web arayuzu (127.0.0.1:5050) acilir: panelleri surukleyip ortami ayarlarsin.

Kullanim:
    python -u stream.py --launch
    python -u stream.py --panels 3 --launch   # monitor yetmezse ilk monitor kopyalanir (test)
"""
import argparse
import ctypes
import os
import socket
import struct
import subprocess
import threading
import time

import mss

try:  # hizli yol: OpenCV + numpy (+ dxcam). Yoksa Pillow'a dus.
    import cv2
    import numpy as np
    HAVE_CV = True
except Exception:
    HAVE_CV = False
    from PIL import Image, ImageDraw

from core import Layout, Link, Sender

try:
    import h264
except Exception:       # PyAV kurulu degil: JPEG'e dus
    h264 = None
HAVE_H264 = False       # main() kodlayiciyi deneyip ayarlar
def layout_watcher(geoms):
    """Windows ekran yerleşimi (ekran eklendi/cikarildi/tasindi) yayin basladiktan sonra degisirse yakalama
    koordinatlari eskir ve bir panel bos kalir. Kararli sekilde degisirse programi yeniden baslat (gozetmen toparlar)."""
    import mss as _mss

    def snap():
        with _mss.MSS() as s:
            return sorted((m["left"], m["top"], m["width"], m["height"]) for m in s.monitors[1:])
    base = sorted((g["left"], g["top"], g["width"], g["height"]) for g in geoms)
    t0 = time.time()
    bad = 0
    while True:
        time.sleep(2.0)
        try:
            cur = snap()
        except Exception:
            continue
        if cur == base:
            bad = 0
            continue
        bad += 1
        if bad >= 3 and time.time() - t0 > 20:          # ~6 sn kararli degisiklik (gecici gecisleri yoksay)
            print(f"EKRAN YERLESIMI DEGISTI: {base} -> {cur}; yeniden baslatiliyor", flush=True)
            try:
                import webui
                webui.restart_self()
            except Exception as e:
                print("yeniden baslatilamadi:", e, flush=True)
            return


def _find_adb():
    """adb'yi PATH'te, yoksa bilinen Android SDK konumlarinda ara (exe olarak calisirken PATH'e guvenme)."""
    import shutil
    p = shutil.which("adb")
    if p:
        return p
    for c in (r"C:\Android\sdk\platform-tools\adb.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Android\Sdk\platform-tools\adb.exe"),
              os.path.join(os.environ.get("ANDROID_HOME", ""), r"platform-tools\adb.exe")):
        if c and os.path.isfile(c):
            return c
    return "adb"


ADB = _find_adb()
DX_LOCK = threading.Lock()   # dxcam yakalamalari (ortak D3D11 baglami) sirayla yapilir
HANDS = None            # el takibi / lazer imlec (hands.HandControl), main() olusturur


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_pos():
    p = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]


class Slot:
    """Tek yuvalik kutu: put() eskisinin ustune yazar, get() en yeniyi alir."""

    def __init__(self):
        self.cond = threading.Condition()
        self.item = None

    def put(self, x):
        with self.cond:
            self.item = x
            self.cond.notify()

    def get(self, stop):
        with self.cond:
            while self.item is None and not stop.is_set():
                self.cond.wait(0.5)
            x, self.item = self.item, None
            return x


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = {}   # son raporun sayilari (web arayuzu gosterir)
        self.started = time.time()
        self.last_frame_t = time.time()
        self.total = 0
        self.reset()

    def reset(self):
        self.t = time.perf_counter()
        self.sums, self.cnt = {}, {}
        self.n = self.bytes = self.skipped = 0
        self.lat_sum, self.lat_n, self.lat_max = 0.0, 0, 0.0
        self.gaps = [0, 0, 0]       # PC gonderme araliklari: <3 ms (birlikte), 3-12 ms (duzenli), >12 ms (bosluk)

    def gap(self, dt):
        with self.lock:
            self.gaps[0 if dt < 0.003 else (1 if dt < 0.012 else 2)] += 1

    def lat(self, ms):
        """Telefondan gelen ack ile olculen yakalama->telefonda GL'e yuklenme gecikmesi (ms)."""
        with self.lock:
            self.lat_sum += ms
            self.lat_n += 1
            self.lat_max = max(self.lat_max, ms)

    def add(self, **kw):
        with self.lock:
            for k, v in kw.items():
                self.sums[k] = self.sums.get(k, 0.0) + v
                self.cnt[k] = self.cnt.get(k, 0) + 1

    def frame(self, nbytes):
        with self.lock:
            self.n += 1
            self.bytes += nbytes
            self.last_frame_t = time.time()      # bekci (watchdog): yayin takildi mi?
            self.total += 1

    def skip(self):
        with self.lock:
            self.skipped += 1

    def report(self):
        with self.lock:
            dt = time.perf_counter() - self.t
            n = max(self.n, 1)
            parts = [f"{k}={v / max(self.cnt[k], 1) * 1000:.1f}ms" for k, v in self.sums.items()]
            line = (f"gonderilen {self.n / dt:.1f} fps | {self.bytes / dt * 8 / 1e6:.1f} Mbit/s | "
                    f"ort kare {self.bytes / n / 1024:.0f} KB | ayni-atlanan {self.skipped} | "
                    + " ".join(parts))
            g = self.gaps
            if sum(g):
                line += f" | PC aralik <3ms:{g[0]} 3-12:{g[1]} >12:{g[2]}"
            lat = round(self.lat_sum / self.lat_n, 1) if self.lat_n else None
            if lat is not None:
                line += f" | gecikme={lat:.0f}ms (max {self.lat_max:.0f})"
            self.last = {"fps": round(self.n / dt, 1), "mbps": round(self.bytes / dt * 8 / 1e6, 1),
                         "kb": round(self.bytes / n / 1024), "skipped": self.skipped,
                         "lat": lat, "lat_max": round(self.lat_max, 1) if self.lat_n else None}
            self.reset()
            return line


# ---------------- yakalayicilar ----------------

class WgcGrabber:
    """Windows Graphics Capture (windows-capture paketi): dxcam/DXGI masaustu kopyalamasi acilamadigi zaman hizli yedek.
    Kareler ekran degistikce gelir; en yenisi saklanir, grab() yeni kare varsa verir, yoksa None (dxcam gibi)."""
    kind = "wgc"

    def __init__(self, monitor_index):
        from windows_capture import WindowsCapture
        self.lock = threading.Lock()
        self.frame = None
        self.fresh = False
        self.closed = False
        cap = WindowsCapture(cursor_capture=False, draw_border=False, monitor_index=monitor_index,
                             minimum_update_interval=0)

        @cap.event
        def on_frame_arrived(frame, ctl):
            buf = frame.frame_buffer
            with self.lock:
                self.frame = buf.copy()          # tampon geri donusumlu: callback disina kopya
                self.fresh = True

        @cap.event
        def on_closed():
            self.closed = True

        self.ctrl = cap.start_free_threaded()

    def grab(self):
        with self.lock:
            if not self.fresh:
                return None
            self.fresh = False
            return self.frame


_WGC = {}


def get_wgc(monitor_index):
    """Ekran basina tek WGC oturumu (baglanti yenilense de ayni oturum kullanilir). Olmazsa None."""
    if monitor_index in _WGC:
        return _WGC[monitor_index]
    try:
        g = WgcGrabber(monitor_index)
    except Exception as e:
        print(f"  WGC acilamadi (ekran {monitor_index}): {type(e).__name__}: {str(e)[:80]}", flush=True)
        g = None
    _WGC[monitor_index] = g
    return g


class MssGrabber:
    kind = "mss"

    def __init__(self, mon_idx):
        self.mon_idx = mon_idx
        self.sct = None

    def grab(self):
        if self.sct is None:
            self.sct = (getattr(mss, "MSS", None) or mss.mss)()
        shot = self.sct.grab(self.sct.monitors[self.mon_idx])
        if not HAVE_CV:
            return (shot.width, shot.height, shot.bgra)
        return np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4)


class DxGrabber:
    kind = "dxcam"

    def __init__(self, cam):
        self.cam = cam
        self.seed = None      # serve() doldurur: ekran durgunken ilk kareyi tek seferlik mss ile almak icin
        # Bu cikisin masaustundeki konumu: mss monitorleriyle eslestirmek icin
        try:
            dc = cam._output.desc.DesktopCoordinates
            self.left, self.top = dc.left, dc.top
        except Exception:
            self.left = self.top = None

    def grab(self):
        # Iki ekranin dxcam kameralari AYNI D3D11 cihaz baglamini paylasir; bu baglam ayni anda iki thread'den
        # kullanilamaz (StageSurface.map/unmap'te kilitlenme: yayin sessizce durur). Yakalamalari sirala.
        with DX_LOCK:
            f = self.cam.grab()  # ekran degismediyse None
            return None if f is None else f.copy()


class SyntheticGrabber:
    """--bench: ekrandan bagimsiz, her karede degisen 1920x1080 test deseni (boru hattinin ust siniri olculur)."""
    kind = "synthetic"

    def __init__(self):
        w, h = 1920, 1080
        yy, xx = np.mgrid[0:h, 0:w + 2000]
        base = np.empty((h, w + 2000, 4), np.uint8)
        base[..., 0] = (xx * 3 + yy) % 251                      # B
        base[..., 1] = ((xx // 40) * 53) % 256                  # G: dikey seritler
        base[..., 2] = ((yy // 24) * 37 + (xx // 7) % 29) % 256  # R: ince ayrintili (metin benzeri) doku
        base[..., 3] = 255
        self.base, self.w, self.i = base, w, 0

    def grab(self):
        self.i += 7
        off = self.i % 2000
        return np.ascontiguousarray(self.base[:, off:off + self.w])


def discover_dx(count):
    if not HAVE_CV:
        return []
    try:
        import dxcam
    except Exception as e:
        print("dxcam yok, mss kullanilacak:", e)
        return []
    out = []
    for dev in range(3):
        for o in range(8):
            if len(out) >= count:
                return out
            try:
                cam = dxcam.create(device_idx=dev, output_idx=o, output_color="BGRA")
            except IndexError:
                continue                       # bu adaptorde o cikis yok (normal)
            except Exception as e:
                print(f"  dxcam cihaz {dev} cikis {o}: acilamadi ({type(e).__name__}: {str(e)[:80]})", flush=True)
                continue
            if cam is None:
                print(f"  dxcam cihaz {dev} cikis {o}: bos dondu", flush=True)
                continue
            try:
                cam.grab()      # baslat. Ekran durgunsa None doner: bu sorun degil (ilk kare mss ile tohumlanir)
            except Exception as e:
                print(f"  dxcam cihaz {dev} cikis {o}: grab hatasi ({type(e).__name__}: {str(e)[:80]})", flush=True)
                continue
            out.append(DxGrabber(cam))
            print(f"  dxcam: cihaz {dev}, cikis {o}, {cam.width}x{cam.height}", flush=True)
    return out


# ---------------- thread'ler ----------------

def boost_process():
    """Yuksek oncelik + 1 ms zamanlayici cozunurlugu: kare ritmi icin (sleep() ~15 ms yerine ~1 ms)."""
    if os.environ.get("VRM_NOBOOST"):          # olcum: oncelik/zamanlayici ayarlarini kapat
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)      # HIGH_PRIORITY_CLASS
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def boost_thread():
    if os.environ.get("VRM_NOBOOST"):
        return
    try:
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), 2)               # THREAD_PRIORITY_HIGHEST
    except Exception:
        pass


def sleep_until(t):
    """Hedef zamana kadar uyu, son ~0.7 ms'yi bekleyerek kullan (ritim titremesi azalir)."""
    while True:
        rem = t - time.perf_counter()
        if rem <= 0:
            return
        if rem > 0.0012:
            time.sleep(rem - 0.0008)


def capture_loop(grabber, mon, slots, stop, stats, args):
    boost_thread()
    is_main = (mon["left"], mon["top"]) == (0, 0)      # Windows'ta ana ekran her zaman (0,0)'dadir
    last = None
    last_cur = None
    last_put = 0.0
    flush_left = 0
    next_t = time.perf_counter()
    seed = getattr(grabber, "seed", None)
    if seed is not None:
        try:
            last = seed.grab()      # durgun ekranda dxcam hic kare vermez; bir kez mss ile baslangic karesini al
        except Exception:
            last = None
    while not stop.is_set():
        sleep_until(next_t)
        t0 = time.perf_counter()
        f = grabber.grab()
        if f is not None:
            last = f
        cur = cursor_pos()
        # imlec bu monitorun disindaysa "imlec degisti" sayma (durgun ikinci ekrana bosuna kare gonderilmesin)
        inside = (mon["left"] <= cur[0] < mon["left"] + mon["width"]
                  and mon["top"] <= cur[1] < mon["top"] + mon["height"])
        cur_cmp = cur if inside else None
        interval = 1.0 / max(1, args.fps if is_main else args.fps_other)
        if last is None:
            next_t = t0 + 0.002
            continue
        if f is not None or cur_cmp != last_cur:
            # H.264 kodlayicisi kareleri icinde tutar (boru hatti): icerik durunca ayni kareyi 2 kez daha it,
            # yoksa son degisiklik birkac kare gecikmeyle (en kotu ~1 sn) cikar
            flush_left = 2 if args.codec == "h264" else 0
        elif flush_left > 0 and t0 - last_put >= interval * 0.9:
            flush_left -= 1
        elif t0 - last_put < 0.5:
            next_t = t0 + 0.002        # degisiklik yok: kisa aralikla tekrar bak (0.5 sn'de bir tazele)
            continue
        stats.add(**{("yakala" if is_main else "yakala2"): time.perf_counter() - t0})   # ana / diger ekran ayri
        for s in slots:
            s.put((last, cur, mon, t0))      # t0: gecikme olcumu icin yakalama zamani
        last_cur, last_put = cur_cmp, t0
        next_t = max(next_t + interval, t0)             # sabit ritim; gecikirse birikmez


def encode_frame_pil(frame, cur, mon, label, args):
    import io
    w, h, raw = frame
    img = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", 0, 1)
    sc = 1.0
    if args.width and w > args.width:        # yalniz KUCULT; kucuk ekrani buyutme (800x600 -> 1920x1440 israfti)
        sc = args.width / w
        img = img.resize((args.width, round(h * sc)), Image.BILINEAR)
    x, y = (cur[0] - mon["left"]) * sc, (cur[1] - mon["top"]) * sc
    d = ImageDraw.Draw(img)
    if 0 <= x < img.width and 0 <= y < img.height:
        d.polygon([(x + px, y + py) for px, py in ARROW], fill=(255, 255, 255), outline=(0, 0, 0))
    if label:
        d.text((14, 10), label, fill=(255, 220, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=args.quality)
    return buf.getvalue()


def encode_frame(frame, cur, mon, label, args):
    if not HAVE_CV:
        return encode_frame_pil(frame, cur, mon, label, args)
    h, w = frame.shape[:2]
    sc = 1.0
    if args.width and w > args.width:        # yalniz KUCULT; kucuk ekrani buyutme (800x600 -> 1920x1440 israfti)
        sc = args.width / w
        frame = cv2.resize(frame, (args.width, round(h * sc)), interpolation=cv2.INTER_AREA)
    img = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    x, y = int((cur[0] - mon["left"]) * sc), int((cur[1] - mon["top"]) * sc)
    if 0 <= x < img.shape[1] and 0 <= y < img.shape[0]:
        pts = np.array([(x + px, y + py) for px, py in ARROW], np.int32)
        cv2.fillPoly(img, [pts], (255, 255, 255))
        cv2.polylines(img, [pts], True, (0, 0, 0), 1)
    if label:
        cv2.putText(img, label, (14, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 255), 2)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
    return buf.tobytes()


def prep_bgra(frame, cur, mon, label, args):
    """H.264 icin BGRA kareyi hazirlar: gerekirse kucultur, imleci cizer.

    Kare yakalama thread'iyle paylasildigi icin imlec cizilen bolgenin yedegi alinir; kodlayici kareyi
    kopyaladiktan sonra geri yuklenir (aksi halde eski imlec izleri birikirdi). (kare, geri_yukle) dondurur.
    """
    h, w = frame.shape[:2]
    sc = 1.0
    arr = frame
    owned = False
    if args.width and w > args.width:        # yalniz KUCULT; kucuk ekrani buyutme (800x600 -> 1920x1440 israfti)
        sc = args.width / w
        arr = cv2.resize(frame, (args.width, round(h * sc)), interpolation=cv2.INTER_AREA)
        owned = True
    if label or not arr.flags.writeable:
        arr = arr.copy()
        owned = True
    H, W = arr.shape[:2]
    x, y = int((cur[0] - mon["left"]) * sc), int((cur[1] - mon["top"]) * sc)
    restore = None
    if 0 <= x < W and 0 <= y < H:
        if not owned:
            y0, y1, x0, x1 = max(y - 1, 0), min(y + 26, H), max(x - 1, 0), min(x + 18, W)
            restore = (y0, y1, x0, x1, arr[y0:y1, x0:x1].copy())
        pts = np.array([(x + px, y + py) for px, py in ARROW], np.int32)
        cv2.fillPoly(arr, [pts], (255, 255, 255, 255))
        cv2.polylines(arr, [pts], True, (0, 0, 0, 255), 1)
    if label:
        cv2.putText(arr, label, (14, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 255, 255), 2)
    return arr, restore


def encode_loop(idx, total, is_clone, slot, out, conn, send_lock, stop, stats, args, keyreq):
    """Kodlama: yakalanan en yeni kareyi H.264'e (donanim) ya da JPEG'e cevirir.

    H.264 paketleri dogrudan burada gonderilir (paket atlanirsa goruntu bozulur, bu yuzden "en yeni kare"
    yerine sirali gider; yavassa girisdeki kare yuvasinda eski kareler ezilir). JPEG gonderici thread'e verilir.
    """
    boost_thread()
    last = b""
    last_sent = 0.0
    label = f"PANEL {idx + 1}" if is_clone else ""
    enc, enc_cfg = None, None
    last_send = 0.0
    while not stop.is_set():
        item = slot.get(stop)
        if item is None:
            continue
        frame, cur, mon, tcap = item
        t0 = time.perf_counter()

        if args.codec == "h264" and HAVE_H264 and HAVE_CV:
            restore = None
            try:
                is_main = (mon["left"], mon["top"]) == (0, 0)
                arr, restore = prep_bgra(frame, cur, mon, label, args)
                hh, ww = arr.shape[:2]
                cfg = (ww, hh, int(args.bitrate * 1_000_000))
                if enc is None or cfg != enc_cfg:        # cozunurluk / bitrate degisti: kodlayiciyi yeniden kur
                    hw = args.hw_encoders
                    enc = h264.H264Encoder(ww, hh, args.fps if is_main else args.fps_other, cfg[2],
                                           prefer=hw[idx % len(hw)] if hw else None)     # paneller farkli motorlara
                    enc_cfg = cfg
                    args.encoder_name = enc.name
                    print(f"H.264 kodlayici (panel {idx + 1}): {enc.name} {ww}x{hh} {args.bitrate:g} Mbit/s", flush=True)
                force = bool(keyreq.get(idx))
                if force:
                    keyreq[idx] = False
                pts_us = int(tcap * 1e6)
                tp = time.perf_counter()
                packets = enc.encode(arr, pts_us, force)
                stats.add(hazirla=tp - t0, nvenc=time.perf_counter() - tp)    # olcum: imlec/kopya hazirligi vs gercek kodlama
            except Exception as e:
                print(f"H.264 hatasi, JPEG'e donuluyor: {e}", flush=True)
                args.codec = "jpeg"
                enc = None
                continue
            finally:
                if restore is not None:
                    y0, y1, x0, x1, backup = restore
                    arr[y0:y1, x0:x1] = backup
            t1 = time.perf_counter()
            try:
                for data, is_key in packets:
                    payload = struct.pack(">QHHB", pts_us, ww, hh, 1 if is_key else 0) + data
                    with send_lock:
                        conn.sendall(struct.pack(">BBI", 16 + idx, total, len(payload)) + payload)
                    stats.frame(len(payload))
            except OSError:
                stop.set()
                return
            t2 = time.perf_counter()
            stats.add(kodla=t1 - t0, gonder=t2 - t1)
            if idx == 0 and packets:                 # panel 1'in gonderme araliklari (kumelenme teshisi)
                if last_send:
                    stats.gap(t1 - last_send)
                last_send = t1
            continue

        enc = None
        data = encode_frame(frame, cur, mon, label, args)
        t1 = time.perf_counter()
        if data == last and t1 - last_sent < 1.0:  # degismedi, bant genisligi harcama
            stats.skip()
            continue
        last, last_sent = data, t1
        stats.add(kodla=t1 - t0)
        out.put(data)      # gonderici yavassa eski kare ezilir, hep en yeni kare gider


def reader_loop(conn, stop, stats, keyreq):
    """Telefondan gelen kucuk mesajlar: [tur:1][panel:1][uzunluk:2][veri].
    tur 1 = ack (en son gosterilen karenin PC damgasi, 8 bayt, mikrosaniye) -> gecikme olcumu
    tur 2 = anahtar kare iste (telefondaki cozucu takildi / yeni basladi)."""
    buf = b""
    while not stop.is_set():
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:                      # telefon baglantiyi kapatti
            break
        buf += chunk
        while len(buf) >= 4:
            typ, panel, ln = buf[0], buf[1], struct.unpack(">H", buf[2:4])[0]
            if len(buf) < 4 + ln:
                break
            payload, buf = buf[4:4 + ln], buf[4 + ln:]
            if typ == 1 and ln == 8:
                pts = struct.unpack(">Q", payload)[0]
                ms = (time.perf_counter() * 1e6 - pts) / 1000.0
                if 0 <= ms < 5000:
                    stats.lat(ms)
            elif typ == 2:
                keyreq[panel] = True
            elif typ == 3 and ln == 6 and HANDS is not None:
                seq, u, v = struct.unpack(">HHH", payload)     # lazer isabeti: panel + (u,v) 0..65535
                try:
                    HANDS.on_hit(seq, panel, u / 65535.0, v / 65535.0)
                except Exception as e:
                    print("el isabeti hatasi:", e, flush=True)
            elif typ == 5 and HANDS is not None:
                HANDS.on_phone_frame(payload)                  # telefon kamerasi karesi (JPEG)
    stop.set()


def send_loop(idx, total, out, conn, send_lock, stop, stats, args):
    """Gonderme: kodlama ile ust uste biner (bir kare giderken sonraki kodlanir) ve kaliteyi hat hizina uydurur."""
    boost_thread()
    ema, n_sent = 0.0, 0
    while not stop.is_set():
        data = out.get(stop)
        if data is None:
            continue
        t1 = time.perf_counter()
        try:
            with send_lock:
                conn.sendall(struct.pack(">BBI", idx, total, len(data)) + data)
        except OSError:
            stop.set()
            return
        t2 = time.perf_counter()
        stats.add(gonder=t2 - t1)
        stats.frame(len(data))
        if args.adapt:
            # Hat yavassa kaliteyi dusur (fps korunur), rahatsa yavasca geri yukselt
            ema = ema * 0.85 + (t2 - t1) * 0.15
            n_sent += 1
            if n_sent % 8 == 0:
                # 60 fps ustunde kare basina veri ve telefondaki cozme yuku onemli: kaliteyi 80 ile sinirla
                qmax = args.max_quality if args.fps <= 60 else min(args.max_quality, 80)
                if args.quality > qmax:
                    args.quality = qmax
                if ema > 0.028 and args.quality > args.min_quality:
                    args.quality = max(args.min_quality, args.quality - 4)
                elif ema < 0.012 and args.quality < qmax:
                    args.quality = min(qmax, args.quality + 1)


def serve(args, mons, geoms, total, dx, link, sender):
    real = len(mons)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(1)
    ips = local_ips()
    print(f"Dinleniyor: port {args.port}  | PC IP: {', '.join(ips)}", flush=True)

    if args.usb:  # USB kablo: telefon 127.0.0.1:5000'e baglanir, adb bunu PC'ye tunel eder
        try:
            r = subprocess.run([ADB, "reverse", f"tcp:{args.port}", f"tcp:{args.port}"],
                               capture_output=True, text=True, timeout=15)
            print("USB tuneli (adb reverse):", (r.stdout + r.stderr).strip() or f"kod {r.returncode}", flush=True)
        except Exception as e:
            print("adb reverse basarisiz:", e)
    if args.usb:
        def usb_keeper():
            """USB tuneli (adb reverse) kablo cikip takilinca / adb yeniden baslayinca sessizce silinir:
            telefon 'ECONNREFUSED' gorur. Her 4 sn kontrol edip gerekirse yeniden kur."""
            while True:
                time.sleep(4)
                try:
                    r = subprocess.run([ADB, "reverse", "--list"], capture_output=True, text=True, timeout=8)
                    if f"tcp:{args.port}" not in r.stdout and "device" not in r.stderr.lower():
                        subprocess.run([ADB, "reverse", f"tcp:{args.port}", f"tcp:{args.port}"],
                                       capture_output=True, text=True, timeout=8)
                        print("USB tuneli yenilendi", flush=True)
                except Exception:
                    pass
        threading.Thread(target=usb_keeper, daemon=True).start()
    if args.launch and (ips or args.usb):
        host = "127.0.0.1" if args.usb else (args.host or ips[0])
        cmd = [ADB, "shell", "am", "start", "-n", "com.vrmonitor/.MainActivity",
               "--es", "host", host, "--ez", "stereo", "true" if args.stereo else "false"]
        if args.camera:
            cmd += ["--ez", "camera", "true"]
        print("Uygulama baslatiliyor:", " ".join(cmd), flush=True)
        try:
            subprocess.run(cmd, check=False, timeout=15)
        except Exception as e:
            print("adb calismadi:", e)

    while True:
        conn, addr = srv.accept()
        print("Baglandi:", addr, flush=True)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Wi-Fi'da kucuk tampon = az eski veri; USB'de buyuk tampon: kare basina gidis-donus beklemesi biter
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, (1024 if args.usb else 64) * 1024)
        conn.settimeout(3.0)  # telefon sessizce koptuysa sendall asili kalmasin, yeni baglanti kabul edilsin
        stop = threading.Event()
        send_lock = threading.Lock()
        stats = Stats()
        link.attach(conn, send_lock)
        sender.mark(bg=True)  # kayitli duzeni ve arka plan gorselini telefona gonder
        if HANDS is not None:
            threading.Timer(1.0, HANDS.on_connect).start()    # telefon kamerasi kaynagi seciliyse kare yollamaya baslasin

        slots = [Slot() for _ in range(total)]
        outs = [Slot() for _ in range(total)]
        keyreq = {}                       # telefon "anahtar kare istiyorum" derse panel -> True
        threads = [threading.Thread(target=reader_loop, daemon=True, args=(conn, stop, stats, keyreq))]
        for m in range(real):
            # koordinati eslesen dxcam yakalayicisi, yoksa bu monitor icin mss
            grabber = next((g for g in dx if (g.left, g.top) == (geoms[m]["left"], geoms[m]["top"])), None)
            if grabber is None:
                # dxcam bu ekrani acamadi (ornegin DXGI_ERROR_UNSUPPORTED): hizli Windows Graphics Capture, olmazsa mss (yavas)
                grabber = get_wgc(mons[m]) or MssGrabber(mons[m])
                print(f"  ekran {m + 1}: yakalama = {grabber.kind}", flush=True)
            if isinstance(grabber, DxGrabber):
                grabber.seed = MssGrabber(mons[m])
            if args.bench:
                grabber = SyntheticGrabber()      # gercek ekran yerine sentetik kare uretici (olcum; her ekran icin)
            ms = [slots[i] for i in range(total) if i % real == m]
            threads.append(threading.Thread(target=capture_loop, daemon=True,
                                            args=(grabber, geoms[m], ms, stop, stats, args)))
        for i in range(total):
            threads.append(threading.Thread(target=encode_loop, daemon=True,
                                            args=(i, total, i >= real, slots[i], outs[i], conn, send_lock,
                                                  stop, stats, args, keyreq)))
            threads.append(threading.Thread(target=send_loop, daemon=True,
                                            args=(i, total, outs[i], conn, send_lock, stop, stats, args)))
        for t in threads:
            t.start()
        while not stop.wait(2.0):
            print(stats.report() + f" | kalite={args.quality}", flush=True)
            sender.layout.stats = stats.last
            # BEKCI: durgun ekran bile >=2 kare/sn gonderir; 8 sn hic kare yoksa yakalama/kodlama kilitlenmis demektir
            # (ornegin D3D11 baglam kilitlenmesi). Telefon baglantisini ve programi bastan baslat: kendini toparlasin.
            if not args.bench and stats.total > 0 and time.time() - stats.last_frame_t > 8.0:
                print("BEKCI: yayin 8 sn kare gondermedi (takildi), program yeniden baslatiliyor...", flush=True)
                try:
                    import webui
                    webui.restart_self()
                except Exception as e:
                    print("yeniden baslatilamadi:", e, flush=True)
                    stop.set()
        link.detach()
        conn.close()
        print("Baglanti kapandi, yeni baglanti bekleniyor...", flush=True)


def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # paket gitmez, sadece rota secilir
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--panels", type=int, default=0,
                    help="panel sayisi (0 = gercek monitor sayisi). Monitor yetmezse ilk monitor kopyalanir")
    ap.add_argument("--width", type=int, default=1920, help="gonderilen genislik (0 = orijinal)")
    ap.add_argument("--quality", type=int, default=80, help="baslangic JPEG kalitesi")
    ap.add_argument("--min-quality", type=int, default=50, help="otomatik ayarda alt sinir")
    ap.add_argument("--max-quality", type=int, default=90, help="otomatik ayarda ust sinir")
    ap.add_argument("--no-adapt", dest="adapt", action="store_false",
                    help="kaliteyi hat hizina gore otomatik ayarlama")
    ap.add_argument("--codec", choices=["auto", "h264", "jpeg"], default="auto",
                    help="auto: donanim H.264 varsa onu kullan, yoksa JPEG")
    # 80 olcuyle gecikmeyi ikiye katladi (48 -> 108 ms): 40 kaldi
    ap.add_argument("--bitrate", type=float, default=40.0, help="H.264 hedef bit hizi (Mbit/s, panel basina)")
    ap.add_argument("--camera", action="store_true", help="--launch ile kamera arka plan modunda ac")
    ap.add_argument("--usb", action="store_true", help="USB kablo uzerinden (adb reverse), en dusuk gecikme")
    # 120/30 ile masaustu aralikli 200-400 ms takiliyordu (yakalama+kodlama yuku ~100 kare/sn'yi asinca); 90/15 temiz
    ap.add_argument("--fps", type=int, default=90, help="ana ekranin (birinci ekran) kare siniri")
    ap.add_argument("--fps-other", type=int, default=15, help="diger ekranlarin kare siniri (cogu durgun)")
    ap.add_argument("--no-dx", action="store_true", help="dxcam kullanma, mss kullan")
    ap.add_argument("--bench", action="store_true",
                    help="ana ekran yerine sentetik hareketli desen gonder (kare hizi / bant genisligi olcumu)")
    ap.add_argument("--no-gui", action="store_true", help="web arayuzunu hic baslatma")
    ap.add_argument("--no-open", action="store_true", help="web arayuzunu sunucuda baslat ama pencereyi acma")
    ap.add_argument("--ui-port", type=int, default=5050, help="web arayuzu portu (yalnizca 127.0.0.1)")
    ap.add_argument("--launch", action="store_true", help="adb ile telefondaki uygulamayi baslat")
    ap.add_argument("--host", default="", help="telefonun baglanacagi PC IP (bos = otomatik)")
    ap.add_argument("--stereo", action="store_true", help="--launch ile stereo modda ac")
    args = ap.parse_args()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    boost_process()

    global HAVE_H264
    args.encoder_name = ""
    args.hw_encoders = []

    with mss.mss() as sct:
        mons = list(range(1, len(sct.monitors)))
        # Siralama = Windows Ayarlar'daki ekran numarasi: ana ekran 1, sonra cihaz adindaki sayiya gore (DISPLAY12 = 2, ...).
        # Eskiden soldan saga siralaniyordu ve VR'daki panel numaralari Windows numaralariyla uyusmuyordu.
        names = {}
        try:
            import re as _re
            import displays as _disp
            ds = [d for d in _disp.list_displays() if d["attached"] and d["w"]]

            def _key(i):
                m = sct.monitors[i]
                for d in ds:
                    if (d["x"], d["y"], d["w"], d["h"]) == (m["left"], m["top"], m["width"], m["height"]):
                        names[i] = d["name"]
                        return (0 if d["primary"] else 1, int(_re.sub(r"\D", "", d["name"]) or 999))
                return (2, m["left"])
            mons.sort(key=_key)
        except Exception as e:
            print("ekran siralamasi Windows numarasindan alinamadi, soldan saga:", e, flush=True)
            mons.sort(key=lambda i: sct.monitors[i]["left"])
        # Kayitli VR duzeni icin kararli imza: ekran cihaz adlari (DISPLAY12...) yeniden baglaninca degisebilir; rol kullan
        geoms = [dict(sct.monitors[i], name=("main" if k == 0 else f"v{k}")) for k, i in enumerate(mons)]
    for g in geoms:
        print(f"  monitor: {g['width']}x{g['height']} @ ({g['left']},{g['top']})")
    if not os.environ.get("VRM_NOWATCH"):             # olcum anahtari
        threading.Thread(target=layout_watcher, args=(geoms,), daemon=True).start()
    real = len(mons)
    total = args.panels or real
    dx = [] if args.no_dx else discover_dx(real)
    print(f"{real} gercek monitor, {total} panel, yakalama: "
          f"{'dxcam' if len(dx) >= real else ('karisik' if dx else 'mss')}, "
          f"{args.width}px, kalite {args.quality}", flush=True)

    # H.264 kodlayici yoklamasi dxcam KESFINDEN SONRA yapilmali: NVENC/AMF baslatilinca sonraki
    # dxcam.create() DXGI_ERROR_UNSUPPORTED ile duser (ana ekran yavas mss'e kalirdi).
    if h264 is not None and HAVE_CV and args.codec != "jpeg":
        name, err = h264.available()
        HAVE_H264 = bool(name)
        args.encoder_name = name
        args.hw_encoders = h264.usable() if HAVE_H264 else []
        # Varsayilan: yalniz RTX (NVENC). AMF (AMD iGPU) ekrani suren iGPU'yu da yorar; kullanici iGPU'ya bagli kalmak istemiyor.
        # Elle secim / olcum: VRM_HW=amf ya da VRM_HW=nvenc (ortam degiskeni)
        _only = os.environ.get("VRM_HW") or ("nvenc" if any("nvenc" in e for e in args.hw_encoders) else "")
        if _only:
            args.hw_encoders = [e for e in args.hw_encoders if _only in e] or args.hw_encoders
        if args.hw_encoders:
            print("Donanim kodlayicilar:", ", ".join(args.hw_encoders), flush=True)
        print(f"H.264 donanim kodlayici: {name or 'YOK (' + err[:80] + ')'}", flush=True)
    args.h264_ok = HAVE_H264
    if args.codec == "auto":
        args.codec = "h264" if HAVE_H264 else "jpeg"
    elif args.codec == "h264" and not HAVE_H264:
        print("H.264 istendi ama kullanilamiyor, JPEG kullanilacak", flush=True)
        args.codec = "jpeg"

    path = os.path.join(os.environ.get("LOCALAPPDATA", "."), "vr-monitor", "layout.json")
    layout = Layout(total, path, geoms)
    link = Link()
    sender = Sender(link, layout)
    sender.start()
    global HANDS
    try:
        if os.environ.get("VRM_NOHANDS"):             # olcum anahtari
            raise RuntimeError("VRM_NOHANDS")
        import hands
        HANDS = hands.HandControl(link, os.path.join(os.path.dirname(path), "hand.json"), geoms, real, layout)
    except Exception as e:
        print("el kontrolu baslatilamadi:", e, flush=True)
    args.hands = HANDS

    threading.Thread(target=serve, daemon=True,
                     args=(args, mons, geoms, total, dx, link, sender)).start()
    try:
        if args.no_gui:
            while True:
                time.sleep(1)
        else:
            import webui
            url = webui.start(args, layout, link, sender, port=args.ui_port)
            print(f"Arayuz: {url}", flush=True)
            if not args.no_open:
                webui.open_ui(url)
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
