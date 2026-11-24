"""Donanim H.264 kodlayici (PyAV / FFmpeg): NVENC (NVIDIA) -> AMF (AMD) -> x264 (CPU) sirasiyla dener.

Cikti Annex-B (00 00 00 01 ...) ve IDR karelerinde SPS/PPS icerir; B-kare yok, kodlayici gecikmesi ~0:
her giren kare icin hemen bir paket cikar. Telefondaki MediaCodec donanim cozucusu bunu dogrudan cozer.
"""
import os
from fractions import Fraction

import av
import numpy as np

try:
    from av.video.frame import PictureType
except Exception:  # eski PyAV
    PictureType = None

# (ad, secenekler, kodlayicinin girdi piksel bicimi)
ENCODERS = [
    ("h264_nvenc", {"preset": os.environ.get("VRM_NV_PRESET", "p3"), "tune": os.environ.get("VRM_NV_TUNE", "ll"),
                    "zerolatency": "1", "rc": "cbr", "delay": os.environ.get("VRM_NV_DELAY", "2"),   # 2: kareler kodlayici icinde boru hatti olur (~2x hiz)
                    "forced-idr": "1", "profile": "high"}, "bgr0"),
    ("h264_amf", {"usage": "ultralowlatency", "quality": "speed", "rc": "cbr", "profile": "high"}, "bgr0"),
    ("libx264", {"preset": "ultrafast", "tune": "zerolatency", "profile": "high"}, "yuv420p"),
]


class H264Encoder:
    def __init__(self, w, h, fps, bitrate_bps, prefer=None):
        self.w, self.h = w, h
        self.ctx = None
        self.name = ""
        self.pix = "bgr0"
        errors = []
        prefer = prefer or os.environ.get("VRM_ENCODER")      # olcum / elle secim: h264_amf, h264_nvenc, libx264
        order = ENCODERS if not prefer else [e for e in ENCODERS if e[0] == prefer] + \
            [e for e in ENCODERS if e[0] != prefer]
        for name, opts, pix in order:
            if name not in av.codecs_available:
                continue
            try:
                ctx = av.CodecContext.create(name, "w")
                ctx.width, ctx.height, ctx.pix_fmt = w, h, pix
                ctx.time_base = Fraction(1, 1_000_000)
                ctx.framerate = Fraction(max(1, int(fps)), 1)
                ctx.bit_rate = int(bitrate_bps)
                ctx.gop_size = max(1, int(fps)) * 4
                ctx.max_b_frames = 0
                ctx.options = dict(opts)
                ctx.open()
                self.ctx, self.name, self.pix = ctx, name, pix
                break
            except Exception as e:      # bu makinede acilmayan kodlayici: siradakini dene
                errors.append(f"{name}: {e}")
        if self.ctx is None:
            raise RuntimeError("H.264 kodlayici acilamadi: " + " | ".join(errors))

    def encode(self, arr, pts_us, force_key=False):
        """arr: (h, w, 4) uint8 BGRA, C-bitisik. [(paket_bayt, anahtar_kare_mi), ...] dondurur."""
        if self.pix == "bgr0":
            fr = av.VideoFrame(self.w, self.h, "bgr0")
            plane = fr.planes[0]
            if arr.nbytes == plane.buffer_size:
                plane.update(arr)                         # kopya, bicim donusumu yok (en hizli yol)
            else:
                fr = av.VideoFrame.from_ndarray(arr, format="bgra").reformat(format="bgr0")
        else:
            fr = av.VideoFrame.from_ndarray(arr, format="bgra").reformat(format=self.pix)
        fr.pts = int(pts_us)
        if force_key and PictureType is not None:
            fr.pict_type = PictureType.I
        return [(bytes(p), bool(p.is_keyframe)) for p in self.ctx.encode(fr)]


def usable():
    """Bu makinede gercekten acilan DONANIM kodlayicilarin adlari (CPU x264 haric). Paneller bunlara
    dagitilir: iki monitor ayni motoru paylasmasin diye (NVENC + AMF paralel calisir)."""
    out = []
    for name in ("h264_nvenc", "h264_amf"):
        try:
            enc = H264Encoder(1280, 720, 60, 10_000_000, prefer=name)
            if enc.name == name:
                enc.encode(np.zeros((720, 1280, 4), np.uint8), 0)
                out.append(name)
        except Exception:
            pass
    return out


def available():
    """Bu makinede H.264 donanim kodlayici acilabiliyor mu? (ad, hata) dondurur."""
    try:
        enc = H264Encoder(1280, 720, 60, 10_000_000)
        arr = np.zeros((720, 1280, 4), np.uint8)
        enc.encode(arr, 0)
        return enc.name, ""
    except Exception as e:
        return "", str(e)
