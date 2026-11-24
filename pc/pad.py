"""DualShock 4 okuyucu (HID, surucu gerekmez).

DS4Windows/ViGEm kurmadan kontrolcunun jiroskopunu ve dugmelerini dogrudan okur. USB ve Bluetooth desteklenir.
Rapor duzeni (USB rapor 0x01, 64 bayt; Bluetooth rapor 0x11 ayni duzen, 2 bayt kaymis):
  [1]=LX [2]=LY [3]=RX [4]=RY [5]=yon/kare-carpi-daire-ucgen [6]=L1 R1 L2 R2 share options L3 R3 [7]=PS touchpad
  [8]=L2 analog [9]=R2 analog [13:19]=jiroskop (pitch, yaw, roll; int16) [19:25]=ivmeolcer

Not: Steam / DS4Windows kontrolcuyu "kapmis" olabilir; onlar kapaliyken calisir.
"""
import struct

try:
    import hid
except Exception:            # hidapi kurulu degil
    hid = None

SONY = 0x054C
DS4_IDS = (0x05C4, 0x09CC, 0x0BA0)      # DS4 v1, v2, USB dongle
GYRO_DPS_PER_LSB = 1.0 / 16.0           # ~ham deger / 16 = derece/sn (kalibrasyonsuz yaklasik)


def available():
    return hid is not None


def find():
    if hid is None:
        return None
    try:
        for d in hid.enumerate(SONY):
            if d["product_id"] in DS4_IDS:
                return d
    except Exception:
        pass
    return None


def parse(d):
    """Ham HID raporunu ayristirir; tanimsizsa None. Donus: sozluk (jiroskop deg/sn degil, ham int)."""
    if not d:
        return None
    rid = d[0]
    if rid == 0x01 and len(d) >= 25:
        o = 0
    elif rid == 0x11 and len(d) >= 27:
        o = 2
    else:
        return None
    b5, b6, b7 = d[o + 5], d[o + 6], d[o + 7]
    gx, gy, gz = struct.unpack_from("<hhh", bytes(d), o + 13)
    ax, ay, az = struct.unpack_from("<hhh", bytes(d), o + 19)
    return {
        "lx": d[o + 1], "ly": d[o + 2], "rx": d[o + 3], "ry": d[o + 4],
        "square": bool(b5 & 0x10), "cross": bool(b5 & 0x20), "circle": bool(b5 & 0x40), "triangle": bool(b5 & 0x80),
        "l1": bool(b6 & 0x01), "r1": bool(b6 & 0x02), "share": bool(b6 & 0x10), "options": bool(b6 & 0x20),
        "ps": bool(b7 & 0x01), "touch": bool(b7 & 0x02),
        "l2": d[o + 8], "r2": d[o + 9],
        "ts": d[o + 10] | (d[o + 11] << 8),          # kontrolcunun ornek zaman damgasi (~5.33 mikrosaniye birimi, 16 bit)
        "gx": gx, "gy": gy, "gz": gz, "ax": ax, "ay": ay, "az": az,
        "bt": rid == 0x11,
    }


class DS4:
    def __init__(self):
        self.dev = None
        self.bt = False

    @property
    def connected(self):
        return self.dev is not None

    def open(self):
        d = find()
        if d is None:
            return False
        try:
            dev = hid.device()
            dev.open_path(d["path"])
            dev.set_nonblocking(False)
            try:
                dev.get_feature_report(0x02, 37)     # Bluetooth: bunu okumak tam (0x11) rapor moduna gecirir
            except Exception:
                pass
            self.dev = dev
            return True
        except Exception:
            self.dev = None
            return False

    def read(self, timeout_ms=100):
        """Bir rapor okur ve ayristirir; zaman asimi/hata: None (hata olursa baglanti kapanir)."""
        if self.dev is None:
            return None
        try:
            d = self.dev.read(78, timeout_ms)
        except Exception:
            self.close()
            return None
        p = parse(d)
        if p:
            self.bt = p["bt"]
        return p

    def close(self):
        try:
            if self.dev is not None:
                self.dev.close()
        except Exception:
            pass
        self.dev = None
