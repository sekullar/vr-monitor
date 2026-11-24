"""Uygulama surumu: tek kaynak depo kokundeki VERSION dosyasi (release.ps1 artirir; etiket = v<surum>).
Exe icinde VERSION, PyInstaller ile paketlenir (sys._MEIPASS)."""
import os
import sys


def get():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(getattr(sys, "_MEIPASS", ""), "VERSION"),
              os.path.join(here, "..", "VERSION"),
              os.path.join(os.path.dirname(sys.executable), "VERSION")):
        try:
            with open(p, encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    return "0.0.0"
