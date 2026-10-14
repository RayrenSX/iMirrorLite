package com.rayren.imirrorlite;

import android.hardware.usb.*;
import java.io.IOException;
import java.util.Arrays;
import com.chaquo.python.PyObject;

/** Owns one authorized Apple device. Never opens another phone as a fallback. */
public final class UsbHost implements AutoCloseable {
    public static final int VIDEO = 0x2a, MUX = 0xfe;
    private final UsbDeviceConnection connection;
    private final UsbInterface videoInterface, muxInterface;
    private final UsbEndpoint videoIn, videoOut, muxIn, muxOut;
    private final int configurationBefore;
    private final boolean configurationChanged, videoAltChanged, muxAltChanged;
    private String startupVideoReset="pending";
    private volatile boolean closed;
    private final Object ioLock=new Object();
    private int activeIo;
    private VideoStream videoStream;
    private final byte[] videoReadBuffer = new byte[65536];
    private final byte[] muxReadBuffer = new byte[16384];
    public final String serial;
    public final int productId;

    static String normalizeSerial(String raw) throws IOException {
        if (raw == null) throw new IOException("无法读取 iPhone USB 序列号");
        String value = raw.replace("\u0000", "").trim();
        if (value.isEmpty()) throw new IOException("无法读取 iPhone USB 序列号");
        return value.length() == 24 && !value.contains("-")
                ? value.substring(0, 8) + "-" + value.substring(8) : value;
    }

    public static boolean hasVideo(UsbDevice device) {
        for (int c = 0; c < device.getConfigurationCount(); c++) {
            UsbConfiguration config = device.getConfiguration(c);
            for (int i = 0; i < config.getInterfaceCount(); i++)
                if (config.getInterface(i).getInterfaceSubclass() == VIDEO &&
                    config.getInterface(i).getInterfaceClass() == 255) return true;
        }
        return false;
    }

    public static String requestVideo(UsbManager manager, UsbDevice device) throws IOException {
        UsbDeviceConnection c = manager.openDevice(device);
        if (c == null) throw new IOException("无法打开 USB；请重新授权");
        try {
            byte[] current = new byte[1];
            int before = c.controlTransfer(0x80, 8, 0, 0, current, 1, 1000) == 1 ? current[0]&255 : -1;
            // A successful switch may disconnect before controlTransfer returns.
            int result=c.controlTransfer(0x40, 0x52, 0, 2, null, 0, 1500);
            return "QuickTime activation: previous="+before+" result="+result;
        } finally { c.close(); }
    }

    public UsbHost(UsbManager manager, UsbDevice device) throws IOException {
        productId = device.getProductId();
        serial = normalizeSerial(device.getSerialNumber());
        connection = manager.openDevice(device);
        if (connection == null) throw new IOException("USB 授权已失效");
        UsbInterface video = null, mux = null;
        try {
            UsbConfiguration selected = null;
            for (int c = 0; c < device.getConfigurationCount(); c++) {
                UsbConfiguration config = device.getConfiguration(c);
                UsbInterface v = null, m = null;
                for (int i = 0; i < config.getInterfaceCount(); i++) {
                    UsbInterface it = config.getInterface(i);
                    if (it.getInterfaceClass() != 255) continue;
                    if (it.getInterfaceSubclass() == VIDEO) v = it;
                    if (it.getInterfaceSubclass() == MUX) m = it;
                }
                if (v != null && m != null) { selected = config; video = v; mux = m; break; }
            }
            if (selected == null) throw new IOException("设备尚未提供投屏和控制 USB 接口");
            configurationBefore = activeConfiguration();
            configurationChanged = configurationBefore != selected.getId();
            // SET_CONFIGURATION resets every endpoint, even when the value is
            // unchanged. Preserve a QuickTime session started by re-enumeration.
            if (configurationChanged && !connection.setConfiguration(selected))
                throw new IOException("无法选择 iPhone 投屏配置");
            if (!connection.claimInterface(video, true)) throw new IOException("无法占用投屏接口");
            if (!connection.claimInterface(mux, true)) throw new IOException("无法占用控制接口");
            videoAltChanged = selectInterface(video);
            muxAltChanged = selectInterface(mux);
            videoInterface = video; muxInterface = mux;
            videoIn = endpoint(video, UsbConstants.USB_DIR_IN);
            videoOut = endpoint(video, UsbConstants.USB_DIR_OUT);
            muxIn = endpoint(mux, UsbConstants.USB_DIR_IN);
            muxOut = endpoint(mux, UsbConstants.USB_DIR_OUT);
            // Match the original iPhoneMirror libusb1/UsbDk startup: a freshly
            // opened QuickTime pipe may retain a halted endpoint/data toggle.
            // This precedes all URBs and is never a replay of an uncertain OUT.
            try {
                clearVideoHalt(videoIn); clearVideoHalt(videoOut);
                startupVideoReset="ok";
            } catch(IOException error) {
                // Older controllers may not support this optional reset.
                startupVideoReset=error.getMessage();
            }
        } catch (Exception e) {
            if (mux != null) connection.releaseInterface(mux);
            if (video != null) connection.releaseInterface(video);
            connection.close();
            throw e instanceof IOException ? (IOException)e : new IOException(e);
        }
    }

