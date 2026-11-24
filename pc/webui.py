"""VR Monitor - web arayuzu (PC tarafi).

Yalnizca bu bilgisayardan (127.0.0.1) erisilir. Sayfa ui/index.html'dir; ayarlar /api/* uzerinden
degisir ve ayni TCP baglantisiyla telefona canli yollanir.

Guvenlik: POST'lar ozel basliga (X-VRM: 1) ve gecerli Host'a baglidir; baska bir sitenin sayfasi
localhost'a istek atsa bile tarayici bu basligi on kontrolsuz gonderemez (CORS) ve reddedilir.
"""
import json
import mimetypes
import os
import shutil
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import sys
import time

import displays
import version
from core import KEYS, LIMITS

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
MAX_BODY = 64 * 1024 * 1024


def restart_self():
    """Programi ayni argumanlarla yeniden baslatir (yeni ekranlar bulunsun diye). Yeni pencere acmaz."""
    if os.environ.get("VRM_SUPERVISED"):          # gozetmen yeniden baslatir (Windows'ta execv guvenilir degil)
        os._exit(75)
    args = [sys.executable, "-u"] + sys.argv
    if "--no-open" not in args:
        args.append("--no-open")
    os.execv(sys.executable, args)


class Api:
    def __init__(self, args, layout, link, sender):
        self.args, self.layout, self.link, self.sender = args, layout, link, sender
        self.busy = ""          # uzun bir islem (ekran ekleme) sirasinda arayuze gosterilen metin
        self.log_lines = []
        self.n_real = None      # akis baslarken bulunan gercek monitor sayisi

    def _log(self, msg):
        self.log_lines = (self.log_lines + [msg])[-8:]
        self.busy = msg
        print("[ekran]", msg, flush=True)

    def set_displays(self, count):
        """Arka planda: sanal ekran sayisini ayarla, sonra yeni ekranlar gorunsun diye programi yeniden baslat."""
        if self.busy:
            return
        count = max(0, min(displays.MAX_VIRTUAL, int(count)))

        def work():
            try:
                self._log(f"Sanal ekran sayisi {count} yapiliyor...")
                ok, disp = displays.set_virtual_count(count, self._log)
                if ok:
                    self._log("Tamam, akis yeniden baslatiliyor...")
                    time.sleep(0.8)
                    restart_self()
                else:
                    self._log("Ekran sayisi degistirilemedi (surucu yanit vermedi)")
                    time.sleep(4)
            except Exception as e:  # ekran islemi hicbir zaman programi dusurmesin
                self._log(f"Hata: {str(e)[:120]}")
                time.sleep(4)
            self.busy = ""
        threading.Thread(target=work, daemon=True).start()

    def state(self):
        L, a = self.layout, self.args
        with L.lock:
            return {
                "n": L.n,
                "version": version.get(),
                "panels": L.panels,
                "focus": L.focus,
                "focus_zoom": L.focus_zoom,
                "env": L.env,
                "has_bg": bool(L.bg_bytes),
                "bg_size": list(L.bg_size),
                "connected": self.link.connected,
                "stats": L.stats,
                "displays": {"virtual": displays.configured_count(), "max": displays.MAX_VIRTUAL,
                             "panels": L.n, "busy": self.busy, "auto": displays.auto_enabled()},
                "stream": {"width": a.width, "quality": a.quality, "adapt": a.adapt, "fps": a.fps,
                           "fps_other": a.fps_other, "codec": a.codec, "bitrate": a.bitrate,
                           "encoder": a.encoder_name, "h264_ok": a.h264_ok,
                           "min_quality": a.min_quality, "max_quality": a.max_quality},
                "limits": {k: LIMITS[k] for k in KEYS},
                "hands": a.hands.snapshot() if getattr(a, "hands", None) else None,
            }

    def update(self, d):
        L, a = self.layout, self.args
        with L.lock:
            if "panels" in d:
                for i, p in enumerate(d["panels"][:L.n]):
                    for k in KEYS:
                        if k in p:
                            L.set(i, k, p[k])
            if "focus" in d:
                L.focus = bool(d["focus"])
            if "focus_zoom" in d:
                L.focus_zoom = max(1.0, min(8.0, float(d["focus_zoom"])))
            if "env" in d:
                L.set_env(d["env"])
        if "hands" in d and getattr(a, "hands", None):
            a.hands.apply(d["hands"])
        s = d.get("stream")
        if s:
            if "width" in s and int(s["width"]) in (0, 1280, 1600, 1920, 2560):
                a.width = int(s["width"])
            if "quality" in s:
                a.quality = max(30, min(95, int(s["quality"])))
            if "adapt" in s:
                a.adapt = bool(s["adapt"])
            if "fps" in s and int(s["fps"]) in (24, 30, 45, 60, 90, 120, 144):
                a.fps = int(s["fps"])
            if "fps_other" in s and int(s["fps_other"]) in (5, 10, 15, 30, 60):
                a.fps_other = int(s["fps_other"])
            if "codec" in s and s["codec"] in ("h264", "jpeg"):
                if s["codec"] == "jpeg" or a.h264_ok:
                    a.codec = s["codec"]
            if "bitrate" in s:
                a.bitrate = max(5.0, min(150.0, float(s["bitrate"])))
        self.sender.mark()

    def command(self, d):
        cmd = d.get("cmd")
        L = self.layout
        if cmd == "recenter":
            self.sender.command("recenter")
        elif cmd == "camrot":
            self.sender.command("camrot")
        elif cmd == "set_displays":
            self.set_displays(d.get("count", 1))
        elif cmd in ("hand_center", "hand_sweep") and getattr(self.args, "hands", None):
            self.args.hands.command("center" if cmd == "hand_center" else "sweep")
        elif cmd == "quit":                                  # temiz cikis (kod 0): gozetmen yeniden baslatmaz, sanal ekranlari ayirir
            def bye():
                time.sleep(0.4)                              # HTTP yaniti gitsin
                print("[ui] kapatma istegi", flush=True)
                os._exit(0)
            threading.Thread(target=bye, daemon=True).start()
        elif cmd == "virtual_auto":
            displays.set_auto(bool(d.get("on", True)))      # sanal ekranlar program ile acilip kapansin mi
        elif cmd == "restart_engine" and getattr(self.args, "hands", None):
            threading.Thread(target=self.args.hands.restart, daemon=True).start()
        elif cmd == "restart_all":
            def later():
                time.sleep(0.6)               # HTTP yaniti gitsin
                print("[ui] yayin + motor yeniden baslatiliyor", flush=True)
                restart_self()
            threading.Thread(target=later, daemon=True).start()
        elif cmd == "clear_bg":
            L.clear_bg()
            self.sender.mark()
        elif cmd == "reset_layout":
            with L.lock:
                L.reset()
            self.sender.mark()
        elif cmd == "zoom_all":
            f = float(d.get("factor", 1.0))
            with L.lock:
                for i in range(L.n):
                    L.set(i, "dist", L.panels[i]["dist"] * f)
            self.sender.mark()


