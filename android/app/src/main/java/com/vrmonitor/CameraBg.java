package com.vrmonitor;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.graphics.ImageFormat;
import android.graphics.Rect;
import android.graphics.SurfaceTexture;
import android.graphics.YuvImage;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.util.Size;
import android.view.Surface;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;

/**
 * Arka kamerayi acar. Iki bagimsiz tuketici: (1) arka plan olarak cizilen goruntu (renderer'in SurfaceTexture'u),
 * (2) PC'deki el takibi icin kucuk JPEG kareleri (YUV ImageReader -> JPEG -> FrameSink). Biri veya ikisi
 * istenirse kamera acik kalir; hicbiri istenmezse kapanir.
 */
final class CameraBg {
    interface Listener {
        void onMessage(String s);
    }

    /** El takibi karesi: rot = goruntuyu dik yapmak icin saat yonunde 90'lik adim sayisi. */
    interface FrameSink {
        void onHandFrame(int rot, float[] headRows, byte[] jpeg, int len);
    }

    private static final class ExposedBaos extends ByteArrayOutputStream {
        ExposedBaos() {
            super(48 * 1024);
        }

        byte[] buf() {
            return buf;
        }
    }

    private final Context ctx;
    private final PanelRenderer renderer;
    private final Listener listener;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private HandlerThread thread;
    private Handler handler;
    private CameraDevice device;
    private CameraCaptureSession session;
    private ImageReader reader;
    private boolean bg, hand;
    private int gen;
    volatile int sensorOrientation = 90;
    volatile FrameSink sink;

    private volatile boolean tsRealtime;          // kamera damgasi sensor damgasiyla ayni saatte mi (elapsedRealtime)
    private final float[] rows = new float[9];
    private long lastSendNs;
    private byte[] nv21;
    private final ExposedBaos baos = new ExposedBaos();

    CameraBg(Context ctx, PanelRenderer renderer, Listener listener) {
        this.ctx = ctx;
        this.renderer = renderer;
        this.listener = listener;
    }

    /** Arka plan icin kamera istendi. */
    void start() {
        bg = true;
        rebuild();
    }

    /** Arka plan icin kamera artik gerekmiyor (el takibi hala istiyorsa acik kalir). */
    void stop() {
        if (!bg) return;
        bg = false;
        rebuild();
    }

    void setHand(boolean on) {
        if (hand == on) return;
        hand = on;
        rebuild();
    }

    void stopAll() {
        bg = false;
        hand = false;
        rebuild();
    }

    private void rebuild() {
        final int g = ++gen;
        closeAll();
        if (!bg && !hand) return;
        // kapanip hemen yeniden acilirken bazi cihazlar hata verir: kisa bekle
        ui.postDelayed(() -> {
            if (g == gen) open(g);
        }, 250);
    }

    @SuppressLint("MissingPermission")
    private void open(final int g) {
        if (device != null) return;
        final Surface bgSurf = bg ? renderer.camSurface : null;
        if (bg && bgSurf == null) {                       // GL henuz hazir degil, biraz sonra tekrar dene
            ui.postDelayed(() -> {
                if (g == gen) open(g);
            }, 300);
            return;
        }
        try {
            CameraManager cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
            String id = null;
            Size best = null, handSize = null;
            for (String cid : cm.getCameraIdList()) {
                CameraCharacteristics cc = cm.getCameraCharacteristics(cid);
                Integer facing = cc.get(CameraCharacteristics.LENS_FACING);
                StreamConfigurationMap map = cc.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
                if (facing == null || facing != CameraCharacteristics.LENS_FACING_BACK || map == null) continue;
                for (Size s : map.getOutputSizes(SurfaceTexture.class)) {
                    if (s.getWidth() > 1920 || s.getWidth() < 640) continue;
                    boolean wide = Math.abs((float) s.getWidth() / s.getHeight() - 16f / 9f) < 0.05f;
                    boolean bestWide = best != null
                            && Math.abs((float) best.getWidth() / best.getHeight() - 16f / 9f) < 0.05f;
                    if (best == null || (wide && !bestWide)
                            || (wide == bestWide && s.getWidth() > best.getWidth())) {
                        best = s;
                    }
                }
                Size[] ys = map.getOutputSizes(ImageFormat.YUV_420_888);
                if (ys != null) {
                    for (Size s : ys) {                  // 640x480'e en yakin boyut (el takibi icin kucuk yeter)
                        if (s.getWidth() > 1280) continue;
                        int d = Math.abs(s.getWidth() - 640) + Math.abs(s.getHeight() - 480);
                        if (handSize == null || d < Math.abs(handSize.getWidth() - 640) + Math.abs(handSize.getHeight() - 480)) {
                            handSize = s;
                        }
                    }
                }
                Integer so = cc.get(CameraCharacteristics.SENSOR_ORIENTATION);
                if (so != null) sensorOrientation = so;
                Integer tsrc = cc.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE);
                tsRealtime = tsrc != null && tsrc == CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME;
                id = cid;
                break;
            }
            if (id == null || best == null || (hand && handSize == null)) {
                listener.onMessage("Arka kamera bulunamadi");
                return;
            }
            if (bg) renderer.setCameraSize(best.getWidth(), best.getHeight());
            thread = new HandlerThread("camera");
            thread.start();
            handler = new Handler(thread.getLooper());
            final List<Surface> targets = new ArrayList<>();
            if (bgSurf != null) targets.add(bgSurf);
            if (hand) {
                reader = ImageReader.newInstance(handSize.getWidth(), handSize.getHeight(), ImageFormat.YUV_420_888, 3);
                reader.setOnImageAvailableListener(this::onImage, handler);
                targets.add(reader.getSurface());
            }
            cm.openCamera(id, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice camera) {
                    device = camera;
                    if (g != gen) {                    // bu arada baska bir yapilandirma istendi
                        camera.close();
                        device = null;
                        return;
                    }
                    try {
                        camera.createCaptureSession(targets, new CameraCaptureSession.StateCallback() {
                            @Override
                            public void onConfigured(CameraCaptureSession s) {
                                session = s;
                                try {
                                    CaptureRequest.Builder b = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                                    for (Surface t : targets) b.addTarget(t);
                                    b.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO);
                                    s.setRepeatingRequest(b.build(), null, handler);
                                } catch (CameraAccessException | IllegalStateException e) {
                                    listener.onMessage("Kamera baslatilamadi: " + e.getMessage());
                                }
                            }

                            @Override
                            public void onConfigureFailed(CameraCaptureSession s) {
                                listener.onMessage("Kamera oturumu kurulamadi");
                            }
                        }, handler);
                    } catch (CameraAccessException | IllegalStateException e) {
                        listener.onMessage("Kamera oturumu hatasi: " + e.getMessage());
                    }
                }

