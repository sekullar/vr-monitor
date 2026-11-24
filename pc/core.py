"""VR Monitor - ortak cekirdek (PC tarafi).

Duzen (panellerin yeri) + ortam (arka plan, golge, isilti...) modeli, telefona giden baglanti ve
degisiklikleri birlestirip (coalesce) gonderen thread. Arayuz (web) bunlari kullanir.

Telefona giden mesajlar, ayni TCP baglantisi uzerinden: [idx:1][0:1][uzunluk:4][veri]
  idx 255 = duzen+ortam JSON'u, 254 = komut metni, 253 = arka plan gorseli (JPEG)
"""
import io
import json
import os
import re
import struct
import threading
import time

KEYS = ("yaw", "pitch", "dist", "scale")
LIMITS = {"yaw": (-180, 180), "pitch": (-70, 70), "dist": (1.0, 8.0), "scale": (0.3, 4.0)}

# Ortam ayarlari (telefondaki cizim motoru kullanir)
DEFAULT_ENV = {
    "bg": "dark",            # dark = duz renk | camera = telefonun kamerasi | image = secilen gorsel
    "bg_bright": 1.0,        # arka plan parlakligi 0..2
    "bg_color": "#0a0a12",   # "dark" modunda arka plan rengi
    "bg_mode": "auto",       # gorsel icin: auto | sphere (360 derece) | flat (sabit duvar kagidi)
    "shadow": 0.55,          # panel golgesi 0..1
    "glow": 0.45,            # ortam isiltisi (panel iceriginin rengi cevreye yansir) 0..1
    "vignette": 0.25,        # kenarlari karartma 0..1
    "panel_bright": 1.0,     # panel parlakligi
    "corner": 0.035,         # kose yuvarlama (panel yuksekligine oran)
    "curve": 0.0,            # panel egriligi: 0 duz, 1 = izleyici etrafinda silindir
    "focus_ring": True,      # odaktaki panelin kenarinda vurgu cizgisi
    "predict_ms": 25.0,      # baş takibi gecikme telafisi (ms): jiroskopla "isik olusacagi ana" gore ciz
    "lens_shift": 100.0,     # stereo: her gozun goruntusunu burna dogru kaydirma (piksel); merkez ayari
    "cam_shift": 0.0,        # stereo: kamera arka plani icin EK kaydirma (piksel); cift goruntu icin ince ayar
    "tex_bias": -0.5,        # panel dokusu mip kaymasi: negatif = daha keskin (biraz titrek), 0 = eski
    "sharpen": 0.3,          # panel keskinlestirme (unsharp mask) 0..1.5
}
ENV_RANGES = {"bg_bright": (0.0, 2.0), "shadow": (0.0, 1.0), "glow": (0.0, 1.0), "vignette": (0.0, 1.0),
              "panel_bright": (0.4, 1.6), "corner": (0.0, 0.12), "curve": (0.0, 1.0),
              "predict_ms": (0.0, 60.0), "lens_shift": (-300.0, 300.0), "cam_shift": (-300.0, 300.0),
              "tex_bias": (-1.5, 1.0), "sharpen": (0.0, 1.5)}
ENV_CHOICES = {"bg": ("dark", "camera", "image"), "bg_mode": ("auto", "sphere", "flat")}
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def default_panels(n):
    return [{"yaw": round((i - (n - 1) / 2) * 60.0, 1), "pitch": 0.0,
             "dist": 2.4 if n == 1 else 3.0, "scale": 1.0} for i in range(n)]


