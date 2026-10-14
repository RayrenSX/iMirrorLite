package com.rayren.imirrorlite;

final class UsbIo {
    static { System.loadLibrary("imirrorio"); }
    static native int transfer(int fd, int endpoint, byte[] data, int timeout);
    static native int clearHalt(int fd, int endpoint);
}
