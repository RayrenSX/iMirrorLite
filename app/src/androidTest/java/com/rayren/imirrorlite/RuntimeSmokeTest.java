package com.rayren.imirrorlite;

import android.app.Instrumentation;
import android.app.Activity;
import android.os.Bundle;
import android.util.Log;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.view.Surface;
import android.view.SurfaceView;
import android.view.SurfaceHolder;
import android.view.PixelCopy;
import android.view.MotionEvent;
import android.view.InputDevice;
import org.json.JSONObject;
import org.json.JSONArray;
import android.os.Handler;
import android.os.Looper;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/** Executes packaged ARM/x86 Android libraries, including native crypto. */
public final class RuntimeSmokeTest extends Instrumentation {
    private boolean runtimeOnly;
    private boolean externalHost;
    private volatile VideoDecoder wirelessDecoder;
    public void onWirelessVideo(byte[] data,byte[] csd,int width,int height,String mime,long pts) throws Exception {
        wirelessDecoder.feed(data,csd,width,height,mime,pts);
    }
    public void onWirelessVideoTimed(byte[] data,byte[] csd,int width,int height,String mime,long pts,long sourceUs) throws Exception {
        wirelessDecoder.feed(data,csd,width,height,mime,pts,sourceUs);
    }
    @Override public void onCreate(Bundle args) {
        runtimeOnly=args!=null && "true".equals(args.getString("runtimeOnly"));
        externalHost=args!=null && "true".equals(args.getString("externalHost"));
        super.onCreate(args); start();
    }
    @Override public void onStart() {
        Bundle output = new Bundle();
        try {
            // Android may expose the USB string descriptor's trailing NULs.
            String serial = "000080301234567890ABCDEF";
            if (!"00008030-1234567890ABCDEF".equals(UsbHost.normalizeSerial(serial + "\u0000\u0000")))
                throw new AssertionError("USB serial normalization failed");
            if (UsbIo.transfer(-1, 134, new byte[16], 1000) != -22)
                throw new AssertionError("Packaged USB JNI library did not load");
            if (!Python.isStarted()) Python.start(new AndroidPlatform(getTargetContext()));
            String path = getTargetContext().getFilesDir().getAbsolutePath();
            String result = Python.getInstance().getModule("imirror.smoke").callAttr("check", path).toString();
            if (!"ok".equals(result)) throw new AssertionError(result);
            Log.i("iMirrorRuntimeTest","WIRELESS_RUNTIME_OK: AirPlay PIN across sockets, crypto, native PlayFair, TLS-PSK, userspace tunnel");
            checkVideoParser();
            checkTouchRouter();
            checkWirelessDiscovery();
            if(!runtimeOnly) checkDecoder();
            output.putString("stream", runtimeOnly?"RUNTIME_PROTOCOL_OK\n":"RUNTIME_SMOKE_OK\n");
            finish(Activity.RESULT_OK, output);
        } catch (Throwable error) {
            output.putString("stream", Log.getStackTraceString(error));
            finish(Activity.RESULT_CANCELED, output);
        }
    }

    private void checkTouchRouter() throws Exception {
        java.util.List<String> messages=new java.util.ArrayList<>();
        TouchRouter router=new TouchRouter(messages::add); router.setCapacity(5);
        long start=android.os.SystemClock.uptimeMillis();
        for(int n=1;n<=6;n++) {
            int action=n==1?MotionEvent.ACTION_DOWN:MotionEvent.ACTION_POINTER_DOWN|((n-1)<<MotionEvent.ACTION_POINTER_INDEX_SHIFT);
            routeTouch(router,start,action,n);
        }
        JSONArray points=new JSONObject(messages.get(messages.size()-1)).getJSONArray("points");
        if(points.length()!=5) throw new AssertionError("Android pointer capacity is not five");
        for(int i=0;i<5;i++) if(points.getJSONObject(i).getInt("id")!=10+i)
            throw new AssertionError("Pointer ID was replaced by its event index");
        routeTouch(router,start,MotionEvent.ACTION_POINTER_UP|(2<<MotionEvent.ACTION_POINTER_INDEX_SHIFT),6);
        points=new JSONObject(messages.get(messages.size()-1)).getJSONArray("points");
        if(!points.getJSONObject(2).getString("action").equals("up")) throw new AssertionError("Independent UP lost");
        // Remove pointer 12, reorder the others, and retain the rejected sixth.
        routeTouch(router,start,MotionEvent.ACTION_MOVE,new int[]{15,14,13,11,10});
        points=new JSONObject(messages.get(messages.size()-1)).getJSONArray("points");
        if(points.length()!=4) throw new AssertionError("Rejected sixth pointer resurrected on MOVE");
        java.util.Set<Integer> ids=new java.util.HashSet<>();
        for(int i=0;i<points.length();i++) ids.add(points.getJSONObject(i).getInt("id"));
        if(!ids.equals(new java.util.HashSet<>(java.util.Arrays.asList(10,11,13,14)))) throw new AssertionError("Reorder changed active IDs");
        router.cancel();
        if(!new JSONObject(messages.get(messages.size()-1)).getBoolean("cancel")) throw new AssertionError("Cancel not forwarded");
    }

