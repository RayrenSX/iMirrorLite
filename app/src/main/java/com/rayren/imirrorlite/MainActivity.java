package com.rayren.imirrorlite;

import android.app.*;
import android.annotation.SuppressLint;
import android.content.*;
import android.content.res.Configuration;
import android.graphics.*;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.content.res.ColorStateList;
import android.hardware.usb.*;
import android.os.*;
import android.view.*;
import android.widget.*;
import com.chaquo.python.Python;
import com.chaquo.python.PyObject;
import com.chaquo.python.android.AndroidPlatform;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;

public final class MainActivity extends Activity implements SurfaceHolder.Callback {
    private static final String USB_PERMISSION = "com.rayren.imirrorlite.USB_PERMISSION";
    private static final int INK = Color.rgb(28,43,42), BLUE = Color.rgb(13,132,119);
    private static final int PAGE = Color.rgb(245,247,246), MUTED = Color.rgb(112,128,124);
    private static final int BORDER = Color.rgb(221,230,226), STAGE = Color.rgb(8,12,13);
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final AtomicBoolean running = new AtomicBoolean();
    private final AtomicBoolean cancelRequested = new AtomicBoolean();
    private final VideoDecoder decoder = new VideoDecoder(error -> {
        onStatus("error", error.getMessage());
        ui.post(() -> { if (!this.destroyed) stopSession(); });
    }, this::onVideoRendered);
    private UsbManager manager;
    private SessionLog diagnostics;
    private TextView connectionText, controlText, previewHint, stats;
    private Button connect, fullscreenButton;
    private LinearLayout emptyState;
    private TextView emptyTitle;
    private View connectionDot;
    private LinearLayout navigation;
    private boolean fullscreen;
    private FrameLayout preview;
    private SurfaceView texture;
    private Surface surface;
    private TouchRouter touch;
    private volatile PyObject runtime;
    private volatile UsbHost usb;
    private volatile WirelessHost wirelessHost;
    private boolean wirelessSession;
    private TextView transport;
    private String pendingSerial, activeDeviceName;
    private volatile boolean waiting, connecting;
    private boolean sessionError;
    private volatile boolean hasRendered;
    private volatile boolean destroyed;
    private volatile int attempt;
    private long waitDeadline;
    private volatile int videoWidth, videoHeight;
    private volatile long frameCount;
    private volatile long renderedCount;
    private volatile long sessionGeneration;
    private long lastReceivedCount, lastRenderedCount;
    private long lastStats;
    private long lastDecoderStats;
    private int requestedVideoWidth, requestedVideoHeight;
    private VideoProfile usbProfile, wirelessProfile;
    private volatile VideoProfile activeProfile;
    private int renderWidth,renderHeight;
    private volatile int surfaceBufferWidth,surfaceBufferHeight;
    private String restartAfterStop="";
    private boolean foreground;
    private final Set<String> permissionsAsked = new HashSet<>();
    private final List<View> portraitChrome = new ArrayList<>();
    private LinearLayout mirrorBody;
    private LinearLayout page;
    private ScrollView shortcutScroll;
    private GridLayout shortcutGrid;
    private final Map<String,Button> shortcutButtons = new LinkedHashMap<>();
    private final Set<String> availableShortcuts = new HashSet<>();
    private boolean shortcutBusy;
    private final Button[] navigationButtons = new Button[3];
    private final String[] navigationActions = new String[3];
    private static final String[] NAV_DEFAULTS={"none","home","app_switcher"};
    private static final String[] NAV_POSITIONS={"左侧","中间","右侧"};
    private static final String[] ACTION_IDS={"none","home","app_switcher","control_center","notification_center","volume_down","volume_up","siri","lock"};
    private static final String[] ACTION_TITLES={"未设置","主页","多任务","控制中心","通知中心","音量 −","音量 +","Siri","锁屏"};

    /** Bind JNI/Python callbacks to the session which created them. */
    public final class SessionCallbacks {
        private final long generation;
        SessionCallbacks(long generation) { this.generation=generation; }
        private boolean current() { return !destroyed && generation==sessionGeneration; }
        public long sessionId() { return generation; }
        public boolean isCancelled() { return !current() || cancelRequested.get(); }
        public boolean hasRenderedVideo() { return current() && hasRendered; }
        public void onVideo(byte[] data,byte[] csd,int w,int h,String mime,long pts) throws Exception {
            if(!isCancelled()) MainActivity.this.onVideo(data,csd,w,h,mime,pts);
        }
        public void onVideoTimed(byte[] data,byte[] csd,int w,int h,String mime,long pts,long sourceUs) throws Exception {
            if(!isCancelled()) MainActivity.this.onVideo(data,csd,w,h,mime,pts,sourceUs);
        }
        public void onDiagnostic(String message) { if(current()) MainActivity.this.onDiagnostic(message); }
        public void onStatus(String kind,String message) { if(current()) MainActivity.this.onStatus(kind,message); }
        public void onControl(int capacity) { if(current()) MainActivity.this.onControl(capacity); }
        public void onShortcuts(String actions) { if(current()) MainActivity.this.onShortcuts(actions); }
        public void onShortcutResult(String action,String error) { if(current()) MainActivity.this.onShortcutResult(action,error); }
        public void onAirPlayReady(int port,String name,String txt) throws Exception {
            if(!isCancelled()) MainActivity.this.onAirPlayReady(port,name,txt);
        }
        public void onAirPlayPin(String pin) { if(current()) MainActivity.this.onAirPlayPin(pin); }
        public void onVideoDisconnected() { if(current()) MainActivity.this.onVideoDisconnected(); }
    }

    private void postSession(Runnable action) {
        long generation=sessionGeneration;
        ui.post(() -> { if(!destroyed && generation==sessionGeneration) action.run(); });
    }