def arrangement_panels(geoms, n):
    """Windows'taki ekran yerlesimini VR'a yansit: panel i = i. ekran (0 = ana ekran). Sol ekran solda, ust ekran ustte, sag
    ekran sagda; boyutlar panellerin ust uste binmeyecegi aci hesabiyla (ana ekranin kenarindan hemen sonra) verilir."""
    import math
    if not geoms:
        return default_panels(n)
    main = geoms[0]
    mw, mh = max(1, main["width"]), max(1, main["height"])
    mcx, mcy = main["left"] + mw / 2.0, main["top"] + mh / 2.0
    D, H = 3.0, 0.9                                     # panel mesafesi (m) ve panel yuksekligi/2 (PANEL_H=1.8)
    main_half_h = math.degrees(math.atan(H * (mw / mh) / D))
    main_half_v = math.degrees(math.atan(H / D))
    out = []
    for i in range(n):
        if i >= len(geoms):                              # ekran sayisindan fazla panel (kopya): yana sirala
            out.append({"yaw": round(60.0 * (i - len(geoms) + 1), 1), "pitch": 0.0, "dist": D, "scale": 1.0})
            continue
        if i == 0:
            out.append({"yaw": 0.0, "pitch": 0.0, "dist": D, "scale": 1.0})
            continue
        g = geoms[i]
        w, h = max(1, g["width"]), max(1, g["height"])
        dx, dy = (g["left"] + w / 2.0 - mcx) / mw, (g["top"] + h / 2.0 - mcy) / mh
        scale = 0.9
        half_h = math.degrees(math.atan(H * scale * (w / h) / D))
        half_v = math.degrees(math.atan(H * scale / D))
        yaw = pitch = 0.0
        if abs(dx) > 0.3:                                # yatayda ayrik: ana ekranin yanina, ust uste binmeden
            yaw = math.copysign(main_half_h + half_h + 4.0, dx)
        if abs(dy) > 0.3:                                # dikeyde ayrik (ust = +eğim)
            pitch = math.copysign(main_half_v + half_v + 3.0, -dy)
        out.append({"yaw": round(yaw, 1), "pitch": round(pitch, 1), "dist": D, "scale": scale})
    return out


