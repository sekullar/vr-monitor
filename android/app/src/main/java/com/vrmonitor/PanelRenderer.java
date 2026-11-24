package com.vrmonitor;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.SurfaceTexture;
import android.opengl.GLES11Ext;
import android.opengl.GLES20;
import android.opengl.GLSurfaceView;
import android.opengl.GLUtils;
import android.opengl.Matrix;
import android.view.Surface;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.FloatBuffer;
import java.nio.ShortBuffer;
import java.util.HashMap;

import javax.microedition.khronos.egl.EGLConfig;
import javax.microedition.khronos.opengles.GL10;

/**
 * Panelleri kullanicinin etrafina dizer. Kafa takibi (view matrisi) telefonun sensorunden yerel gelir;
 * PC'den sadece panel goruntusu, panel duzeni ve ortam ayarlari akar.
 *
 * Ortam: arka plan (duz renk / kamera / gorsel veya 360 panorama), arka plan parlakligi, panel golgesi,
 * ortam isiltisi (panel iceriginin rengi cevreye yansir), vinyet, yuvarlak kose, egrilik, fokus cercevesi.
 */
final class PanelRenderer implements GLSurfaceView.Renderer {
    static final int MAX = 8;
    static final float RADIUS = 3f;     // varsayilan panel uzakligi (m)
    static final float PANEL_H = 1.8f;  // panel yuksekligi (m)
    static final float GAP_DEG = 4f;
    static final float IPD = 0.064f;
    static final int COLS = 40;         // panel agindaki dilim sayisi (egrilik icin)

    final Panel[] panels = new Panel[MAX];
    volatile int count = 1;
    volatile boolean stereo = false;

    private final float[] head = new float[16];
    private final Object headLock = new Object();
    private boolean haveHead = false;
    private volatile boolean recenterRequested = true;
    private final float[] calInv = new float[16];   // kalibrasyon anindaki bakisin tersi
    private volatile float dragYaw = 0f, dragPitch = 0f;

    // Dinamik fokus: ekranin ortasindaki hayali nokta (bakis yonu) hangi panele denk gelirse o panel one gelir
    volatile boolean dynamicFocus = true;
    volatile float focusZoom = 1.25f;               // odaktaki panelin yakinlastirma carpani, 1x..8x
    volatile int focusedIdx = -1;
    private final float[] focusAmt = new float[MAX];   // 0..1, yumusak gecis
    private long lastNs = 0;
    private final float[] curYaw = new float[MAX], curPitch = new float[MAX],
            curDist = new float[MAX], curScl = new float[MAX];

    // PC'den gelen duzen (yoksa otomatik yay)
    private final Object layoutLock = new Object();
    private final float[] lYaw = new float[MAX], lPitch = new float[MAX],
            lDist = new float[MAX], lScale = new float[MAX];
    private volatile boolean customLayout = false;

    // Ortam ayarlari (PC'den)
    volatile int bgMode = 0;                 // 0 duz renk, 1 kamera, 2 gorsel
    volatile float bgBright = 1f;
    volatile int bgColor = 0xFF0A0A12;
    volatile boolean bgSphere = true;
    volatile float shadow = 0.55f, glow = 0.45f, vignette = 0.25f, panelBright = 1f, corner = 0.035f, curve = 0f;
    volatile boolean focusRing = true;
    volatile float predictMs = 25f;          // baş takibi gecikme telafisi (jiroskopla ileri tahmin)
    // Stereo: her gozun goruntusu, yari ekranin ortasindan mercek merkezine dogru (burna) bu kadar piksel kaydirilir.
    // A36'da yari ekran merkezleri ~77 mm arayla, merceklerin ortasi ~62 mm: kaydirma yoksa gozler birbirinden
    // uzaklasmaya zorlanir (cift goruntu, sol-sag gozde pencere kenari farkli yerde).
    volatile float lensShift = 100f;
    volatile float camShift = 0f;            // kamera arka plani icin EK kaydirma (px, mercek kaydirmasina eklenir)
    private float curEyeSign, curEyeW = 1f;  // cizilen gozun yonu (+ sol, - sag, 0 tek ekran) ve genisligi

    // Kamera arka plani (gecis / passthrough)
    volatile boolean cameraOn = false;
    volatile boolean camFlip180 = false;
    volatile int camRotUser = 3;
    volatile Surface camSurface;
    private SurfaceTexture camSt;
    private int camTex;
    private final float[] camMat = new float[16];
    private volatile boolean camFrame;
    private boolean camHasFrame, bgOn;
    private volatile float camAspect = 16f / 9f;

    // Arka plan gorseli
    private volatile byte[] bgBytes;             // son gelen gorsel (GL baglami yenilenirse tekrar yuklemek icin)
    private volatile Bitmap pendingBg;
    private int bgTex;
    private boolean bgTexReady;
    private float bgAspect = 2f;

    // GL nesneleri
    private Prog panelProg, shadowProg, glowProg, vigProg, camProg, flatProg, sphereProg, ptrProg;
    private FloatBuffer meshBuf, quadBuf, sphereBuf, ptrBuf;
    private ShortBuffer sphereIdx;
    private int sphereIdxCount;
    private int width = 1, height = 1;

    // Her karede yeniden kullanilan gecici diziler: saniyede 100+ kare x onlarca "new float[16]" cop toplayiciyi
    // tetikler ve kare atlamasina (takilma) yol acar.
    private final float[] sV = new float[16], sVc = new float[16], sRp = new float[16], sOut = new float[16],
            sProj = new float[16], sShift = new float[16], sVe = new float[16], sPv = new float[16],
            sPvRot = new float[16], sModel = new float[16], sMvp = new float[16], sHit = new float[16];
    private final int[] sOrder = new int[MAX];
    private final float[] sEff = new float[MAX];
    private static final float[] IDENT = new float[16];

    static {
        Matrix.setIdentityM(IDENT, 0);
    }

    volatile int renderFrames;      // HUD: saniyedeki cizilen kare sayisi

    PanelRenderer() {
        for (int i = 0; i < MAX; i++) panels[i] = new Panel(i);
        Matrix.setIdentityM(head, 0);
        Matrix.setIdentityM(calInv, 0);
        Matrix.setIdentityM(camMat, 0);
    }

    // ---- UI / ag thread API ----

    // Bas yonelimi gecmisi (son ~0.6 sn): el takibi karesinin cekildigi andaki yonu bulmak icin
    private final long[] hT = new long[128];
    private final float[][] hM = new float[128][16];
    private int hPos, hCount;
    private final float[] qRaw = new float[16], qV = new float[16], qRp = new float[16], qOut = new float[16];

    /** tsNs: bu yonelimin gecerli oldugu an (SystemClock.elapsedRealtimeNanos tabanli; tahmin payi dahil). */
    void setHead(float[] v, long tsNs) {
        synchronized (headLock) {
            System.arraycopy(v, 0, head, 0, 16);
            haveHead = true;
            hT[hPos] = tsNs;
            System.arraycopy(v, 0, hM[hPos], 0, 16);
            hPos = (hPos + 1) & 127;
            if (hCount < 128) hCount++;
        }
    }