    @SuppressLint("UnspecifiedRegisterReceiverFlag") // Explicit flags on API 33+, platform overload unavailable on older Android.
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        manager = (UsbManager)getSystemService(USB_SERVICE);
        diagnostics = new SessionLog(this);
        usbProfile=loadVideoProfile(false); wirelessProfile=loadVideoProfile(true); activeProfile=usbProfile;
        touch = new TouchRouter(this::sendTouch);
        buildUi();
        IntentFilter filter = new IntentFilter(USB_PERMISSION);
        filter.addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED);
        filter.addAction(UsbManager.ACTION_USB_DEVICE_DETACHED);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(receiver, filter, getPackageName()+".USB_INTERNAL", null);
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        diagnostics.record("usb", "Existing activity received USB attach");
        if (waiting) { ui.removeCallbacks(pollUsb); ui.post(pollUsb); }
    }

    private void buildUi() {
        page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        page.setPadding(dp(16), dp(8), dp(16), dp(8));
        page.setBackgroundColor(PAGE);
        page.setOnApplyWindowInsetsListener((v, insets) -> {
            int left=0, top=0, right=0, bottom=0;
            if(insets.getDisplayCutout()!=null) {
                left=insets.getDisplayCutout().getSafeInsetLeft(); top=insets.getDisplayCutout().getSafeInsetTop();
                right=insets.getDisplayCutout().getSafeInsetRight(); bottom=insets.getDisplayCutout().getSafeInsetBottom();
            }
            int padding=fullscreen?0:dp(16);
            v.setPadding(padding+left, (fullscreen?0:dp(8))+top, padding+right, (fullscreen?0:dp(8))+bottom);
            return insets;
        });
        setContentView(page);
        LinearLayout header = new LinearLayout(this); header.setGravity(Gravity.CENTER_VERTICAL);
        ImageView mark = new ImageView(this); mark.setImageDrawable(new UiIcon("brand",BLUE,dp(24)));
        mark.setPadding(dp(9),dp(9),dp(9),dp(9)); mark.setBackground(round(Color.rgb(226,242,236),14));
        header.addView(mark,new LinearLayout.LayoutParams(dp(42),dp(42)));
        LinearLayout brand = new LinearLayout(this); brand.setOrientation(LinearLayout.VERTICAL);
        TextView title = label("iMirror", 20, INK); title.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));
        brand.addView(title);
        TextView lite = label("LITE  /  iPHONE 投屏", 9, MUTED); lite.setLetterSpacing(.12f); brand.addView(lite);
        LinearLayout.LayoutParams brandParams=new LinearLayout.LayoutParams(-2,-2); brandParams.leftMargin=dp(10);
        header.addView(brand,brandParams);
        Space spacer = new Space(this); header.addView(spacer, new LinearLayout.LayoutParams(0,1,1));
        connect = uiButton("连接 iPhone",12,Color.WHITE);
        connect.setBackground(buttonSurface(BLUE,0,14));
        header.addView(connect,new LinearLayout.LayoutParams(dp(108),dp(44)));
        connect.setOnClickListener(v -> { if(running.get() || connecting) stopSession(); else chooseConnection(); });
        Button help = uiButton("",12,MUTED); help.setContentDescription("使用说明");
        help.setCompoundDrawables(null,new UiIcon("help",MUTED,dp(22)),null,null);
        help.setBackground(buttonSurface(Color.TRANSPARENT,0,14)); help.setOnClickListener(v -> showHelp());
        LinearLayout.LayoutParams helpParams=new LinearLayout.LayoutParams(dp(44),dp(44)); helpParams.leftMargin=dp(4);
        header.addView(help,helpParams);
        page.addView(header,new LinearLayout.LayoutParams(-1,dp(56)));
        portraitChrome.add(header);
        LinearLayout card = new LinearLayout(this); card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(12),dp(9),dp(12),dp(9)); card.setBackground(outline(Color.WHITE,BORDER,14));
        LinearLayout statusRow=new LinearLayout(this); statusRow.setGravity(Gravity.CENTER_VERTICAL);
        connectionDot=new View(this); connectionDot.setBackground(round(Color.rgb(164,180,174),4));
        LinearLayout.LayoutParams dotParams=new LinearLayout.LayoutParams(dp(6),dp(6)); dotParams.rightMargin=dp(8);
        statusRow.addView(connectionDot,dotParams);
        connectionText = label("等待连接", 12, INK); connectionText.setSingleLine();
        connectionText.setEllipsize(android.text.TextUtils.TruncateAt.END);
        statusRow.addView(connectionText,new LinearLayout.LayoutParams(0,-2,1));
        transport=label("USB / Wi-Fi",9,BLUE); transport.setLetterSpacing(.1f); statusRow.addView(transport);
        Button settings=uiButton("画面设置",10,BLUE);
        settings.setContentDescription("分辨率、帧率和解码器设置");
        settings.setBackground(buttonSurface(Color.TRANSPARENT,0,8)); settings.setOnClickListener(v -> showVideoSettings());
        LinearLayout.LayoutParams settingsParams=new LinearLayout.LayoutParams(dp(68),dp(30)); settingsParams.leftMargin=dp(6);
        statusRow.addView(settings,settingsParams);
        card.addView(statusRow);
        controlText = label("连接后自动检测触控能力", 10, MUTED); controlText.setSingleLine();
        controlText.setEllipsize(android.text.TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams controlParams=new LinearLayout.LayoutParams(-1,-2); controlParams.topMargin=dp(3); controlParams.leftMargin=dp(14);
        card.addView(controlText,controlParams);
        LinearLayout.LayoutParams cardParams=new LinearLayout.LayoutParams(-1,-2); cardParams.topMargin=dp(6); cardParams.bottomMargin=dp(12);
        page.addView(card,cardParams); portraitChrome.add(card);

        mirrorBody = new LinearLayout(this);
        mirrorBody.setOrientation(LinearLayout.VERTICAL);
        page.addView(mirrorBody,new LinearLayout.LayoutParams(-1,0,1));

        preview = new FrameLayout(this); preview.setBackground(round(STAGE, 20)); preview.setClipToOutline(true);
        LinearLayout.LayoutParams box = new LinearLayout.LayoutParams(-1,0,1); mirrorBody.addView(preview, box);
        texture = new SurfaceView(this); texture.getHolder().addCallback(this);
        FrameLayout.LayoutParams tv = new FrameLayout.LayoutParams(-1,-1,Gravity.CENTER); preview.addView(texture,tv);
        texture.setContentDescription("iPhone 投屏与触控区域");
        texture.setOnTouchListener((v,e) -> {
            if(shortcutBusy) return true;
            if(e.getActionMasked()==MotionEvent.ACTION_UP) v.performClick();
            return touch.onTouch(e,v.getWidth(),v.getHeight());
        });
        emptyState=new LinearLayout(this); emptyState.setOrientation(LinearLayout.VERTICAL);
        emptyState.setBackgroundColor(STAGE);
        emptyState.setGravity(Gravity.CENTER); emptyState.setPadding(dp(20),dp(20),dp(20),dp(68));
        ImageView phone=new ImageView(this); phone.setImageDrawable(new UiIcon("device",Color.rgb(111,203,182),dp(48)));
        phone.setPadding(dp(24),dp(24),dp(24),dp(24)); phone.setBackground(outline(Color.rgb(18,33,31),Color.rgb(39,64,57),32));
        emptyState.addView(phone,new LinearLayout.LayoutParams(dp(96),dp(96)));
        emptyTitle=label("连接你的 iPhone",20,Color.rgb(236,243,239));
        emptyTitle.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL));
        LinearLayout.LayoutParams emptyTitleParams=new LinearLayout.LayoutParams(-2,-2); emptyTitleParams.topMargin=dp(24);
        emptyState.addView(emptyTitle,emptyTitleParams);
        previewHint = label("支持 USB 或同一 Wi-Fi 无线连接\n点上方「连接 iPhone」选择连接方式", 12, Color.rgb(133,154,145));
        previewHint.setGravity(Gravity.CENTER); previewHint.setLineSpacing(dp(6),1);
        LinearLayout.LayoutParams hintParams=new LinearLayout.LayoutParams(-1,-2); hintParams.topMargin=dp(12);
        emptyState.addView(previewHint,hintParams);
        preview.addView(emptyState,new FrameLayout.LayoutParams(-1,-1));
        preview.addOnLayoutChangeListener((v,l,t,r,b,ol,ot,or,ob) -> fitVideo());
        shortcutGrid = new GridLayout(this);
        shortcutScroll = new ScrollView(this); shortcutScroll.setFillViewport(false); shortcutScroll.setVerticalScrollBarEnabled(false);
        shortcutScroll.setBackground(outline(Color.WHITE,BORDER,16));
        shortcutScroll.setPadding(dp(4),dp(4),dp(4),dp(4));
        shortcutScroll.addView(shortcutGrid);
        mirrorBody.addView(shortcutScroll,new LinearLayout.LayoutParams(-1,dp(56)));
        addShortcut("control_center", "控制中心"); addShortcut("notification_center", "通知中心");
        addShortcut("volume_down", "音量 −");
        addShortcut("volume_up", "音量 +"); addShortcut("siri", "Siri"); addShortcut("lock", "锁屏");
        stats = label("",10,MUTED);
        stats.setMaxLines(2);
        page.addView(stats,new LinearLayout.LayoutParams(-1,-2)); portraitChrome.add(stats);
        navigation=new LinearLayout(this); navigation.setGravity(Gravity.CENTER);
        navigation.setBackgroundColor(Color.BLACK);
        preview.addView(navigation,new FrameLayout.LayoutParams(-1,dp(48),Gravity.BOTTOM|Gravity.CENTER_HORIZONTAL));
        SharedPreferences preferences=getSharedPreferences("navigation_bindings",MODE_PRIVATE);
        for(int i=0;i<3;i++) {
            final int slot=i;
            String saved=preferences.getString("slot_"+i,NAV_DEFAULTS[i]);
            navigationActions[i]=Arrays.asList(ACTION_IDS).contains(saved)?saved:NAV_DEFAULTS[i];
            Button button=uiButton("",9,Color.rgb(186,200,193));
            button.setCompoundDrawablePadding(dp(3));
            button.setBackground(buttonSurface(Color.TRANSPARENT,0,12));
            button.setOnClickListener(v -> sendShortcut(navigationActions[slot]));
            button.setOnLongClickListener(v -> { configureNavigation(slot); return true; });
            navigationButtons[i]=button; navigation.addView(button,new LinearLayout.LayoutParams(0,-1,1));
        }
        fullscreenButton=uiButton("全屏",10,Color.rgb(227,237,231));
        fullscreenButton.setContentDescription("全屏显示 iPhone");
        fullscreenButton.setPadding(dp(10),0,dp(10),0);
        fullscreenButton.setCompoundDrawables(new UiIcon("fullscreen",Color.rgb(227,237,231),dp(14)),null,null,null);
        fullscreenButton.setCompoundDrawablePadding(dp(6));
        fullscreenButton.setBackground(buttonSurface(Color.argb(215,19,25,23),Color.argb(80,133,154,145),20));
        FrameLayout.LayoutParams fullParams=new FrameLayout.LayoutParams(dp(72),dp(40),Gravity.TOP|Gravity.END);
        fullParams.setMargins(dp(10),dp(10),dp(10),dp(10)); preview.addView(fullscreenButton,fullParams);
        fullscreenButton.setOnClickListener(v -> setFullscreen(!fullscreen));
        Button overlaySettings=uiButton("设置",10,Color.rgb(227,237,231));
        overlaySettings.setContentDescription("画面设置");
        overlaySettings.setBackground(buttonSurface(Color.argb(215,19,25,23),0,16));
        FrameLayout.LayoutParams overlayParams=new FrameLayout.LayoutParams(dp(52),dp(36),Gravity.TOP|Gravity.START);
        overlayParams.setMargins(dp(10),dp(10),dp(10),dp(10)); preview.addView(overlaySettings,overlayParams);
        overlaySettings.setOnClickListener(v -> showVideoSettings());
        updateShortcutButtons();
        adaptOrientation();
    }

    private void chooseConnection() {
        new AlertDialog.Builder(this).setTitle("连接方式")
            .setItems(new String[]{"USB 投屏与控制", "无线投屏与控制"},(dialog,which) -> {
                if(which==0) discover();
                else new AlertDialog.Builder(this).setTitle("无线连接")
                    .setMessage("安卓和 iPhone 连接同一 Wi-Fi。启动后，在 iPhone 控制中心 → 屏幕镜像选择 iMirror Lite。\n\n无线控制需要 iOS 18+、开发者模式，并先用 USB 连接一次以保存无线配对。若重启 iPhone 后控制不可用，请再用 USB 准备开发者环境。")
                    .setPositiveButton("启动无线连接",(d,w) -> startWireless()).setNegativeButton("取消",null).show();
            }).show();
    }

    private VideoProfile loadVideoProfile(boolean wireless) {
        SharedPreferences saved=getSharedPreferences("video_profiles",MODE_PRIVATE);
        String prefix=wireless?"wireless_":"usb_";
        try {
            return new VideoProfile(saved.getInt(prefix+"height",wireless?1080:0),
                saved.getInt(prefix+"fps",60),saved.getString(prefix+"decoder","auto"),wireless);
        } catch(IllegalArgumentException | ClassCastException error) {
            return new VideoProfile(wireless?1080:0,60,"auto",wireless);
        }
    }

    private Spinner optionSpinner(String[] options,int selected) {
        Spinner spinner=new Spinner(this);
        ArrayAdapter<String> adapter=new ArrayAdapter<>(this,android.R.layout.simple_spinner_item,options);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        spinner.setAdapter(adapter); spinner.setSelection(selected); return spinner;
    }

    private void showVideoSettings() {
        new AlertDialog.Builder(this).setTitle("画面设置")
            .setItems(new String[]{"有线 · 本地渲染", "无线 · AirPlay 协商"},(d,which) -> editVideoProfile(which==1)).show();
    }

    private void editVideoProfile(boolean wireless) {
        VideoProfile previous=wireless?wirelessProfile:usbProfile;
        LinearLayout form=new LinearLayout(this); form.setOrientation(LinearLayout.VERTICAL); form.setPadding(dp(20),dp(8),dp(20),dp(12));
        TextView note=label(wireless?
            "向 iPhone 请求所选分辨率上限和帧率。修改后重新启动无线接收，请在 iPhone 重新选择屏幕镜像。实际输出取决于 iPhone 和网络。":
            "仅改变本机渲染尺寸和显示帧率，保持 iPhone 原始视频接收与解码。画面保持比例，不放大超过原始尺寸。",12,MUTED);
        note.setLineSpacing(dp(4),1); form.addView(note); space(form,12);
        int offset=wireless?1:0, selectedHeight=0;
        String[] resolutions=new String[VideoProfile.HEIGHTS.length-offset];
        for(int i=0;i<resolutions.length;i++) {
            int height=VideoProfile.HEIGHTS[i+offset];
            resolutions[i]=new VideoProfile(height,previous.fps,"auto",wireless).resolutionLabel();
            if(height==previous.height) selectedHeight=i;
        }
        form.addView(label(wireless?"协商分辨率上限":"渲染分辨率上限",12,INK));
        Spinner resolution=optionSpinner(resolutions,selectedHeight); form.addView(resolution,new LinearLayout.LayoutParams(-1,dp(48)));
        String[] rates=new String[VideoProfile.FRAME_RATES.length]; int selectedRate=0;
        for(int i=0;i<rates.length;i++) {
            rates[i]=VideoProfile.FRAME_RATES[i]+" fps";
            if(VideoProfile.FRAME_RATES[i]==previous.fps) selectedRate=i;
        }
        space(form,8); form.addView(label(wireless?"协商帧率上限":"显示帧率上限",12,INK));
        Spinner frameRate=optionSpinner(rates,selectedRate); form.addView(frameRate,new LinearLayout.LayoutParams(-1,dp(48)));
        List<VideoDecoder.Choice> choices=VideoDecoder.choices(); int selectedDecoder=0;
        for(int i=0;i<choices.size();i++) if(choices.get(i).name.equals(previous.decoder)) selectedDecoder=i;
        if(selectedDecoder==0 && !previous.decoder.equals("auto")) {
            choices.add(new VideoDecoder.Choice(previous.decoder,previous.decoder+"（当前不可用，将自动回退）")); selectedDecoder=choices.size()-1;
        }
        String[] decoderLabels=new String[choices.size()];
        for(int i=0;i<choices.size();i++) decoderLabels[i]=choices.get(i).label;
        space(form,8); form.addView(label("本机解码器",12,INK));
        Spinner decoderChoice=optionSpinner(decoderLabels,selectedDecoder); form.addView(decoderChoice,new LinearLayout.LayoutParams(-1,dp(48)));
        form.addView(label("自动优先低延迟硬解。指定解码器不支持当前视频或启动失败时自动回退。更换解码器需要重连。120 fps 不会增加源视频的帧数。",11,MUTED));
        ScrollView scroll=new ScrollView(this); scroll.addView(form);
        new AlertDialog.Builder(this).setTitle(wireless?"无线画面设置":"有线画面设置").setView(scroll)
            .setNegativeButton("取消",null).setPositiveButton("应用",(dialog,which) -> {
                VideoProfile next=new VideoProfile(VideoProfile.HEIGHTS[resolution.getSelectedItemPosition()+offset],
                    VideoProfile.FRAME_RATES[frameRate.getSelectedItemPosition()],choices.get(decoderChoice.getSelectedItemPosition()).name,wireless);
                if(next.height==previous.height && next.fps==previous.fps && next.decoder.equals(previous.decoder)) return;
                String prefix=wireless?"wireless_":"usb_";
                getSharedPreferences("video_profiles",MODE_PRIVATE).edit().putInt(prefix+"height",next.height)
                    .putInt(prefix+"fps",next.fps).putString(prefix+"decoder",next.decoder).apply();
                if(wireless) wirelessProfile=next; else usbProfile=next;
                diagnostics.record("settings",(wireless?"Wireless negotiate ":"USB render ")+next.summary()+" decoder="+next.decoder);
                if(running.get() && wirelessSession==wireless) {
                    if(wireless || !next.decoder.equals(previous.decoder)) {
                        stopSession(); restartAfterStop=wireless?"wireless":"usb";
                        Toast.makeText(this,wireless?"已应用，请在 iPhone 重新选择屏幕镜像":"正在按新解码器重新连接",Toast.LENGTH_LONG).show();
                    } else {
                        activeProfile=next; decoder.setOutputOptions(next.fps,next.decoder,false); fitVideo();
                        Toast.makeText(this,"渲染设置已应用",Toast.LENGTH_SHORT).show();
                    }
                } else Toast.makeText(this,"已保存，下次连接使用",Toast.LENGTH_SHORT).show();
            }).show();
    }

    private void startWireless() {
        if(surface==null || destroyed || !running.compareAndSet(false,true)) return;
        cancelRequested.set(false); wirelessSession=true; transport.setText("Wi-Fi");
        final VideoProfile profile=wirelessProfile; activeProfile=profile;
        decoder.setOutputOptions(profile.fps,profile.decoder,true);
        diagnostics.record("settings","Wireless request "+profile.summary()+" decoder="+profile.decoder);
        SessionCallbacks callbacks=new SessionCallbacks(++sessionGeneration);
        activeDeviceName=null; sessionError=false; hasRendered=false;
        frameCount=renderedCount=0; lastReceivedCount=lastRenderedCount=0;
        lastStats=SystemClock.elapsedRealtime(); videoWidth=videoHeight=0;
        lastDecoderStats=0; requestedVideoWidth=requestedVideoHeight=0;
        connect.setText("断开连接"); connectionText.setText("正在启动无线接收…");
        controlText.setText("正在发现无线设备");
        previewHint.setText("安卓和 iPhone 连接同一 Wi-Fi\n在 iPhone 屏幕镜像中选择 iMirror Lite");
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        new Thread(() -> {
            try {
                wirelessHost=new WirelessHost(getApplicationContext(),new WirelessHost.Listener() {
                    public void device(String json) {
                        PyObject module=runtime;
                        if(module!=null && !callbacks.isCancelled()) module.callAttr("device",json);
                    }
                    public void status(String kind,String message) {
                        if(callbacks.isCancelled()) return;
                        callbacks.onStatus(kind,message);
                        if(kind.equals("error")) postSession(() -> stopSession());
                    }
                });
                if(!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
                runtime=Python.getInstance().getModule("imirror.runtime");
                if(cancelRequested.get() || destroyed) return;
                runtime.callAttr("run_wireless",callbacks,getFilesDir().getAbsolutePath(),"iMirror Lite · "+Build.MODEL,
                    profile.canvasWidth(),profile.height,profile.fps);
            } catch(Exception error) { onStatus("error",error.getMessage()==null?error.toString():error.getMessage()); }
            finally {
                if(wirelessHost!=null) wirelessHost.close(); wirelessHost=null;
                onStopped(callbacks.generation);
            }
        },"imirror-wireless").start();
    }

    public void onAirPlayReady(int port,String name,String txt) throws Exception {
        WirelessHost host=wirelessHost;
        if(host!=null && !cancelRequested.get()) host.start(port,name,txt);
        diagnostics.record("wireless","AirPlay listening on port "+port);
    }

    public void onAirPlayPin(String pin) {
        postSession(() -> {
            if(destroyed || cancelRequested.get()) return;
            if(pin.isEmpty()) {
                emptyTitle.setText("正在建立无线投屏");
                previewHint.setText("配对成功，正在等待 iPhone 画面");
            } else {
                emptyTitle.setText("配对码："+pin);
                previewHint.setText("请在 iPhone 输入此配对码\n配对码有效期为 3 分钟");
                connectionText.setText("请在 iPhone 输入安卓显示的配对码");
            }
        });
    }

    public void onVideoDisconnected() {
        // Release the old codec before the receiver admits another stream;
        // a delayed UI callback must never close that next stream's codec.
        if(destroyed || !wirelessSession || cancelRequested.get()) return;
        decoder.close(); hasRendered=false;
        requestedVideoWidth=requestedVideoHeight=0;
        postSession(() -> {
            if(destroyed || !wirelessSession || cancelRequested.get()) return;
            if(hasRendered) return;
            touch.setCapacity(0);
            emptyState.setVisibility(View.VISIBLE); emptyTitle.setText("等待重新投屏");
            connectionText.setText("无线画面已断开，重新选择屏幕镜像即可连接");
            connectionDot.setBackground(round(Color.rgb(225,174,66),4));
        });
    }

    private void discover() {
        wirelessSession=false; transport.setText("USB");
        if (surface == null) { connectionText.setText("预览窗口尚未准备好，请稍后重试"); return; }
        ArrayList<UsbDevice> phones = new ArrayList<>();
        for (UsbDevice d: manager.getDeviceList().values()) if(d.getVendorId()==0x05ac) phones.add(d);
        diagnostics.record("usb", "Discovery: Apple devices="+phones.size());
        if (phones.isEmpty()) { connectionText.setText("未发现 iPhone，请检查数据线与安卓 OTG"); return; }
        attempt++; connecting = true;
        connect.setText("取消连接");
        connectionDot.setBackground(round(Color.rgb(225,174,66),4));
        if (phones.size() == 1) authorize(phones.get(0));
        else {
            String[] names = new String[phones.size()];
            for(int i=0;i<names.length;i++) names[i] = phones.get(i).getProductName() + " · " + phones.get(i).getDeviceName();
            new AlertDialog.Builder(this).setTitle("选择要连接的 iPhone").setItems(names,(d,which)->authorize(phones.get(which)))
                    .setOnCancelListener(dialog -> stopSession()).show();
        }
    }

    private void authorize(UsbDevice device) {
        diagnostics.record("usb", "Authorize PID="+device.getProductId()+" configurations="+device.getConfigurationCount()+" video="+UsbHost.hasVideo(device));
        if(manager.hasPermission(device)) { authorized(device); return; }
        permissionsAsked.add(device.getDeviceName());
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT>=31 ? PendingIntent.FLAG_MUTABLE : 0);
        PendingIntent pending = PendingIntent.getBroadcast(this,attempt,
                new Intent(USB_PERMISSION).setPackage(getPackageName()).putExtra("attempt",attempt),flags);
        connectionText.setText("请允许访问 USB 设备");
        manager.requestPermission(device,pending);
    }

    private void authorized(UsbDevice device) {
        if (destroyed || running.get() || !connecting) return;
        String serial;
        try { serial = UsbHost.normalizeSerial(device.getSerialNumber()); }
        catch (Exception e) { stopSession(); connectionText.setText("无法读取 USB 序列号，请重新授权"); return; }
        if (serial == null) { connectionText.setText("无法识别 iPhone，请重新连接"); return; }
        if (waiting && pendingSerial != null && !pendingSerial.equals(serial)) return;
        // A Valeria descriptor only advertises a configuration. Selecting it
        // does not start iOS's capture service: request the vendor transition
        // once for every new connection, including devices exposing it early.
        if(!waiting) {
            waiting = true; pendingSerial = serial; waitDeadline = SystemClock.elapsedRealtime()+25000;
            connect.setText("取消连接"); connectionText.setText("正在启用 iPhone USB 投屏");
            diagnostics.record("usb", "Request QuickTime mode");
            int requestAttempt=attempt;
            new Thread(() -> {
                if(destroyed || !waiting || attempt!=requestAttempt) return;
                try { diagnostics.record("usb",UsbHost.requestVideo(manager, device)); }
                catch(Exception e) { if(attempt==requestAttempt && waiting) onStatus("error",e.toString()); }
                ui.postDelayed(() -> { if(attempt==requestAttempt && waiting) pollUsb.run(); }, 700);
            }, "usb-activate").start();
            return;
        }
        if(!UsbHost.hasVideo(device)) return;
        waiting = false; connecting = false; pendingSerial = null; ui.removeCallbacks(pollUsb);
        activeDeviceName = device.getDeviceName();
        if (!running.compareAndSet(false,true)) return;
        cancelRequested.set(false);
        activeProfile=usbProfile; decoder.setOutputOptions(usbProfile.fps,usbProfile.decoder,false);
        diagnostics.record("settings","USB render "+usbProfile.summary()+" decoder="+usbProfile.decoder);
        SessionCallbacks callbacks=new SessionCallbacks(++sessionGeneration);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        hasRendered = false;
        sessionError = false;
        frameCount = renderedCount = 0; lastReceivedCount = lastRenderedCount = 0;
        lastStats = SystemClock.elapsedRealtime(); videoWidth = videoHeight = 0;
        lastDecoderStats=0; requestedVideoWidth=requestedVideoHeight=0;
        connect.setText("断开连接"); connectionText.setText("正在启动 USB 会话…");
        controlText.setText("正在准备触控通道");
        new Thread(() -> {
            try {
                usb = new UsbHost(manager,device);
                diagnostics.record("usb",usb.describe());
                if(!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
                runtime = Python.getInstance().getModule("imirror.runtime");
                if(cancelRequested.get() || destroyed) return;
                runtime.callAttr("run",usb,callbacks,getFilesDir().getAbsolutePath());
            } catch(Exception e) { onStatus("error",e.getMessage()==null?e.toString():e.getMessage()); }
            finally {
                if(usb!=null) usb.close(); usb=null;
                onStopped(callbacks.generation);
            }
        },"imirror-session").start();
    }

    private final Runnable pollUsb = new Runnable() {
        public void run() {
            if(!waiting || destroyed) return;
            if(SystemClock.elapsedRealtime()>waitDeadline) {
                diagnostics.record("error", "QuickTime re-enumeration timed out");
                waiting=false; connecting=false; connect.setText("连接 iPhone");
                connectionText.setText("投屏模式未就绪，请解锁 iPhone 后重新插拔数据线"); return;
            }
            for(UsbDevice d:manager.getDeviceList().values()) {
                if(d.getVendorId()!=0x05ac || !UsbHost.hasVideo(d)) continue;
                if(manager.hasPermission(d)) authorized(d);
                else if(!permissionsAsked.contains(d.getDeviceName())) authorize(d);
                if(!waiting) return;
            }
            ui.postDelayed(this,700);
        }
    };

    private final BroadcastReceiver receiver = new BroadcastReceiver() {
        @Override public void onReceive(Context context,Intent intent) {
            UsbDevice device = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE);
            if(device==null) return;
            if(USB_PERMISSION.equals(intent.getAction())) {
                if (!connecting || intent.getIntExtra("attempt",-1)!=attempt) return;
                diagnostics.record("usb", "Permission result="+intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED,false));
                if(intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED,false) && manager.hasPermission(device)) authorized(device);
                else { stopSession(); connectionText.setText("USB 访问未授权，点击连接可重试"); }
            } else if(UsbManager.ACTION_USB_DEVICE_ATTACHED.equals(intent.getAction()) && waiting) {
                ui.removeCallbacks(pollUsb); ui.post(pollUsb);
            } else if(UsbManager.ACTION_USB_DEVICE_DETACHED.equals(intent.getAction()) && running.get()
                      && device.getDeviceName().equals(activeDeviceName)) {
                onStatus("error","USB 数据线已断开"); stopSession();
            }
        }
    };

    private void sendTouch(String json) {
        PyObject r=runtime;
        if(r!=null && running.get()) {
            try { r.callAttr("submit",json); }
            catch(RuntimeException e) { onStatus("control","触控发送失败，请重新连接"); stopSession(); }
        }
    }
    private void stopSession() {
        restartAfterStop="";
        cancelRequested.set(true);
        availableShortcuts.clear(); shortcutBusy=false; updateShortcutButtons();
        waiting=false; connecting=false; attempt++; pendingSerial=null; permissionsAsked.clear(); ui.removeCallbacks(pollUsb); touch.cancel();
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        connect.setText(running.get()?"正在断开…":"连接 iPhone");
        if(running.get()) {
            connect.setEnabled(false);
            PyObject sessionRuntime=runtime;
            long generation=sessionGeneration;
            new Thread(() -> {
                if(sessionRuntime!=null) sessionRuntime.callAttr("stop",generation);
            },"imirror-stop").start();
        }
    }

    // Called by Chaquopy worker threads.
    public void onDiagnostic(String message) { diagnostics.record("protocol",message); }
    public boolean isCancelled() { return cancelRequested.get(); }
    public boolean hasRenderedVideo() { return hasRendered; }
    public void onStatus(String kind,String message) { diagnostics.record(kind,message);
        if(kind.equals("diagnostic")) return;
        postSession(() -> {
        if(destroyed) return;
        if(cancelRequested.get()) return;
        if(kind.equals("control")) controlText.setText(message);
        else { connectionText.setText(message); if(kind.equals("error")) { sessionError=true; connectionDot.setBackground(round(Color.rgb(206,107,88),4)); touch.setCapacity(0); availableShortcuts.clear(); updateShortcutButtons(); } }
    }); }
    public void onShortcuts(String actions) { diagnostics.record("shortcut", "Available="+actions); postSession(() -> {
        availableShortcuts.clear(); shortcutBusy=false;
        if(!destroyed && !cancelRequested.get() && !actions.isEmpty()) availableShortcuts.addAll(Arrays.asList(actions.split(",")));
        updateShortcutButtons();
    }); }
    public void onShortcutResult(String action,String error) { postSession(() -> {
        shortcutBusy=false; updateShortcutButtons();
        if(!destroyed && !error.isEmpty()) Toast.makeText(this,error,Toast.LENGTH_SHORT).show();
    }); }
    public void onControl(int capacity) { diagnostics.record("hid","Channel capacity="+capacity); postSession(() -> {
        if(destroyed || (capacity>0 && cancelRequested.get())) return; touch.setCapacity(capacity);
        if(capacity>1) controlText.setText("触控通道已连接 · 最多 "+capacity+" 指");
        else if(capacity==1) controlText.setText("触控通道已连接 · 仅单指");
    }); }
    public void onVideo(byte[] data,byte[] csd,int width,int height,String mime,long pts) throws Exception {
        onVideo(data,csd,width,height,mime,pts,-1);
    }
    private void onVideo(byte[] data,byte[] csd,int width,int height,String mime,long pts,long sourceUs) throws Exception {
        if (destroyed || cancelRequested.get()) return;
        if(frameCount==0) diagnostics.record("video","First encoded frame: "+width+"x"+height+" "+mime);
        decoder.feed(data,csd,width,height,mime,pts,sourceUs); frameCount++;
        if(requestedVideoWidth!=width || requestedVideoHeight!=height) {
            requestedVideoWidth=width; requestedVideoHeight=height;
            postSession(() -> {
                if(cancelRequested.get()) return;
                touch.cancel(); videoWidth=width; videoHeight=height; fitVideo();
                if (!hasRendered) connectionText.setText("视频流已收到，正在解码");
            });
        }
        long now=SystemClock.elapsedRealtime();
        if(now-lastDecoderStats>5000) {
            lastDecoderStats=now; diagnostics.record("decoder",decoder.counters());
        }
        if(now-lastStats>1000) {
            float seconds=(now-lastStats)/1000f;
            float received=(frameCount-lastReceivedCount)/seconds, displayed=(renderedCount-lastRenderedCount)/seconds;
            lastStats=now; lastReceivedCount=frameCount; lastRenderedCount=renderedCount;
            String message=String.format(Locale.ROOT,"接收 %d × %d · %.1f fps · 渲染 %d × %d · %.1f fps",
                width,height,received,surfaceBufferWidth,surfaceBufferHeight,displayed);
            diagnostics.record("video-rate",message);
            postSession(() -> stats.setText(message));
        }
    }
    public void onStopped() {
        onStopped(sessionGeneration);
    }
    private void onStopped(long generation) {
        diagnostics.record("session","Stopped; encoded frames="+frameCount);
        // Transport cleanup must finish before a new session is allowed.
        ui.post(() -> {
            if(generation!=sessionGeneration) return;
            if(usb!=null) return;
            running.set(false); decoder.close(); touch.setCapacity(0); runtime=null;
            availableShortcuts.clear(); shortcutBusy=false; updateShortcutButtons();
            if(destroyed) return;
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            connect.setEnabled(true); connect.setText("连接 iPhone");
            connectionDot.setBackground(round(sessionError?Color.rgb(206,107,88):Color.rgb(164,180,174),4));
            if (!sessionError) connectionText.setText("连接已断开");
            controlText.setText("控制已断开");
            emptyTitle.setText("准备好，再次连接");
            previewHint.setText("解锁 iPhone，检查 USB 或 Wi-Fi\n点「连接 iPhone」继续投屏"); emptyState.setVisibility(View.VISIBLE);
            String restart=restartAfterStop; restartAfterStop="";
            if(foreground) {
                if(restart.equals("wireless")) startWireless();
                else if(restart.equals("usb")) discover();
            }
        });
    }

    private void fitVideo() {
        int availableHeight=preview.getHeight()-dp(48);
        if(preview.getWidth()==0 || availableHeight<=0) return;
        int w=preview.getWidth(), h=availableHeight;
        if(videoWidth>0 && videoHeight>0) {
            float scale=Math.min((float)w/videoWidth,(float)h/videoHeight);
            w=Math.max(1,Math.round(videoWidth*scale)); h=Math.max(1,Math.round(videoHeight*scale));
            int[] size=wirelessSession?new int[]{videoWidth,videoHeight}:activeProfile.renderSize(videoWidth,videoHeight);
            renderWidth=size[0]; renderHeight=size[1];
            if(surfaceBufferWidth!=renderWidth || surfaceBufferHeight!=renderHeight) {
                surfaceBufferWidth=renderWidth; surfaceBufferHeight=renderHeight;
                texture.getHolder().setFixedSize(renderWidth,renderHeight);
                diagnostics.record("render","Surface "+renderWidth+"x"+renderHeight+"; source="+videoWidth+"x"+videoHeight);
            }
        }
        int top=Math.max(0,(preview.getHeight()-h-dp(48))/2);
        FrameLayout.LayoutParams p=(FrameLayout.LayoutParams)texture.getLayoutParams();
        if(p.width!=w || p.height!=h || p.topMargin!=top || p.gravity!=(Gravity.TOP|Gravity.CENTER_HORIZONTAL)) {
            touch.cancel(); p.width=w; p.height=h; p.topMargin=top; p.gravity=Gravity.TOP|Gravity.CENTER_HORIZONTAL; texture.setLayoutParams(p);
        }
        FrameLayout.LayoutParams nav=(FrameLayout.LayoutParams)navigation.getLayoutParams();
        if(nav.width!=w || nav.topMargin!=top+h || nav.gravity!=(Gravity.TOP|Gravity.CENTER_HORIZONTAL)) {
            nav.width=w; nav.height=dp(48); nav.topMargin=top+h; nav.gravity=Gravity.TOP|Gravity.CENTER_HORIZONTAL; navigation.setLayoutParams(nav);
        }
    }
    private void adaptOrientation() {
        boolean landscape=getResources().getConfiguration().orientation==Configuration.ORIENTATION_LANDSCAPE;
        for(View v:portraitChrome) v.setVisibility(fullscreen || (landscape && v!=portraitChrome.get(0))?View.GONE:View.VISIBLE);
        mirrorBody.setOrientation(landscape?LinearLayout.HORIZONTAL:LinearLayout.VERTICAL);
        preview.setLayoutParams(new LinearLayout.LayoutParams(landscape?0:-1,landscape?-1:0,1));
        shortcutScroll.setVisibility(fullscreen?View.GONE:View.VISIBLE);
        LinearLayout.LayoutParams rail=new LinearLayout.LayoutParams(landscape?dp(116):-1,landscape?-1:dp(72));
        if(landscape) rail.leftMargin=dp(12); else rail.topMargin=dp(12);
        shortcutScroll.setLayoutParams(rail);
        int columns=landscape?2:6;
        // Existing column specs must be removed before shrinking the grid.
        shortcutGrid.removeAllViews();
        shortcutGrid.setColumnCount(columns);
        int i=0;
        for(Button button:shortcutButtons.values()) {
            GridLayout.LayoutParams p=new GridLayout.LayoutParams(GridLayout.spec(i/columns),GridLayout.spec(i%columns,1f));
            p.width=0; p.height=dp(60); p.setMargins(dp(2),dp(2),dp(2),dp(2));
            shortcutGrid.addView(button,p); i++;
        }
        touch.cancel();
        preview.post(this::fitVideo);
    }
    private void setFullscreen(boolean enabled) {
        fullscreen=enabled; touch.cancel();
        fullscreenButton.setText(enabled?"":"全屏");
        fullscreenButton.setCompoundDrawables(new UiIcon(enabled?"exit_fullscreen":"fullscreen",Color.rgb(227,237,231),dp(14)),null,null,null);
        FrameLayout.LayoutParams fullParams=(FrameLayout.LayoutParams)fullscreenButton.getLayoutParams();
        fullParams.width=dp(enabled?44:72); fullParams.height=dp(enabled?48:40);
        fullParams.gravity=(enabled?Gravity.BOTTOM:Gravity.TOP)|Gravity.END;
        fullParams.setMargins(dp(10),dp(10),dp(enabled?4:10),dp(enabled?0:10));
        fullscreenButton.setLayoutParams(fullParams);
        fullscreenButton.setBackground(buttonSurface(enabled?Color.TRANSPARENT:Color.argb(215,19,25,23),enabled?0:Color.argb(80,133,154,145),20));
        fullscreenButton.setContentDescription(enabled?"退出全屏":"全屏显示 iPhone");
        getWindow().getDecorView().setSystemUiVisibility(enabled?
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY|View.SYSTEM_UI_FLAG_FULLSCREEN|View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            |View.SYSTEM_UI_FLAG_LAYOUT_STABLE|View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN|View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION:
            View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR|View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        getWindow().setStatusBarColor(enabled?Color.BLACK:PAGE);
        getWindow().setNavigationBarColor(enabled?Color.BLACK:PAGE);
        page.setPadding(enabled?0:dp(16),enabled?0:dp(8),enabled?0:dp(16),enabled?0:dp(8));
        page.setBackgroundColor(enabled?Color.BLACK:PAGE);
        preview.setBackground(round(enabled?Color.BLACK:STAGE,enabled?0:20));
        emptyState.setBackgroundColor(enabled?Color.BLACK:STAGE);
        page.requestApplyInsets(); adaptOrientation();
    }
    @Override public void onBackPressed() {
        if(fullscreen) setFullscreen(false); else super.onBackPressed();
    }
    private void addShortcut(String action,String title) {
        Button button=uiButton(title,10,INK); button.setSingleLine();
        button.setAutoSizeTextTypeUniformWithConfiguration(9,10,1,android.util.TypedValue.COMPLEX_UNIT_SP);
        button.setCompoundDrawables(null,new UiIcon(action,BLUE,dp(20)),null,null);
        button.setCompoundDrawablePadding(dp(7));
        button.setPadding(dp(2),dp(4),dp(2),dp(4));
        button.setBackground(buttonSurface(Color.TRANSPARENT,0,12));
        button.setContentDescription("iPhone "+title); button.setEnabled(false);
        button.setOnClickListener(v -> sendShortcut(action));
        shortcutButtons.put(action,button); shortcutGrid.addView(button);
    }
    private void updateShortcutButtons() {
        for(Map.Entry<String,Button> entry:shortcutButtons.entrySet()) {
            boolean enabled=running.get() && !cancelRequested.get() && !shortcutBusy && availableShortcuts.contains(entry.getKey());
            entry.getValue().setEnabled(enabled); entry.getValue().setAlpha(enabled?1f:.5f);
        }
        for(int i=0;i<3;i++) {
            Button button=navigationButtons[i];
            if(button==null) continue;
            String action=navigationActions[i];
            String title=ACTION_TITLES[Arrays.asList(ACTION_IDS).indexOf(action)];
            boolean enabled=running.get() && !cancelRequested.get() && !shortcutBusy && availableShortcuts.contains(action);
            // Keep long-press customization available while disconnected or unassigned.
            button.setAlpha(enabled?1f:.45f);
            boolean customized=!action.equals(NAV_DEFAULTS[i]) && !action.equals("none");
            button.setText(customized?title:"");
            button.setCompoundDrawables(null,new UiIcon(action,Color.rgb(212,226,217),dp(20)),null,null);
            button.setContentDescription(NAV_POSITIONS[i]+"按键："+title+"；长按自定义");
        }
    }
    private void configureNavigation(int slot) {
        touch.cancel();
        int selected=Arrays.asList(ACTION_IDS).indexOf(navigationActions[slot]);
        new AlertDialog.Builder(this).setTitle(NAV_POSITIONS[slot]+"按键功能")
            .setSingleChoiceItems(ACTION_TITLES,selected,(dialog,which)-> {
                navigationActions[slot]=ACTION_IDS[which];
                getSharedPreferences("navigation_bindings",MODE_PRIVATE).edit().putString("slot_"+slot,ACTION_IDS[which]).apply();
                updateShortcutButtons(); dialog.dismiss();
            }).setNegativeButton("取消",null).show();
    }
    private void sendShortcut(String action) {
        if(shortcutBusy || !availableShortcuts.contains(action) || !running.get() || cancelRequested.get()) return;
        touch.cancel(); shortcutBusy=true; updateShortcutButtons();
        sendTouch("{\"shortcut\":\""+action+"\"}");
    }
    @Override public boolean onKeyDown(int keyCode,KeyEvent event) {
        if(event.isCtrlPressed() && event.isAltPressed()) {
            String action=null;
            switch(keyCode) {
                case KeyEvent.KEYCODE_H: action="home"; break;
                case KeyEvent.KEYCODE_TAB: action="app_switcher"; break;
                case KeyEvent.KEYCODE_C: action="control_center"; break;
                case KeyEvent.KEYCODE_N: action="notification_center"; break;
                case KeyEvent.KEYCODE_S: action="siri"; break;
                case KeyEvent.KEYCODE_DPAD_UP: action="volume_up"; break;
                case KeyEvent.KEYCODE_DPAD_DOWN: action="volume_down"; break;
                case KeyEvent.KEYCODE_L: action="lock"; break;
            }
            if(action!=null) { if(event.getRepeatCount()==0) sendShortcut(action); return true; }
        }
        return super.onKeyDown(keyCode,event);
    }
    @Override public void onConfigurationChanged(Configuration c) { super.onConfigurationChanged(c); adaptOrientation(); }
    @Override public void surfaceCreated(SurfaceHolder holder) {
        surface=holder.getSurface(); decoder.setSurface(surface);
    }
    @Override public void surfaceChanged(SurfaceHolder holder,int format,int w,int h) { touch.cancel(); }
    @Override public void surfaceDestroyed(SurfaceHolder holder) { stopSession(); decoder.setSurface(null); surface=null; surfaceBufferWidth=surfaceBufferHeight=0; }
    private void onVideoRendered() {
        if (destroyed || cancelRequested.get() || !running.get() || frameCount==0 || sessionError) return;
        renderedCount++;
        if (hasRendered) return;
        hasRendered=true;
        diagnostics.record("video","First frame rendered to SurfaceView");
        emptyState.setVisibility(View.GONE); connectionText.setText("iPhone 已连接");
        connectionDot.setBackground(round(BLUE,4));
    }
    @Override protected void onStart() { super.onStart(); foreground=true; }
    @Override protected void onStop() { foreground=false; restartAfterStop=""; if(running.get() || connecting) stopSession(); super.onStop(); }
    @Override protected void onDestroy() { destroyed=true; stopSession(); decoder.shutdown(); unregisterReceiver(receiver); ui.removeCallbacks(pollUsb); super.onDestroy(); }

    private void showHelp() { new AlertDialog.Builder(this).setTitle("安卓连接 iPhone")
        .setMessage("1. 安卓设备需支持 USB Host / OTG，使用可传输数据的线缆。\n\n2. 解锁 iPhone，连接后允许安卓访问 USB，并在 iPhone 上确认信任。切换投屏模式后可能需再次授权 USB。\n\n3. 反向控制需要 iOS 18+ 和开发者模式；开发者镜像已内置，无需下载，首次设置仍需联网向 Apple 请求设备个性化验证。\n\n4. 点镜像右上角「全屏」放大显示；点右下角收起图标或安卓返回键可退出全屏。画面下方三键可长按自定义，默认左侧空置、中间主页、右侧多任务。\n\n5. 无线连接：安卓和 iPhone 连接同一 Wi-Fi，启动后在 iPhone 屏幕镜像中选择 iMirror Lite。无线控制首次需 USB 连接保存配对；iPhone 重启后可能需再次 USB 准备开发者环境。最多五指触控，实际能力显示在状态中。\n\n这是开发测试版，其他设备的 USB 兼容性与手势表现仍需验证。当前版本不传输音频。离开 App 会断开连接。")
        .setNeutralButton("诊断日志",(dialog,which)->showDiagnostics())
        .setNegativeButton("本机多指检测",(dialog,which)->touchTest())
        .setPositiveButton("知道了",null).show(); }
    private void showDiagnostics() {
        String log = diagnostics.read();
        TextView text = label(log,11,INK); text.setTextIsSelectable(true); text.setPadding(dp(14),dp(14),dp(14),dp(14));
        ScrollView scroll = new ScrollView(this); scroll.addView(text);
        new AlertDialog.Builder(this).setTitle("连接诊断日志").setView(scroll)
            .setNeutralButton("复制",(dialog,which)-> {
                ClipboardManager clipboard=(ClipboardManager)getSystemService(CLIPBOARD_SERVICE);
                clipboard.setPrimaryClip(ClipData.newPlainText("iMirror 诊断日志",log));
                Toast.makeText(this,"日志已复制",Toast.LENGTH_SHORT).show();
            }).setPositiveButton("关闭",null).show();
    }
    private void touchTest() {
        touch.cancel();
        Dialog dialog=new Dialog(this); LinearLayout content=new LinearLayout(this); content.setOrientation(LinearLayout.VERTICAL); content.setPadding(dp(18),dp(18),dp(18),dp(18));
        TextView title=label("本机多指检测 · 不向 iPhone 发送触控",14,INK); content.addView(title);
        content.addView(new View(this) {
            final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG); final Map<Integer,PointF> points=new HashMap<>();
            @Override public boolean onTouchEvent(MotionEvent e) {
                if(e.getActionMasked()==MotionEvent.ACTION_UP) performClick();
                points.clear();
                if(e.getActionMasked()!=MotionEvent.ACTION_CANCEL && e.getActionMasked()!=MotionEvent.ACTION_UP)
                    for(int i=0;i<e.getPointerCount();i++)
                        if(!(e.getActionMasked()==MotionEvent.ACTION_POINTER_UP && i==e.getActionIndex())) points.put(e.getPointerId(i),new PointF(e.getX(i),e.getY(i)));
                title.setText("本机检测到 "+points.size()+" 指 · 不向 iPhone 发送"); invalidate(); return true;
            }
            @Override public boolean performClick() { return super.performClick(); }
            @Override protected void onDraw(Canvas c) { c.drawColor(Color.rgb(235,240,249)); for(Map.Entry<Integer,PointF> p:points.entrySet()) {
                paint.setColor(BLUE); c.drawCircle(p.getValue().x,p.getValue().y,dp(27),paint);
                paint.setColor(Color.WHITE); paint.setTextSize(dp(16)); c.drawText(""+p.getKey(),p.getValue().x-dp(6),p.getValue().y+dp(6),paint);
            } }
        },new LinearLayout.LayoutParams(-1,dp(360)));
        Button close=new Button(this); close.setText("完成"); close.setOnClickListener(v->dialog.dismiss()); content.addView(close);
        dialog.setContentView(content); dialog.show(); dialog.getWindow().setLayout(getResources().getDisplayMetrics().widthPixels-dp(32),-2);
    }
    private int dp(int n) { return Math.round(n*getResources().getDisplayMetrics().density); }
    private TextView label(String text,int size,int color) { TextView v=new TextView(this); v.setText(text); v.setTextSize(size); v.setTextColor(color); return v; }
    private Button uiButton(String text,int size,int color) {
        Button button=new Button(this) {
            @Override protected void onDraw(Canvas canvas) {
                if(getText().length()==0) {
                    android.graphics.drawable.Drawable[] icons=getCompoundDrawables();
                    android.graphics.drawable.Drawable icon=icons[1]!=null?icons[1]:icons[0];
                    if(icon!=null) {
                        canvas.save(); canvas.translate((getWidth()-icon.getBounds().width())/2f,(getHeight()-icon.getBounds().height())/2f);
                        icon.draw(canvas); canvas.restore(); return;
                    }
                }
                super.onDraw(canvas);
            }
        }; button.setText(text); button.setAllCaps(false); button.setTextSize(size);
        button.setTextColor(color); button.setGravity(Gravity.CENTER); button.setPadding(0,0,0,0);
        button.setMinWidth(0); button.setMinimumWidth(0); button.setMinHeight(0); button.setMinimumHeight(0);
        button.setStateListAnimator(null); button.setBackgroundTintList(null);
        button.setTypeface(Typeface.create("sans-serif-medium",Typeface.NORMAL)); return button;
    }
    private GradientDrawable outline(int color,int stroke,int radius) {
        GradientDrawable drawable=round(color,radius); if(stroke!=0) drawable.setStroke(dp(1),stroke); return drawable;
    }
    private RippleDrawable buttonSurface(int color,int stroke,int radius) {
        return new RippleDrawable(ColorStateList.valueOf(Color.argb(35,99,176,151)),outline(color,stroke,radius),round(Color.WHITE,radius));
    }
    private GradientDrawable round(int color,int radius) { GradientDrawable d=new GradientDrawable(); d.setColor(color); d.setCornerRadius(dp(radius)); return d; }
    private void space(LinearLayout layout,int height) { layout.addView(new Space(this),new LinearLayout.LayoutParams(1,dp(height))); }
}
