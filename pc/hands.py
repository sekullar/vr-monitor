"""VR Monitor - el takibi + lazer imlec (PC tarafi).

Akis: kamera karesi (PC kamerasi, ileride telefon kamerasi) -> MediaPipe el noktalari -> avuc merkezinden
"lazer" isini (yaw/pitch) -> telefona yolla (idx 252). Telefon isini panellerle keser (odak yakinlastirmasi
dahil gercek geometri), lazeri cizer ve isabeti geri bildirir (tur 3: panel + u,v). Isabet gelince fare
o piksele gider; basparmak+isaret parmagi degdirme = sol tus, basparmak+orta parmak = sag tus.

Sol tus "asagi" degdirmede, "yukari" ayirmada gonderilir: bir kez = tiklama, iki kez = cift tiklama
(Windows kendisi algilar), degdirip bekleme = basili tutma/surukleme.
"""
import collections
import ctypes
import json
import math
import os
import struct
import threading
import time

MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "hand_landmarker.task")

DEFAULTS = {
    "source": "off",        # off | pc | phone
    "cam": 0,               # PC kamerasi numarasi
    "mouse": False,         # gercek fareyi surer (guvenlik: her acilista kapali)
    "hand": "right",        # right | left | any
    "pad_gain": 1.5,        # kontrolcu: jiroskop acisi x bu carpan = lazer acisi
    "stab": 0.5,           # titreme engelleme 0..1 (el konumu icin olu bolge; yuksek = daha az titreme, hassas hareket azalir)
    "mode": "rel",          # palm = AVUC YONU (lazer avucun icinden, avucun baktigi yone cikar; bilek hareketi yeter)
                            # rel = goreli (fare gibi: el gorunumden cikip girince imlec yerinde kalir)
                            # abs = mutlak (elin goruntudeki yeri = lazerin yonu)
    "palm_gain": 1.6,       # avuc yonu hassasiyeti: avuc aciya x bu carpan = lazer acisi
    "po_y": 0.0, "po_p": 0.0,   # (kullanilmiyor; eski ayar dosyalari icin)
    "flip_x": True, "flip_y": True,     # kontrolcu: DS4 jiroskop isaretleri (kullanici testinde ikisi de ters cikti)
    "accel": 0.5,           # goreli modda hizlanma: yavas = hassas, hizli = genis tarama
    "phone_rot": 0,         # telefon kamerasi goruntusu yan/ters ise ek 90 derecelik donus adimi
    "cam_fov": 68.0,        # telefon kamerasinin yatay gorus acisi (derece): el yonu hesabi icin
    "gain_x": 200.0,        # elin goruntu genisligini gezmesi = kac derece (yatay)
    "gain_y": 140.0,        # dikey
    "cx": 0.5, "cy": 0.55,  # "tam karsi" demek olan el konumu (goruntude)
    "pinch_on": 0.32,       # basparmak-parmak ucu mesafesi / avuc boyu: bunun altinda "degdi"
    "pinch_off": 0.48,      # bunun ustunde "ayrildi"
    "smooth": 0.6,          # 0 = ham, 1 = cok yumusak
    "deadzone": 2.0,        # degdirirken imlecin kilitli kaldigi aci (derece)
}
RANGES = {"gain_x": (30, 400), "gain_y": (20, 300), "cx": (0, 1), "cy": (0, 1), "pinch_on": (0.1, 0.6),
          "pinch_off": (0.2, 0.9), "smooth": (0, 1), "deadzone": (0, 8), "cam": (0, 9),
          "accel": (0, 3), "phone_rot": (0, 3), "cam_fov": (40, 110), "palm_gain": (0.5, 4), "stab": (0, 1), "pad_gain": (0.3, 5),
          "po_y": (-90, 90), "po_p": (-90, 90)}
CHOICES = {"source": ("off", "pad", "pc", "phone"), "hand": ("right", "left", "any"), "mode": ("palm", "rel", "abs")}

CONN = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12),
        (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]

u32 = ctypes.windll.user32


class OneEuro:
    """One Euro filtresi: yavas harekette yumusak (titreme yok), hizli harekette gecikmesiz."""

    def __init__(self):
        self.x = None
        self.dx = 0.0
        self.t = 0.0

    @staticmethod
    def _a(fc, dt):
        return 1.0 / (1.0 + (1.0 / (2 * math.pi * fc)) / dt)

    def __call__(self, x, t, mincut, beta):
        if self.x is None:
            self.x, self.t = x, t
            return x
        dt = max(1e-3, t - self.t)
        self.t = t
        self.dx += self._a(1.0, dt) * ((x - self.x) / dt - self.dx)
        self.x += self._a(mincut + beta * abs(self.dx), dt) * (x - self.x)
        return self.x

    def reset(self):
        self.x = None