    /**
     * tsNs anindaki bas yoneliminin "gozden dunyaya" donusunu (satir-sirali 3x3, 9 sayi) verir; dunya = panellerin
     * yasadigi, kalibre/surukleme uygulanmis uzay. Gecmis yoksa false.
     */
    boolean headRowsAt(long tsNs, float[] rows9) {
        synchronized (headLock) {
            if (hCount == 0) return false;
            int best = 0;
            long bd = Long.MAX_VALUE;
            for (int i = 0; i < hCount; i++) {
                long d = Math.abs(hT[i] - tsNs);
                if (d < bd) {
                    bd = d;
                    best = i;
                }
            }
            System.arraycopy(hM[best], 0, qRaw, 0, 16);
        }
        Matrix.multiplyMM(qV, 0, qRaw, 0, calInv, 0);
        Matrix.rotateM(qV, 0, dragYaw, 0f, 1f, 0f);
        float[] v = qV;
        if (dragPitch != 0f) {
            Matrix.setRotateM(qRp, 0, dragPitch, 1f, 0f, 0f);
            Matrix.multiplyMM(qOut, 0, qRp, 0, qV, 0);
            v = qOut;
        }
        rows9[0] = v[0]; rows9[1] = v[1]; rows9[2] = v[2];
        rows9[3] = v[4]; rows9[4] = v[5]; rows9[5] = v[6];
        rows9[6] = v[8]; rows9[7] = v[9]; rows9[8] = v[10];
        return true;
    }

    void recenter() {
        recenterRequested = true;
        dragYaw = 0f;
        dragPitch = 0f;
    }

    void addDrag(float dYaw, float dPitch) {
        dragYaw += dYaw;
        dragPitch = Math.max(-80f, Math.min(80f, dragPitch + dPitch));
    }

    void setCameraSize(int w, int h) {
        camAspect = (float) w / h;
        SurfaceTexture st = camSt;
        if (st != null) st.setDefaultBufferSize(w, h);
    }

    /** Ag thread'inden: duzen + ortam JSON'u. Arka plan modunu (0/1/2) dondurur, hata olursa -1. */
    int applyLayout(String json) {
        try {
            JSONObject root = new JSONObject(json);
            JSONArray a = root.getJSONArray("panels");
            if (root.has("focus_zoom")) {
                focusZoom = Math.max(1f, Math.min(8f, (float) root.optDouble("focus_zoom", 1.25)));
            }
            if (root.has("focus")) dynamicFocus = root.optBoolean("focus", true);
            synchronized (layoutLock) {
                for (int i = 0; i < Math.min(a.length(), MAX); i++) {
                    JSONObject p = a.getJSONObject(i);
                    lYaw[i] = (float) p.optDouble("yaw", 0);
                    lPitch[i] = (float) p.optDouble("pitch", 0);
                    lDist[i] = (float) p.optDouble("dist", RADIUS);
                    lScale[i] = (float) p.optDouble("scale", 1);
                }
            }
            customLayout = true;

            JSONObject env = root.optJSONObject("env");
            if (env == null) return -1;
            String bg = env.optString("bg", "dark");
            int mode = "camera".equals(bg) ? 1 : ("image".equals(bg) ? 2 : 0);
            bgMode = mode;
            bgBright = clamp((float) env.optDouble("bg_bright", 1), 0f, 2f);
            try {
                bgColor = Color.parseColor(env.optString("bg_color", "#0a0a12"));
            } catch (IllegalArgumentException ignored) {
            }
            bgSphere = env.optBoolean("bg_sphere", true);
            shadow = clamp((float) env.optDouble("shadow", 0.55), 0f, 1f);
            glow = clamp((float) env.optDouble("glow", 0.45), 0f, 1f);
            vignette = clamp((float) env.optDouble("vignette", 0.25), 0f, 1f);
            panelBright = clamp((float) env.optDouble("panel_bright", 1), 0.2f, 2f);
            corner = clamp((float) env.optDouble("corner", 0.035), 0f, 0.15f);
            curve = clamp((float) env.optDouble("curve", 0), 0f, 1f);
            focusRing = env.optBoolean("focus_ring", true);
            predictMs = clamp((float) env.optDouble("predict_ms", 25), 0f, 80f);
            lensShift = clamp((float) env.optDouble("lens_shift", 100), -300f, 400f);
            camShift = clamp((float) env.optDouble("cam_shift", 0), -300f, 300f);
            texBias = clamp((float) env.optDouble("tex_bias", -0.5), -1.5f, 1f);
            sharpen = clamp((float) env.optDouble("sharpen", 0.3), 0f, 1.5f);
            return mode;
        } catch (JSONException e) {
            return -1;
        }
    }

    /** Ag thread'inden: arka plan gorseli (JPEG). Cozer, GL thread'i dokuya yukler. */
    void setBackgroundImage(byte[] data) {
        bgBytes = data;
        decodeBg(data);
    }

    private void decodeBg(byte[] data) {
        try {
            BitmapFactory.Options o = new BitmapFactory.Options();
            o.inPreferredConfig = Bitmap.Config.ARGB_8888;
            Bitmap b = BitmapFactory.decodeByteArray(data, 0, data.length, o);
            if (b != null) pendingBg = b;
        } catch (OutOfMemoryError ignored) {
        }
    }

    /** Ag thread'inden. */
    void onFrame(int idx, int total, byte[] data, int len) {
        if (idx < 0 || idx >= MAX) return;
        count = Math.max(1, Math.min(MAX, total));
        panels[idx].submit(data, len);
    }

    /** PC'ye giden kucuk mesajlar (gecikme olcumu icin damga geri bildirimi, anahtar kare istegi). */
    interface AckSink {
        void sendAck(int panel, long ptsUs);

        void sendKeyReq(int panel);

        /** Lazer isabeti: ray #seq hangi panelin (u,v) noktasina degdi (panel 255 = hicbirine). u,v 0..1, sol-ust orijin. */
        void sendHit(int seq, int panel, float u, float v);

        /** El takibi icin telefon kamerasi karesi (JPEG); rot = goruntuyu dik yapan saat yonu 90'lik adim. */
        void sendHandFrame(int rot, float[] headRows, byte[] jpeg, int len);
    }

    // ---- el lazeri (PC el takibi isin yollar; burada gercek panel geometrisiyle kesilip cizilir) ----
    private final Object rayLock = new Object();
    private final float[] rayRaw = new float[6];            // dogrultu: orijin(3) + yon(3)
    private volatile int rayFlags, raySeq;                  // bit0 gecerli, bit1 basili, bit2 kafaya bagli
    private volatile long rayNs;
    private int lastHitSeq = -1;
    private final float[] ptrO = new float[3], ptrH = new float[3], sRay = new float[6];
    private boolean ptrOn, ptrHit, ptrPressed;
    volatile int ptrPanel = -1;                              // HUD: lazerin gosterdigi panel

    void setRay(int flags, int seq, float[] od) {
        synchronized (rayLock) {
            System.arraycopy(od, 0, rayRaw, 0, 6);
        }
        raySeq = seq;
        rayNs = System.nanoTime();
        rayFlags = flags;
    }

