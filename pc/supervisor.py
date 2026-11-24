"""Gozetmen: stream.py'yi calistirir, olurse (cokme, kilitlenme, GPU surucu sifirlama) otomatik yeniden baslatir ve
neden oldugunu (cikis kodu) kaydeder:  %LOCALAPPDATA%\\vr-monitor\\exit.log

Kullanim:  python -X faulthandler -u supervisor.py --usb --launch --no-open   (argumanlar stream.py'ye gider)
Cikis kodlari: 0 = temiz cikis (durur), 75 = program kendi yeniden baslatma istedi, digerleri = cokme/olum.
  -1073741819 (0xC0000005) erisim ihlali, -1073740791 (0xC0000409) yigin hatasi, 1 = Python istisnasi
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(os.environ.get("LOCALAPPDATA", "."), "vr-monitor", "exit.log")


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass
    print(msg, flush=True)


def virtual_displays(attach):
    """Sanal ekranlar yalniz program calisirken masaustunde olsun: baslarken bagla, kapanirken ayir (oyunlarda ekstra
    ekran kasma yapmasin). Surucuyu yeniden yuklemez; yonetici gerekmez. Arayuzden kapatilabilir (virtual.json)."""
    try:
        import displays
        if not displays.auto_enabled():
            return
        n = displays.attach_virtual(log) if attach else displays.detach_virtual(log)
        log(f"sanal ekranlar {'baglandi' if attach else 'ayrildi'}: {n}")
    except Exception as e:
        log(f"sanal ekran islemi basarisiz: {e}")


def main():
    if "--stop" in sys.argv:                              # temiz durdurma: yayin kapanir, gozetmen sanal ekranlari ayirip cikar
        try:
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:5050/api/command", data=b'{"cmd":"quit"}',
                                         headers={"X-VRM": "1", "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            print("durdurma istegi gonderildi")
        except Exception as e:
            print("durdurma istegi gonderilemedi (yayin calismiyor olabilir):", e)
        return
    import atexit
    import signal
    done = {"d": False}

    def cleanup(*_):
        if not done["d"]:
            done["d"] = True
            virtual_displays(False)
    atexit.register(cleanup)
    for sig in ("SIGTERM", "SIGBREAK"):                  # Ctrl+Break / sonlandirma: finally calissin
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    virtual_displays(True)
    args = sys.argv[1:]
    env = dict(os.environ, VRM_SUPERVISED="1")       # webui.restart_self: execv yerine 75 koduyla cik
    fast = 0
    while True:
        cmd = [sys.executable, "-X", "faulthandler", "-u", os.path.join(HERE, "stream.py")] + args
        if "--no-open" not in cmd:
            cmd.append("--no-open")
        t0 = time.time()
        log(f"baslatiliyor: {' '.join(args)}")
        try:
            code = subprocess.call(cmd, cwd=HERE, env=env)
        except KeyboardInterrupt:
            log("gozetmen: Ctrl+C, cikiliyor")
            return
        up = time.time() - t0
        log(f"stream.py cikti: kod={code} calisma={up:.0f} sn")
        if code in (0, 130):                              # 0 = temiz cikis (quit komutu): kalici dur
            log("gozetmen: temiz cikis, kapaniyor")
            return
        fast = fast + 1 if up < 15 else 0              # hemen tekrar tekrar olursa yavasla (dongu olmasin)
        time.sleep(min(30, 2 * (fast + 1)))

if __name__ == "__main__":
    main()
