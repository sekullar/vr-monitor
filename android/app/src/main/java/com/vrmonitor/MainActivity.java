package com.vrmonitor;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ActivityInfo;
import android.Manifest;
import android.content.pm.PackageManager;
import android.net.wifi.WifiManager;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.view.ViewGroup;
import android.view.Display;
import android.view.SurfaceHolder;
import android.graphics.Typeface;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.opengl.GLSurfaceView;
import android.opengl.Matrix;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.Surface;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

public class MainActivity extends Activity implements SensorEventListener {
    private static final int PORT = 5000;
    // GL dunyasi (X dogu, Y yukari, -Z kuzey) <- ENU cevirisinin tersi
    private static final float[] CINV = {1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1};

    private PanelRenderer renderer;
    private GLSurfaceView glView;
    private LinearLayout menu;
    private TextView hud, status;
    private EditText hostEdit;
    private StreamClient client;
    private SensorManager sm;
    private Sensor rotSensor;
    private boolean streaming, stereo, calibrating, reverse, cameraWanted;
    private CameraBg cam;
    private WifiManager.WifiLock wifiLock;
    private Button camBtn, focusBtn, focusPullBtn;
    private float maxRefresh = 60f;     // ekranin destekledigi en yuksek yenileme hizi
    private long lastRenderCount, lastRenderTime;
    private int lowHzTicks;
    private long lastDrawNs, lastDrawFrames;
    private long mDn, mUn, mDns, mLns, mUns, mWns, mArr;   // HUD olcum sayaclarinin onceki degerleri
    private float shownRate;
    private long[] mAg = new long[3], mOg = new long[3];
    private long mDrop;
    private static final float[] ZOOM_STEPS = {1f, 1.25f, 1.5f, 2f, 3f, 4f, 6f, 8f};
    private FrameLayout calibHolder;
    private LinearLayout calibMono, calibStereo;
    private final Handler ui = new Handler(Looper.getMainLooper());

    private final float[] r16 = new float[16], r2 = new float[16], rt = new float[16], v = new float[16];
    private float downX, downY, lastX, lastY;
    private boolean moved;
    private final long[] lastFrames = new long[PanelRenderer.MAX];
    private long lastHudTime;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        reverse = getSharedPreferences("vr", MODE_PRIVATE).getBoolean("reverse", false);
        applyOrientation();
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        requestHighRefresh();
        try {
            getWindow().setSustainedPerformanceMode(true);   // isinma yuzunden yavaslamayi azaltir (destekleyen cihazda)
        } catch (RuntimeException ignored) {
        }
        if (Build.VERSION.SDK_INT >= 28) {
            getWindow().getAttributes().layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }

