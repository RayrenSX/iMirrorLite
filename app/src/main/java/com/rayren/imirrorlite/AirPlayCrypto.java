package com.rayren.imirrorlite;

/** Stream key unwrap only; lengths checked before entering PlayFair. */
public final class AirPlayCrypto {
    static { System.loadLibrary("imirrorairplay"); }
    private AirPlayCrypto() {}
    public static native byte[] unwrap(byte[] handshake, byte[] encrypted);
}