    /** Isi (dunya uzayinda) her panelin gercek (odak yakinlastirmali) duzlemiyle keser; en yakin isabet kazanir. */
    private void updatePointer(int n, float[] view) {
        int fl = rayFlags;
        if ((fl & 1) == 0 || System.nanoTime() - rayNs > 400_000_000L) {
            ptrOn = false;
            ptrPanel = -1;
            return;
        }
        float[] r = sRay;
        int seq;
        synchronized (rayLock) {
            System.arraycopy(rayRaw, 0, r, 0, 6);
            seq = raySeq;
        }
        if ((fl & 4) != 0) {                                 // kafaya bagli isin: gorunum uzayindan dunyaya
            for (int k = 0; k < 6; k += 3) {
                float x = r[k], y = r[k + 1], z = r[k + 2];
                r[k] = view[0] * x + view[1] * y + view[2] * z;
                r[k + 1] = view[4] * x + view[5] * y + view[6] * z;
                r[k + 2] = view[8] * x + view[9] * y + view[10] * z;
            }
        }
        float ox = r[0], oy = r[1], oz = r[2], dx = r[3], dy = r[4], dz = r[5];
        float best = Float.MAX_VALUE, bu = 0f, bv = 0f;
        int bi = -1;
        float[] m = sHit;
        for (int i = 0; i < n; i++) {
            float eff = curDist[i] / (1f + (focusZoom - 1f) * focusAmt[i]);
            Matrix.setIdentityM(m, 0);
            Matrix.rotateM(m, 0, -curYaw[i], 0f, 1f, 0f);
            Matrix.rotateM(m, 0, curPitch[i], 1f, 0f, 0f);
            float nx = m[8], ny = m[9], nz = m[10];
            float nd = nx * dx + ny * dy + nz * dz;
            if (nd >= -1e-4f) continue;
            float px = -eff * nx, py = -eff * ny, pz = -eff * nz;
            float t = ((px - ox) * nx + (py - oy) * ny + (pz - oz) * nz) / nd;
            if (t <= 0f || t >= best) continue;
            float hx = ox + dx * t - px, hy = oy + dy * t - py, hz = oz + dz * t - pz;
            float u = hx * m[0] + hy * m[1] + hz * m[2];
            float v = hx * m[4] + hy * m[5] + hz * m[6];
            float halfW = PANEL_H * panels[i].aspect / 2 * curScl[i], halfH = PANEL_H / 2 * curScl[i];
            if (Math.abs(u) > halfW || Math.abs(v) > halfH) continue;
            best = t;
            bi = i;
            bu = (u / halfW + 1f) * 0.5f;
            bv = (1f - v / halfH) * 0.5f;
        }
        float tt = bi >= 0 ? best : 6f;
        ptrO[0] = ox; ptrO[1] = oy; ptrO[2] = oz;
        ptrH[0] = ox + dx * tt; ptrH[1] = oy + dy * tt; ptrH[2] = oz + dz * tt;
        ptrOn = true;
        ptrHit = bi >= 0;
        ptrPressed = (fl & 2) != 0;
        ptrPanel = bi;
        if (seq != lastHitSeq) {                             // her yeni isin icin bir isabet raporu
            lastHitSeq = seq;
            AckSink s = ackSink;
            if (s != null) s.sendHit(seq, bi >= 0 ? bi : 255, bu, bv);
        }
    }

    private static final String FS_PTR =
            "precision mediump float; uniform vec4 uColor; uniform float uKind; varying vec2 vTex;"
                    + "void main(){ float a;"
                    + " if (uKind < 0.5) { float d = abs(vTex.y); a = exp(-d * d * 3.5) * (0.12 + 0.88 * vTex.x); }"
                    + " else { float r = length(vTex); float core = 1.0 - smoothstep(0.30, 0.42, r);"
                    + "   float ring = smoothstep(0.62, 0.70, r) * (1.0 - smoothstep(0.88, 0.97, r));"
                    + "   a = max(core, ring * 0.85); a *= 1.0 - smoothstep(0.97, 1.0, r); }"
                    + " gl_FragColor = vec4(uColor.rgb, uColor.a * a); }";

    private final float[] sClipA = new float[4], sClipB = new float[4], sPt = new float[4];

    private void putV(int i, float x, float y, float u, float v) {
        ptrBuf.put(i * 4, x);
        ptrBuf.put(i * 4 + 1, y);
        ptrBuf.put(i * 4 + 2, u);
        ptrBuf.put(i * 4 + 3, v);
    }

