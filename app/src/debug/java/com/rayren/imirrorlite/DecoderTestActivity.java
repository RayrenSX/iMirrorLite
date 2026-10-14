package com.rayren.imirrorlite;

/** Isolated surface host for instrumentation; excluded from release builds. */
public final class DecoderTestActivity extends android.app.Activity {
    static volatile DecoderTestActivity current;
    @Override public void onCreate(android.os.Bundle state) { super.onCreate(state); current=this; }
    @Override public void onDestroy() { if(current==this) current=null; super.onDestroy(); }
}