        sm = (SensorManager) getSystemService(Context.SENSOR_SERVICE);
        rotSensor = sm.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR);
        if (rotSensor == null) rotSensor = sm.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR);

        renderer = new PanelRenderer();
        cameraWanted = getSharedPreferences("vr", MODE_PRIVATE).getBoolean("camera", false);
        renderer.camRotUser = getSharedPreferences("vr", MODE_PRIVATE).getInt("camrot", 3);
        renderer.dynamicFocus = getSharedPreferences("vr", MODE_PRIVATE).getBoolean("focus", true);
        renderer.focusZoom = getSharedPreferences("vr", MODE_PRIVATE).getFloat("focuszoom", 1.25f);
        cam = new CameraBg(this, renderer,
                m -> ui.post(() -> Toast.makeText(this, m, Toast.LENGTH_LONG).show()));
        cam.sink = (rot, rows, buf, len) -> {        // el takibi: kamera karesi + o anki bas yonu PC'ye (algilama PC'de)
            PanelRenderer.AckSink s = renderer.ackSink;
            if (s != null) s.sendHandFrame(rot, rows, buf, len);
        };
        glView = new GLSurfaceView(this);
        glView.setEGLContextClientVersion(2);
        glView.setEGLConfigChooser(8, 8, 8, 8, 16, 0);
        glView.setPreserveEGLContextOnPause(true);
        glView.getHolder().addCallback(new SurfaceHolder.Callback() {
            @Override
            public void surfaceCreated(SurfaceHolder holder) {
                // yuzeyin kare hizini ekranin en yuksek hizina oyla (sistem 60'a dusurmesin)
                if (Build.VERSION.SDK_INT >= 30) {
                    holder.getSurface().setFrameRate(maxRefresh, Surface.FRAME_RATE_COMPATIBILITY_DEFAULT);
                }
            }

            @Override
            public void surfaceChanged(SurfaceHolder holder, int format, int w, int h) {
            }

            @Override
            public void surfaceDestroyed(SurfaceHolder holder) {
            }
        });
        glView.setRenderer(renderer);
        glView.setRenderMode(GLSurfaceView.RENDERMODE_CONTINUOUSLY);
        glView.setVisibility(View.GONE);
        glView.setOnTouchListener((view, e) -> {
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    downX = lastX = e.getX();
                    downY = lastY = e.getY();
                    moved = false;
                    break;
                case MotionEvent.ACTION_MOVE:
                    if (Math.abs(e.getX() - downX) + Math.abs(e.getY() - downY) > 25) moved = true;
                    if (moved && !stereo) {
                        renderer.addDrag((e.getX() - lastX) * 0.15f, (e.getY() - lastY) * 0.15f);
                    }
                    lastX = e.getX();
                    lastY = e.getY();
                    break;
                case MotionEvent.ACTION_UP:
                    if (!moved) doCalibrate();
                    break;
            }
            return true;
        });

        hud = new TextView(this);
        hud.setTextColor(Color.rgb(120, 255, 120));
        hud.setTypeface(Typeface.MONOSPACE);
        hud.setTextSize(11);
        hud.setPadding(24, 16, 24, 16);
        hud.setVisibility(View.GONE);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        root.addView(glView, new FrameLayout.LayoutParams(-1, -1));
        root.addView(hud, new FrameLayout.LayoutParams(-2, -2, Gravity.TOP | Gravity.START));
        root.addView(buildCalib(), new FrameLayout.LayoutParams(-1, -1));
        root.addView(buildMenu(), new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);

        handleIntent(getIntent());
    }

    private View buildMenu() {
        menu = new LinearLayout(this);
        menu.setOrientation(LinearLayout.VERTICAL);
        menu.setGravity(Gravity.CENTER);
        menu.setBackgroundColor(Color.rgb(15, 15, 22));
        menu.setPadding(80, 20, 80, 20);

        TextView title = new TextView(this);
        title.setText("VR Monitor");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        title.setGravity(Gravity.CENTER);
        menu.addView(title);

        hostEdit = new EditText(this);
        hostEdit.setHint("PC IP (orn. 192.168.1.5)");
        hostEdit.setHintTextColor(Color.GRAY);
        hostEdit.setTextColor(Color.WHITE);
        hostEdit.setSingleLine(true);
        hostEdit.setInputType(InputType.TYPE_CLASS_TEXT);
        SharedPreferences sp = getSharedPreferences("vr", MODE_PRIVATE);
        hostEdit.setText(sp.getString("host", ""));
        menu.addView(hostEdit, new LinearLayout.LayoutParams(-1, -2));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER);
        Button mono = new Button(this);
        mono.setText("Mono (elde dene)");
        mono.setOnClickListener(x -> start(hostEdit.getText().toString().trim(), false));
        Button st = new Button(this);
        st.setText("Stereo (VR gozluk)");
        st.setOnClickListener(x -> start(hostEdit.getText().toString().trim(), true));
        Button flip = new Button(this);
        flip.setText("Yonu cevir (180)");
        flip.setOnClickListener(x -> {
            reverse = !reverse;
            getSharedPreferences("vr", MODE_PRIVATE).edit().putBoolean("reverse", reverse).apply();
            applyOrientation();
        });
        row.addView(mono);
        row.addView(st);
        row.addView(flip);
        camBtn = new Button(this);
        updateCamBtn();
        camBtn.setOnClickListener(x -> setCamera(!cameraWanted));
        row.addView(camBtn);
        menu.addView(row);

        LinearLayout row2 = new LinearLayout(this);
        row2.setGravity(Gravity.CENTER);
        Button camRotBtn = new Button(this);
        camRotBtn.setText("Kamera yonunu cevir (90)");
        camRotBtn.setOnClickListener(x -> rotateCamera());
        row2.addView(camRotBtn);
        focusBtn = new Button(this);
        updateFocusBtn();
        focusBtn.setOnClickListener(x -> setFocus(!renderer.dynamicFocus));
        row2.addView(focusBtn);
        menu.addView(row2);

        LinearLayout row3 = new LinearLayout(this);
        row3.setGravity(Gravity.CENTER);
        focusPullBtn = new Button(this);
        updateFocusPullBtn();
        focusPullBtn.setOnClickListener(x -> cycleFocusPull());
        row3.addView(focusPullBtn);
        menu.addView(row3);

        status = new TextView(this);
        status.setTextColor(Color.LTGRAY);
        status.setGravity(Gravity.CENTER);
        status.setText("Dokun / Ses -: kalibre et | Ses +: mono/stereo | Geri: menu");
        menu.addView(status);

        menu.setBackground(new GradientDrawable(GradientDrawable.Orientation.TL_BR,
                new int[]{Color.rgb(9, 12, 24), Color.rgb(22, 28, 52)}));
        styleTree(menu);
        accent(mono);
        accent(st);
        title.setTextColor(Color.rgb(170, 200, 255));
        title.setTextSize(28);
        status.setTextColor(Color.rgb(130, 142, 175));
        status.setTextSize(12);
        return menu;
    }

    private int dp(float v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    /** Menudeki dugme ve giris alanlarini ortak koyu, yuvarlak stile sokar. */
    private void styleTree(View v) {
        if (v instanceof Button) {
            Button b = (Button) v;
            GradientDrawable g = new GradientDrawable();
            g.setCornerRadius(dp(14));
            g.setColor(Color.rgb(31, 36, 56));
            g.setStroke(dp(1), Color.argb(80, 140, 170, 255));
            b.setBackground(g);
            b.setTextColor(Color.rgb(225, 232, 255));
            b.setAllCaps(false);
            b.setTextSize(13);
            b.setPadding(dp(14), dp(8), dp(14), dp(8));
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-2, -2);
            lp.setMargins(dp(5), dp(4), dp(5), dp(4));
            b.setLayoutParams(lp);
        } else if (v instanceof EditText) {
            GradientDrawable g = new GradientDrawable();
            g.setCornerRadius(dp(12));
            g.setColor(Color.rgb(20, 24, 40));
            g.setStroke(dp(1), Color.argb(90, 140, 170, 255));
            v.setBackground(g);
            v.setPadding(dp(14), dp(10), dp(14), dp(10));
        }
        if (v instanceof ViewGroup) {
            ViewGroup vg = (ViewGroup) v;
            for (int i = 0; i < vg.getChildCount(); i++) styleTree(vg.getChildAt(i));
        }
    }

    private void accent(Button b) {
        GradientDrawable g = new GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT,
                new int[]{Color.rgb(78, 140, 255), Color.rgb(140, 105, 255)});
        g.setCornerRadius(dp(14));
        b.setBackground(g);
        b.setTextColor(Color.WHITE);
    }

    private void updateCamBtn() {
        camBtn.setText(cameraWanted ? "Kamera arka plan: ACIK" : "Kamera arka plan: kapali");
    }

    /** Arka plani telefonun arka kamerasindan al (gecis modu). */
    private void setCamera(boolean on) {
        cameraWanted = on;
        renderer.cameraOn = on;
        getSharedPreferences("vr", MODE_PRIVATE).edit().putBoolean("camera", on).apply();
        updateCamBtn();
        if (!on) {
            cam.stop();
            return;
        }
        if (streaming) startCameraIfAllowed();
    }

    /**
     * Ekranin ayni cozunurlukteki en yuksek yenileme hizli modunu secer (A36: 120 Hz). Not: Samsung'da
     * Ayarlar > Ekran > Hareket akiciligi "Standart" ise sistem uygulamalara 120 Hz vermez.
     */
    private void requestHighRefresh() {
        try {
            Display disp = null;
            try {
                if (Build.VERSION.SDK_INT >= 30) disp = getDisplay();
            } catch (RuntimeException ignored) {
            }
            if (disp == null) disp = getWindowManager().getDefaultDisplay();
            Display.Mode cur = disp.getMode(), best = cur;
            for (Display.Mode m : disp.getSupportedModes()) {
                if (m.getPhysicalWidth() == cur.getPhysicalWidth() && m.getPhysicalHeight() == cur.getPhysicalHeight()
                        && m.getRefreshRate() > best.getRefreshRate() + 1f) {
                    best = m;
                }
            }
            maxRefresh = best.getRefreshRate();
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.preferredDisplayModeId = best.getModeId();
            lp.preferredRefreshRate = best.getRefreshRate();
            getWindow().setAttributes(lp);
        } catch (RuntimeException ignored) {
        }
    }

    private void updateFocusBtn() {
        focusBtn.setText(renderer.dynamicFocus ? "Dinamik fokus: ACIK" : "Dinamik fokus: kapali");
    }

    /** Dinamik fokus: bakilan (ekranin ortasindaki hayali noktanin denk geldigi) pencere one gelir. */
    private void setFocus(boolean on) {
        renderer.dynamicFocus = on;
        getSharedPreferences("vr", MODE_PRIVATE).edit().putBoolean("focus", on).apply();
        updateFocusBtn();
    }

    private void updateFocusPullBtn() {
        focusPullBtn.setText(String.format(java.util.Locale.US, "Fokus zoom: %.2fx (degistir, en fazla 8x)",
                renderer.focusZoom));
    }

    /** Odaktaki pencerenin yakinlastirma carpanini adim adim degistirir, 8x'e kadar (kaydedilir). */
    private void cycleFocusPull() {
        float cur = renderer.focusZoom;
        int idx = 0;
        for (int i = 0; i < ZOOM_STEPS.length; i++) if (ZOOM_STEPS[i] <= cur + 0.01f) idx = i;
        float next = ZOOM_STEPS[(idx + 1) % ZOOM_STEPS.length];
        renderer.focusZoom = next;
        getSharedPreferences("vr", MODE_PRIVATE).edit().putFloat("focuszoom", next).apply();
        updateFocusPullBtn();
    }

    /** Kamera goruntusu yan/ters ise 90 derece cevir (kaydedilir). */
    private void rotateCamera() {
        int r = (renderer.camRotUser + 1) & 3;
        renderer.camRotUser = r;
        getSharedPreferences("vr", MODE_PRIVATE).edit().putInt("camrot", r).apply();
        Toast.makeText(this, "Kamera donusu: " + (r * 90) + " derece", Toast.LENGTH_SHORT).show();
    }

    private boolean handCamWanted;

    /** PC el takibi icin telefon kamerasi karesi istiyor / artik istemiyor (kaydedilmez; PC her baglantida bildirir). */
    private void setHandCam(boolean on) {
        handCamWanted = on;
        if (!on) {
            cam.setHand(false);
            return;
        }
        if (!streaming) return;
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            cam.setHand(true);
        } else {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, 7);
        }
    }

    private void startCameraIfAllowed() {
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            cam.start();
        } else {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, 7);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] res) {
        super.onRequestPermissionsResult(code, perms, res);
        if (code != 7) return;
        if (res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) {
            if (cameraWanted && streaming) cam.start();
            if (handCamWanted && streaming) cam.setHand(true);
        } else {
            Toast.makeText(this, "Kamera izni verilmedi", Toast.LENGTH_LONG).show();
            setCamera(false);
            handCamWanted = false;
        }
    }

    /** Wi-Fi uyku moduna girmesin: aralikli kopmalarin ana sebebi. */
    private void acquireWifiLock() {
        try {
            if (wifiLock == null) {
                WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
                wifiLock = wm.createWifiLock(Build.VERSION.SDK_INT >= 29
                        ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY : WifiManager.WIFI_MODE_FULL_HIGH_PERF, "vrmonitor");
                wifiLock.setReferenceCounted(false);
            }
            if (!wifiLock.isHeld()) wifiLock.acquire();
        } catch (RuntimeException ignored) {
        }
    }

    private void releaseWifiLock() {
        try {
            if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        } catch (RuntimeException ignored) {
        }
    }

    /** Ekran dondurme kapali: sabit yatay. Ters gorunuyorsa menuden 180 cevrilir. */
    private void applyOrientation() {
        setRequestedOrientation(reverse
                ? ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE
                : ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
    }

    private View buildCalib() {
        calibHolder = new FrameLayout(this);
        calibHolder.setVisibility(View.GONE);

        // Mono: metin + buton
        calibMono = new LinearLayout(this);
        calibMono.setOrientation(LinearLayout.VERTICAL);
        calibMono.setGravity(Gravity.CENTER);
        calibMono.setBackgroundColor(Color.argb(210, 0, 0, 0));
        calibMono.setPadding(40, 24, 40, 24);
        TextView t = new TextView(this);
        t.setText("Telefonu / gozlugu ekranin tam ortasina bakacak sekilde duz tut.\nHazir olunca HAZIRIM'a bas.");
        t.setTextColor(Color.WHITE);
        t.setTextSize(16);
        t.setGravity(Gravity.CENTER);
        calibMono.addView(t);
        Button ready = new Button(this);
        ready.setText("HAZIRIM");
        ready.setOnClickListener(x -> doCalibrate());
        calibMono.addView(ready);
        calibHolder.addView(calibMono,
                new FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL));

        // Stereo: her goze ayni metin (dokunmatige erisilemez, ses - ile onaylanir)
        calibStereo = new LinearLayout(this);
        calibStereo.setOrientation(LinearLayout.HORIZONTAL);
        calibStereo.setBackgroundColor(Color.argb(210, 0, 0, 0));
        for (int i = 0; i < 2; i++) {
            TextView s = new TextView(this);
            s.setText("Duz bak\nHazir olunca SES - tusuna bas");
            s.setTextColor(Color.WHITE);
            s.setTextSize(14);
            s.setGravity(Gravity.CENTER);
            s.setPadding(10, 20, 10, 20);
            calibStereo.addView(s, new LinearLayout.LayoutParams(0, -2, 1f));
        }
        calibHolder.addView(calibStereo, new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM));
        return calibHolder;
    }

    private void showCalib() {
        calibrating = true;
        calibHolder.setVisibility(View.VISIBLE);
        calibMono.setVisibility(stereo ? View.GONE : View.VISIBLE);
        calibStereo.setVisibility(stereo ? View.VISIBLE : View.GONE);
    }

    /** Su anki bakisi "tam karsi" yap. Acilista Hazirim, sonra istedigin an tekrar kullanilir. */
    private void doCalibrate() {
        renderer.recenter();
        if (calibrating) {
            calibrating = false;
            calibHolder.setVisibility(View.GONE);
        }
        Toast.makeText(this, "Kalibre edildi", Toast.LENGTH_SHORT).show();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleIntent(intent);
    }

    private void handleIntent(Intent i) {
        if (i == null) return;
        String host = i.getStringExtra("host");
        if (host != null && !host.isEmpty()) {
            hostEdit.setText(host);
            if (i.hasExtra("camera")) setCamera(i.getBooleanExtra("camera", false));
            start(host, i.getBooleanExtra("stereo", false));
        }
    }

    private void start(String host, boolean st) {
        if (host.isEmpty()) {
            Toast.makeText(this, "PC IP gir", Toast.LENGTH_SHORT).show();
            return;
        }
        if (rotSensor == null) Toast.makeText(this, "Donus sensoru yok!", Toast.LENGTH_LONG).show();
        getSharedPreferences("vr", MODE_PRIVATE).edit().putString("host", host).apply();
        if (client != null) client.shutdown();
        stereo = st;
        renderer.stereo = st;
        renderer.recenter();
        client = new StreamClient(host, PORT, renderer, new StreamClient.Listener() {
            @Override
            public void onStatus(String s) {
                ui.post(() -> status.setText(s));
            }

            @Override
            public void onCommand(String c) {
                // PC'deki erisim panelinden "Telefonu ortala"
                if ("recenter".equals(c)) ui.post(MainActivity.this::doCalibrate);
                else if ("camera:1".equals(c)) ui.post(() -> setCamera(true));
                else if ("camera:0".equals(c)) ui.post(() -> setCamera(false));
                else if ("camrot".equals(c)) ui.post(MainActivity.this::rotateCamera);
                else if ("handcam:1".equals(c)) ui.post(() -> setHandCam(true));
                else if ("handcam:0".equals(c)) ui.post(() -> setHandCam(false));
                else if ("focus:1".equals(c)) ui.post(() -> setFocus(true));
                else if ("focus:0".equals(c)) ui.post(() -> setFocus(false));
            }
        });
        client.start();
        streaming = true;
        showCalib();
        menu.setVisibility(View.GONE);
        glView.setVisibility(View.VISIBLE);
        hud.setVisibility(st ? View.GONE : View.VISIBLE);
        registerSensor();
        acquireWifiLock();
        renderer.cameraOn = cameraWanted;
        if (cameraWanted) startCameraIfAllowed();
        ui.removeCallbacks(hudTick);
        ui.post(hudTick);
    }

    private void stopStreaming() {
        streaming = false;
        calibrating = false;
        calibHolder.setVisibility(View.GONE);
        if (client != null) client.shutdown();
        client = null;
        sm.unregisterListener(this);
        releaseWifiLock();
        cam.stopAll();
        handCamWanted = false;
        renderer.cameraOn = false;
        ui.removeCallbacks(hudTick);
        glView.setVisibility(View.GONE);
        hud.setVisibility(View.GONE);
        menu.setVisibility(View.VISIBLE);
    }

    private void registerSensor() {
        if (rotSensor != null) sm.registerListener(this, rotSensor, SensorManager.SENSOR_DELAY_FASTEST);
        Sensor g = sm.getDefaultSensor(Sensor.TYPE_GYROSCOPE);        // hareket tahmini icin acisal hiz
        if (g != null) sm.registerListener(this, g, SensorManager.SENSOR_DELAY_FASTEST);
    }

    private float gx, gy, gz;                                      // son jiroskop hizi (rad/s, cihaz ekseni)
    private final float[] rd = new float[9], rtmp = new float[9];

    /**
     * Baş takibi gecikmesini telafi eder: sensor ornegi ile ekranda isik olusmasi arasi ~20-35 ms var.
     * Yonelimi jiroskop hiziyla o kadar ileri tasir: R' = R * exp([w]x dt)  (R satir-sirali, cihaz->dunya).
     */
    private void predict(float[] r) {
        float dt = renderer.predictMs / 1000f;
        float mag = (float) Math.sqrt(gx * gx + gy * gy + gz * gz);
        if (dt <= 0f || mag < 0.03f) return;                       // durgun: gurultuyu buyutme
        float th = Math.min(mag * dt, 0.25f);                      // en fazla ~14 derece
        float kx = gx / mag, ky = gy / mag, kz = gz / mag;
        float s = (float) Math.sin(th), c = (float) Math.cos(th), v = 1f - c;
        rd[0] = c + kx * kx * v;      rd[1] = kx * ky * v - kz * s; rd[2] = kx * kz * v + ky * s;
        rd[3] = ky * kx * v + kz * s; rd[4] = c + ky * ky * v;      rd[5] = ky * kz * v - kx * s;
        rd[6] = kz * kx * v - ky * s; rd[7] = kz * ky * v + kx * s; rd[8] = c + kz * kz * v;
        for (int i = 0; i < 3; i++) {                              // rtmp = R(3x3) * Rd
            for (int j = 0; j < 3; j++) {
                rtmp[i * 3 + j] = r[i * 4] * rd[j] + r[i * 4 + 1] * rd[3 + j] + r[i * 4 + 2] * rd[6 + j];
            }
        }
        for (int i = 0; i < 3; i++) {
            for (int j = 0; j < 3; j++) r[i * 4 + j] = rtmp[i * 3 + j];
        }
    }

    private final Runnable hudTick = new Runnable() {
        @Override
        public void run() {
            long now = System.currentTimeMillis();
            float dt = Math.max(1, now - lastHudTime) / 1000f;
            StringBuilder sb = new StringBuilder();
            sb.append(status.getText()).append('\n');
            // ekran hizi (Hz) ve saniyede cizilen kare: 120 Hz'de ~120 olmali, 60 ise sistem 120'yi vermiyor
            long rc = renderer.renderFrames, nowT = System.nanoTime();
            float rdt = lastRenderTime == 0 ? 1f : (nowT - lastRenderTime) / 1e9f;
            sb.append(String.format(java.util.Locale.US, "ekran %.0f Hz | cizim %.0f fps%n",
                    getWindowManager().getDefaultDisplay().getRefreshRate(), (rc - lastRenderCount) / rdt));
            lastRenderCount = rc;
            lastRenderTime = nowT;
            // Samsung adaptif yenileme bazen ekrani 60 Hz'e dusurur (basinda uzun sure dokunma yok / pil / isinma):
            // 3 saniye ust uste dusukse 120 Hz'i yeniden iste
            float hzNow = getWindowManager().getDefaultDisplay().getRefreshRate();
            if (streaming && maxRefresh > 61f && hzNow < maxRefresh - 5f) {
                if (++lowHzTicks >= 3) {
                    lowHzTicks = 0;
                    requestHighRefresh();
                    android.util.Log.i("VRM", "ekran " + Math.round(hzNow) + " Hz'e dusmus: yuksek yenileme yeniden istendi");
                }
            } else {
                lowHzTicks = 0;
            }
            long dn2 = renderer.drawNsSum, dc2 = Math.max(1, renderer.renderFrames - lastDrawFrames);
            sb.append(String.format(java.util.Locale.US, "ciz %.1f ms ort | %.1f ms maks%n",
                    (dn2 - lastDrawNs) / 1e6 / dc2, renderer.drawNsMax / 1e6));
            lastDrawNs = dn2;
            lastDrawFrames = renderer.renderFrames;
            renderer.drawNsMax = 0;
            if (renderer.dynamicFocus) {
                int f = renderer.focusedIdx;
                sb.append("odak: ").append(f >= 0 ? "P" + (f + 1) : "-").append('\n');
            }
            for (int i = 0; i < Math.min(renderer.count, PanelRenderer.MAX); i++) {
                Panel p = renderer.panels[i];
                long f = p.frames;
                shownRate = (f - lastFrames[i]) / dt;
                sb.append(String.format("P%d  %4.1f fps  %dx%d %s%n", i + 1, shownRate, p.tw, p.th,
                        p.videoMode ? "H264-HW" : "JPEG"));
                lastFrames[i] = f;
                if (i == 0) {   // olcum: cozme / kilit / GL yukleme / bosta bekleme (ms, ortalama)
                    long dn = p.decN.get(), un = p.upN.get(), dns = p.decNs.get(), lns = p.lockNs.get(),
                            uns = p.upNs.get(), wns = p.waitNs.get();
                    long ddn = Math.max(1, dn - mDn), dun = Math.max(1, un - mUn), an = p.arrN.get();
                    sb.append(String.format(java.util.Locale.US,
                            "  gelen %.0f/s | cozulen %.0f/s | gosterilen %.0f/s%n  coz %.1f ms | kilit %.1f | yukle %.1f | bosta %.0f ms/s%n",
                            (an - mArr) / dt, (dn - mDn) / dt, shownRate,
                            (dns - mDns) / 1e6 / ddn, (lns - mLns) / 1e6 / ddn, (uns - mUns) / 1e6 / dun,
                            (wns - mWns) / 1e6 / dt / 4));
                    mDn = dn; mUn = un; mDns = dns; mLns = lns; mUns = uns; mWns = wns; mArr = an;
                    if (p.videoMode) {   // kare araliklari: gelen / cozucu cikisi  (kisa=birlikte gelen, bosluk=>12ms)
                        long[] ag = {p.arrGap[0].get(), p.arrGap[1].get(), p.arrGap[2].get()};
                        long[] og = {p.outGap[0].get(), p.outGap[1].get(), p.outGap[2].get()};
                        sb.append(String.format(java.util.Locale.US,
                                "  aralik gelen <3ms:%d 3-12:%d >12:%d | atilan %d/s%n",
                                ag[0] - mAg[0], ag[1] - mAg[1], ag[2] - mAg[2],
                                p.dropN.get() - mDrop));
                        mDrop = p.dropN.get();
                        mAg = ag;
                        mOg = og;
                    }
                }
            }
            lastHudTime = now;
            hud.setText(sb);
            // Gozlukte HUD gizli: ayni olcumleri logcat'e de yaz (adb logcat -s VRM) + isinma durumu
            int th = -1;
            try {
                if (Build.VERSION.SDK_INT >= 29) {
                    th = ((android.os.PowerManager) getSystemService(Context.POWER_SERVICE)).getCurrentThermalStatus();
                }
            } catch (RuntimeException ignored) {
            }
            android.util.Log.i("VRM", sb.toString().replace('\n', '|') + " isinma=" + th);
            if (streaming) ui.postDelayed(this, 1000);
        }
    };

    @Override
    public void onSensorChanged(SensorEvent e) {
        if (e.sensor.getType() == Sensor.TYPE_GYROSCOPE) {         // yalnizca hizi sakla; cizim yonelim vektorunden gelir
            gx = e.values[0];
            gy = e.values[1];
            gz = e.values[2];
            return;
        }
        SensorManager.getRotationMatrixFromVector(r16, e.values);
        predict(r16);
        int ax, ay;
        switch (getWindowManager().getDefaultDisplay().getRotation()) {
            case Surface.ROTATION_90:
                ax = SensorManager.AXIS_Y;
                ay = SensorManager.AXIS_MINUS_X;
                break;
            case Surface.ROTATION_270:
                ax = SensorManager.AXIS_MINUS_Y;
                ay = SensorManager.AXIS_X;
                break;
            case Surface.ROTATION_180:
                ax = SensorManager.AXIS_MINUS_X;
                ay = SensorManager.AXIS_MINUS_Y;
                break;
            default:
                ax = SensorManager.AXIS_X;
                ay = SensorManager.AXIS_Y;
        }
        // kamera goruntusu ekran yonune gore ters mi (arka kamera: sensor yonu - ekran donusu)
        int rd = getWindowManager().getDefaultDisplay().getRotation() * 90;
        renderer.camFlip180 = ((cam.sensorOrientation - rd + 360) % 360) == 180;
        SensorManager.remapCoordinateSystem(r16, ax, ay, r2);
        // SensorManager matrisleri satir-sirali (cihaz->dunya). OpenGL sutun-sirali okur, yani r2
        // GL gozunde zaten dunya->cihaz (view) matrisi. Ek transpoz YAPMA.
        Matrix.multiplyMM(v, 0, r2, 0, CINV, 0);
        // v tahmin payi (predictMs) kadar ileriyi gosterir: gecerli oldugu an = olcum ani + pay (el karesi eslemesi icin)
        renderer.setHead(v, e.timestamp + (long) (renderer.predictMs * 1e6f));
    }

    @Override
    public void onAccuracyChanged(Sensor sensor, int accuracy) {
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (streaming && keyCode == KeyEvent.KEYCODE_VOLUME_UP) {
            stereo = !stereo;
            renderer.stereo = stereo;
            hud.setVisibility(stereo ? View.GONE : View.VISIBLE);
            if (calibrating) showCalib();
            return true;
        }
        if (streaming && keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) {
            doCalibrate();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public void onBackPressed() {
        if (streaming) stopStreaming();
        else super.onBackPressed();
    }

    @Override
    protected void onResume() {
        super.onResume();
        glView.onResume();
        if (streaming) {
            registerSensor();
            if (cameraWanted && checkSelfPermission(Manifest.permission.CAMERA)
                    == PackageManager.PERMISSION_GRANTED) cam.start();
            if (handCamWanted && checkSelfPermission(Manifest.permission.CAMERA)
                    == PackageManager.PERMISSION_GRANTED) cam.setHand(true);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        glView.onPause();
        sm.unregisterListener(this);
        cam.stopAll();                    // arka planda kamera kullanilamaz; onResume'da geri acilir
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (client != null) client.shutdown();
        renderer.shutdown();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
        }
    }
}