class Layout:
    def __init__(self, n, path, geoms=None):
        self.n = n
        self.path = path
        self.dir = os.path.dirname(path)
        self.bg_path = os.path.join(self.dir, "bg.jpg")
        self.lock = threading.RLock()
        self.geoms = list(geoms or [])                       # panel sirasiyla Windows ekranlari (ad, konum, boyut)
        self.sig = [g.get("name") for g in self.geoms]       # ekran cihaz listesi: degisirse kayitli duzen gecersiz
        self.panels = self._defaults()
        self.focus = True          # dinamik fokus acik mi
        self.focus_zoom = 2.0      # odaktaki panelin yakinlastirma carpani 1x..8x
        self.env = dict(DEFAULT_ENV)
        self.bg_bytes = None       # telefona gonderilen (kucultulmus) gorsel, JPEG
        self.bg_size = (0, 0)
        self.stats = {}            # canli akis istatistikleri (arayuz gosterir)
        self._load()

    # ---- kalicilik ----
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.focus = bool(d.get("focus", True))
            self.focus_zoom = max(1.0, min(8.0, float(d.get("focus_zoom", 2.0))))
            saved = d.get("panels", [])
            # Kayitli duzen yalniz ayni ekran cihazlari (ayni sira) icin gecerli; ekran listesi degistiyse (ekran eklendi/cikarildi/
            # numaralandirma degisti) Windows yerlesiminden uretilen varsayilan kullanilir. Ekranlari birkac piksel surukleme duzeni bozmaz.
            same = (d.get("sig") == self.sig) if self.sig else True
            if saved and same:
                loaded = [{k: float(p[k]) for k in KEYS} for p in saved]
                self.panels = loaded[:self.n] + self._defaults()[len(loaded):]
            self.set_env(d.get("env", {}))
        except Exception:
            pass
        try:
            if os.path.exists(self.bg_path):
                with open(self.bg_path, "rb") as f:
                    data = f.read()
                from PIL import Image
                self.bg_size = Image.open(io.BytesIO(data)).size
                self.bg_bytes = data
        except Exception:
            self.bg_bytes = None
        if self.env["bg"] == "image" and not self.bg_bytes:
            self.env["bg"] = "dark"

    def save(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with self.lock, open(self.path, "w", encoding="utf-8") as f:
                json.dump({"panels": self.panels, "focus": self.focus, "focus_zoom": self.focus_zoom,
                           "env": self.env, "sig": self.sig}, f)
        except OSError:
            pass

    # ---- telefona giden JSON ----
    def bg_aspect(self):
        w, h = self.bg_size
        return (w / h) if h else 2.0

    def to_json(self):
        with self.lock:
            env = dict(self.env)
            if env["bg"] == "image" and not self.bg_bytes:
                env["bg"] = "dark"
            mode = env["bg_mode"]
            env["bg_sphere"] = mode == "sphere" or (mode == "auto" and self.bg_aspect() >= 1.9)
            return json.dumps({"panels": self.panels, "focus": self.focus,
                               "focus_zoom": self.focus_zoom, "env": env})

    # ---- degistirme ----
    def _defaults(self):
        return arrangement_panels(self.geoms, self.n) if self.geoms else default_panels(self.n)

    def reset(self):
        self.panels = self._defaults()

    def set(self, i, key, value):
        lo, hi = LIMITS[key]
        self.panels[i][key] = max(lo, min(hi, float(value)))

    def set_env(self, updates):
        """Gecerli anahtarlari dogrulayip uygular; degisen anahtarlarin listesini dondurur."""
        changed = []
        for k, v in (updates or {}).items():
            if k in ENV_RANGES:
                lo, hi = ENV_RANGES[k]
                nv = max(lo, min(hi, float(v)))
            elif k in ENV_CHOICES:
                if v not in ENV_CHOICES[k]:
                    continue
                nv = v
            elif k == "bg_color":
                if not (isinstance(v, str) and _COLOR.match(v)):
                    continue
                nv = v.lower()
            elif k == "focus_ring":
                nv = bool(v)
            else:
                continue
            if self.env.get(k) != nv:
                self.env[k] = nv
                changed.append(k)
        return changed

    def set_bg_image(self, data):
        """Kullanicinin gorselini dogrular, kucultur, JPEG olarak saklar. (genislik, yukseklik) dondurur."""
        from PIL import Image
        if len(data) > 60 * 1024 * 1024:
            raise ValueError("gorsel cok buyuk (en fazla 60 MB)")
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
        w, h = img.size
        if w < 16 or h < 16:
            raise ValueError("gorsel cok kucuk")
        max_w = 4096 if w / h >= 1.9 else 2560      # 360 panorama icin daha genis
        if w > max_w:
            img = img.resize((max_w, round(h * max_w / w)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90, optimize=True)
        with self.lock:
            self.bg_bytes = buf.getvalue()
            self.bg_size = img.size
            self.env["bg"] = "image"
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.bg_path, "wb") as f:
                f.write(self.bg_bytes)
        except OSError:
            pass
        return img.size


    def clear_bg(self):
        """Yuklenen arka plan gorselini siler (dosya dahil); arka plan duz renge doner."""
        with self.lock:
            self.bg_bytes = None
            self.bg_size = (0, 0)
            if self.env["bg"] == "image":
                self.env["bg"] = "dark"
        try:
            os.remove(self.bg_path)
        except OSError:
            pass


class Link:
    """Telefona giden aktif baglanti (video kareleriyle ayni kilidi paylasir)."""

    def __init__(self):
        self.conn = None
        self.lock = None
        self.m = threading.Lock()

    def attach(self, conn, lock):
        with self.m:
            self.conn, self.lock = conn, lock

    def detach(self):
        with self.m:
            self.conn = self.lock = None

    @property
    def connected(self):
        return self.conn is not None

    def send(self, idx, payload):
        with self.m:
            conn, lock = self.conn, self.lock
        if conn is None:
            return False
        try:
            with lock:
                conn.sendall(struct.pack(">BBI", idx, 0, len(payload)) + payload)
            return True
        except OSError:
            return False


class Sender(threading.Thread):
    """Degisiklikleri birlestirip telefona yollar ve kaydeder."""

    def __init__(self, link, layout):
        super().__init__(daemon=True)
        self.link, self.layout = link, layout
        self.ev = threading.Event()
        self.bg = False

    def mark(self, bg=False):
        if bg:
            self.bg = True
        self.ev.set()

    def command(self, text):
        threading.Thread(target=self.link.send, args=(254, text.encode()), daemon=True).start()

    def run(self):
        while True:
            self.ev.wait()
            time.sleep(0.03)
            self.ev.clear()
            if self.bg and self.layout.bg_bytes:
                self.bg = False
                self.link.send(253, self.layout.bg_bytes)    # gorsel duzenden once gelsin
            self.link.send(255, self.layout.to_json().encode())
            self.layout.save()
