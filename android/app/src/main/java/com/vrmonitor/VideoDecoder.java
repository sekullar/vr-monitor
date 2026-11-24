package com.vrmonitor;

import android.graphics.SurfaceTexture;
import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Process;
import android.util.Log;
import android.view.Surface;

import java.nio.ByteBuffer;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * Bir panel icin donanim H.264 cozucusu. PC'den gelen Annex-B access unit'leri MediaCodec'e verir,
 * cikis dogrudan bir SurfaceTexture'a (GPU) gider: CPU'da piksel kopyasi / JPEG cozme yok.
 *
 * Gecikme: KEY_LOW_LATENCY + gercek zamanli oncelik, cikista birikmis eski kareler atilir (sadece en yenisi
 * gosterilir). Cozucu takilirsa PC'den anahtar kare istenir.
 */
final class VideoDecoder {
    interface Callbacks {
        void requestKey(int panel);
    }

    private static final class Au {
        byte[] buf;
        int off, len;
        long pts;
        int w, h;
        boolean key;
    }

    private final Panel owner;
    private final Callbacks cb;
    private final ArrayBlockingQueue<Au> queue = new ArrayBlockingQueue<>(4);
    private final HandlerThread frameThread = new HandlerThread("video-frames");
    private final Handler frameHandler;
    private volatile boolean running = true;
    private final Thread thread;

    private MediaCodec codec;
    private Surface surface;
    private final Object stLock = new Object();
    private SurfaceTexture st;
    private int w, h;
    private boolean needKey = true;
    private long lastKeyReq;

    // GL thread'i ile paylasilan durum
    private final java.util.concurrent.atomic.AtomicInteger framesPending = new java.util.concurrent.atomic.AtomicInteger();
    volatile boolean frameAvail;       // (HUD / eski bayrak) en az bir kare bekliyor
    volatile long presentedPts;
    volatile int vw, vh;
    boolean attached;
    int oesTex;

    VideoDecoder(Panel owner, Callbacks cb) {
        this.owner = owner;
        this.cb = cb;
        frameThread.start();
        frameHandler = new Handler(frameThread.getLooper());
        thread = new Thread(this::run, "video-dec-" + owner.index);
        thread.setDaemon(true);
        thread.start();
    }

    /** Ag thread'i. buf, Panel havuzundan alinmis tampon; isimiz bitince geri verilir. */
    void feed(byte[] buf, int off, int len, long pts, int w, int h, boolean key) {
        Au a = new Au();
        a.buf = buf;
        a.off = off;
        a.len = len;
        a.pts = pts;
        a.w = w;
        a.h = h;
        a.key = key;
        if (!queue.offer(a)) {          // cozucu yetismiyor: eskileri at, bir sonraki anahtar kareden devam et
            Au old;
            while ((old = queue.poll()) != null) owner.recycle(old.buf);
            needKeyFlag();
            owner.recycle(buf);
            requestKey();
        }
    }

    private void needKeyFlag() {
        needKey = true;
    }

    private void requestKey() {
        long now = System.nanoTime();
        if (now - lastKeyReq > 300_000_000L) {      // en fazla 300 ms'de bir
            lastKeyReq = now;
            cb.requestKey(owner.index);
        }
    }

    void stop() {
        running = false;
        thread.interrupt();
    }

    /** GL baglami yeniden yaratildi: doku gecersiz, cozucuyu bastan kur. */
    void invalidateGl() {
        attached = false;
        oesTex = 0;
        w = 0;                      // bir sonraki anahtar karede codec yeniden kurulur
        needKey = true;
        requestKey();
    }

    /** GL thread'i: yeni kare varsa dokuya yukler. true = guncellendi; mat = dokuya uygulanacak donusum. */
    boolean latch(float[] mat) {
        synchronized (stLock) {
            if (st == null || oesTex == 0) return false;
            try {
                if (!attached) {
                    st.attachToGLContext(oesTex);
                    attached = true;
                }
                int n = framesPending.getAndSet(0);
                if (n <= 0) return false;
                frameAvail = false;
                // Her updateTexImage kuyruktan YALNIZ BIR kare alir; biriken hepsini tuketip en yenide kal
                // (aksi halde kuyruk buyur: hem gecikme hem kare kaybi)
                for (int i = 0; i < Math.min(n, 4); i++) st.updateTexImage();
                st.getTransformMatrix(mat);
                return true;
            } catch (RuntimeException e) {
                Log.w("VideoDecoder", "latch: " + e);
                return false;
            }
        }
    }

