package com.rayren.imirrorlite;

import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaFormat;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.util.LongSparseArray;
import android.view.Surface;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

/** Called by the USB video worker only; Surface destruction waits for close. */
final class VideoDecoder implements AutoCloseable {
    private volatile MediaCodec codec;
    private Surface surface;
    private int width, height;
    private String mime;
    private String codecInfo="";
    private byte[] config;
    private IOException failure;
    private boolean needsKeyframe = true;
    private long queuedFrames, outputFrames, inputMisses, skippedFrames, droppedOutputs;
    private long releasedSamples, releaseAgeTotalUs;
    private final BoundedRenderSchedule burstSchedule = new BoundedRenderSchedule();
    private final SourceRenderSchedule sourceSchedule = new SourceRenderSchedule();
    private final LongSparseArray<Long> sourceTimes = new LongSparseArray<>();
    private final int[] outputIndices = new int[3];
    private final long[] outputPts = new long[3];
    private boolean wirelessOutput;
    private long lastInputPts;
    private int targetFps=60, decodeFps=60;
    private String requestedDecoder="auto";
    private final FrameRateGate outputGate=new FrameRateGate();
    private long fpsDrops;
    private long maxOutputBatch, scheduleDrops;
    private final Object renderMetricsLock = new Object();
    private long renderedSamples, renderAgeTotalUs, renderAgeMaxUs;
    private long renderedFrames;
    private long statsAt, statsQueued, statsOutput, statsRendered, statsDropped;
    private final Runnable onRendered;
    private final Consumer<IOException> onFailure;
    private final MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
    private final ScheduledExecutorService pump = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "video-drain"); t.setDaemon(true); return t;
    });

    VideoDecoder() { this(error -> {}); }

    VideoDecoder(Consumer<IOException> onFailure) { this(onFailure, () -> {}); }

    VideoDecoder(Consumer<IOException> onFailure, Runnable onRendered) {
        this.onRendered = onRendered;
        this.onFailure = onFailure;
        pump.scheduleWithFixedDelay(() -> {
            IOException outputError = null;
            synchronized (this) {
                if (codec != null) {
                    try { drain(); } catch (IllegalStateException e) {
                        close(); outputError = failure = new IOException("视频解码器停止输出",e);
                    }
                }
            }
            if (outputError != null) onFailure.accept(outputError);
        }, 2, 2, TimeUnit.MILLISECONDS);
    }

    synchronized void setSurface(Surface next) {
        close(); surface = next;
        updateSurfaceFrameRate();
    }

    synchronized void setOutputOptions(int fps,String decoderName,boolean wireless) {
        if(!VideoProfile.contains(VideoProfile.FRAME_RATES,fps)) throw new IllegalArgumentException("Invalid FPS");
        targetFps=fps;
        decodeFps=wireless?fps:60;
        requestedDecoder=decoderName==null?"auto":decoderName;
        outputGate.configure(fps); sourceSchedule.setFrameRate(fps);
        burstSchedule.setFrameRate(fps);
        updateSurfaceFrameRate();
    }

    private void updateSurfaceFrameRate() {
        Surface next=surface;
        if(Build.VERSION.SDK_INT>=30 && next!=null && next.isValid()) {
            try { next.setFrameRate(targetFps,Surface.FRAME_RATE_COMPATIBILITY_FIXED_SOURCE); }
            catch(RuntimeException ignored) { /* Refresh hints are optional. */ }
        }
    }

    synchronized void feed(byte[] data, byte[] csd, int w, int h, String type, long pts) throws IOException {
        feed(data,csd,w,h,type,pts,-1);
    }

    synchronized void feed(byte[] data, byte[] csd, int w, int h, String type, long pts, long sourceUs) throws IOException {
        if (failure!=null) { IOException error=failure; failure=null; throw error; }
        if (surface == null || !surface.isValid()) return;
        if (codec == null || width != w || height != h || !type.equals(mime) || !Arrays.equals(config, csd)) {
            close(); width = w; height = h; mime = type; config = csd.clone();
            queuedFrames=outputFrames=inputMisses=skippedFrames=droppedOutputs=0;
            releasedSamples=releaseAgeTotalUs=0;
            maxOutputBatch=scheduleDrops=fpsDrops=0;
            synchronized(renderMetricsLock) { renderedFrames=renderedSamples=renderAgeTotalUs=renderAgeMaxUs=0; }
            statsAt=System.nanoTime(); statsQueued=statsOutput=statsRendered=statsDropped=0;
            try {
            String preferredName=selectRequestedDecoder(type);
            boolean selectionFallback=DecoderFallback.start(
                preferredName==null?null:() -> startNamedCodec(preferredName,type,w,h,csd),
                () -> startAutomaticCodec(type,w,h,csd));
            if(selectionFallback) codecInfo+=" selectionFallback=true";
            needsKeyframe = true;
            codec.setVideoScalingMode(MediaCodec.VIDEO_SCALING_MODE_SCALE_TO_FIT);
            codec.setOnFrameRenderedListener((source, presentationTimeUs, nanoTime) -> {
                if (source == codec) {
                    synchronized(renderMetricsLock) {
                        renderedFrames++;
                        long age=nanoTime/1000-presentationTimeUs;
                        if(age>=0 && age<10_000_000) {
                            renderedSamples++; renderAgeTotalUs+=age;
                            renderAgeMaxUs=Math.max(renderAgeMaxUs,age);
                        }
                    }
                    this.onRendered.run();
                }
            }, new Handler(Looper.getMainLooper()));
            } catch(IOException | RuntimeException error) {
                close();
                throw error;
            }
        }
        wirelessOutput=sourceUs>=0;
        drain();
        boolean keyframe = isKeyframe(data, type);
        if (needsKeyframe && !keyframe) { skippedFrames++; return; }
        int index = codec.dequeueInputBuffer(10000);
        if (index < 0) {
            inputMisses++;
            long deadline=System.nanoTime()+100_000_000L;
            do {
                drain(); index=codec.dequeueInputBuffer(10000);
            } while(index<0 && System.nanoTime()<deadline);
            if(index<0) throw new IOException("硬件解码器持续无法接收视频帧");
        }
        ByteBuffer input = codec.getInputBuffer(index);
        if (input == null || input.capacity() < data.length) {
            codec.queueInputBuffer(index, 0, 0, pts, 0);
            needsKeyframe = true;
            throw new IOException("视频帧超过硬件解码缓冲区");
        }
        input.clear(); input.put(data);
        pts=Math.max(pts,lastInputPts+1); lastInputPts=pts;
        if(wirelessOutput) {
            // Codec timestamps remain local receipt times for the age metrics.
            // Keep sender cadence separately until this decoded output returns.
            if(sourceTimes.size()>=128) sourceTimes.removeAt(0);
            sourceTimes.put(pts,sourceUs);
        }
        codec.queueInputBuffer(index, 0, data.length, pts, 0);
        queuedFrames++;
        needsKeyframe = false;
        drain();
    }

    private static boolean supportsLowLatency(MediaCodecInfo decoder,String type) {
        try {
            return Build.VERSION.SDK_INT>=30 && decoder.getCapabilitiesForType(type)
                .isFeatureSupported(MediaCodecInfo.CodecCapabilities.FEATURE_LowLatency);
        } catch(RuntimeException ignored) { return false; }
    }

    static final class Choice {
        final String name,label;
        Choice(String name,String label) { this.name=name; this.label=label; }
    }

    static List<Choice> choices() {
        List<Choice> result=new ArrayList<>();
        result.add(new Choice("auto","自动选择（优先低延迟硬解）"));
        result.add(new Choice("software","软件解码（兼容模式）"));
        try {
            for(MediaCodecInfo info:new MediaCodecList(MediaCodecList.REGULAR_CODECS).getCodecInfos()) {
                if(info.isEncoder()) continue;
                boolean supported=false;
                for(String type:info.getSupportedTypes())
                    if(type.equalsIgnoreCase("video/avc") || type.equalsIgnoreCase("video/hevc")) {
                        MediaCodecInfo.CodecCapabilities caps=info.getCapabilitiesForType(type);
                        if(!caps.isFeatureRequired(MediaCodecInfo.CodecCapabilities.FEATURE_SecurePlayback)
                            && !caps.isFeatureRequired(MediaCodecInfo.CodecCapabilities.FEATURE_TunneledPlayback)) supported=true;
                    }
                if(supported) result.add(new Choice(info.getName(),info.getName()+"（"+(isSoftware(info)?"软件":"硬件")+"）"));
            }
        } catch(RuntimeException ignored) { /* Automatic choices remain available. */ }
        return result;
    }

    private static boolean isSoftware(MediaCodecInfo info) {
        if(Build.VERSION.SDK_INT>=29) return info.isSoftwareOnly();
        String name=info.getName().toLowerCase(Locale.ROOT);
        return name.startsWith("omx.google.") || name.startsWith("c2.android.");
    }

    private String selectRequestedDecoder(String type) {
        if(!requestedDecoder.equals("auto")) {
            try {
                for(MediaCodecInfo candidate:new MediaCodecList(MediaCodecList.REGULAR_CODECS).getCodecInfos()) {
                    if(candidate.isEncoder() || !(candidate.getName().equals(requestedDecoder)
                        || requestedDecoder.equals("software") && isSoftware(candidate))) continue;
                    for(String mime:candidate.getSupportedTypes()) {
                        if(!mime.equalsIgnoreCase(type)) continue;
                        MediaCodecInfo.CodecCapabilities caps=candidate.getCapabilitiesForType(mime);
                        if(!caps.isFeatureRequired(MediaCodecInfo.CodecCapabilities.FEATURE_SecurePlayback)
                            && !caps.isFeatureRequired(MediaCodecInfo.CodecCapabilities.FEATURE_TunneledPlayback)) return candidate.getName();
                    }
                }
            } catch(RuntimeException ignored) { }
        }
        return null;
    }

    private void startNamedCodec(String name,String type,int w,int h,byte[] csd) throws IOException {
        try {
            codec=MediaCodec.createByCodecName(name);
            configureCodec(type,w,h,csd);
        } catch(IOException | RuntimeException error) {
            releaseCodec();
            throw error;
        }
    }

    private void startAutomaticCodec(String type,int w,int h,byte[] csd) throws IOException {
        // Query metadata before creating an instance. A failed default hardware
        // allocation must not prevent trying a requested software decoder.
        MediaCodecList decoders=new MediaCodecList(MediaCodecList.REGULAR_CODECS);
        MediaFormat query=new MediaFormat(); query.setString(MediaFormat.KEY_MIME,type);
        String defaultName=decoders.findDecoderForFormat(query);
        if(defaultName==null) throw new IOException("没有可用的视频解码器："+type);
        String preferredName=defaultName;
        for(MediaCodecInfo candidate:decoders.getCodecInfos()) {
            if(candidate.getName().equals(defaultName)) {
                preferredName=findLowLatencyDecoder(candidate,type,w,h,decodeFps); break;
            }
        }
        final String selectedName=preferredName;
        boolean selectionFallback=DecoderFallback.start(
            selectedName.equals(defaultName)?null:() -> startNamedCodec(selectedName,type,w,h,csd),
            () -> startNamedCodec(defaultName,type,w,h,csd));
        if(selectionFallback) codecInfo+=" selectionFallback=true";
    }

    private static String findLowLatencyDecoder(MediaCodecInfo defaultDecoder,String type,int w,int h,int fps) {
        String defaultName=defaultDecoder.getName();
        if(Build.VERSION.SDK_INT<30 || supportsLowLatency(defaultDecoder,type)) return defaultName;
        // Some Qualcomm devices publish a separate .low_latency codec after
        // their default decoder. createDecoderByType alone never selects it.
        try {
            for(MediaCodecInfo candidate:new MediaCodecList(MediaCodecList.ALL_CODECS).getCodecInfos()) {
                if(candidate.isEncoder() || !candidate.isHardwareAccelerated()) continue;
                for(String supported:candidate.getSupportedTypes()) {
                    if(!supported.equalsIgnoreCase(type)) continue;
                    MediaCodecInfo.CodecCapabilities caps=candidate.getCapabilitiesForType(supported);
                    if(caps.isFeatureRequired(MediaCodecInfo.CodecCapabilities.FEATURE_SecurePlayback)
                        || caps.isFeatureRequired(MediaCodecInfo.CodecCapabilities.FEATURE_TunneledPlayback)) continue;
                    if(supportsLowLatency(candidate,supported) && caps.getVideoCapabilities()!=null
                        && caps.getVideoCapabilities().areSizeAndRateSupported(w,h,fps)) return candidate.getName();
                }
            }
        } catch(RuntimeException ignored) { /* Keep the default on buggy capability queries. */ }
        return defaultName;
    }

    private void configureCodec(String type,int w,int h,byte[] csd) throws IOException {
        boolean standard=supportsLowLatency(codec.getCodecInfo(),type);
        String name=codec.getName(), normalized=name.toLowerCase(Locale.ROOT);
        boolean kirin=normalized.startsWith("omx.hisi.");
        boolean qualcomm=normalized.startsWith("c2.qti.") || normalized.startsWith("omx.qcom.");
        int attempts=qualcomm?3:kirin?2:1;
        String vendor="none";
        for(int attempt=0;attempt<attempts;attempt++) {
            MediaFormat format=createFormat(type,w,h,csd);
            if(Build.VERSION.SDK_INT>=30 && standard) format.setInteger(MediaFormat.KEY_LOW_LATENCY,1);
            // K60 still buffers ~16 frames with the standard feature alone.
            // Try QTI's output-order option alongside low latency (Moonlight's
            // compatibility path), even on the dedicated .low_latency codec.
            if(qualcomm && attempt<2) {
                format.setInteger("vendor.qti-ext-dec-low-latency.enable",1);
                if(attempt==0) format.setInteger("vendor.qti-ext-dec-picture-order.enable",1);
                vendor=attempt==0?"qti-low-latency+picture-order-requested":"qti-low-latency-requested";
            } else if(kirin && attempt==0) {
                format.setInteger("vendor.hisi-ext-low-latency-video-dec.video-scene-for-low-latency-req",1);
                format.setInteger("vendor.hisi-ext-low-latency-video-dec.video-scene-for-low-latency-rdy",-1);
                vendor="kirin-requested";
            } else if(attempt>0) vendor="fallback";
            try {
                codec.configure(format,surface,null,0);
                codec.start();
                break;
            } catch(RuntimeException error) {
                if(attempt==attempts-1) throw error;
                codec.release(); codec=null;
                codec=MediaCodec.createByCodecName(name);
            }
        }
        // Keep the configuration summary short: SessionLog truncates at 2000
        // characters, so enumerating vendor parameters hides every counter.
        codecInfo=name+" androidLowLatency="+standard+" vendorLowLatency="+vendor;
    }

    private MediaFormat createFormat(String type,int w,int h,byte[] csd) throws IOException {
        MediaFormat format=MediaFormat.createVideoFormat(type,w,h);
        if(type.equals("video/avc")) {
            int pps=-1;
            for(int i=0;i+4<csd.length;i++)
                if(csd[i]==0 && csd[i+1]==0 && csd[i+2]==0 && csd[i+3]==1 && (csd[i+4]&31)==8) { pps=i; break; }
            if(pps<=0) throw new IOException("缺少 H.264 SPS/PPS");
            format.setByteBuffer("csd-0",ByteBuffer.wrap(Arrays.copyOfRange(csd,0,pps)));
            format.setByteBuffer("csd-1",ByteBuffer.wrap(Arrays.copyOfRange(csd,pps,csd.length)));
        } else format.setByteBuffer("csd-0",ByteBuffer.wrap(csd));
        format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE,Math.max(2*1024*1024,w*h));
        format.setInteger(MediaFormat.KEY_FRAME_RATE,decodeFps);
        format.setInteger(MediaFormat.KEY_OPERATING_RATE,decodeFps);
        format.setInteger(MediaFormat.KEY_PRIORITY,0);
        return format;
    }

    private void drain() {
        int count=0, batch=0, index;
        int window=wirelessOutput?3:2;
        while((index=codec.dequeueOutputBuffer(info,0))>=0) {
            if((info.flags&MediaCodec.BUFFER_FLAG_CODEC_CONFIG)!=0) {
                codec.releaseOutputBuffer(index,false); continue;
            }
            // Retain a small decoded burst. Repeatedly discarding the first
            // of a pair imposes ~30 fps on a 60 fps batched decoder.
            if(count==window) {
                codec.releaseOutputBuffer(outputIndices[0],false); droppedOutputs++;
                sourceTimes.remove(outputPts[0]);
                for(int i=1;i<count;i++) {
                    outputIndices[i-1]=outputIndices[i]; outputPts[i-1]=outputPts[i];
                }
                count--;
            }
            outputIndices[count]=index; outputPts[count]=info.presentationTimeUs; count++;
            outputFrames++; batch++;
        }
        maxOutputBatch=Math.max(maxOutputBatch,batch);
        if(count==0) return;
        long now=System.nanoTime();
        if(wirelessOutput) {
            for(int i=0;i<count;i++) {
                Long sourceUs=sourceTimes.get(outputPts[i]); sourceTimes.remove(outputPts[i]);
                if(!outputGate.allow(sourceUs==null || sourceUs<=0?outputPts[i]:sourceUs)) {
                    codec.releaseOutputBuffer(outputIndices[i],false); droppedOutputs++; fpsDrops++; continue;
                }
                long renderNs=sourceSchedule.time(now,outputPts[i],sourceUs==null?0:sourceUs);
                if(renderNs<0) {
                    codec.releaseOutputBuffer(outputIndices[i],false); droppedOutputs++; scheduleDrops++;
                } else {
                    long age=now/1000-outputPts[i];
                    if(age>=0 && age<10_000_000) { releasedSamples++; releaseAgeTotalUs+=age; }
                    codec.releaseOutputBuffer(outputIndices[i],renderNs);
                }
            }
            return;
        }
        // USB PTS are receipt times: several source frames in one read can be
        // only 1 us apart. Limit presentation slots, never that receipt cadence.
        // Use the same bounded schedule at every FPS and for every USB codec.
        int planned=burstSchedule.plan(now,count), discard=count-planned;
        for(int i=0;i<count;i++) {
            if(i<discard) {
                codec.releaseOutputBuffer(outputIndices[i],false);
                droppedOutputs++; scheduleDrops++;
            } else {
                long age=now/1000-outputPts[i];
                if(age>=0 && age<10_000_000) { releasedSamples++; releaseAgeTotalUs+=age; }
                codec.releaseOutputBuffer(outputIndices[i],burstSchedule.time(i-discard));
            }
        }
    }

    synchronized String counters() {
        synchronized (renderMetricsLock) {
        long now=System.nanoTime();
        double seconds=Math.max(1,now-statsAt)/1e9;
        String rates=String.format(Locale.ROOT," queueFps=%.1f outputFps=%.1f renderFps=%.1f dropFps=%.1f",
            (queuedFrames-statsQueued)/seconds,(outputFrames-statsOutput)/seconds,
            (renderedFrames-statsRendered)/seconds,(droppedOutputs-statsDropped)/seconds);
        statsAt=now; statsQueued=queuedFrames; statsOutput=outputFrames;
        statsRendered=renderedFrames; statsDropped=droppedOutputs;
        return codecInfo+" pacing="+(wirelessOutput?"source-32ms":"usb-bounded")
            +" requestedDecoder="+requestedDecoder+" targetFps="+targetFps+" fpsDrops="+fpsDrops
            +" outputWindow="+(wirelessOutput?3:2)
            +" queued="+queuedFrames+" output="+outputFrames+" rendered="+renderedFrames
            +" inFlight="+(queuedFrames-outputFrames)+" inputMisses="+inputMisses+" skipped="+skippedFrames
            +" droppedOutputs="+droppedOutputs+" maxOutputBatch="+maxOutputBatch+" scheduleDrops="+scheduleDrops
            +" releaseAgeAvgMs="+(releaseAgeTotalUs/Math.max(1,releasedSamples)/1000f)
            +" renderAgeAvgMs="+(renderAgeTotalUs/Math.max(1,renderedSamples)/1000f)+" renderAgeMaxMs="+(renderAgeMaxUs/1000f)+rates;
        }
    }

    private static boolean isKeyframe(byte[] data, String mime) {
        for (int i = 0; i + 4 < data.length; i++) {
            if (data[i] == 0 && data[i+1] == 0 && data[i+2] == 0 && data[i+3] == 1) {
                int type = mime.equals("video/hevc") ? (data[i+4] >> 1) & 63 : data[i+4] & 31;
                if (mime.equals("video/hevc") ? type >= 16 && type <= 21 : type == 5) return true;
            }
        }
        return false;
    }

    private void releaseCodec() {
        if (codec != null) {
            MediaCodec previous=codec; codec=null;
            try { previous.stop(); } catch (RuntimeException ignored) { }
            try { previous.release(); } catch(RuntimeException ignored) { }
        }
    }

    @Override public synchronized void close() {
        failure=null;
        releaseCodec();
        needsKeyframe = true;
        burstSchedule.reset();
        sourceSchedule.reset(); sourceTimes.clear(); lastInputPts=0; wirelessOutput=false;
        outputGate.reset();
    }
    void shutdown() {
        close(); pump.shutdownNow();
    }
}
