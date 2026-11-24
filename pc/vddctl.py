"""Virtual Display Driver (MikeTheTech) komut borusu istemcisi.

Surucu \\\\.\\pipe\\MTTVirtualDisplayPipe uzerinden metin komutlari alir (herkese acik, yonetici gerekmez):
  GETSETTINGS, SETDISPLAYCOUNT <n>, RELOAD_DRIVER ...
Okuma hicbir zaman bloklamaz (PeekNamedPipe ile bakilir), kapatma kilitlenmez.
"""
import ctypes
import time
from ctypes import wintypes

PIPE = r"\\.\pipe\MTTVirtualDisplayPipe"
GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING = 0x80000000, 0x40000000, 3

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
INVALID = wintypes.HANDLE(-1).value


def _open():
    h = k32.CreateFileW(PIPE, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID or h is None:
        raise OSError(f"sanal ekran surucusunun borusu acilamadi (hata {ctypes.get_last_error()})")
    return h


def send(cmd, reply_timeout=3.0, encoding="utf-16le"):
    """Komutu yollar; surucudan gelen yaniti (varsa) metin olarak dondurur."""
    h = _open()
    try:
        data = cmd.encode(encoding)
        written = wintypes.DWORD(0)
        if not k32.WriteFile(h, data, len(data), ctypes.byref(written), None):
            raise OSError(f"boruya yazilamadi (hata {ctypes.get_last_error()})")
        out = b""
        t0 = time.time()
        while time.time() - t0 < reply_timeout:
            avail = wintypes.DWORD(0)
            if not k32.PeekNamedPipe(h, None, 0, None, ctypes.byref(avail), None):
                break
            if avail.value:
                buf = ctypes.create_string_buffer(avail.value)
                got = wintypes.DWORD(0)
                k32.ReadFile(h, buf, avail.value, ctypes.byref(got), None)
                out += buf.raw[:got.value]
                if len(out) > 0 and not avail.value > got.value:
                    time.sleep(0.15)       # yanit parcali gelebilir
                    k32.PeekNamedPipe(h, None, 0, None, ctypes.byref(avail), None)
                    if not avail.value:
                        break
            else:
                time.sleep(0.05)
        return out.decode(encoding, "replace")
    finally:
        k32.CloseHandle(h)


if __name__ == "__main__":
    import sys
    print(send(" ".join(sys.argv[1:]) or "GETSETTINGS"))