    private void checkWirelessDiscovery() throws Exception {
        CountDownLatch advertised=new CountDownLatch(1);
        java.util.concurrent.atomic.AtomicReference<String> error=new java.util.concurrent.atomic.AtomicReference<>();
        WirelessHost host=new WirelessHost(getTargetContext(),new WirelessHost.Listener() {
            public void device(String json) {}
            public void status(String kind,String message) {
                if(kind.equals("video")) advertised.countDown();
                if(kind.equals("error")) { error.set(message); advertised.countDown(); }
            }
        });
        try {
            host.start(12345,"iMirror Runtime Test","{\"deviceid\":\"02:00:00:00:00:42\",\"features\":\"0x5A7FFEE6,0x0\",\"pk\":\"00\",\"model\":\"AppleTV3,2\",\"srcvers\":\"220.68\"}");
            if(!advertised.await(15,TimeUnit.SECONDS) || error.get()!=null)
                throw new AssertionError("AirPlay NSD advertisement failed: "+error.get());
            Log.i("iMirrorRuntimeTest","WIRELESS_NSD_OK: AirPlay registration and multicast lock");
        } finally { host.close(); }
    }

    private void routeTouch(TouchRouter router,long start,int action,int count) {
        int[] ids=new int[count]; for(int i=0;i<count;i++) ids[i]=10+i;
        routeTouch(router,start,action,ids);
    }
    private void routeTouch(TouchRouter router,long start,int action,int[] ids) {
        MotionEvent.PointerProperties[] props=new MotionEvent.PointerProperties[ids.length];
        MotionEvent.PointerCoords[] coords=new MotionEvent.PointerCoords[ids.length];
        for(int i=0;i<ids.length;i++) {
            props[i]=new MotionEvent.PointerProperties(); props[i].id=ids[i]; props[i].toolType=MotionEvent.TOOL_TYPE_FINGER;
            coords[i]=new MotionEvent.PointerCoords(); coords[i].x=100+i*80; coords[i].y=200; coords[i].pressure=1; coords[i].size=.1f;
        }
        MotionEvent event=MotionEvent.obtain(start,android.os.SystemClock.uptimeMillis(),action,ids.length,props,coords,0,0,1,1,0,0,InputDevice.SOURCE_TOUCHSCREEN,0);
        try { router.onTouch(event,1000,1000); } finally { event.recycle(); }
    }

