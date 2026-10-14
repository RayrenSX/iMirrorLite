package com.rayren.imirrorlite;

import com.chaquo.python.PyObject;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.util.Arrays;
import java.util.List;

/** Video bytes stay in Java. Only infrequent protocol/format messages cross Python. */
public final class VideoStream implements AutoCloseable {
    private final UsbHost usb;
    private final PyObject protocol;
    interface Sink {
        void frame(byte[] data,byte[] csd,int width,int height,String mime,long pts) throws Exception;
        void write(byte[] data) throws IOException;
        void diagnostic(String message);
    }
    private final Sink callback;
    private final Thread reader;
    private volatile boolean closing, stopping;
    private volatile long frames;
    private volatile String failure;
    private byte[] pending = new byte[262144];
    private int used, width, height, lengthSize;
    private String mime;
    private byte[] csd;
    private long clock;
    private long lastPts;
    private long reportAt = System.nanoTime(), readCount, readNs, processNs;

    VideoStream(UsbHost usb, PyObject protocol, MainActivity.SessionCallbacks callback) {
        this(usb,protocol,new Sink() {
            public void frame(byte[] data,byte[] csd,int w,int h,String mime,long pts) throws Exception { callback.onVideo(data,csd,w,h,mime,pts); }
            public void write(byte[] data) throws IOException { usb.write(UsbHost.VIDEO,data); }
            public void diagnostic(String message) { callback.onDiagnostic(message); }
        });
    }
    VideoStream(PyObject protocol,Sink callback) { this(null,protocol,callback); }
    private VideoStream(UsbHost usb,PyObject protocol,Sink callback) {
        this.usb=usb; this.protocol=protocol; this.callback=callback;
        reader=usb==null ? null : new Thread(this::read, "video-usb-java");
        if(reader!=null) reader.start();
    }
    public long frameCount() { return frames; }
    public String failure() { return failure; }
    public void beginStop() { stopping=true; }

    private void read() {
        try {
            while (!closing) {
                long started=System.nanoTime();
                byte[] data=usb.read(UsbHost.VIDEO);
                long received=System.nanoTime();
                readNs+=received-started; readCount++;
                if(data.length==0) continue;
                feed(data,received);
                processNs+=System.nanoTime()-received;
                if(received-reportAt>=5_000_000_000L) {
                    callback.diagnostic("Java video performance: reads="+readCount+" readAvgMs="
                        +(readNs/Math.max(1,readCount)/1e6)+" processAvgMs="
                        +(processNs/Math.max(1,readCount)/1e6));
                    reportAt=received; readCount=readNs=processNs=0;
                }
            }
        } catch (Exception e) {
            if(!closing && !stopping) failure=e.toString();
        }
    }

    private static int number(byte[] data,int offset) throws IOException {
        if(offset<0 || offset+4>data.length) throw new IOException("视频包长度不足");
        return (data[offset]&255) | (data[offset+1]&255)<<8 | (data[offset+2]&255)<<16 | (data[offset+3]&255)<<24;
    }
    private static boolean tag(byte[] data,int offset,String name) {
        if(offset+4>data.length) return false;
        for(int i=0;i<4;i++) if(data[offset+i]!=(byte)name.charAt(3-i)) return false;
        return true;
    }
    void feed(byte[] data,long received) throws Exception {
        if((long)used+data.length>32L*1024*1024+65536) throw new IOException("视频组包缓存超过上限");
        if(used+data.length>pending.length) pending=Arrays.copyOf(pending,Math.max(pending.length*2,used+data.length));
        System.arraycopy(data,0,pending,used,data.length); used+=data.length;
        int offset=0;
        while(used-offset>=4) {
            int size=number(pending,offset);
            if(size<8 || size>32*1024*1024) throw new IOException("无效视频包长度");
            if(size>used-offset) break;
            if((tag(pending,offset+4,"asyn") || tag(pending,offset+4,"sync")) && size<20)
                throw new IOException("视频控制包截断");
            if(tag(pending,offset+4,"asyn") && tag(pending,offset+16,"feed")) {
                if(!stopping) {
                    if(clock!=0) requestFrame();
                    media(pending,offset+20,offset+size,received/1000);
                }
            } else {
                if(!stopping && tag(pending,offset+4,"sync") && tag(pending,offset+16,"cvrp")) {
                    if(size<36) throw new IOException("缺少视频时钟");
                    clock=0;
                    for(int i=0;i<8;i++) clock|=(long)(pending[offset+28+i]&255)<<(8*i);
                }
                protocol.callAttr("feed_java",Arrays.copyOfRange(pending,offset,offset+size),received);
            }
            offset+=size;
        }
        if(offset>0) { used-=offset; System.arraycopy(pending,offset,pending,0,used); }
    }
    private void requestFrame() throws IOException {
        byte[] need=new byte[20]; need[0]=20;
        for(int i=0;i<4;i++) { need[4+i]=(byte)"nysa".charAt(i); need[16+i]=(byte)"deen".charAt(i); }
        for(int i=0;i<8;i++) need[8+i]=(byte)(clock>>>(8*i));
        callback.write(need);
    }
    private void media(byte[] data,int start,int end,long pts) throws Exception {
        if(end-start<8 || number(data,start)!=end-start || !tag(data,start+4,"sbuf"))
            throw new IOException("无效视频采样包");
        int encodedStart=-1,encodedEnd=-1;
        for(int offset=start+8;offset<end;) {
            if(end-offset<8) throw new IOException("视频字段截断");
            int size=number(data,offset);
            if(size<8 || size>end-offset) throw new IOException("视频字段长度无效");
            if(tag(data,offset+4,"fdsc")) {
                List<PyObject> format=protocol.callAttr("format_java",Arrays.copyOfRange(data,offset+8,offset+size)).asList();
                width=format.get(0).toInt(); height=format.get(1).toInt(); mime=format.get(2).toString();
                lengthSize=format.get(3).toInt(); csd=format.get(4).toJava(byte[].class);
            } else if(tag(data,offset+4,"sdat")) { encodedStart=offset+8; encodedEnd=offset+size; }
            offset+=size;
        }
        if(csd==null || encodedStart<0 || encodedStart==encodedEnd) return;
        ByteArrayOutputStream annex=new ByteArrayOutputStream(encodedEnd-encodedStart+64);
        for(int offset=encodedStart;offset<encodedEnd;) {
            if(encodedEnd-offset<lengthSize) throw new IOException("NAL 长度截断");
            long length=0;
            for(int i=0;i<lengthSize;i++) length=(length<<8)|(data[offset+i]&255);
            offset+=lengthSize;
            if(length==0 || length>encodedEnd-offset) throw new IOException("NAL 长度无效");
            annex.write(new byte[]{0,0,0,1}); annex.write(data,offset,(int)length); offset+=(int)length;
        }
        // A USB read can contain several complete samples. MediaCodec must
        // receive distinct timestamps even when their local arrival is equal.
        pts=Math.max(pts,lastPts+1); lastPts=pts;
        callback.frame(annex.toByteArray(),csd,width,height,mime,pts); frames++;
    }
    @Override public void close() {
        closing=true;
        if(reader!=null && Thread.currentThread()!=reader) {
            try { reader.join(2500); } catch(InterruptedException e) { Thread.currentThread().interrupt(); }
        }
    }
}