def make_handler(api, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "VRMonitorUI"

        def log_message(self, *a):  # sessiz
            pass

        def _host_ok(self):
            return self.headers.get("Host", "") in allowed_hosts

        def _send(self, code, body=b"", ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode())

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"{}")
            path = urlparse(self.path).path
            if path == "/api/state":
                return self._json(api.state())
            if path == "/api/hand.jpg":
                h = getattr(api.args, "hands", None)
                if h is None:
                    return self._send(404, b"{}")
                h.preview_until = time.time() + 3.0       # onizleme yalnizca arayuz bakarken uretilir
                if not h.preview:
                    return self._send(404, b"{}")
                return self._send(200, h.preview, "image/jpeg")
            if path == "/api/bg.jpg":
                data = api.layout.bg_bytes
                if not data:
                    return self._send(404, b"{}")
                return self._send(200, data, "image/jpeg")
            if path in ("/", "/index.html"):
                path = "/index.html"
            # yalnizca ui/ klasorunden, ust dizine cikmadan
            full = os.path.normpath(os.path.join(UI_DIR, path.lstrip("/")))
            if not full.startswith(UI_DIR + os.sep) or not os.path.isfile(full):
                return self._send(404, b"{}")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-VRM") != "1":
                return self._send(403, b"{}")
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._json({"ok": False, "error": "gecersiz boyut"}, 400)
            body = self.rfile.read(n)
            path = urlparse(self.path).path
            try:
                if path == "/api/state":
                    api.update(json.loads(body))
                    return self._json({"ok": True})
                if path == "/api/command":
                    api.command(json.loads(body))
                    return self._json({"ok": True})
                if path == "/api/bgimage":
                    size = api.layout.set_bg_image(body)
                    api.sender.mark(bg=True)
                    return self._json({"ok": True, "size": list(size)})
            except Exception as e:  # kotu gorsel / JSON
                return self._json({"ok": False, "error": str(e)[:200]}, 400)
            self._send(404, b"{}")

    return Handler


def start(args, layout, link, sender, port=5050):
    api = Api(args, layout, link, sender)
    server = None
    # Once istenen portu bekleyerek dene (program yeniden baslarken eski surec portu birakir; arayuz penceresi
    # ayni adrese yeniden baglanir). Olmazsa siradaki portlari dene.
    for p in [port] * 20 + list(range(port + 1, port + 20)):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(api, p))
            port = p
            break
        except OSError:
            time.sleep(0.25 if p == port else 0)
            continue
    if server is None:
        raise RuntimeError("arayuz icin bos port bulunamadi")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}/"


def open_ui(url):
    """Mumkunse uygulama penceresi (Edge --app), degilse varsayilan tarayici."""
    for exe in (shutil.which("msedge"),
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1240,820"])
                return
            except OSError:
                pass
    webbrowser.open(url)