    private void checkVideoParser() throws Exception {
        byte[] sample=readAsset("quicktime-decoder.bin"), clock=readAsset("quicktime-cvrp.bin");
        byte[] expected=readAsset("decoder-frame.bin"), config=readAsset("decoder-csd.bin");
        java.util.List<byte[]> writes=new java.util.ArrayList<>();
        int[] received={0};
        VideoStream stream=new VideoStream(Python.getInstance().getModule("imirror.smoke").callAttr("video_protocol"),new VideoStream.Sink() {
            public void frame(byte[] data,byte[] csd,int w,int h,String mime,long pts) {
                if(!java.util.Arrays.equals(data,expected) || !java.util.Arrays.equals(csd,config)
                    || w!=640 || h!=960 || !mime.equals("video/avc") || pts!=123456789L+received[0])
                    throw new AssertionError("Java video parser changed sample/configuration/timestamp");
                if(writes.isEmpty() || writes.get(writes.size()-1).length!=20)
                    throw new AssertionError("NEED was not sent before decoding");
                received[0]++;
            }
            public void write(byte[] data) { writes.add(data); }
            public void diagnostic(String message) { }
        });
        try {
            // Split inside every header and length field, then send a second
            // sample in one transfer. Both must preserve the complete frame.
            for(int i=0;i<clock.length;i+=7)
                stream.feed(java.util.Arrays.copyOfRange(clock,i,Math.min(i+7,clock.length)),123456789000L);
            for(int i=0;i<sample.length;i+=7)
                stream.feed(java.util.Arrays.copyOfRange(sample,i,Math.min(i+7,sample.length)),123456789000L);
            stream.feed(sample,123456789000L);
            if(received[0]!=2 || stream.frameCount()!=2) throw new AssertionError("Lost fragmented QuickTime sample");
            stream.beginStop(); stream.feed(sample,123456789000L);
            if(received[0]!=2 || writes.size()!=2) throw new AssertionError("Video continued during release");
        } finally { stream.close(); }
        VideoStream invalid=new VideoStream(Python.getInstance().getModule("imirror.smoke").callAttr("video_protocol"),new VideoStream.Sink() {
            public void frame(byte[] data,byte[] csd,int w,int h,String mime,long pts) { throw new AssertionError("Malformed sample decoded"); }
            public void write(byte[] data) { }
            public void diagnostic(String message) { }
        });
        try {
            try { invalid.feed(new byte[]{7,0,0,0},0); throw new AssertionError("Invalid QuickTime length accepted"); }
            catch(java.io.IOException expectedError) { }
        } finally { invalid.close(); }
    }

