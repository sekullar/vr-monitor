package com.vrmonitor;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.opengl.GLES20;
import android.opengl.GLUtils;
import android.os.Process;

import java.nio.ByteBuffer;
import java.util.ArrayDeque;

/**
 * Tek bir sanal monitor: paralel decode thread'leri ve GL dokusu var.
 * 1080p JPEG tek thread'de ~15-60 ms surebilir; DECODERS thread sirayla kareleri alip paralel cozer,
 * sira numarasiyla en yenisi gosterilir, eski kareler atilir.
 *
 * Takilma azaltma: kare tamponlari havuzdan alinip geri verilir (saniyede 100+ kare x ~300 KB'lik
 * "new byte[]" cop toplayiciyi surekli tetikler ve kare atlamalarina yol acar).
 */
final class Panel {
    static final int DECODERS = 4;

    /** GL_EXT_texture_filter_anisotropic destegi varsa renderer ayarlar (egik panelde net yazi). */
    static volatile float aniso = 0f;

    final int index;

    // Sadece GL thread'i
    int tex = 0, tw = 0, th = 0;

    private Bitmap front;
    private boolean dirty;
    private long shownSeq = 0;
    private final Object frameLock = new Object();

    private final Object ordLock = new Object();                          // cozulen karelerin sirali gosterimi
    private final java.util.TreeSet<Long> inflight = new java.util.TreeSet<>();   // hala cozulmekte olan kare numaralari
    private final Object inLock = new Object();
    private byte[] pending;
    private int pendingLen;
    private long pendingSeq, seqIn;
    private Thread[] threads;
    private volatile boolean running = true;

    private final ArrayDeque<byte[]> pool = new ArrayDeque<>();

    volatile float aspect = 16f / 9f;
    volatile int frames;
    volatile long bytes;

    // Olcum (HUD): cozme / kilit bekleme / GL yukleme sureleri ve sayilari
    final java.util.concurrent.atomic.AtomicLong decNs = new java.util.concurrent.atomic.AtomicLong(),
            lockNs = new java.util.concurrent.atomic.AtomicLong(), upNs = new java.util.concurrent.atomic.AtomicLong(),
            decN = new java.util.concurrent.atomic.AtomicLong(), upN = new java.util.concurrent.atomic.AtomicLong(),
            waitNs = new java.util.concurrent.atomic.AtomicLong(), arrN = new java.util.concurrent.atomic.AtomicLong();

    Panel(int index) {
        this.index = index;
    }

    /** Ag thread'i: kareyi okumak icin tampon (en az len bayt) verir. */
    byte[] borrow(int len) {
        synchronized (pool) {
            byte[] b = pool.pollFirst();
            if (b != null && b.length >= len) return b;
        }
        return new byte[Math.max(len + (len >> 2), 1 << 19)];
    }

    void recycle(byte[] b) {
        if (b == null) return;
        synchronized (pool) {
            if (pool.size() < 8) pool.addLast(b);
        }
    }

    // Aralik olcumu (HUD): ardisik karelerin arasi <3 ms (birlikte gelmis), 3-12 ms (duzenli), >12 ms (bosluk)
    final java.util.concurrent.atomic.AtomicLong[] arrGap = {new java.util.concurrent.atomic.AtomicLong(),
            new java.util.concurrent.atomic.AtomicLong(), new java.util.concurrent.atomic.AtomicLong()};
    final java.util.concurrent.atomic.AtomicLong[] outGap = {new java.util.concurrent.atomic.AtomicLong(),
            new java.util.concurrent.atomic.AtomicLong(), new java.util.concurrent.atomic.AtomicLong()};
    private long lastArrNs, lastOutNs;
    final java.util.concurrent.atomic.AtomicLong dropN = new java.util.concurrent.atomic.AtomicLong();   // kuyruk tasti, atilan kare

    private static void gap(java.util.concurrent.atomic.AtomicLong[] a, long dtNs) {
        a[dtNs < 3_000_000L ? 0 : (dtNs < 12_000_000L ? 1 : 2)].incrementAndGet();
    }

    void noteOut() {
        long now = System.nanoTime();
        if (lastOutNs != 0) gap(outGap, now - lastOutNs);
        lastOutNs = now;
    }