                @Override
                public void onDisconnected(CameraDevice camera) {
                    camera.close();
                    device = null;
                }

                @Override
                public void onError(CameraDevice camera, int error) {
                    camera.close();
                    device = null;
                    listener.onMessage("Kamera hatasi " + error);
                }
            }, handler);
        } catch (CameraAccessException | SecurityException e) {
            listener.onMessage("Kamera acilamadi: " + e.getMessage());
        }
    }

    /** ImageReader (kamera thread'i): en yeni kareyi al, ~30 fps'e kisitla, JPEG'e cevirip PC'ye yolla. */
    private void onImage(ImageReader r) {
        Image img = null;
        try {
            img = r.acquireLatestImage();
            if (img == null) return;
            long now = System.nanoTime();
            FrameSink s = sink;
            if (s == null || now - lastSendNs < 28_000_000L) return;
            lastSendNs = now;
            int w = img.getWidth(), h = img.getHeight();
            Image.Plane[] p = img.getPlanes();
            int need = w * h * 3 / 2;
            if (nv21 == null || nv21.length != need) nv21 = new byte[need];
            ByteBuffer yb = p[0].getBuffer();
            int ys = p[0].getRowStride();
            for (int row = 0; row < h; row++) {
                yb.position(row * ys);
                yb.get(nv21, row * w, w);
            }
            ByteBuffer ub = p[1].getBuffer(), vb = p[2].getBuffer();
            int us = p[1].getRowStride(), ups = p[1].getPixelStride();
            int vs = p[2].getRowStride(), vps = p[2].getPixelStride();
            int o = w * h;
            for (int row = 0; row < h / 2; row++) {
                for (int col = 0; col < w / 2; col++) {
                    nv21[o++] = vb.get(row * vs + col * vps);
                    nv21[o++] = ub.get(row * us + col * ups);
                }
            }
            YuvImage yuv = new YuvImage(nv21, ImageFormat.NV21, w, h, null);
            int q = 65;
            do {
                baos.reset();
                yuv.compressToJpeg(new Rect(0, 0, w, h), q, baos);
                q -= 15;
            } while (baos.size() > 64000 && q > 20);          // PC'ye giden mesajin sinirina sigsin (65535)
            if (baos.size() > 64000) return;
            int disp = 0;
            try {
                disp = ((Activity) ctx).getWindowManager().getDefaultDisplay().getRotation() * 90;
            } catch (RuntimeException ignored) {
            }
            int rot = ((sensorOrientation - disp + 360) % 360) / 90;
            // karenin cekildigi anki bas yonu (pozlama ortasi ~8 ms); damga saati uymuyorsa tahmini gecikme (50 ms)
            long tCap = tsRealtime ? img.getTimestamp() + 8_000_000L
                    : android.os.SystemClock.elapsedRealtimeNanos() - 50_000_000L;
            if (!renderer.headRowsAt(tCap, rows)) return;
            s.onHandFrame(rot, rows, baos.buf(), baos.size());
        } catch (RuntimeException ignored) {
        } finally {
            if (img != null) img.close();
        }
    }

    private void closeAll() {
        try {
            if (session != null) session.close();
        } catch (Exception ignored) {
        }
        try {
            if (device != null) device.close();
        } catch (Exception ignored) {
        }
        try {
            if (reader != null) reader.close();
        } catch (Exception ignored) {
        }
        session = null;
        device = null;
        reader = null;
        if (thread != null) {
            thread.quitSafely();
            thread = null;
        }
    }
}