    private void checkDecoder() throws Exception {
        byte[] config=readAsset("decoder-csd.bin"), frame=readAsset("decoder-frame.bin");
        // MainActivity is singleTask for USB re-enumeration. If it is already
        // open, startActivitySync can wait forever for a new instance. Use a
        // debug-only standard activity so live app launches cannot steal the
        // decoder test's surface or instrumentation launch notification.
        Activity activity;
        if(externalHost) {
            Bundle status=new Bundle(); status.putString("stream","EXTERNAL_DECODER_HOST_READY\n"); sendStatus(0,status);
            long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(30);
            while(DecoderTestActivity.current==null && System.nanoTime()<deadline) Thread.sleep(100);
            activity=DecoderTestActivity.current;
            if(activity==null) throw new AssertionError("ADB did not open the foreground decoder host");
        } else activity=startActivitySync(new Intent(getTargetContext(),DecoderTestActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        CountDownLatch ready = new CountDownLatch(1);
        java.util.concurrent.atomic.AtomicReference<CountDownLatch> rendered =
            new java.util.concurrent.atomic.AtomicReference<>(new CountDownLatch(1));
        java.util.concurrent.atomic.AtomicReference<java.io.IOException> failure =
            new java.util.concurrent.atomic.AtomicReference<>();
        java.util.concurrent.atomic.AtomicInteger displayed=new java.util.concurrent.atomic.AtomicInteger();
        VideoDecoder decoder = new VideoDecoder(failure::set, () -> { displayed.incrementAndGet(); rendered.get().countDown(); });
        SurfaceView[] view = new SurfaceView[1];
        Surface[] surface = new Surface[1];
        try {
            runOnMainSync(() -> {
                view[0] = new SurfaceView(activity);
                view[0].getHolder().addCallback(new SurfaceHolder.Callback() {
                    public void surfaceCreated(SurfaceHolder holder) {
                        surface[0]=holder.getSurface(); decoder.setSurface(surface[0]); ready.countDown();
                    }
                    public void surfaceChanged(SurfaceHolder holder,int format,int w,int h) { }
                    public void surfaceDestroyed(SurfaceHolder holder) { decoder.setSurface(null); }
                });
                activity.setContentView(view[0]);
            });
            if (!ready.await(8,TimeUnit.SECONDS)) throw new AssertionError("No decoder output surface");
            // Reconnection closes the codec while keeping the same decoder
            // and display callback. Both initial and rebuilt codecs must render.
            for (int pass=0;pass<4;pass++) {
                if(pass>0) { decoder.close(); rendered.set(new CountDownLatch(1)); }
                decoder.setOutputOptions(pass==2?30:60,pass==1?"unavailable.test.decoder":pass==2?"software":"auto",false);
                if(pass==2) runOnMainSync(() -> view[0].getHolder().setFixedSize(320,480));
                // Some hardware codecs retain initial buffers until more frames
                // arrive. Exercise a short stream, as real mirroring does.
                for (int i=0;i<12 && rendered.get().getCount()>0;i++) {
                    decoder.feed(frame,config,640,960,"video/avc",System.nanoTime()/1000);
                    Thread.sleep(50);
                }
                if (!rendered.get().await(8,TimeUnit.SECONDS))
                    throw new AssertionError("No H.264 frame rendered on decoder pass "+pass, failure.get());
                if(failure.get()!=null) throw new AssertionError("Decoder failed",failure.get());
                String counters=decoder.counters();
                if(counters.length()>2000 || !counters.contains("rendered=")
                    || !counters.contains("outputFps=") || !counters.contains("renderFps=")
                    || !counters.contains("renderAgeAvgMs="))
                    throw new AssertionError("Decoder diagnostics would hide throughput or latency: "+counters);
                Log.i("iMirrorRuntimeTest","DECODER_COUNTERS: "+counters);
                if(pass==1 && !counters.contains("requestedDecoder=unavailable.test.decoder"))
                    throw new AssertionError("Requested decoder fallback was not observable");
            }
            // Keep receiving/decoding 60 fps while rendering only 30 fps.
            decoder.close(); decoder.setOutputOptions(30,"auto",false);
            displayed.set(0); rendered.set(new CountDownLatch(1));
            long pacingStart=System.nanoTime();
            for(int i=0;i<90;i++) {
                long deadline=pacingStart+i*16_666_667L;
                long wait=deadline-System.nanoTime();
                if(wait>0) TimeUnit.NANOSECONDS.sleep(wait);
                decoder.feed(frame,config,640,960,"video/avc",System.nanoTime()/1000);
            }
            Thread.sleep(150);
            String limited=decoder.counters();
            if(!limited.contains("queued=90 ") || displayed.get()<30 || displayed.get()>51)
                throw new AssertionError("60 fps input / 30 fps output cap failed: rendered="+displayed.get()+" "+limited);
            if(view[0].getHolder().getSurfaceFrame().width()!=320 || view[0].getHolder().getSurfaceFrame().height()!=480)
                throw new AssertionError("Local render buffer resize failed");
            Log.i("iMirrorRuntimeTest","VIDEO_SETTINGS_OK: 640x960 input -> 320x480 Surface, 90 frames decoded, rendered="+displayed.get());
            decoder.close(); rendered.set(new CountDownLatch(1)); wirelessDecoder=decoder;
            decoder.setOutputOptions(60,"auto",true);
            String wirelessResult=Python.getInstance().getModule("imirror.wireless_smoke")
                .callAttr("render",getTargetContext().getFilesDir().getAbsolutePath(),this,config,frame).toString();
            if(!"wireless_video_ok".equals(wirelessResult) || !rendered.get().await(8,TimeUnit.SECONDS))
                throw new AssertionError("Encrypted AirPlay H.264 did not render: "+decoder.counters(),failure.get());
            Log.i("iMirrorRuntimeTest","WIRELESS_VIDEO_OK: signed AirPlay socket -> AES-CTR -> MediaCodec -> SurfaceView");
            Bitmap bitmap=Bitmap.createBitmap(1,1,Bitmap.Config.ARGB_8888);
            CountDownLatch copied=new CountDownLatch(1);
            int[] copyResult=new int[1];
            try {
                PixelCopy.request(surface[0],bitmap,result -> {
                    copyResult[0]=result; copied.countDown();
                },new Handler(Looper.getMainLooper()));
                if(!copied.await(8,TimeUnit.SECONDS) || copyResult[0]!=PixelCopy.SUCCESS)
                    throw new AssertionError("Cannot inspect SurfaceView video pixels: "+copyResult[0]);
                int pixel=bitmap.getPixel(0,0);
                if (Color.red(pixel)<180 || Color.green(pixel)>60 || Color.blue(pixel)>60)
                throw new AssertionError("H.264 decoded frame is not the expected red test pattern");
            } finally { bitmap.recycle(); }
        } finally {
            decoder.shutdown();
            runOnMainSync(activity::finish);
        }
    }

    private byte[] readAsset(String name) throws Exception {
        try (java.io.InputStream in=getContext().getAssets().open(name);
             java.io.ByteArrayOutputStream out=new java.io.ByteArrayOutputStream()) {
            byte[] buffer=new byte[4096]; int count;
            while ((count=in.read(buffer))!=-1) out.write(buffer,0,count);
            return out.toByteArray();
        }
    }
}
