package com.vrmonitor;

import java.io.BufferedInputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

/**
 * PC'ye baglanir, [idx:1][toplam:1][uzunluk:4][veri] cerceveleri okur.
 * idx 0..7 = panel JPEG'i, 255 = duzen JSON'u, 254 = komut metni. Koparsa tekrar dener.
 */
final class StreamClient extends Thread implements PanelRenderer.AckSink {
    interface Listener {
        void onStatus(String s);

        void onCommand(String c);
    }

    private final String host;
    private final int port;
    private final PanelRenderer renderer;
    private final Listener listener;
    private volatile boolean running = true;
    private volatile Socket sock;
    private int lastBg = -1;
    private volatile java.io.OutputStream outStream;
    private final Object outLock = new Object();

    /** PC'ye kucuk mesaj: [tur:1][panel:1][uzunluk:2][veri]. Yazma hatasi sessizce yutulur (okuma tarafi baglantiyi yeniler). */
    private void sendMsg(int type, int panel, byte[] payload) {
        java.io.OutputStream o = outStream;
        if (o == null) return;
        byte[] m = new byte[4 + payload.length];
        m[0] = (byte) type;
        m[1] = (byte) panel;
        m[2] = (byte) (payload.length >> 8);
        m[3] = (byte) payload.length;
        System.arraycopy(payload, 0, m, 4, payload.length);
        synchronized (outLock) {
            try {
                o.write(m);
                o.flush();
            } catch (IOException ignored) {
            }
        }
    }

    @Override
    public void sendAck(int panel, long ptsUs) {
        byte[] p = new byte[8];
        for (int i = 0; i < 8; i++) p[i] = (byte) (ptsUs >> (56 - 8 * i));
        sendMsg(1, panel, p);
    }

    @Override
    public void sendKeyReq(int panel) {
        sendMsg(2, panel, new byte[0]);
    }

    private byte[] handBuf = new byte[0];

    @Override
    public void sendHandFrame(int rot, float[] headRows, byte[] jpeg, int len) {
        java.io.OutputStream o = outStream;
        int pl = 37 + len;                                    // [donus:1][bas yonelimi 9 x float][JPEG]
        if (o == null || pl > 65535) return;
        synchronized (outLock) {
            int n = 4 + pl;
            if (handBuf.length < n) handBuf = new byte[n + 8192];
            handBuf[0] = 5;                                  // tur 5: el takibi karesi
            handBuf[1] = 0;
            handBuf[2] = (byte) (pl >> 8);
            handBuf[3] = (byte) pl;
            handBuf[4] = (byte) rot;
            for (int k = 0; k < 9; k++) {
                int b = Float.floatToIntBits(headRows[k]);
                handBuf[5 + k * 4] = (byte) (b >> 24);
                handBuf[6 + k * 4] = (byte) (b >> 16);
                handBuf[7 + k * 4] = (byte) (b >> 8);
                handBuf[8 + k * 4] = (byte) b;
            }
            System.arraycopy(jpeg, 0, handBuf, 41, len);
            try {
                o.write(handBuf, 0, n);
                o.flush();
            } catch (IOException ignored) {
            }
        }
    }

    @Override
    public void sendHit(int seq, int panel, float u, float v) {
        int iu = Math.round(Math.max(0f, Math.min(1f, u)) * 65535f), iv = Math.round(Math.max(0f, Math.min(1f, v)) * 65535f);
        sendMsg(3, panel, new byte[]{(byte) (seq >> 8), (byte) seq, (byte) (iu >> 8), (byte) iu, (byte) (iv >> 8), (byte) iv});
    }

    StreamClient(String host, int port, PanelRenderer renderer, Listener listener) {
        super("stream-client");
        setDaemon(true);
        this.host = host;
        this.port = port;
        this.renderer = renderer;
        this.listener = listener;
    }

    void shutdown() {
        running = false;
        try {
            Socket s = sock;
            if (s != null) s.close();
        } catch (IOException ignored) {
        }
        interrupt();
    }

    @Override
    public void run() {
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_DISPLAY);
        while (running) {
            Socket s = new Socket();
            sock = s;
            try {
                s.setReceiveBufferSize(1 << 20);    // baglanmadan once: buyuk alma penceresi (yuksek kare hizinda gidis-donus beklemesi azalir)
                listener.onStatus("Baglaniyor: " + host + ":" + port);
                s.connect(new InetSocketAddress(host, port), 3000);
                s.setTcpNoDelay(true);
                s.setKeepAlive(true);
                // PC en az saniyede bir kare yollar; 5 sn sessizlik = baglanti olu, hemen yeniden baglan
                s.setSoTimeout(5000);
                listener.onStatus("Bagli: " + host);
                DataInputStream in = new DataInputStream(new BufferedInputStream(s.getInputStream(), 1 << 16));
                outStream = s.getOutputStream();
                renderer.ackSink = this;
                while (running) {
                    int idx = in.readUnsignedByte();
                    int total = in.readUnsignedByte();
                    int len = in.readInt();
                    if (len <= 0 || len > 16_000_000) throw new IOException("bozuk kare");
                    // panel kareleri (JPEG: 0..7, H.264: 16..23) havuzdan alinan tampona okunur (cop uretmez)
                    int pan = idx < PanelRenderer.MAX ? idx
                            : (idx >= 16 && idx < 16 + PanelRenderer.MAX ? idx - 16 : -1);
                    byte[] d = pan >= 0 ? renderer.panels[pan].borrow(len) : new byte[len];
                    in.readFully(d, 0, len);
                    if (idx >= 16 && idx < 16 + PanelRenderer.MAX) {
                        renderer.onVideo(pan, total, d, len);
                    } else if (idx == 255) {
                        // duzen + ortam; PC'deki arka plan modu kamerayi da acip kapatir
                        int m = renderer.applyLayout(new String(d, StandardCharsets.UTF_8));
                        if (m >= 0 && m != lastBg) {
                            lastBg = m;
                            listener.onCommand(m == 1 ? "camera:1" : "camera:0");
                        }
                    } else if (idx == 252) {
                        // el lazeri: [bayrak:1][seq:2][orijin 3 x float][yon 3 x float]
                        if (len >= 27) {
                            java.nio.ByteBuffer bb = java.nio.ByteBuffer.wrap(d, 0, len);
                            int fl = bb.get() & 0xFF;
                            int sq = bb.getShort() & 0xFFFF;
                            float[] od = new float[6];
                            for (int k = 0; k < 6; k++) od[k] = bb.getFloat();
                            renderer.setRay(fl, sq, od);
                        }
                    } else if (idx == 253) {
                        renderer.setBackgroundImage(d);
                    } else if (idx == 254) {
                        listener.onCommand(new String(d, StandardCharsets.UTF_8));
                    } else {
                        renderer.onFrame(idx, total, d, len);
                    }
                }
            } catch (IOException e) {
                if (running) listener.onStatus("Baglanti yok (" + e.getMessage() + "), tekrar deneniyor");
            } finally {
                renderer.ackSink = null;
                outStream = null;
                try {
                    s.close();
                } catch (IOException ignored) {
                }
            }
            if (running) {
                try {
                    Thread.sleep(1000);
                } catch (InterruptedException ignored) {
                }
            }
        }
    }
}