    // ---- cozucu thread'i ----

    private void run() {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_DISPLAY);
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        while (running) {
            Au au = null;
            try {
                au = queue.poll(2, TimeUnit.MILLISECONDS);
            } catch (InterruptedException e) {
                break;
            }
            try {
                if (au != null) handle(au);
                drain(info);
            } catch (RuntimeException e) {          // codec hatasi: sifirla, anahtar kare iste
                Log.w("VideoDecoder", "codec hatasi: " + e);
                releaseCodec();
                w = 0;
                needKey = true;
                requestKey();
            }
        }
        releaseCodec();
        frameThread.quitSafely();
    }

    private void handle(Au au) {
        try {
            if (codec == null || au.w != w || au.h != h) {
                if (!au.key) {               // baslamak icin anahtar kare gerekir
                    requestKey();
                    return;
                }
                startCodec(au);
            }
            if (needKey) {
                if (!au.key) {
                    requestKey();
                    return;
                }
                needKey = false;
            }
            int idx = codec.dequeueInputBuffer(8000);
            if (idx < 0) {                   // cozucu doldu: bu kareyi atamayiz (bagimli kareler bozulur)
                needKey = true;
                requestKey();
                return;
            }
            ByteBuffer ib = codec.getInputBuffer(idx);
            ib.clear();
            ib.put(au.buf, au.off, au.len);
            codec.queueInputBuffer(idx, 0, au.len, au.pts, 0);
        } finally {
            owner.recycle(au.buf);
        }
    }

    private void startCodec(Au first) {
        releaseCodec();
        synchronized (stLock) {
            st = new SurfaceTexture(false);     // GL baglamindan ayrik; GL thread'i attachToGLContext eder
            st.setDefaultBufferSize(first.w, first.h);
            st.setOnFrameAvailableListener(t -> {
                framesPending.incrementAndGet();
                frameAvail = true;
            }, frameHandler);
            surface = new Surface(st);
            attached = false;
        }
        MediaFormat f = MediaFormat.createVideoFormat("video/avc", first.w, first.h);
        try {
            f.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
        } catch (RuntimeException ignored) {
        }
        f.setInteger(MediaFormat.KEY_PRIORITY, 0);                       // gercek zamanli
        f.setInteger(MediaFormat.KEY_OPERATING_RATE, 240);
        f.setInteger("vendor.qti-ext-dec-low-latency.enable", 1);        // Qualcomm: cikis gecikmesi yok
        // SPS/PPS'i ilk anahtar kareden cikarip csd olarak ver (standart yontem)
        ByteBuffer[] csd = extractCsd(first.buf, first.off, first.len);
        if (csd[0] != null) f.setByteBuffer("csd-0", csd[0]);
        if (csd[1] != null) f.setByteBuffer("csd-1", csd[1]);
        MediaCodec c;
        try {
            c = MediaCodec.createDecoderByType("video/avc");
            c.configure(f, surface, null, 0);
            c.start();
        } catch (Exception e) {
            throw new IllegalStateException("codec kurulamadi: " + e.getMessage());
        }
        codec = c;
        w = first.w;
        h = first.h;
        vw = w;
        vh = h;
        needKey = true;
    }

    private void releaseCodec() {
        synchronized (readyLock) {
            ready.clear();
        }
        try {
            if (codec != null) {
                codec.stop();
                codec.release();
            }
        } catch (Exception ignored) {
        }
        codec = null;
        synchronized (stLock) {
            if (surface != null) surface.release();
            surface = null;
            if (st != null) {
                st.setOnFrameAvailableListener(null);
                try {
                    st.release();
                } catch (RuntimeException ignored) {
                }
            }
            st = null;
            attached = false;
        }
    }

    private static final class Out {
        final int idx;
        final long pts;

        Out(int idx, long pts) {
            this.idx = idx;
            this.pts = pts;
        }
    }

    private static final int MAX_WAITING = 2;       // gosterilmeyi bekleyen en fazla kare (gecikme birikmesin)
    private final Object readyLock = new Object();
    private final java.util.ArrayDeque<Out> ready = new java.util.ArrayDeque<>();

    /**
     * Cozulen kareleri HEMEN ekrana vermez, kuyruga koyar. USB/adb tuneli kareleri kumeler halinde (2-3'u bir
     * arada) tasidigi icin "en yeniyi goster, digerini at" 120 Hz'de kare kaybettirir; kuyruktan ekran
     * yenilemesi basina bir kare gostermek kumeleri duzlestirir (bkz. present()).
     */
    private void drain(MediaCodec.BufferInfo info) {
        if (codec == null) return;
        while (true) {
            int o = codec.dequeueOutputBuffer(info, 0);
            if (o >= 0) {
                owner.decN.incrementAndGet();      // HUD: cozucunun urettigi kare sayisi
                owner.noteOut();
                synchronized (readyLock) {
                    ready.addLast(new Out(o, info.presentationTimeUs));
                    while (ready.size() > MAX_WAITING + 1) {     // yetisilemiyor: en eskiyi at
                        Out old = ready.pollFirst();
                        try {
                            codec.releaseOutputBuffer(old.idx, false);
                        } catch (RuntimeException ignored) {
                        }
                        owner.dropN.incrementAndGet();
                    }
                }
            } else if (o == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED || o == MediaCodec.INFO_OUTPUT_BUFFERS_CHANGED) {
                continue;
            } else {
                break;
            }
        }
    }

    /** GL thread'i, her cizimde: sirada bekleyen EN ESKI kareyi ekrana (SurfaceTexture'a) verir. */
    void present() {
        Out r;
        synchronized (readyLock) {
            r = ready.pollFirst();
        }
        if (r == null) return;
        MediaCodec c = codec;
        if (c == null) return;
        try {
            c.releaseOutputBuffer(r.idx, true);
            presentedPts = r.pts;
        } catch (RuntimeException ignored) {          // codec bu arada yeniden kuruldu: kare gecersiz
        }
    }

    /** Annex-B akisindan SPS (tur 7) ve PPS (tur 8) NAL'larini (baslangic koduyla) ayiklar. */
    private static ByteBuffer[] extractCsd(byte[] b, int off, int len) {
        ByteBuffer[] out = new ByteBuffer[2];
        int end = off + len;
        int i = off;
        while (i + 4 < end) {
            int sc = startCode(b, i, end);
            if (sc < 0) break;
            int next = i + sc;
            int j = next;
            int nxtStart = end;
            for (; j + 3 < end; j++) {
                if (b[j] == 0 && b[j + 1] == 0 && (b[j + 2] == 1 || (b[j + 2] == 0 && b[j + 3] == 1))) {
                    nxtStart = j;
                    break;
                }
            }
            int type = b[next] & 0x1F;
            if (type == 7 || type == 8) {
                byte[] nal = new byte[4 + (nxtStart - next)];
                nal[3] = 1;
                System.arraycopy(b, next, nal, 4, nxtStart - next);
                out[type == 7 ? 0 : 1] = ByteBuffer.wrap(nal);
            }
            i = nxtStart;
            if (nxtStart >= end) break;
        }
        return out;
    }

    /** i konumunda baslangic kodu varsa uzunlugunu (3 ya da 4), yoksa ilk koda kadar ilerleyip yine uzunlugu dondurur. */
    private static int startCode(byte[] b, int i, int end) {
        for (int k = i; k + 3 < end; k++) {
            if (b[k] == 0 && b[k + 1] == 0 && b[k + 2] == 1) return k - i + 3;
            if (b[k] == 0 && b[k + 1] == 0 && b[k + 2] == 0 && b[k + 3] == 1) return k - i + 4;
        }
        return -1;
    }
}