    // ---- donanim H.264 yolu ----
    VideoDecoder video;                 // null = JPEG
    volatile boolean videoMode;
    int fbo, fboW, fboH;                // GL thread'i: H.264 karesinin 2D dokuya cizildigi hedef

    /** Ag thread'i. buf: [damga:8][genislik:2][yukseklik:2][bayrak:1][Annex-B kare]. */
    void submitVideo(byte[] buf, int len, VideoDecoder.Callbacks cb) {
        if (len < 14) {
            recycle(buf);
            return;
        }
        long pts = 0;
        for (int i = 0; i < 8; i++) pts = (pts << 8) | (buf[i] & 0xFFL);
        int w = ((buf[8] & 0xFF) << 8) | (buf[9] & 0xFF);
        int h = ((buf[10] & 0xFF) << 8) | (buf[11] & 0xFF);
        boolean key = (buf[12] & 1) != 0;
        if (w <= 0 || h <= 0) {
            recycle(buf);
            return;
        }
        arrN.incrementAndGet();
        long nowNs = System.nanoTime();
        if (lastArrNs != 0) gap(arrGap, nowNs - lastArrNs);
        lastArrNs = nowNs;
        videoMode = true;
        aspect = (float) w / h;
        if (video == null) video = new VideoDecoder(this, cb);
        video.feed(buf, 13, len - 13, pts, w, h, key);
    }

