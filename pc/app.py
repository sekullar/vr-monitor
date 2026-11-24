"""VR Monitor - tek giris noktasi (exe).

  VRMonitor.exe            -> gozetmen penceresi: yayin + kontrol motorunu (worker) calistirir, olurse yeniden baslatir,
                              durumu gosterir; "Paneli ac", "Yeniden baslat", "Kapat" dugmeleri var. Pencere acik kalir.
  VRMonitor.exe --worker   -> yayin + motor (stream.main). Gozetmen bunu kendisi baslatir.

Ek argumanlar stream.py'ye gider (varsayilan: --usb --launch).  Gunluk: %LOCALAPPDATA%\\vr-monitor\\vr.log
"""
import os
import socket
import subprocess
import sys
import threading
import time

APPDIR = os.path.join(os.environ.get("LOCALAPPDATA", "."), "vr-monitor")
LOG = os.path.join(APPDIR, "vr.log")
EXITLOG = os.path.join(APPDIR, "exit.log")
UI_URL = "http://127.0.0.1:5050/"
CREATE_NO_WINDOW = 0x08000000


def find_adb():
    import shutil
    p = shutil.which("adb")
    if p:
        return p
    for c in (r"C:\Android\sdk\platform-tools\adb.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Android\Sdk\platform-tools\adb.exe")):
        if os.path.isfile(c):
            return c
    return "adb"