    private void drawPointer(float[] pv, int w, int h, float fovY) {
        if (!ptrOn) return;
        float[] a = sClipA, b = sClipB, p = sPt;
        p[0] = ptrO[0]; p[1] = ptrO[1]; p[2] = ptrO[2]; p[3] = 1f;
        Matrix.multiplyMV(a, 0, pv, 0, p, 0);
        p[0] = ptrH[0]; p[1] = ptrH[1]; p[2] = ptrH[2];
        Matrix.multiplyMV(b, 0, pv, 0, p, 0);
        GLES20.glEnable(GLES20.GL_BLEND);
        GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA);
        use(ptrProg);
        float cr = ptrPressed ? 1f : 0.45f, cg = ptrPressed ? 0.45f : 0.85f, cb = ptrPressed ? 0.30f : 1f;
        float hw = w * 0.5f, hh = h * 0.5f;
        if (a[3] > 0.02f && b[3] > 0.02f) {
            float ax = a[0] / a[3], ay = a[1] / a[3], bx = b[0] / b[3], by = b[1] / b[3];
            float sx = (bx - ax) * hw, sy = (by - ay) * hh;          // piksel uzayinda
            float len = (float) Math.sqrt(sx * sx + sy * sy);
            if (len > 1f) {
                float nxp = -sy / len * 5f / hw, nyp = sx / len * 5f / hh;   // 5 px yari kalinlik
                putV(0, ax - nxp, ay - nyp, 0f, -1f);
                putV(1, ax + nxp, ay + nyp, 0f, 1f);
                putV(2, bx - nxp, by - nyp, 1f, -1f);
                putV(3, bx + nxp, by + nyp, 1f, 1f);
                ptrBuf.position(0);
                GLES20.glUniform4f(ptrProg.loc("uColor"), cr, cg, cb, ptrHit ? 0.55f : 0.30f);
                uni1(ptrProg, "uKind", 0f);
                pointers(ptrBuf, 2, 16);
                GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);
            }
            if (ptrHit && b[3] > 0.02f) {
                float rpx = (ptrPressed ? 0.028f : 0.04f) * (h * 0.5f / (float) Math.tan(Math.toRadians(fovY / 2))) / b[3];
                rpx = Math.max(6f, Math.min(80f, rpx));
                float rx = rpx / hw, ry = rpx / hh;
                putV(0, bx - rx, by - ry, -1f, -1f);
                putV(1, bx + rx, by - ry, 1f, -1f);
                putV(2, bx - rx, by + ry, -1f, 1f);
                putV(3, bx + rx, by + ry, 1f, 1f);
                ptrBuf.position(0);
                GLES20.glUniform4f(ptrProg.loc("uColor"), cr, cg, cb, 1f);
                uni1(ptrProg, "uKind", 1f);
                pointers(ptrBuf, 2, 16);
                GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);
            }
        }
    }

    volatile AckSink ackSink;
    private final VideoDecoder.Callbacks videoCb = panel -> {
        AckSink s = ackSink;
        if (s != null) s.sendKeyReq(panel);
    };
    private final float[] sVideoMat = new float[16];
    private int ackTick;

    /** Ag thread'inden: donanim H.264 kare (damga + boyut + Annex-B). */
    void onVideo(int panel, int total, byte[] data, int len) {
        if (panel < 0 || panel >= MAX) return;
        count = Math.max(1, Math.min(MAX, total));
        panels[panel].submitVideo(data, len, videoCb);
    }

    /** GL thread'i: cozucudan gelen en yeni kareyi 2D dokuya (mipmap'li) aktarir ve gecikme icin ack yollar. */
    private void updateVideo(Panel p) {
        VideoDecoder v = p.video;
        if (v == null || !p.videoMode) return;
        if (v.oesTex == 0) {
            int[] t = new int[1];
            GLES20.glGenTextures(1, t, 0);
            v.oesTex = t[0];
        }
        boolean got = v.latch(sVideoMat);    // onceki cizimde verilen kare
        v.present();                          // sonraki cizim icin siradaki en eski kareyi ekrana ver
        if (!got) return;
        int w = v.vw, h = v.vh;
        if (w <= 0 || h <= 0) return;
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, v.oesTex);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE);
        p.ensureFbo(w, h);

        // OES -> 2D: panel agi v=0'i ust satir sayar, FBO'da v=0 alt satir; bu yuzden dikey cevirerek ciz
        GLES20.glBindFramebuffer(GLES20.GL_FRAMEBUFFER, p.fbo);
        GLES20.glViewport(0, 0, w, h);
        GLES20.glDisable(GLES20.GL_BLEND);
        use(camProg);
        GLES20.glUniformMatrix4fv(camProg.loc("uMat"), 1, false, sVideoMat, 0);
        GLES20.glUniform2f(camProg.loc("uCrop"), 1f, -1f);
        GLES20.glUniform2f(camProg.loc("uRot"), 1f, 0f);
        uni1(camProg, "uBright", 1f);
        uni1(camProg, "uOffX", 0f);                  // H.264 -> doku donusumu: stereo kaydirma UYGULANMAZ
        uni1(camProg, "uScaleX", 1f);
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0);
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, v.oesTex);
        GLES20.glUniform1i(camProg.loc("uTex"), 0);
        pointers(quadBuf, 2, 16);
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);
        GLES20.glBindFramebuffer(GLES20.GL_FRAMEBUFFER, 0);
        GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, p.tex);
        GLES20.glGenerateMipmap(GLES20.GL_TEXTURE_2D);

        p.aspect = (float) w / h;
        p.frames++;
        if ((++ackTick & 7) == 0) {          // her 8 karede bir damga geri yolla (PC gecikmeyi olcer)
            AckSink s = ackSink;
            if (s != null) s.sendAck(p.index, v.presentedPts);
        }
    }

    void shutdown() {
        for (Panel p : panels) p.stop();
    }

    private static float clamp(float v, float lo, float hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    // ---- GL kurulum ----

    private static final class Prog {
        final int id;
        private final HashMap<String, Integer> u = new HashMap<>();

        Prog(int id) {
            this.id = id;
        }

        int loc(String n) {
            Integer v = u.get(n);
            if (v == null) {
                v = GLES20.glGetUniformLocation(id, n);
                u.put(n, v);
            }
            return v;
        }
    }

    // fragment shader'larda mumkunse highp: yuvarlak kose / yumusak kenar hesabi mediump'ta titrer
    private static final String PREC =
            "#ifdef GL_FRAGMENT_PRECISION_HIGH\nprecision highp float;\n#else\nprecision mediump float;\n#endif\n";

    private static final String SDF =
            "float sdBox(vec2 p, vec2 b, float r){ vec2 q = abs(p) - b + r;"
                    + " return length(max(q,0.0)) + min(max(q.x,q.y),0.0) - r; }";

    // panel / golge / isilti ortak vertex shader: egilebilir serit ag
    private static final String VS_PANEL =
            "attribute vec2 aPos; attribute vec2 aTex;"
                    + "uniform mat4 uMvp; uniform vec2 uHalf; uniform float uCurve; uniform float uExpand;"
                    + "varying vec2 vUv;"
                    + "void main(){ vec2 p = aPos * uExpand * uHalf; float z = 0.0;"
                    + " if (uCurve > 0.0001) { float a = clamp(p.x * uCurve, -1.55, 1.55);"
                    + "   p.x = sin(a) / uCurve; z = (1.0 - cos(a)) / uCurve; }"
                    + " gl_Position = uMvp * vec4(p, z, 1.0);"
                    + " vUv = (aTex - 0.5) * uExpand + 0.5; }";

    /**
     * Panel shader'i. uBias: mip kaymasi (negatif = daha keskin, biraz daha titrek). uSharp: ekran-uzayinda keskinlestirme
     * (unsharp mask; turev uzantisi varsa). Panel 1920x1080 doku ~400 piksele kuculuyor (2.7:1): metin icin keskinlik onemli.
     */
    private static String fsPanel(boolean sharp) {
        return (sharp ? "#extension GL_OES_standard_derivatives : enable\n#define SHARP 1\n" : "")
                + PREC + " uniform sampler2D uTex;"
                + "uniform float uBright, uCorner, uAspect, uFocus, uBias, uSharp; uniform vec3 uAccent; varying vec2 vUv;"
                + SDF
                + "void main(){ vec2 sz = vec2(uAspect, 1.0); vec2 p = (vUv - 0.5) * sz;"
                + " float d = sdBox(p, sz * 0.5, uCorner);"
                + " float alpha = 1.0 - smoothstep(-0.004, 0.004, d);"
                + " vec2 uv = clamp(vUv, 0.0, 1.0);"
                + " vec3 c = texture2D(uTex, uv, uBias).rgb;\n"
                + "#ifdef SHARP\n"
                + " if (uSharp > 0.001) { vec2 dx = dFdx(vUv) * 0.7; vec2 dy = dFdy(vUv) * 0.7;"
                + "  vec3 b = texture2D(uTex, uv + dx, uBias).rgb + texture2D(uTex, uv - dx, uBias).rgb"
                + "         + texture2D(uTex, uv + dy, uBias).rgb + texture2D(uTex, uv - dy, uBias).rgb;"
                + "  c = c + (c - b * 0.25) * uSharp; }\n"
                + "#endif\n"
                + " c = clamp(c, 0.0, 1.0) * uBright;"
                + " float ring = (1.0 - smoothstep(0.0, 0.012, -d)) * uFocus;"
                + " c = mix(c, uAccent, ring * 0.85);"
                + " gl_FragColor = vec4(c, alpha); }";
    }

    private boolean sharpOk;
    volatile float texBias = -0.5f, sharpen = 0.3f;

    private static final String FS_SHADOW =
            PREC + " uniform float uAspect, uCorner, uStrength, uBlur, uOffY; varying vec2 vUv;"
                    + SDF
                    + "void main(){ vec2 sz = vec2(uAspect, 1.0); vec2 p = (vUv - 0.5) * sz - vec2(0.0, uOffY);"
                    + " float d = sdBox(p, sz * 0.5, uCorner);"
                    + " float a = uStrength * (1.0 - smoothstep(-uBlur * 0.4, uBlur, d));"
                    + " gl_FragColor = vec4(0.0, 0.0, 0.0, a); }";

    // panel iceriginin (dusuk mip = bulaniklasmis ortalama) rengi panelin cevresine isik gibi yayilir
    private static final String FS_GLOW =
            PREC + " uniform sampler2D uTex;"
                    + "uniform float uAspect, uCorner, uStrength, uSpread; varying vec2 vUv;"
                    + SDF
                    + "void main(){ vec2 sz = vec2(uAspect, 1.0); vec2 p = (vUv - 0.5) * sz;"
                    + " float d = sdBox(p, sz * 0.5, uCorner);"
                    // 3x3 ornek + kenarda kaydirma: dusuk cozunurluklu mip tek ornekte BLOKLU/kare gorunuyordu
                    + " vec3 c = vec3(0.0);"
                    + " for (int ix = -1; ix <= 1; ix++) { for (int iy = -1; iy <= 1; iy++) {"
                    + "   c += texture2D(uTex, clamp(vUv + vec2(float(ix), float(iy)) * 0.11, 0.02, 0.98), 5.0).rgb; } }"
                    + " c = c / 9.0 * 1.9 + 0.025;"
                    + " float f = 1.0 - smoothstep(0.0, uSpread, d); f = f * f;"
                    + " gl_FragColor = vec4(c, uStrength * f); }";

    private static final String VS_FULL =
            "attribute vec2 aPos; attribute vec2 aTex; varying vec2 vTex; uniform vec2 uCrop; uniform vec2 uRot;"
                    + "uniform mat4 uMat; uniform float uOffX; uniform float uScaleX;"
                    + "void main(){ gl_Position = vec4(aPos.x * uScaleX + uOffX, aPos.y, 0.0, 1.0);"
                    + " vec2 t = (aTex - 0.5) * uCrop;"
                    + " t = vec2(t.x * uRot.x - t.y * uRot.y, t.x * uRot.y + t.y * uRot.x) + 0.5;"
                    + " vTex = (uMat * vec4(t, 0.0, 1.0)).xy; }";

    private static final String FS_CAM =
            "#extension GL_OES_EGL_image_external : require\n"
                    + "precision mediump float; uniform samplerExternalOES uTex; uniform float uBright; varying vec2 vTex;"
                    + "void main(){ gl_FragColor = vec4(texture2D(uTex, vTex).rgb * uBright, 1.0); }";

    private static final String FS_FLAT =
            "precision mediump float; uniform sampler2D uTex; uniform float uBright; varying vec2 vTex;"
                    + "void main(){ gl_FragColor = vec4(texture2D(uTex, vTex).rgb * uBright, 1.0); }";

    private static final String VS_SPHERE =
            "attribute vec3 aPos; attribute vec2 aTex; uniform mat4 uMvp; varying vec2 vTex;"
                    + "void main(){ gl_Position = uMvp * vec4(aPos, 1.0); vTex = aTex; }";

    private static final String VS_VIG =
            "attribute vec2 aPos; attribute vec2 aTex; varying vec2 vTex;"
                    + "void main(){ gl_Position = vec4(aPos, 0.0, 1.0); vTex = aTex; }";

    private static final String FS_VIG =
            "precision mediump float; uniform float uStrength; varying vec2 vTex;"
                    + "void main(){ vec2 q = (vTex - 0.5) * 2.0; float r = length(q);"
                    + " gl_FragColor = vec4(0.0, 0.0, 0.0, uStrength * smoothstep(0.55, 1.45, r)); }";

    @Override
    public void onSurfaceCreated(GL10 unused, EGLConfig config) {
        // cizim thread'i en yuksek ekran onceligi: kare atlamasini azaltir
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_DISPLAY);
        GLES20.glClearColor(0.03f, 0.03f, 0.05f, 1f);
        String ext0 = GLES20.glGetString(GLES20.GL_EXTENSIONS);
        sharpOk = ext0 != null && ext0.contains("GL_OES_standard_derivatives");
        panelProg = mk(VS_PANEL, fsPanel(sharpOk));
        int[] linkOk = new int[1];
        GLES20.glGetProgramiv(panelProg.id, GLES20.GL_LINK_STATUS, linkOk, 0);
        if (linkOk[0] == 0 && sharpOk) {                       // turev uzantili shader derlenmediyse keskinlestirmesiz kur
            sharpOk = false;
            panelProg = mk(VS_PANEL, fsPanel(false));
        }
        shadowProg = mk(VS_PANEL, FS_SHADOW);
        glowProg = mk(VS_PANEL, FS_GLOW);
        vigProg = mk(VS_VIG, FS_VIG);
        camProg = mk(VS_FULL, FS_CAM);
        flatProg = mk(VS_FULL, FS_FLAT);
        sphereProg = mk(VS_SPHERE, FS_FLAT);
        ptrProg = mk(VS_VIG, FS_PTR);
        ptrBuf = fb(new float[16]);

        String ext = GLES20.glGetString(GLES20.GL_EXTENSIONS);
        if (ext != null && ext.contains("GL_EXT_texture_filter_anisotropic")) {
            float[] m = new float[1];
            GLES20.glGetFloatv(0x84FF, m, 0);   // GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT
            Panel.aniso = Math.min(8f, m[0]);
        }

        // panel agi: COLS+1 sutun, her sutunda ust/alt iki kose (TRIANGLE_STRIP)
        float[] mesh = new float[(COLS + 1) * 2 * 4];
        int k = 0;
        for (int i = 0; i <= COLS; i++) {
            float x = -1f + 2f * i / COLS, u = (float) i / COLS;
            mesh[k++] = x; mesh[k++] = 1f; mesh[k++] = u; mesh[k++] = 0f;
            mesh[k++] = x; mesh[k++] = -1f; mesh[k++] = u; mesh[k++] = 1f;
        }
        meshBuf = fb(mesh);

        // tam ekran dortgen (kamera, sabit duvar kagidi, vinyet)
        quadBuf = fb(new float[]{
                -1, -1, 0, 0,
                1, -1, 1, 0,
                -1, 1, 0, 1,
                1, 1, 1, 1});
        buildSphere();

        // kamera: harici (OES) doku
        int[] ct = new int[1];
        GLES20.glGenTextures(1, ct, 0);
        camTex = ct[0];
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, camTex);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE);
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE);
        if (camSt != null) camSt.release();
        camSt = new SurfaceTexture(camTex);
        camSt.setOnFrameAvailableListener(st -> camFrame = true);
        camSurface = new Surface(camSt);
        camHasFrame = false;

        // arka plan gorseli dokusu
        int[] bt = new int[1];
        GLES20.glGenTextures(1, bt, 0);
        bgTex = bt[0];
        bgTexReady = false;
        byte[] keep = bgBytes;
        if (keep != null) decodeBg(keep);     // GL baglami yeniden yaratildiysa gorseli tekrar yukle

        // GL baglami yeniden yaratildiysa dokular gecersiz
        for (Panel p : panels) {
            p.tex = 0;
            p.tw = 0;
            p.th = 0;
            p.fbo = 0;
            p.fboW = 0;
            p.fboH = 0;
            if (p.video != null) p.video.invalidateGl();    // H.264 dokulari gecersiz: cozucuyu bastan kur
        }
    }

    private Prog mk(String vs, String fs) {
        int v = compile(GLES20.GL_VERTEX_SHADER, vs), f = compile(GLES20.GL_FRAGMENT_SHADER, fs);
        int p = GLES20.glCreateProgram();
        GLES20.glAttachShader(p, v);
        GLES20.glAttachShader(p, f);
        GLES20.glBindAttribLocation(p, 0, "aPos");     // tum programlarda ayni konumlar
        GLES20.glBindAttribLocation(p, 1, "aTex");
        GLES20.glLinkProgram(p);
        return new Prog(p);
    }

    private static FloatBuffer fb(float[] a) {
        FloatBuffer b = ByteBuffer.allocateDirect(a.length * 4).order(ByteOrder.nativeOrder()).asFloatBuffer();
        b.put(a).position(0);
        return b;
    }

    /** Ic yuzeyi bakilan kure: equirectangular 360 panorama icin (u=0.5, enlem 0 = tam karsi / -Z). */
    private void buildSphere() {
        final int lon = 64, lat = 32;
        final float r = 30f;
        float[] v = new float[(lon + 1) * (lat + 1) * 5];
        int k = 0;
        for (int j = 0; j <= lat; j++) {
            double la = Math.PI / 2 - Math.PI * j / lat;
            for (int i = 0; i <= lon; i++) {
                double lo = -Math.PI + 2 * Math.PI * i / lon;
                v[k++] = (float) (r * Math.sin(lo) * Math.cos(la));
                v[k++] = (float) (r * Math.sin(la));
                v[k++] = (float) (-r * Math.cos(lo) * Math.cos(la));
                v[k++] = (float) i / lon;
                v[k++] = (float) j / lat;
            }
        }
        sphereBuf = fb(v);
        short[] idx = new short[lon * lat * 6];
        int n = 0;
        for (int j = 0; j < lat; j++) {
            for (int i = 0; i < lon; i++) {
                int a = j * (lon + 1) + i, b = a + lon + 1;
                idx[n++] = (short) a; idx[n++] = (short) b; idx[n++] = (short) (a + 1);
                idx[n++] = (short) (a + 1); idx[n++] = (short) b; idx[n++] = (short) (b + 1);
            }
        }
        sphereIdxCount = idx.length;
        sphereIdx = ByteBuffer.allocateDirect(idx.length * 2).order(ByteOrder.nativeOrder()).asShortBuffer();
        sphereIdx.put(idx).position(0);
    }

    @Override
    public void onSurfaceChanged(GL10 unused, int w, int h) {
        width = w;
        height = h;
    }

    // ---- cizim ----

    volatile long drawNsSum, drawNsMax;      // HUD/logcat: onDrawFrame CPU suresi (ms)

    @Override
    public void onDrawFrame(GL10 unused) {
        long t0 = System.nanoTime();
        drawFrameInner();
        long d = System.nanoTime() - t0;
        drawNsSum += d;
        if (d > drawNsMax) drawNsMax = d;
    }

    private void drawFrameInner() {
        renderFrames++;
        float[] v = sV;
        synchronized (headLock) {
            System.arraycopy(head, 0, v, 0, 16);
        }

        if (recenterRequested && haveHead) {
            // Su an baktigin yon (yatay + dikey + yan yatma) "tam karsi" olsun: v0'in tersini sakla
            Matrix.transposeM(calInv, 0, v, 0);
            recenterRequested = false;
        }
        float[] vc = sVc;
        Matrix.multiplyMM(vc, 0, v, 0, calInv, 0);
        v = vc;
        Matrix.rotateM(v, 0, dragYaw, 0f, 1f, 0f);
        if (dragPitch != 0f) {
            float[] rp = sRp;
            float[] out = sOut;
            Matrix.setRotateM(rp, 0, dragPitch, 1f, 0f, 0f);
            Matrix.multiplyMM(out, 0, rp, 0, v, 0);
            v = out;
        }

        int n = Math.min(count, MAX);
        computeLayout(n);
        updateFocus(n, v);
        updatePointer(n, v);
        for (int i = 0; i < n; i++) {
            panels[i].ensureTexture();
            panels[i].upload();
            updateVideo(panels[i]);
        }

        // yeni arka plan gorseli geldiyse dokuya yukle
        Bitmap nb = pendingBg;
        if (nb != null) {
            pendingBg = null;
            GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, bgTex);
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR_MIPMAP_LINEAR);
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR);
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE);
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE);
            if (Panel.aniso > 1f) GLES20.glTexParameterf(GLES20.GL_TEXTURE_2D, 0x84FE, Panel.aniso);
            GLUtils.texImage2D(GLES20.GL_TEXTURE_2D, 0, nb, 0);
            GLES20.glGenerateMipmap(GLES20.GL_TEXTURE_2D);
            bgAspect = (float) nb.getWidth() / nb.getHeight();
            bgTexReady = true;
            nb.recycle();
        }

        if (cameraOn && camSt != null) {
            if (camFrame) {
                camFrame = false;
                try {
                    camSt.updateTexImage();
                    camSt.getTransformMatrix(camMat);
                    camHasFrame = true;
                } catch (RuntimeException ignored) {
                }
            }
        } else {
            camHasFrame = false;
        }
        bgOn = cameraOn && camHasFrame;

        // duz renk modunda arka plan rengi x parlaklik
        int c = bgColor;
        float k = bgBright;
        GLES20.glClearColor(clamp(Color.red(c) / 255f * k, 0f, 1f), clamp(Color.green(c) / 255f * k, 0f, 1f),
                clamp(Color.blue(c) / 255f * k, 0f, 1f), 1f);
        GLES20.glViewport(0, 0, width, height);
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT);

        if (stereo) {
            int half = width / 2;
            drawEye(v, -IPD / 2, 0, half, height, 90f, n);
            drawEye(v, +IPD / 2, half, half, height, 90f, n);
        } else {
            drawEye(v, 0f, 0, width, height, 65f, n);
        }
    }

    /** Panel yerlesimi (yaw/pitch/mesafe/boyut): PC'den gelen duzen varsa o, yoksa otomatik yay. */
    private void computeLayout(int n) {
        if (customLayout) {
            synchronized (layoutLock) {
                for (int i = 0; i < n; i++) {
                    curYaw[i] = lYaw[i];
                    curPitch[i] = lPitch[i];
                    curDist[i] = lDist[i];
                    curScl[i] = lScale[i];
                }
            }
            return;
        }
        float[] ang = new float[n];
        float total = (n - 1) * GAP_DEG;
        for (int i = 0; i < n; i++) {
            float pw = PANEL_H * panels[i].aspect;
            ang[i] = (float) Math.toDegrees(2 * Math.atan(pw / 2 / RADIUS));
            total += ang[i];
        }
        float cur = -total / 2;
        for (int i = 0; i < n; i++) {
            curYaw[i] = cur + ang[i] / 2;
            cur += ang[i] + GAP_DEG;
            curPitch[i] = 0f;
            curDist[i] = RADIUS;
            curScl[i] = 1f;
        }
    }

    /**
     * Bakis isini (kameranin -Z yonu, dunya uzayinda) her panelin duzlemiyle keser; en yakin kesisen
     * panel odak olur. Odak 0..1 arasinda yumusakca gecer (bir panelin one gelip digerinin geri gitmesi).
     */
    private long focusLostNs, candSinceNs;
    private int candIdx = -1;

    private void updateFocus(int n, float[] view) {
        long now = System.nanoTime();
        float dt = lastNs == 0 ? 0.016f : Math.min(0.1f, (now - lastNs) / 1e9f);
        lastNs = now;

        int target = -1;
        if (dynamicFocus) {
            float dx = -view[2], dy = -view[6], dz = -view[10];   // view'in 3. satirinin negatifi
            float best = Float.MAX_VALUE;
            for (int i = 0; i < n; i++) {
                // Odaktaki panel buyur (yaklasir); isabet testi GORUNEN (buyumus) panele gore yapilir ve odaktakine genis pay
                // verilir: yoksa buyuyen panelin kenarina bakinca odak kayiyordu.
                float t = hitPanel(i, dx, dy, dz, i == focusedIdx ? 1.35f : 1.0f);
                if (t > 0f && t < best) {
                    best = t;
                    target = i;
                }
            }
            // Zaman histerezisi: (1) odak kaybi ancak 0.4 sn bosta kalinirsa, (2) baska panele gecis ancak 0.15 sn
            // kararli bakilirsa olur (kisa bir bakis kaymasi odagi bozmasin)
            if (focusedIdx >= 0 && target != focusedIdx) {
                if (target < 0) {
                    if (focusLostNs == 0) focusLostNs = now;
                    if (now - focusLostNs < 400_000_000L) target = focusedIdx;
                } else {
                    if (candIdx != target) {
                        candIdx = target;
                        candSinceNs = now;
                    }
                    if (now - candSinceNs < 150_000_000L) target = focusedIdx;
                    else focusLostNs = 0;
                }
            } else {
                focusLostNs = 0;
                candIdx = -1;
            }
        } else {
            focusLostNs = 0;
            candIdx = -1;
        }
        focusedIdx = target;
        float k = 1f - (float) Math.exp(-dt * 9f);
        for (int i = 0; i < MAX; i++) {
            focusAmt[i] += ((i == target ? 1f : 0f) - focusAmt[i]) * k;
        }
    }

    /** Isin (orijinden d yonunde) panelin dikdortgenine isabet ederse mesafeyi, yoksa -1 dondurur. */
    private float hitPanel(int i, float dx, float dy, float dz, float margin) {
        float[] m = sHit;
        Matrix.setIdentityM(m, 0);
        Matrix.rotateM(m, 0, -curYaw[i], 0f, 1f, 0f);
        Matrix.rotateM(m, 0, curPitch[i], 1f, 0f, 0f);
        // sutunlar: m[0..2] = panelin sagi, m[4..6] = yukarisi, m[8..10] = normali (izleyiciye bakan)
        float nx = m[8], ny = m[9], nz = m[10];
        float nd = nx * dx + ny * dy + nz * dz;
        if (nd >= -1e-4f) return -1f;                   // panelin arkasi / paralel
        // cizimle ayni (odak yakinlastirmali) mesafe: odaktaki panel gozde buyur, isabet alani da onunla buyur
        float dist = curDist[i] / (1f + (focusZoom - 1f) * focusAmt[i]);
        float t = -dist / nd;                           // merkez P = -dist * N
        if (t <= 0f) return -1f;
        float px = -dist * nx, py = -dist * ny, pz = -dist * nz;
        float hx = dx * t - px, hy = dy * t - py, hz = dz * t - pz;
        float u = hx * m[0] + hy * m[1] + hz * m[2];
        float v = hx * m[4] + hy * m[5] + hz * m[6];
        float halfW = PANEL_H * panels[i].aspect / 2 * curScl[i] * margin;
        float halfH = PANEL_H / 2 * curScl[i] * margin;
        return (Math.abs(u) <= halfW && Math.abs(v) <= halfH) ? t : -1f;
    }

    private void use(Prog p) {
        GLES20.glUseProgram(p.id);
        GLES20.glEnableVertexAttribArray(0);
        GLES20.glEnableVertexAttribArray(1);
    }

    private void pointers(FloatBuffer b, int posSize, int stride) {
        b.position(0);
        GLES20.glVertexAttribPointer(0, posSize, GLES20.GL_FLOAT, false, stride, b);
        b.position(posSize);
        GLES20.glVertexAttribPointer(1, 2, GLES20.GL_FLOAT, false, stride, b);
    }

    private void uni1(Prog p, String n, float v) {
        GLES20.glUniform1f(p.loc(n), v);
    }

    /** Kamera goruntusunu viewport'u dolduracak sekilde (cover) cizer. */
    private void drawCamera(int w, int h) {
        use(camProg);
        int r = (camRotUser + (camFlip180 ? 2 : 0)) & 3;      // toplam 90'lik donus adimi
        float rc = (r == 0) ? 1f : (r == 2 ? -1f : 0f);
        float rs = (r == 1) ? 1f : (r == 3 ? -1f : 0f);
        float va = (float) w / h;
        // SurfaceTexture matrisi goruntuyu zaten dik (portre) yapiyor; 90/270 ek donus yatay yapar
        float ca = (r & 1) == 1 ? camAspect : 1f / camAspect;
        GLES20.glUniformMatrix4fv(camProg.loc("uMat"), 1, false, camMat, 0);
        GLES20.glUniform2f(camProg.loc("uCrop"), va > ca ? 1f : va / ca, va > ca ? ca / va : 1f);
        GLES20.glUniform2f(camProg.loc("uRot"), rc, rs);
        uni1(camProg, "uBright", bgBright);
        float off = bgOff(true);                     // stereo: kamera goruntusu de mercek merkezine kaydirilir (cift goruntu olmasin)
        uni1(camProg, "uOffX", off);
        uni1(camProg, "uScaleX", 1f + Math.abs(off));    // kaydirinca kenarda bosluk kalmasin: yatayda biraz buyut
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0);
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, camTex);
        GLES20.glUniform1i(camProg.loc("uTex"), 0);
        pointers(quadBuf, 2, 16);
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);
    }

    /** Tam ekran arka plan (kamera / sabit duvar kagidi) icin bu gozun NDC yatay kaydirmasi. */
    private float bgOff(boolean camera) {
        if (curEyeSign == 0f) return 0f;
        return curEyeSign * 2f * (lensShift + (camera ? camShift : 0f)) / curEyeW;
    }

    /** Sabit (kafaya bagli) duvar kagidi: viewport'u dolduracak sekilde (cover). */
    private void drawFlat(int w, int h) {
        use(flatProg);
        float va = (float) w / h, ca = bgAspect;
        GLES20.glUniformMatrix4fv(flatProg.loc("uMat"), 1, false, IDENT, 0);
        GLES20.glUniform2f(flatProg.loc("uCrop"), va > ca ? 1f : va / ca, va > ca ? ca / va : 1f);
        GLES20.glUniform2f(flatProg.loc("uRot"), 1f, 0f);
        uni1(flatProg, "uBright", bgBright);
        float off = bgOff(false);
        uni1(flatProg, "uOffX", off);
        uni1(flatProg, "uScaleX", 1f + Math.abs(off));
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0);
        GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, bgTex);
        GLES20.glUniform1i(flatProg.loc("uTex"), 0);
        pointers(quadBuf, 2, 16);
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);
    }

    /** 360 panorama: dunyaya sabit kure (kafa dondukce gorsel sabit kalir). */
    private void drawSphere(float[] pvRot) {
        use(sphereProg);
        GLES20.glUniformMatrix4fv(sphereProg.loc("uMvp"), 1, false, pvRot, 0);
        uni1(sphereProg, "uBright", bgBright);
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0);
        GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, bgTex);
        GLES20.glUniform1i(sphereProg.loc("uTex"), 0);
        pointers(sphereBuf, 3, 20);
        GLES20.glDrawElements(GLES20.GL_TRIANGLES, sphereIdxCount, GLES20.GL_UNSIGNED_SHORT, sphereIdx);
    }

    private void panelUniforms(Prog p, float[] mvp, float hw, float hh, float cv, float expand, float aspect) {
        GLES20.glUniformMatrix4fv(p.loc("uMvp"), 1, false, mvp, 0);
        GLES20.glUniform2f(p.loc("uHalf"), hw, hh);
        uni1(p, "uCurve", cv);
        uni1(p, "uExpand", expand);
        uni1(p, "uAspect", aspect);
        uni1(p, "uCorner", corner);
    }

    private void drawEye(float[] view, float eyeX, int x, int w, int h, float fovY, int n) {
        GLES20.glViewport(x, 0, w, h);
        float[] proj = sProj;
        Matrix.perspectiveM(proj, 0, fovY, (float) w / h, 0.05f, 60f);
        curEyeSign = eyeX < 0f ? 1f : (eyeX > 0f ? -1f : 0f);
        curEyeW = w;
        if (eyeX != 0f && lensShift != 0f) {              // stereo: icerigi mercek merkezine (burna dogru) kaydir
            float s = 2f * lensShift / w;                 // NDC
            proj[8] -= (eyeX < 0f ? 1f : -1f) * s;        // sol goz icerigi saga, sag goz sola
        }
        float[] shift = sShift;
        Matrix.setIdentityM(shift, 0);
        Matrix.translateM(shift, 0, -eyeX, 0f, 0f);
        float[] ve = sVe;
        Matrix.multiplyMM(ve, 0, shift, 0, view, 0);
        float[] pv = sPv;
        Matrix.multiplyMM(pv, 0, proj, 0, ve, 0);

        // ---- arka plan ----
        GLES20.glDisable(GLES20.GL_BLEND);
        if (bgOn) {
            drawCamera(w, h);
        } else if (bgMode == 2 && bgTexReady) {
            if (bgSphere) {
                float[] pvRot = sPvRot;
                Matrix.multiplyMM(pvRot, 0, proj, 0, view, 0);
                drawSphere(pvRot);
            } else {
                drawFlat(w, h);
            }
        }

        // Odaktaki panel one gelir (mesafe azalir); uzaktan yakina cizilir ki en yakin en ustte kalsin
        int[] order = sOrder;
        float[] eff = sEff;
        for (int i = 0; i < n; i++) {
            order[i] = i;
            // Zoom Z: panel mesafesi Z kat azalir, acisal boyutu ~Z kat buyur (1x..8x)
            eff[i] = curDist[i] / (1f + (focusZoom - 1f) * focusAmt[i]);
        }
        for (int a = 1; a < n; a++) {
            int k = order[a], b = a - 1;
            while (b >= 0 && eff[order[b]] < eff[k]) {
                order[b + 1] = order[b];
                b--;
            }
            order[b + 1] = k;
        }

        GLES20.glEnable(GLES20.GL_BLEND);
        float[] model = sModel;
        float[] mvp = sMvp;
        for (int oi = 0; oi < n; oi++) {
            int i = order[oi];
            float asp = panels[i].aspect;
            float hh = PANEL_H / 2 * curScl[i], hw = hh * asp;

            Matrix.setIdentityM(model, 0);
            Matrix.rotateM(model, 0, -curYaw[i], 0f, 1f, 0f);   // negatif yaw = solda
            Matrix.rotateM(model, 0, curPitch[i], 1f, 0f, 0f);   // pozitif pitch = yukari
            Matrix.translateM(model, 0, 0f, 0f, -eff[i]);
            Matrix.multiplyMM(mvp, 0, pv, 0, model, 0);

            // egrilik: izleyici etrafinda silindir (yaricap = panel mesafesi); cok yakin/buyukte yumusar
            float cv = curve <= 0.001f ? 0f : Math.min(curve / eff[i], 1.2f / hw);

            // 1) golge: panelin biraz altinda yumusak koyu hale
            if (shadow > 0.01f) {
                GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA);
                use(shadowProg);
                panelUniforms(shadowProg, mvp, hw, hh, cv, 1.7f, asp);
                uni1(shadowProg, "uStrength", shadow);
                uni1(shadowProg, "uBlur", 0.16f);
                uni1(shadowProg, "uOffY", 0.035f);
                pointers(meshBuf, 2, 16);
                GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, (COLS + 1) * 2);
            }

            // 2) ortam isiltisi: panel iceriginin rengi cevreye yayilir (odaktaki panelde daha guclu)
            if (glow > 0.01f) {
                GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE);
                use(glowProg);
                panelUniforms(glowProg, mvp, hw, hh, cv, 2.0f, asp);
                uni1(glowProg, "uStrength", glow * (0.55f + 0.45f * focusAmt[i]));
                uni1(glowProg, "uSpread", 0.55f);
                GLES20.glActiveTexture(GLES20.GL_TEXTURE0);
                GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, panels[i].tex);
                GLES20.glUniform1i(glowProg.loc("uTex"), 0);
                pointers(meshBuf, 2, 16);
                GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, (COLS + 1) * 2);
            }

            // 3) panelin kendisi: yuvarlak kose, parlaklik, odak cercevesi
            GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA);
            use(panelProg);
            panelUniforms(panelProg, mvp, hw, hh, cv, 1f, asp);
            uni1(panelProg, "uBright", panelBright);
            uni1(panelProg, "uBias", texBias);
            uni1(panelProg, "uSharp", sharpOk ? sharpen : 0f);
            uni1(panelProg, "uFocus", focusRing ? focusAmt[i] : 0f);
            GLES20.glUniform3f(panelProg.loc("uAccent"), 0.36f, 0.62f, 1f);
            GLES20.glActiveTexture(GLES20.GL_TEXTURE0);
            GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, panels[i].tex);
            GLES20.glUniform1i(panelProg.loc("uTex"), 0);
            pointers(meshBuf, 2, 16);
            GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, (COLS + 1) * 2);
        }

        // ---- el lazeri ----
        drawPointer(pv, w, h, fovY);

        // ---- vinyet: kenarlari karartir (lens/ekran kenarinda dikkat dagitmasin) ----
        if (vignette > 0.01f) {
            GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA);
            use(vigProg);
            uni1(vigProg, "uStrength", vignette);
            pointers(quadBuf, 2, 16);
            GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);
        }
        GLES20.glDisable(GLES20.GL_BLEND);
    }

    private static int compile(int type, String src) {
        int s = GLES20.glCreateShader(type);
        GLES20.glShaderSource(s, src);
        GLES20.glCompileShader(s);
        int[] ok = new int[1];
        GLES20.glGetShaderiv(s, GLES20.GL_COMPILE_STATUS, ok, 0);
        if (ok[0] == 0) throw new RuntimeException("shader: " + GLES20.glGetShaderInfoLog(s));
        return s;
    }
}