class HandControl:
    def __init__(self, link, path, geoms, real, layout=None):
        self.link, self.path, self.geoms, self.real = link, path, geoms, max(1, real)
        self.layout = layout
        self._lim = (-60.0, 60.0, -25.0, 25.0)
        self._lim_t = 0.0
        self.cfg = dict(DEFAULTS)
        self._load()
        self.cfg["mouse"] = False
        self.gen = 0
        self.seq = 0
        self.lock = threading.Lock()
        self.state = {"tracking": False, "fps": 0.0, "idx": 1.0, "mid": 1.0, "btn": "", "hit": None,
                      "error": "", "frames": 0}
        self.preview = None
        self.preview_until = 0.0
        self.events = collections.deque()     # (seq, "down"/"up", "L"/"R", t)
        self.down = set()
        self.last_up = (0.0, "", -999, -999)
        self.last_palm = (0.5, 0.5)
        self.palm_raw = (0.0, 0.0)             # avuc yonu modu: son gecerli avuc yonu (yaw, pitch; derece)
        self.pad_reset = False                 # kontrolcu: lazeri ortala istegi
        self.palm_reset = True                 # el yeniden goruntude: ofseti imlecin yerine gore yeniden hesapla
        self.palm_last = (0.0, 0.0)            # imlecin son yeri (derece)
        self.po = [0.0, 0.0]                   # avuc yonu ofseti (calisma zamani; el her gorundugunde yenilenir)
        self.rel = [0.0, 0.0]                  # goreli mod: imlecin yaw/pitch'i (el gidince de korunur)
        self.phone_q = collections.deque(maxlen=1)
        self.phone_ev = threading.Event()
        self.phone_frames = 0
        self.last_hit_t = 0.0
        self.thread = None
        self.ready = True
        self._start()

    # ---- ayarlar ----
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.apply(json.load(f), save=False)
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            d = {k: v for k, v in self.cfg.items() if k != "mouse"}
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except OSError:
            pass

    def apply(self, d, save=True):
        restart = False
        for k, v in (d or {}).items():
            if k in RANGES:
                lo, hi = RANGES[k]
                nv = max(lo, min(hi, float(v)))
                if k in ("cam", "phone_rot"):
                    nv = int(nv)
            elif k in CHOICES:
                if v not in CHOICES[k]:
                    continue
                nv = v
            elif k in ("mouse", "flip_x", "flip_y"):
                nv = bool(v)
            else:
                continue
            if self.cfg.get(k) != nv:
                self.cfg[k] = nv
                if k in ("source", "cam"):
                    restart = True
                if k == "mouse" and not nv:
                    self._release_all()
        if self.cfg["pinch_off"] < self.cfg["pinch_on"] + 0.05:
            self.cfg["pinch_off"] = self.cfg["pinch_on"] + 0.05
        if save:
            self._save()
        if restart and getattr(self, "ready", False):
            self._start()

    def command(self, cmd):
        if cmd == "center":
            if self.cfg["source"] == "pad":
                self.pad_reset = True                                  # kontrolcu: lazeri tam karsiya al
            elif self.cfg["mode"] == "palm":
                self.palm_last = (0.0, 0.0)                            # imleci tam karsiya al; su anki durus "karsi" olsun
                self.palm_reset = True
            elif self.cfg["mode"] == "rel":
                self.rel = [0.0, 0.0]               # imleci tam karsiya al
            else:
                self.cfg["cx"], self.cfg["cy"] = self.last_palm
                self._save()
        elif cmd == "sweep":
            threading.Thread(target=self._sweep, daemon=True).start()

    def _sweep(self):
        """Lazer testi (el gerekmez): isin panellerin uzerinde soldan saga yatay, sonra dikey gezer."""
        t0 = time.perf_counter()
        try:
            while True:
                t = time.perf_counter() - t0
                if t > 6.0:
                    break
                yaw = -75 + 150 * (t / 6.0)
                pit = 18 * math.sin(t * 2.2)
                self._send_ray(True, False, yaw, pit)
                time.sleep(1 / 30)
        finally:
            self._send_ray(False, False, 0, 0)

    def snapshot(self):
        s = dict(self.state)
        s["cfg"] = dict(self.cfg)
        s["geoms"] = len(self.geoms)
        return s

    def _dbg(self, msg):
        """Olay kaydi (el goruldu/kayboldu, reddedilen ziplama, tus degisimi): kontrolden cikma sebebini bulmak icin."""
        try:
            p = os.path.join(os.path.dirname(self.path), "hand_debug.log")
            if os.path.exists(p) and os.path.getsize(p) > 300_000 and not getattr(self, "_dbg_trim", False):
                self._dbg_trim = True
                open(p, "w").close()
            with open(p, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S") + " " + msg + "\n")
        except OSError:
            pass

    def limits(self):
        """Lazerin gidebilecegi aci araligi = panellerin kapladigi alan (yaw_min, yaw_max, pitch_min, pitch_max).
        Lazer bunun disina cikmaz: gurultulu olcum imleci panelden dusurup 'hic hareket etmiyor' yapmasin."""
        now = time.time()
        if self.layout is None or now - self._lim_t < 0.5:
            return self._lim
        self._lim_t = now
        try:
            with self.layout.lock:
                panels = [dict(p) for p in self.layout.panels]
            y0 = p0 = 1e9
            y1 = p1 = -1e9
            zoom = max(1.0, float(self.layout.focus_zoom)) if self.layout.focus else 1.0   # odaktaki panel yakinlasir
            for i, p in enumerate(panels):
                g = self.geoms[i % self.real]
                asp = g["width"] / max(1, g["height"])
                d = max(0.5, p["dist"])
                hw, hh = 0.9 * p["scale"] * asp, 0.9 * p["scale"]
                ya, pa = math.degrees(math.atan2(hw, d / zoom)), math.degrees(math.atan2(hh, d / zoom))
                # isin baslangici 12 cm asagida (lazer gorunsun diye): panelin ust kenarina ulasmak icin ek aci
                up = math.degrees(math.atan2(0.12, d / zoom))
                y0, y1 = min(y0, p["yaw"] - ya), max(y1, p["yaw"] + ya)
                p0, p1 = min(p0, p["pitch"] - pa), max(p1, p["pitch"] + pa + up)
            if y0 < y1:
                # kenarlardan 2-3 derece tasma payi: ekranin tam kenarina/kosesine erisilsin (disari tasarsa imlec kenarda kalir)
                self._lim = (y0 - 2.0, y1 + 2.0, p0 - 2.0, p1 + 3.0)
        except Exception:
            pass
        return self._lim

    # ---- fare ----
    def _btn(self, b, down):
        f = {"L": (0x2, 0x4), "R": (0x8, 0x10)}[b][0 if down else 1]
        u32.mouse_event(f, 0, 0, 0, 0)

    def _release_all(self):
        for b in list(self.down):
            self._btn(b, False)
        self.down.clear()

    def on_hit(self, seq, panel, u, v):
        """Telefon: ray #seq panel/u/v noktasina isabet etti (panel 255 = hicbir yere)."""
        self.last_hit_t = time.time()
        pos = None
        if panel != 255 and u is not None:
            g = self.geoms[panel % self.real] if self.geoms else None
            if g:
                x = int(min(g["width"] - 1, max(0, u * g["width"]))) + g["left"]
                y = int(min(g["height"] - 1, max(0, v * g["height"]))) + g["top"]
                pos = (x, y)
        self.state["hit"] = None if pos is None else [panel, round(u, 3), round(v, 3)]
        mouse = self.cfg["mouse"]
        # bu isa ve oncesine bagli tiklama olaylari
        while self.events and ((seq - self.events[0][0]) & 0xFFFF) < 0x8000:
            _, kind, b, t = self.events.popleft()
            if not mouse:
                continue
            if kind == "down":
                if pos is None:
                    continue                      # bos yere degdirme: tiklama yok
                x, y = pos
                lt, lb, lx, ly = self.last_up
                if lb == b and time.time() - lt < 0.6 and abs(x - lx) < 40 and abs(y - ly) < 40:
                    x, y = lx, ly                 # cift tiklama: elin titremesi Windows'un 4 px sinirini asmasin
                u32.SetCursorPos(x, y)
                pos = (x, y)
                self._btn(b, True)
                self.down.add(b)
            elif b in self.down:
                if pos is not None:
                    u32.SetCursorPos(*pos)
                self._btn(b, False)
                self.down.discard(b)
                p = pos or (0, 0)
                self.last_up = (time.time(), b, p[0], p[1])
        if mouse and pos is not None:
            u32.SetCursorPos(*pos)

    # ---- takip dongusu ----
    def _start(self):
        self.gen += 1
        gen = self.gen
        self._release_all()
        self.state.update(tracking=False, error="", hit=None)
        self.on_connect()
        if self.cfg["source"] == "off":
            return
        target = self._run_pad if self.cfg["source"] == "pad" else self._run
        self.thread = threading.Thread(target=target, args=(gen,), daemon=True)
        self.thread.start()

    def restart(self):
        """Kontrol motorunu (kamera/kontrolcu takibi) durdurup bastan baslatir; yayina dokunmaz."""
        self.state.update(error="", hit=None)
        self._start()
        self._dbg("motor yeniden baslatildi (arayuz)")

    def on_connect(self):
        """Telefon baglaninca / kaynak degisince: telefon kamerasi kare gondersin mi?"""
        self.link.send(254, b"handcam:1" if self.cfg["source"] == "phone" else b"handcam:0")

    def on_phone_frame(self, payload):
        """Telefondan: [donus:1][R: 9 x float32][JPEG]. donus = goruntuyu dik yapmak icin saat yonunde 90'lik adim;
        R = karenin cekildigi andaki bas yonelimi (gozden dunyaya, satir-sirali 3x3): kafa donusunu elden ayirmak icin."""
        if len(payload) < 140:
            return
        self.phone_frames += 1
        R = struct.unpack(">9f", payload[1:37])
        self.phone_q.append((payload[0] & 3, R, bytes(payload[37:])))
        self.phone_ev.set()

    def _send_ray(self, valid, pressed, yaw, pitch):
        self.seq = (self.seq + 1) & 0xFFFF
        if valid:
            ya, pi = math.radians(yaw), math.radians(pitch)
            d = (math.sin(ya) * math.cos(pi), math.sin(pi), -math.cos(ya) * math.cos(pi))
            o = (d[0] * 0.45, d[1] * 0.45 - 0.12, d[2] * 0.45)
        else:
            d = (0.0, 0.0, -1.0)
            o = (0.0, 0.0, 0.0)
        # isin her zaman dunya uzayinda (telefon kamerasinda da: kafa donusu karede cikarildi)
        flags = (1 if valid else 0) | (2 if pressed else 0)
        self.link.send(252, struct.pack(">BH6f", flags, self.seq, *o, *d))
        return self.seq

    def _run_pad(self, gen):
        """DualShock 4: jiroskopla nisan (fare gibi, kontrolcu cevrilince lazer doner), tetik = tik.
        R2/Capraz/Touchpad = sol tik, L2/Daire = sag tik, L1 basili = imleci dondur (kontrolcuyu rahat yere getir),
        Share = imleci tam karsiya al, sag cubuk = kaydirma."""
        try:
            import pad as padmod
        except Exception as e:
            self.state["error"] = f"kontrolcu modulu yuklenemedi: {str(e)[:80]}"
            return
        if not padmod.available():
            self.state["error"] = "hidapi kurulu degil (pip install hidapi)"
            return
        ds = padmod.DS4()
        pos = [0.0, 0.0]                       # lazerin yonu (yaw, pitch; derece): jiroskop hizinin integrali
        bias = None
        win = collections.deque()
        chk = 0
        bias_ok = False
        last_ts = None
        grav = None                           # yercekimi yonu (kontrolcu ekseninde, birim vektor)
        hist = collections.deque()
        anchor = None
        btn = None
        last_t = time.perf_counter()
        last_send = 0.0
        last_open = 0.0
        scroll_acc = 0.0
        share_prev = False
        n_rep, n_t = 0, time.perf_counter()
        self.state["pad"] = ""
        try:
            while self.gen == gen:
                now = time.perf_counter()
                if not ds.connected:
                    self.state.update(tracking=False, btn="", pad="")
                    if now - last_open > 1.0:
                        last_open = now
                        if not ds.open():
                            self.state["error"] = "Kontrolcu bulunamadi (USB ile tak ya da Bluetooth ile eslestir; Steam/DS4Windows kapali olsun)"
                            self._send_ray(False, False, 0, 0)
                        else:
                            self.state["error"] = ""
                            bias = None
                            bias_ok = False
                            win.clear()
                            last_ts = None
                            self._dbg("kontrolcu baglandi")
                    else:
                        time.sleep(0.05)
                    continue
                p = ds.read(100)
                if p is None:
                    if not ds.connected:
                        self._release_all()
                        self._dbg("kontrolcu koptu")
                    continue
                now = time.perf_counter()
                # sure: kontrolcunun kendi zaman damgasi (Bluetooth raporlari toplu gelir; PC saati yanilir)
                tsd = ((p["ts"] - last_ts) & 0xFFFF) * 5.333e-6 if last_ts is not None else 0.0
                last_ts = p["ts"]
                last_t = now
                if tsd <= 0.0:
                    continue                          # ayni ornek tekrar geldi: bir sey yapma
                dt = min(0.05, tsd)
                n_rep += 1
                if now - n_t >= 1.0:
                    self.state["fps"] = round(n_rep / (now - n_t), 1)
                    n_rep, n_t = 0, now
                self.state.update(tracking=True, pad="Bluetooth" if ds.bt else "USB", error="")

                raw = (p["gx"], p["gy"], p["gz"])
                if bias is None:
                    bias = [float(v) for v in raw]
                # sapmayi (bias) durgunken ogren. "Durgun" = son ~0.25 sn'de HAM deger neredeyse degismemis (sapmaya gore
                # degil: ilk tahmin yanlissa sapmaya gore olcum hic durgun saymaz ve lazer hep ayni yone kayar).
                win.append((now, raw))
                while win and now - win[0][0] > 0.5:
                    win.popleft()
                chk += 1
                if chk >= 25 and now - win[0][0] >= 0.4:        # son 0.5 sn dolu; her ~25 raporda bir bak (CPU)
                    chk = 0
                    vals = [w[1] for w in win]
                    rng = max(max(v[i] for v in vals) - min(v[i] for v in vals) for i in range(3))
                    # durgun = 0.5 sn boyunca ham deger neredeyse hic degismemis (~1.2 derece/sn). Yavas ama surekli bir
                    # suprme (ornegin 20 derece/sn) durgun SAYILMAZ: yoksa sapma diye ogrenilir ve lazeri geri iter.
                    if rng < 20 and not (p["r2"] > 30 or p["l2"] > 30 or p["cross"] or p["circle"] or p["touch"]):
                        m = [sum(v[i] for v in vals) / len(vals) for i in range(3)]
                        dev = max(abs(m[i] - bias[i]) for i in range(3))
                        if not bias_ok:                        # ilk ogrenme: hangi sapma olursa olsun al
                            bias = m
                            bias_ok = True
                        elif dev < 240:                        # sonra yalniz kucuk duzeltmeler (<15 derece/sn)
                            k = min(1.0, 0.5 * 1.5)            # ~0.5 sn'lik pencere basina yumusak adim
                            for i in range(3):
                                bias[i] += k * 0.5 * (m[i] - bias[i])
                # ELDE TUTARKEN de sapma takibi: kucuk (<6 derece/sn) ve uzun sureli hiz sapma sayilir (isinma ile degisir).
                # Cok yavas (~8 sn zaman sabiti): bilincli yavas bir suprmeyi yutmasin.
                if bias_ok and dt > 0:
                    dv = max(abs(raw[i] - bias[i]) for i in range(3))
                    if dv < 96:
                        kk = min(0.05, dt / 8.0)
                        for i in range(3):
                            bias[i] += kk * (raw[i] - bias[i])
                gx = (raw[0] - bias[0]) / 16.0       # kontrolcu ekseni X (yanal), derece/sn
                gy = (raw[1] - bias[1]) / 16.0       # Y (yuz normali)
                gz = (raw[2] - bias[2]) / 16.0       # Z (ileri/geri)
                # YERCEKIMI: ivmeolcer "yukari" yonu verir (durgunken +yukari). Kontrolcu nasil tutulursa tutulsun
                # sag-sol donus = yukari eksen etrafindaki donus, yukari-asagi = yanal eksen etrafindaki donus.
                am = math.sqrt(p["ax"] ** 2 + p["ay"] ** 2 + p["az"] ** 2)
                if am > 1.0:
                    anx, any_, anz = p["ax"] / am, p["ay"] / am, p["az"] / am
                    if grav is None:
                        grav = [anx, any_, anz]
                    elif 0.75 * 8192 < am < 1.25 * 8192:             # yalniz ~1 g iken (hizlanma yokken) guncelle
                        for i, v in enumerate((anx, any_, anz)):
                            grav[i] += 0.004 * (v - grav[i])
                    gl = math.sqrt(sum(v * v for v in grav)) or 1.0
                    ux, uy, uz = grav[0] / gl, grav[1] / gl, grav[2] / gl
                else:
                    ux, uy, uz = 0.0, 1.0, 0.0
                yaw_rate = -(gx * ux + gy * uy + gz * uz)             # yukari ekseni etrafinda (saga donus = +)
                pit_rate = gx * uy - gy * ux                          # yanal eksen = ileri(-z) x yukari
                # yumuslak olu bolge: ~1.2 derece/sn'nin altindaki hiz (jiroskop sapmasi, el titremesi) yok sayilir,
                # ustu esigi cikararak akar (esikte ziplama olmaz). Sapma kucukse lazer artik kaymaz.
                yaw_rate = math.copysign(max(0.0, abs(yaw_rate) - 1.2), yaw_rate)
                pit_rate = math.copysign(max(0.0, abs(pit_rate) - 1.2), pit_rate)

                self.state["gyro"] = [round(yaw_rate, 1), round(pit_rate, 1), round(pos[0], 1), round(pos[1], 1),
                                      round(ux, 2), round(uy, 2), round(uz, 2)]   # hata ayiklama (hizlar, konum, yukari yonu)
                clutch = p["l1"]
                if not clutch:
                    g = self.cfg["pad_gain"]
                    sx = -1.0 if self.cfg["flip_x"] else 1.0
                    sy = -1.0 if self.cfg["flip_y"] else 1.0
                    pos[0] += sx * yaw_rate * dt * g       # saga cevirince lazer saga
                    pos[1] += sy * pit_rate * dt * g       # one-yukari kaldirinca lazer yukari
                ly0, ly1, lp0, lp1 = self.limits()
                pos[0] = max(ly0, min(ly1, pos[0]))
                pos[1] = max(lp0, min(lp1, pos[1]))
                if (p["share"] and not share_prev) or self.pad_reset:
                    pos[0] = pos[1] = 0.0
                    self.pad_reset = False
                share_prev = p["share"]

                hist.append((now, pos[0], pos[1]))
                while hist and now - hist[0][0] > 0.4:
                    hist.popleft()
                want = "L" if (p["r2"] > 120 or p["cross"] or p["touch"]) else ("R" if (p["l2"] > 120 or p["circle"]) else None)
                if want is not None and btn is None:
                    back = next((h for h in hist if h[0] >= now - 0.06), hist[-1])      # tetige basarken kontrolcu sarsilir: geri sar
                    anchor = (back[1], back[2])
                if want is None:
                    anchor = None
                ay, ap = pos
                if anchor is not None:
                    dy, dp = pos[0] - anchor[0], pos[1] - anchor[1]
                    dist = math.hypot(dy, dp)
                    dz = self.cfg["deadzone"]
                    if dist <= dz:
                        ay, ap = anchor
                    else:
                        kk = (dist - dz) / dist
                        ay, ap = anchor[0] + dy * kk, anchor[1] + dp * kk

                edge = want != btn
                if edge or now - last_send >= 0.01:        # ~100 Hz isin (tus degisiminde hemen)
                    seq = self._send_ray(True, want is not None, ay, ap)
                    last_send = now
                    if edge:
                        if btn is not None:
                            self.events.append((seq, "up", btn, now))
                        if want is not None:
                            self.events.append((seq, "down", want, now))
                        btn = want
                        self.state["btn"] = btn or ""
                # telefon yanit vermezse eski olaylari temizle; basili tus takili kalmasin
                while self.events and now - self.events[0][3] > 0.5:
                    _, kind, b, _t = self.events.popleft()
                    if kind == "up" and b in self.down:
                        self._btn(b, False)
                        self.down.discard(b)
                # sag cubuk: kaydirma
                v = (p["ry"] - 128) / 128.0
                if abs(v) > 0.25 and self.cfg["mouse"] and self.state.get("hit"):
                    scroll_acc += -v * dt * 14.0           # yukari = yukari kaydir (saniyede en fazla ~14 tik)
                    n = int(scroll_acc)
                    if n:
                        scroll_acc -= n
                        u32.mouse_event(0x0800, 0, 0, 120 * n, 0)
                else:
                    scroll_acc = 0.0
        except Exception as e:
            self.state["error"] = f"kontrolcu hatasi: {type(e).__name__}: {str(e)[:80]}"
        finally:
            ds.close()
            self._release_all()
            self.state["tracking"] = False
            self.state["pad"] = ""

    def _run(self, gen):
        try:
            import cv2
            import numpy as np
            import mediapipe as mp
            from mediapipe.tasks.python import vision, BaseOptions
        except Exception as e:
            self.state["error"] = f"mediapipe yuklenemedi: {str(e)[:80]}"
            return
        try:
            opts = vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=MODEL),
                running_mode=vision.RunningMode.VIDEO, num_hands=2,
                min_hand_detection_confidence=0.35, min_hand_presence_confidence=0.35,
                min_tracking_confidence=0.35)
            lm = vision.HandLandmarker.create_from_options(opts)
        except Exception as e:
            self.state["error"] = f"el modeli acilamadi: {str(e)[:80]}"
            return
        src = self.cfg["source"]
        cap = None
        if src == "pc":
            cap = cv2.VideoCapture(int(self.cfg["cam"]), cv2.CAP_DSHOW)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)      # olcum: varsayilanda 10 fps, bununla ~20 fps (loş ortam)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                self.state["error"] = "PC kamerasi acilamadi"
                return

        fy, fx = OneEuro(), OneEuro()
        hist = collections.deque()
        btn = None
        anchor = None
        ts, last_ts = 0, time.perf_counter()
        fps_n, fps_t = 0, time.perf_counter()
        last_idle_send = 0.0
        was = False
        prev_p, prev_t, prev_w = None, 0.0, None
        hp = None
        glitch_n = pinch_n = 0
        acq_t = last_seen = 0.0
        last_ray = None
        rhist = collections.deque(maxlen=60)       # goreli mod: son ~2 sn imlec konumlari (el inince geri almak icin)
        try:
            while self.gen == gen:
                if src == "pc":
                    tr = time.perf_counter()
                    ok, frame = cap.read()
                    rd = (time.perf_counter() - tr) * 1000
                    self.state["read_ms"] = round(self.state.get("read_ms", rd) * 0.9 + rd * 0.1, 1)
                    if not ok:
                        time.sleep(0.01)
                        continue
                else:
                    if not self.phone_ev.wait(0.5):
                        self.state["tracking"] = False
                        if self.link.connected:
                            self.state["error"] = "Telefondan kamera karesi gelmiyor (kamera izni verildi mi?)"
                        else:
                            self.state["error"] = "Telefon bagli degil"
                        continue
                    self.phone_ev.clear()
                    try:
                        rot, Rm, jpg = self.phone_q.pop()
                    except IndexError:
                        continue
                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    self.state["error"] = ""
                    k = (rot + int(self.cfg["phone_rot"])) & 3
                    if k:
                        frame = cv2.rotate(frame, (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180,
                                                   cv2.ROTATE_90_COUNTERCLOCKWISE)[k - 1])
                mirror = src == "pc"
                if mirror:                        # PC kamerasi: ayna (sag el sagda gorunsun, MediaPipe etiketleri dogru olsun)
                    frame = cv2.flip(frame, 1)
                now = time.perf_counter()
                ts = max(ts + 1, int(now * 1000))
                res = lm.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), ts)
                inf_ms = (time.perf_counter() - now) * 1000
                self.state["infer_ms"] = round(self.state.get("infer_ms", inf_ms) * 0.9 + inf_ms * 0.1, 1)

                pick = None
                want = self.cfg["hand"]
                for i, hd in enumerate(res.handedness):
                    if hd[0].score < 0.6:         # dusuk guvenli tespit (el sanilan nesne) kullanilmaz
                        continue
                    label = hd[0].category_name.lower()
                    if not mirror:                # ayna olmayan goruntude MediaPipe sag/sol'u ters etiketler
                        label = "left" if label == "right" else "right"
                    if want != "any" and label != want:
                        continue
                    pick = i
                    pick_label = label
                    break
                fps_n += 1
                if now - fps_t >= 1.0:
                    self.state["fps"] = round(fps_n / (now - fps_t), 1)
                    fps_n, fps_t = 0, now

                if pick is None and was and now - last_seen < 0.3:
                    # kisa tespit kaybi (1-5 kare; loş ortam/bulanik): el gitmis sayma, son durumu koru (tus basili kalir,
                    # filtreler ve imlec yerinde). Telefon isin 0.4 sn gecerli sayar: ayni isini tekrar yolla.
                    if last_ray is not None:
                        self._send_ray(True, last_ray[2], last_ray[0], last_ray[1])
                    self._preview(cv2, frame, None)
                    continue
                if pick is None:
                    if was or now - last_idle_send > 0.25:
                        self._send_ray(False, False, 0, 0)
                        last_idle_send = now
                    if was:
                        self._release_all()
                        self.events.clear()
                        btn = None
                        anchor = None
                        fy.reset()
                        fx.reset()
                        hist.clear()
                    if was:
                        last_ray = None
                        self._dbg("el kayboldu")
                        # elini indirirken imlec de asagi gitmisti: kayiptan ~0.4 sn onceki konuma geri al (tutma/birakma gibi)
                        if self.cfg["mode"] == "rel" and rhist:
                            old = [h for h in rhist if h[0] <= now - 0.4]
                            back = old[-1] if old else rhist[0]
                            self.rel = [back[1], back[2]]
                        rhist.clear()
                    prev_p = prev_w = hp = None   # goreli mod: el geri gelince imlec yerinde kalsin, ziplamasin
                    self.palm_reset = True
                    was = False
                    self.state.update(tracking=False, btn="")
                    self._preview(cv2, frame, None)
                    continue

                was = True
                last_seen = now
                pts = res.hand_landmarks[pick]
                w = res.hand_world_landmarks[pick]
                px = sum(pts[k].x for k in (0, 5, 9, 13, 17)) / 5
                py = sum(pts[k].y for k in (0, 5, 9, 13, 17)) / 5
                self.last_palm = (px, py)
                # titreme engelleme: avuc konumu, avuc boyunun kucuk bir kesrinden fazla oynamadikca yerinde kalir
                # (yumusak esik: esigi asinca ziplamadan akar). Landmark gurultusu imleci titretmesin.
                pl = math.hypot(pts[0].x - pts[9].x, pts[0].y - pts[9].y)
                dzp = self.cfg["stab"] * 0.10 * pl
                if hp is None:
                    hp = [px, py]
                    glitch_n = 0
                    acq_t = now                    # el (yeniden) goruldu: ilk ~0.5 sn imleci/tiki etkilemez (el girerken olcum kararsiz)
                    self._dbg("el goruldu")
                else:
                    # tek karelik olcum hatasi: el bir karede avuc boyunun 0.7 kati kadar ziplayamaz (~3 m/s)
                    jump = math.hypot(px - hp[0], py - hp[1])      # goruntu genisligi cinsinden (avuc boyu degil: el yandan kisalir)
                    if jump > 0.3 and glitch_n < 3:
                        glitch_n += 1
                        self._dbg(f"ziplama reddedildi ({jump:.2f} goruntu genisligi), skor={res.handedness[pick][0].score:.2f}")
                        px, py = hp
                    elif jump > 0.3:               # 3 kare ust uste ayni yerde: gercek hizli hareket, kabul et (imlec ziplamasin)
                        glitch_n = 0
                        hp = [px, py]
                        prev_p = prev_w = None
                    else:
                        glitch_n = 0
                    sdx, sdy = px - hp[0], py - hp[1]
                    sd = math.hypot(sdx, sdy)
                    if sd > dzp:
                        sk = (sd - dzp) / sd
                        hp[0] += sdx * sk
                        hp[1] += sdy * sk
                px, py = hp
                W = np.array([[p.x, p.y, p.z] for p in w])
                palm = max(1e-4, float(np.linalg.norm(W[0] - W[9])))
                r_idx = float(np.linalg.norm(W[4] - W[8])) / palm
                r_mid = float(np.linalg.norm(W[4] - W[12])) / palm

                # durum makinesi (histerezisli)
                on, off = self.cfg["pinch_on"], self.cfg["pinch_off"]
                if btn is None:
                    new = "L" if (r_idx < on and r_idx <= r_mid) else ("R" if r_mid < on else None)
                else:
                    new = btn if (r_idx if btn == "L" else r_mid) < off else None
                # el yeni goruldukten sonraki 0.5 sn icinde degdirme sayilmaz (gevsek inen elde parmaklar birbirine yakin)
                if btn is None and now - acq_t < 0.5:
                    new = None
                # degdirme 2 ust uste karede dogrulanmadan basilmaz (tek karelik olcum gurultusu sahte tik uretmesin)
                if btn is None:
                    if new is not None:
                        pinch_n += 1
                        if pinch_n < 2:
                            new = None
                    else:
                        pinch_n = 0
                else:
                    pinch_n = 0

                # hedef acisi (yumusatilmis); degdirme aninda elin kaymasini geri sararak kilitle
                sm = self.cfg["smooth"]
                mincut = 0.4 + 5.0 * (1 - sm) ** 2
                gx, gy = self.cfg["gain_x"], self.cfg["gain_y"]
                if self.cfg["mode"] == "palm":
                    # AVUC YONU: lazer avucun icinden, avucun baktigi yone cikar (bilek hareketi yeter, kol dinlenir).
                    # Avuc normali, 3B el noktalarindan (kamera ekseni: x sag, y asagi, z kameradan uzaga).
                    mincut = 0.3 + 3.0 * (1 - sm) ** 2           # normal gurultulu: daha guclu yumusatma
                    upd = False
                    n1 = np.cross(W[5] - W[17], W[9] - W[0])
                    n2 = np.cross(W[5] - W[0], W[17] - W[0])
                    l1, l2 = float(np.linalg.norm(n1)), float(np.linalg.norm(n2))
                    if l1 > 1e-7 and l2 > 1e-7:
                        n = n1 / l1 + n2 / l2
                        ln = float(np.linalg.norm(n))
                        if ln > 1e-6:
                            n = n / ln
                            if src == "pc":       # kamera karsida: kamera z'si (kullaniciya dogru) = dunya +z (geriye)
                                dwp = np.array([n[0], -n[1], n[2]])
                            else:                 # telefon kamerasi ileri bakar: kafa uzayi, sonra baş yonuyle dunyaya
                                dwp = np.array(Rm).reshape(3, 3) @ np.array([n[0], -n[1], -n[2]])
                            # normalin isareti (avuc ici mi, el sirti mi) sag/sol etiketine ve aynaya bagli ve guvenilmez:
                            # elin PANELLERE bakan yuzunun normalini al (dunya -z yonu = ileri)
                            if dwp[2] > 0:
                                dwp = -dwp
                            if -dwp[2] > 0.15:    # el ileri (panellere) bakiyorsa guncelle, degilse son yonde tut
                                self.palm_raw = (math.degrees(math.atan2(dwp[0], -dwp[2])),
                                                 math.degrees(math.atan2(dwp[1], math.hypot(dwp[0], dwp[2]))))
                                upd = True
                    pg = self.cfg["palm_gain"]
                    if self.palm_reset and upd:
                        # el (yeniden) gorundu: bu durus, imlecin SU ANKI yerine denk gelsin (sicrama yok, kalibrasyon yok)
                        self.po = [self.palm_raw[0] - self.palm_last[0] / pg, self.palm_raw[1] - self.palm_last[1] / pg]
                        self.palm_reset = False
                    sx = -1.0 if self.cfg["flip_x"] else 1.0
                    sy = -1.0 if self.cfg["flip_y"] else 1.0
                    ly0, ly1, lp0, lp1 = self.limits()
                    raw_y = sx * (self.palm_raw[0] - self.po[0]) * pg
                    raw_p = sy * (self.palm_raw[1] - self.po[1]) * pg
                    tgt_y = max(ly0, min(ly1, raw_y))
                    tgt_p = max(lp0, min(lp1, raw_p))
                    # sinira dayaninca "birikme" olmasin: avuc geri donunce lazer hemen geri gelsin
                    self.po[0] += sx * (raw_y - tgt_y) / pg
                    self.po[1] += sy * (raw_p - tgt_p) / pg
                    self.palm_last = (tgt_y, tgt_p)
                    self.state["aim"] = [round(self.palm_raw[0], 1), round(self.palm_raw[1], 1)]
                elif src == "phone":
                    # elin DUNYADAKI yonu: kamera goruntusundeki konum -> kafa uzayi yonu -> karenin cekildigi andaki
                    # bas yonelimiyle dunyaya. Kafa donse bile el sabitse yon degismez (imlec kafayla kaymaz).
                    fovd = self.cfg["cam_fov"]
                    th = math.tan(math.radians(fovd) / 2)
                    Hh, Ww = frame.shape[:2]
                    tv = th * Hh / Ww
                    dh = np.array([(2 * px - 1) * th, -(2 * py - 1) * tv, -1.0])
                    dw = np.array(Rm).reshape(3, 3) @ dh
                    wyaw = math.degrees(math.atan2(dw[0], -dw[2]))
                    wpit = math.degrees(math.atan2(dw[1], math.hypot(dw[0], dw[2])))
                    vfovd = math.degrees(2 * math.atan(tv))
                    if self.cfg["mode"] == "abs":
                        tgt_y, tgt_p = wyaw, wpit
                    else:
                        if prev_w is None:
                            prev_w, prev_t = (wyaw, wpit), now
                        ddy = (wyaw - prev_w[0] + 180.0) % 360.0 - 180.0
                        ddp = wpit - prev_w[1]
                        speed = math.hypot(ddy / fovd, ddp / vfovd) / max(1e-3, now - prev_t)
                        acc = 1.0 + self.cfg["accel"] * min(1.5, speed / 0.8)
                        prev_w, prev_t = (wyaw, wpit), now
                        if now - acq_t < 0.15:
                            ddy = ddp = 0.0
                        ly0, ly1, lp0, lp1 = self.limits()
                        self.rel[0] = max(ly0, min(ly1, self.rel[0] + ddy * gx / fovd * acc))
                        self.rel[1] = max(lp0, min(lp1, self.rel[1] + ddp * gy / vfovd * acc))
                        rhist.append((now, self.rel[0], self.rel[1]))
                        tgt_y, tgt_p = self.rel
                elif self.cfg["mode"] == "abs":
                    tgt_y, tgt_p = (px - self.cfg["cx"]) * gx, -(py - self.cfg["cy"]) * gy
                else:
                    # goreli: avucun hareketi imleci surer (fare gibi); yavas = hassas, hizli = genis tarama
                    if prev_p is None:
                        prev_p, prev_t = (px, py), now
                    ddx, ddy = px - prev_p[0], py - prev_p[1]
                    speed = math.hypot(ddx, ddy) / max(1e-3, now - prev_t)     # goruntu genisligi / sn
                    acc = 1.0 + self.cfg["accel"] * min(1.5, speed / 0.8)
                    prev_p, prev_t = (px, py), now
                    if now - acq_t < 0.15:
                        ddx = ddy = 0.0                       # el girerken ilk kareler: kararsiz, imleci oynatma
                    ly0, ly1, lp0, lp1 = self.limits()        # imlec panel alanindan cikmaz (yoksa geri gelince hic isabet olmaz)
                    self.rel[0] = max(ly0, min(ly1, self.rel[0] + ddx * gx * acc))
                    self.rel[1] = max(lp0, min(lp1, self.rel[1] - ddy * gy * acc))
                    rhist.append((now, self.rel[0], self.rel[1]))
                    tgt_y, tgt_p = self.rel
                yaw = fx(tgt_y, now, mincut, 0.08)
                pit = fy(tgt_p, now, mincut, 0.08)
                hist.append((now, yaw, pit))
                while hist and now - hist[0][0] > 0.6:
                    hist.popleft()
                if new is not None and btn is None:
                    back = next((h for h in hist if h[0] >= now - 0.12), hist[-1])
                    anchor = (back[1], back[2])
                if new is None:
                    anchor = None
                ay, ap = yaw, pit
                if anchor is not None:
                    dy, dp = yaw - anchor[0], pit - anchor[1]
                    dist = math.hypot(dy, dp)
                    dz = self.cfg["deadzone"]
                    if dist <= dz:
                        ay, ap = anchor
                    else:
                        k = (dist - dz) / dist
                        ay, ap = anchor[0] + dy * k, anchor[1] + dp * k

                seq = self._send_ray(True, new is not None, ay, ap)
                last_ray = (ay, ap, new is not None)
                if new != btn:
                    self._dbg(f"tus {btn}->{new} idx={r_idx:.2f} mid={r_mid:.2f} skor={res.handedness[pick][0].score:.2f}")
                    if btn is not None:
                        self.events.append((seq, "up", btn, now))
                    if new is not None:
                        self.events.append((seq, "down", new, now))
                    btn = new
                # telefon yanit vermiyorsa (isabet gelmiyor) eski olaylari temizle; basili tus asla takili kalmasin
                while self.events and now - self.events[0][3] > 0.5:
                    _, kind, b, _t = self.events.popleft()
                    if kind == "up" and b in self.down:
                        self._btn(b, False)
                        self.down.discard(b)
                self.state.update(tracking=True, idx=round(r_idx, 2), mid=round(r_mid, 2), btn=btn or "",
                                  frames=self.state["frames"] + 1)
                self._preview(cv2, frame, pts, btn)
        except Exception as e:
            self.state["error"] = f"takip hatasi: {type(e).__name__}: {str(e)[:80]}"
        finally:
            if cap is not None:
                cap.release()
            try:
                lm.close()
            except Exception:
                pass
            self._release_all()
            self.state["tracking"] = False

    def _preview(self, cv2, frame, pts, btn=None):
        if time.time() > self.preview_until:
            return
        img = cv2.resize(frame, (320, 240))
        if pts is not None:
            col = (80, 200, 255) if not btn else (60, 90, 255)
            xy = [(int(p.x * 320), int(p.y * 240)) for p in pts]
            for a, b in CONN:
                cv2.line(img, xy[a], xy[b], col, 2)
            for p in xy:
                cv2.circle(img, p, 3, (255, 255, 255), -1)
            cv2.line(img, xy[4], xy[8], (0, 255, 120) if not btn else (60, 90, 255), 2)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            self.preview = buf.tobytes()