    private int activeConfiguration() throws IOException {
        byte[] value = new byte[1];
        if (connection.controlTransfer(0x80, 8, 0, 0, value, 1, 1000) != 1)
            throw new IOException("无法读取当前 USB 配置");
        return value[0] & 255;
    }

    private void clearVideoHalt(UsbEndpoint ep) throws IOException {
        int result = UsbIo.clearHalt(connection.getFileDescriptor(), ep.getAddress());
        if (result < 0) throw new IOException("无法清理 USB 视频端点："+ep.getAddress()+" errno="+(-result));
    }

    private boolean selectInterface(UsbInterface iface) throws IOException {
        byte[] value = new byte[1];
        int result = connection.controlTransfer(0x81, 10, 0, iface.getId(), value, 1, 1000);
        if (result != 1) throw new IOException("无法读取 USB 接口状态：" + iface.getId());
        if ((value[0] & 255) == iface.getAlternateSetting()) return false;
        if (!connection.setInterface(iface)) throw new IOException("无法启用 USB 接口：" + iface.getId());
        return true;
    }

    private static UsbEndpoint endpoint(UsbInterface iface, int direction) throws IOException {
        for (int i = 0; i < iface.getEndpointCount(); i++) {
            UsbEndpoint ep = iface.getEndpoint(i);
            if (ep.getType() == UsbConstants.USB_ENDPOINT_XFER_BULK && ep.getDirection() == direction) return ep;
        }
        throw new IOException("USB bulk 端点缺失");
    }

    public int muxPacketSize() { return muxOut.getMaxPacketSize(); }
    public VideoStream startVideo(PyObject protocol, MainActivity.SessionCallbacks callback) {
        if(videoStream!=null) throw new IllegalStateException("视频读取已启动");
        return videoStream=new VideoStream(this,protocol,callback);
    }

    public String describe() throws IOException {
        return "Active USB configuration="+activeConfiguration()+" previous="+configurationBefore
                +" changed="+configurationChanged+"; video interface="+videoInterface.getId()
                +" altChanged="+videoAltChanged+" IN="+videoIn.getAddress()+" OUT="+videoOut.getAddress()
                +" startupVideoReset="+startupVideoReset
                +"; mux altChanged="+muxAltChanged;
    }

    public int wakeVideo() {
        try { beginIo(); }
        catch(IOException error) { return -19; }
        try { return connection.controlTransfer(0x40, 0x40, 0x6400, 0x6400, null, 0, 1000); }
        finally { endIo(); }
    }

    private void beginIo() throws IOException {
        synchronized(ioLock) {
            if(closed) throw new IOException("USB 已关闭");
            activeIo++;
        }
    }
    private void endIo() {
        synchronized(ioLock) { activeIo--; ioLock.notifyAll(); }
    }

    public byte[] read(int channel) throws IOException {
        if(channel!=VIDEO && channel!=MUX) throw new IOException("无效 USB 通道");
        beginIo();
        try {
        // Native video URBs support larger reads. Mux packets retain their
        // separate Android bulkTransfer path and framing.
        byte[] data = channel == VIDEO ? videoReadBuffer : muxReadBuffer;
        int length = channel == VIDEO
                ? UsbIo.transfer(connection.getFileDescriptor(), videoIn.getAddress(), data, 1000)
                : connection.bulkTransfer(muxIn, data, data.length, 1000);
        if (channel == VIDEO && length == -32) { clearVideoHalt(videoIn); return new byte[0]; }
        if (channel == VIDEO && length < 0 && length != -110)
            throw new IOException("USB 视频读取失败 errno="+(-length));
        if (closed) throw new IOException("USB 已断开");
        return length <= 0 ? new byte[0] : Arrays.copyOf(data, length);
        } finally { endIo(); }
    }

    public void write(int channel, byte[] data) throws IOException {
        if(channel!=VIDEO && channel!=MUX) throw new IOException("无效 USB 通道");
        UsbEndpoint ep = channel == VIDEO ? videoOut : muxOut;
        synchronized (ep) {
            beginIo();
            try {
            // usbmux packet boundaries matter. One complete packet per transfer,
            // including its separate ZLP. Never retry an uncertain input write.
            int size = channel == VIDEO
                    ? UsbIo.transfer(connection.getFileDescriptor(), ep.getAddress(), data, 2000)
                    : connection.bulkTransfer(ep, data, data.length, 2000);
            if (channel == VIDEO && size == -32) clearVideoHalt(ep);
            if (size != data.length) throw new IOException("USB 写入未完成 channel="+channel+" result="+size+" expected="+data.length);
            } finally { endIo(); }
        }
    }

    @Override public void close() {
        synchronized(ioLock) {
            if(closed) return;
            closed=true;
        }
        if(videoStream!=null) videoStream.close();
        // A timed join alone does not prove JNI has relinquished its buffers
        // and fd. Wait for every bounded USB call before releasing interfaces.
        boolean interrupted=false;
        synchronized(ioLock) {
            while(activeIo>0) {
                try { ioLock.wait(); }
                catch(InterruptedException error) { interrupted=true; }
            }
        }
        connection.releaseInterface(muxInterface);
        connection.releaseInterface(videoInterface);
        connection.controlTransfer(0x40, 0x52, 0, 0, null, 0, 1000);
        connection.close();
        if(interrupted) Thread.currentThread().interrupt();
    }
}
