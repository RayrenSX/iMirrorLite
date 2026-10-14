package com.rayren.imirrorlite;

/** Select decoded outputs by source cadence; compressed reference frames are never skipped. */
final class FrameRateGate {
    private long nextUs, lastUs;
    private int fps=60;
    private boolean started;
    void configure(int fps) { if(fps<1) throw new IllegalArgumentException("Invalid frame rate"); this.fps=fps; reset(); }
    boolean allow(long ptsUs) {
        long period=1_000_000L/fps;
        if(!started || ptsUs<lastUs || ptsUs-lastUs>1_000_000L) {
            started=true; nextUs=ptsUs+period; lastUs=ptsUs; return true;
        }
        lastUs=ptsUs;
        // Arrival jitter must not turn 59.94->30 fps into an uneven 20 fps cap.
        if(ptsUs+Math.min(2_000L,period/8)<nextUs) return false;
        do { nextUs+=period; } while(nextUs<=ptsUs);
        return true;
    }
    void reset() { started=false; nextUs=lastUs=0; }
}