    /** GL thread'i: hedef dokuyu (RGBA) w x h yapar ve FBO'ya baglar. */
    void ensureFbo(int w, int h) {
        if (fbo != 0 && fboW == w && fboH == h) return;
        ensureTexture();
        GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, tex);
        GLES20.glTexImage2D(GLES20.GL_TEXTURE_2D, 0, GLES20.GL_RGBA, w, h, 0, GLES20.GL_RGBA,
                GLES20.GL_UNSIGNED_BYTE, null);
        if (fbo == 0) {
            int[] f = new int[1];
            GLES20.glGenFramebuffers(1, f, 0);
            fbo = f[0];
        }
        GLES20.glBindFramebuffer(GLES20.GL_FRAMEBUFFER, fbo);
        GLES20.glFramebufferTexture2D(GLES20.GL_FRAMEBUFFER, GLES20.GL_COLOR_ATTACHMENT0, GLES20.GL_TEXTURE_2D, tex, 0);
        GLES20.glBindFramebuffer(GLES20.GL_FRAMEBUFFER, 0);
        fboW = w;
        fboH = h;
        tw = w;
        th = h;
    }

    /** Ag thread'inden. Henuz alinmamis kare varsa ustune yazilir (en yeni kare onemli). */
    void submit(byte[] data, int len) {
        arrN.incrementAndGet();
        videoMode = false;
        byte[] old;
        synchronized (inLock) {
            old = pending;
            pending = data;
            pendingLen = len;
            pendingSeq = ++seqIn;
            if (threads == null) {
                threads = new Thread[DECODERS];
                for (int i = 0; i < DECODERS; i++) {
                    threads[i] = new Thread(this::decodeLoop, "decode-" + index + "-" + i);
                    threads[i].setDaemon(true);
                    threads[i].start();
                }
            }
            inLock.notify();
        }
        recycle(old);   // hic cozulmeden ezilen kare: tamponu havuza geri ver
    }

    void stop() {
        running = false;
        if (video != null) video.stop();
        synchronized (inLock) {
            inLock.notifyAll();
        }
    }

    private void decodeLoop() {
        Process.setThreadPriority(Process.THREAD_PRIORITY_DISPLAY);
        BitmapFactory.Options o = new BitmapFactory.Options();
        o.inPreferredConfig = Bitmap.Config.ARGB_8888;
        o.inMutable = true;
        Bitmap mine = null;   // bu thread'in kendi hedef bitmap'i
        while (running) {
            byte[] data;
            int len;
            long seq;
            long w0 = System.nanoTime();
            synchronized (inLock) {
                while (pending == null && running) {
                    try {
                        inLock.wait();
                    } catch (InterruptedException e) {
                        return;
                    }
                }
                waitNs.addAndGet(System.nanoTime() - w0);   // kare bekleme suresi (cozucu bosta)
                data = pending;
                len = pendingLen;
                seq = pendingSeq;
                pending = null;
            }
            if (data == null) continue;

            synchronized (ordLock) {
                inflight.add(seq);
            }
            try {
                Bitmap bmp;
                o.inBitmap = mine;
                long d0 = System.nanoTime();
                try {
                    bmp = BitmapFactory.decodeByteArray(data, 0, len, o);
                } catch (IllegalArgumentException e) { // cozunurluk degisti, bitmap yeniden kullanilamadi
                    o.inBitmap = null;
                    bmp = BitmapFactory.decodeByteArray(data, 0, len, o);
                }
                long d1 = System.nanoTime();
                decNs.addAndGet(d1 - d0);
                decN.incrementAndGet();
                recycle(data);
                if (bmp == null) continue;       // finally yine calisir

                // Sirali gosterim: daha eski bir kare hala cozuluyorsa kisaca bekle (en fazla 5 ms). Paralel
                // cozucuden eski kare yeniden once biterse o kare atiliyordu (%20'ye varan kare kaybi).
                long deadline = d1 + 5_000_000L;
                synchronized (ordLock) {
                    while (inflight.lower(seq) != null) {
                        long rem = deadline - System.nanoTime();
                        if (rem <= 0) break;
                        try {
                            ordLock.wait(rem / 1_000_000L, (int) (rem % 1_000_000L));
                        } catch (InterruptedException e) {
                            break;
                        }
                    }
                }

                synchronized (frameLock) {
                    lockNs.addAndGet(System.nanoTime() - d1);   // sira bekleme + GL yuklemesi kilidi
                    if (seq > shownSeq) {
                        Bitmap old = front;
                        front = bmp;
                        shownSeq = seq;
                        dirty = true;
                        mine = old;               // eski on bitmap'i bir sonraki decode icin kullan
                        aspect = (float) bmp.getWidth() / bmp.getHeight();
                        frames++;
                        bytes += len;
                    } else {
                        mine = bmp;               // daha yeni kare zaten gosterildi, bunu at
                    }
                }
            } finally {
                synchronized (ordLock) {
                    inflight.remove(seq);
                    ordLock.notifyAll();
                }
            }
        }
    }

    // ---- GL thread ----

    void ensureTexture() {
        if (tex != 0) return;
        int[] t = new int[1];
        GLES20.glGenTextures(1, t, 0);
        tex = t[0];
        GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, tex);
        // Panel ekranda kaynaktan kucuk gorunuyor: mipmap + trilinear olmazsa yazilar tirtiklanir
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR_MIPMAP_LINEAR);
        if (aniso > 1f) GLES20.glTexParameterf(GLES20.GL_TEXTURE_2D, 0x84FE, aniso);
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR);
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE);
        GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE);
        ByteBuffer px = ByteBuffer.allocateDirect(4);
        px.put(new byte[]{30, 30, 40, (byte) 255}).position(0);
        GLES20.glTexImage2D(GLES20.GL_TEXTURE_2D, 0, GLES20.GL_RGBA, 1, 1, 0,
                GLES20.GL_RGBA, GLES20.GL_UNSIGNED_BYTE, px);
        tw = 1;
        th = 1;
    }

    void upload() {
        if (videoMode) return;          // H.264 panelinde doku FBO ile guncellenir
        synchronized (frameLock) {
            if (!dirty || front == null) return;
            long u0 = System.nanoTime();
            GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, tex);
            if (front.getWidth() != tw || front.getHeight() != th) {
                GLUtils.texImage2D(GLES20.GL_TEXTURE_2D, 0, front, 0);
                tw = front.getWidth();
                th = front.getHeight();
            } else {
                GLUtils.texSubImage2D(GLES20.GL_TEXTURE_2D, 0, 0, 0, front);
            }
            GLES20.glGenerateMipmap(GLES20.GL_TEXTURE_2D);
            dirty = false;
            upNs.addAndGet(System.nanoTime() - u0);
            upN.incrementAndGet();
        }
    }
}