def usb_serial():
    """USB ile bagli telefonun seri numarasi (kablosuz/tls girdilerini atla): ANDROID_SERIAL icin."""
    try:
        r = subprocess.run([find_adb(), "devices"], capture_output=True, text=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        for ln in r.stdout.splitlines()[1:]:
            parts = ln.split()
            if len(parts) >= 2 and parts[1] == "device" and ":" not in parts[0] and "adb-" not in parts[0]:
                return parts[0]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- worker
def run_worker():
    os.makedirs(APPDIR, exist_ok=True)
    logp = os.environ.get("VRM_LOG", LOG)
    f = open(logp, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = f
    sys.stderr = f
    try:
        import faulthandler
        faulthandler.enable(file=f)               # yerel cokmelerin izi gunluge duser
    except Exception:
        pass
    # exe pencere-modunda calisirken adb/powershell cagrilari konsol penceresi acmasin
    _orig = subprocess.Popen.__init__

    def _init(self, *a, **kw):
        if not kw.get("creationflags"):
            kw["creationflags"] = CREATE_NO_WINDOW
        _orig(self, *a, **kw)
    subprocess.Popen.__init__ = _init
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--worker"]
    import stream
    stream.main()


# ---------------------------------------------------------------- gozetmen penceresi
def open_panel():
    import shutil
    for exe in (shutil.which("msedge"),
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={UI_URL}", "--window-size=1240,820"], creationflags=CREATE_NO_WINDOW)
                return
            except OSError:
                pass
    import webbrowser
    webbrowser.open(UI_URL)


class Supervisor:
    def __init__(self, args):
        self.args = args
        self.proc = None
        self.stop = False
        self.state = "baslatiliyor"
        self.detail = ""
        self.restarts = 0
        self.manual = False
        self.lock = threading.Lock()

    def _log(self, msg):
        try:
            with open(EXITLOG, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except OSError:
            pass

    def cmd(self):
        base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        return base + ["--worker"] + self.args

    def virtual(self, attach):
        """Sanal ekranlar yalniz program calisirken masaustunde olsun (oyunlarda kasma yapmasin); kapaninca ayrilir."""
        try:
            import displays
            if displays.auto_enabled():
                n = displays.attach_virtual(self._log) if attach else displays.detach_virtual(self._log)
                self._log(f"sanal ekranlar {'baglandi' if attach else 'ayrildi'}: {n}")
        except Exception as e:
            self._log(f"sanal ekran islemi basarisiz: {e}")

    def run(self):
        self.virtual(True)
        fast = 0
        while not self.stop:
            env = dict(os.environ, VRM_SUPERVISED="1", VRM_LOG=LOG, PYTHONUTF8="1")
            ser = usb_serial()
            if ser:
                env["ANDROID_SERIAL"] = ser
            self.state = "calisiyor"
            t0 = time.time()
            self._log(f"baslatiliyor: {' '.join(self.args)} (telefon={ser or 'yok'})")
            try:
                self.proc = subprocess.Popen(self.cmd(), env=env, creationflags=CREATE_NO_WINDOW)
                code = self.proc.wait()
            except Exception as e:
                code = -1
                self._log(f"baslatilamadi: {e}")
            up = time.time() - t0
            self._log(f"worker cikti: kod={code} calisma={up:.0f} sn")
            if self.stop:
                break
            self.restarts += 1
            self.detail = f"son kapanis: kod {code}, {up:.0f} sn sonra"
            if code == 0:
                self.detail = "program temiz kapandi"
            fast = 0 if (up >= 15 or self.manual) else fast + 1
            self.manual = False
            self.state = "yeniden baslatiliyor"
            time.sleep(min(30, 1 + 2 * fast))

    def restart(self):
        self.manual = True
        p = self.proc
        if p is not None and p.poll() is None:
            self.state = "yeniden baslatiliyor"
            p.terminate()

    def shutdown(self):
        already = self.stop
        self.stop = True
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=4)
            except Exception:
                p.kill()
        if not already:
            self.virtual(False)                  # kapanirken sanal ekranlari masaustunden ayir


def poll_status():
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(UI_URL + "api/state", timeout=1.5) as r:
            d = json.loads(r.read())
        h = d.get("hands") or {}
        s = d.get("stats") or {}
        src = (h.get("cfg") or {}).get("source", "off")
        kontrol = {"off": "kapalı", "pad": "DualShock", "pc": "PC kamerası", "phone": "Telefon kamerası"}.get(src, src)
        if src == "pad":
            kontrol += " (bağlı · " + h["pad"] + ")" if h.get("pad") else " (bulunamadı)"
        return {"ok": True, "tel": bool(d.get("connected")), "fps": s.get("fps"), "lat": s.get("lat"), "kontrol": kontrol}
    except Exception:
        return {"ok": False}


def gui(sup):
    import tkinter as tk
    from tkinter import messagebox

    BG, FG, MUTE, OK, WARN, BAD = "#0e1220", "#e8ecff", "#8b93b4", "#4cd29a", "#ffb454", "#ff6b6b"
    root = tk.Tk()
    import version
    root.title("VR Monitor v" + version.get())
    root.geometry("620x470")
    root.configure(bg=BG)
    root.minsize(520, 380)

    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=16, pady=(14, 4))
    dot = tk.Label(head, text="●", fg=WARN, bg=BG, font=("Segoe UI", 20))
    dot.pack(side="left")
    st = tk.Label(head, text="Başlatılıyor…", fg=FG, bg=BG, font=("Segoe UI", 15, "bold"))
    st.pack(side="left", padx=8)
    sub = tk.Label(root, text="", fg=MUTE, bg=BG, font=("Segoe UI", 10), anchor="w", justify="left")
    sub.pack(fill="x", padx=18)
    det = tk.Label(root, text="", fg=MUTE, bg=BG, font=("Segoe UI", 9), anchor="w")
    det.pack(fill="x", padx=18, pady=(0, 6))

    btns = tk.Frame(root, bg=BG)
    btns.pack(fill="x", padx=16, pady=6)

    def mk(text, cmd, color="#262e4d"):
        b = tk.Button(btns, text=text, command=cmd, bg=color, fg=FG, activebackground="#39446f", activeforeground=FG,
                      relief="flat", padx=14, pady=7, font=("Segoe UI", 10, "bold"), bd=0, cursor="hand2")
        b.pack(side="left", padx=(0, 8))
        return b

    def do_restart():
        sup.restart()

    def do_quit():
        if messagebox.askyesno("VR Monitor", "Yayın ve kontrol motoru kapansın mı?"):
            sup.shutdown()
            root.destroy()

    mk("Paneli aç", open_panel, "#3d6bff")
    mk("↻ Yeniden başlat", do_restart)
    mk("Günlük klasörü", lambda: os.startfile(APPDIR))
    mk("Kapat", do_quit, "#5a2a36")

    box = tk.Frame(root, bg="#0a0d18")
    box.pack(fill="both", expand=True, padx=16, pady=(8, 14))
    txt = tk.Text(box, bg="#0a0d18", fg="#aab3d6", insertbackground=FG, relief="flat", font=("Consolas", 9), wrap="none",
                  state="disabled", height=10)
    sb = tk.Scrollbar(box, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    txt.pack(side="left", fill="both", expand=True)

    status = {"v": {"ok": False}}

    def poller():
        while not sup.stop:
            status["v"] = poll_status()
            time.sleep(1.5)
    threading.Thread(target=poller, daemon=True).start()

    pos = {"p": 0}
    try:
        pos["p"] = os.path.getsize(LOG)
    except OSError:
        pass

    def tail():
        try:
            size = os.path.getsize(LOG)
            if size < pos["p"]:
                pos["p"] = 0
            if size > pos["p"]:
                with open(LOG, "rb") as f:
                    f.seek(pos["p"])
                    chunk = f.read(60000)
                    pos["p"] += len(chunk)
                s = chunk.decode("utf-8", "replace")
                txt.configure(state="normal")
                txt.insert("end", s)
                lines = int(txt.index("end-1c").split(".")[0])
                if lines > 400:
                    txt.delete("1.0", f"{lines - 400}.0")
                txt.see("end")
                txt.configure(state="disabled")
        except OSError:
            pass

    def refresh():
        v = status["v"]
        if sup.state == "yeniden baslatiliyor":
            dot.config(fg=WARN)
            st.config(text="Yeniden başlatılıyor…")
        elif v.get("ok"):
            dot.config(fg=OK)
            st.config(text="Çalışıyor")
        else:
            dot.config(fg=WARN)
            st.config(text="Başlatılıyor…")
        if v.get("ok"):
            lat = f" · gecikme {v['lat']:.0f} ms" if v.get("lat") else ""
            sub.config(text=f"Telefon: {'bağlı' if v['tel'] else 'bekleniyor'} · yayın {v.get('fps') or 0:.0f} fps{lat}"
                            f"\nKontrol: {v['kontrol']}")
        else:
            sub.config(text="Yayın başlıyor… (birkaç saniye sürebilir)\n")
        det.config(text=f"Panel: {UI_URL}   ·   yeniden başlatma sayısı: {sup.restarts}   {sup.detail}")
        tail()
        root.after(700, refresh)

    root.protocol("WM_DELETE_WINDOW", do_quit)
    root.after(300, refresh)
    # ilk acilista paneli bir kez ac (arayuz hazir olunca)

    def first_open():
        for _ in range(60):
            if status["v"].get("ok"):
                root.after(0, open_panel)
                return
            time.sleep(1)
    threading.Thread(target=first_open, daemon=True).start()
    root.mainloop()


def main():
    if "--worker" in sys.argv:
        run_worker()
        return
    os.makedirs(APPDIR, exist_ok=True)
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 3_000_000:
            os.replace(LOG, LOG + ".old")
    except OSError:
        pass
    # tek kopya: ikinci kez acilirsa yalniz paneli ac
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 50577))
    except OSError:
        open_panel()
        return
    args = [a for a in sys.argv[1:]] or ["--usb", "--launch", "--stereo"]      # gozluk (stereo) varsayilan
    if "--no-open" not in args:
        args.append("--no-open")                  # panel penceresini gozetmen acar
    sup = Supervisor(args)
    threading.Thread(target=sup.run, daemon=True).start()
    try:
        gui(sup)
    finally:
        sup.shutdown()


if __name__ == "__main__":
    main()
