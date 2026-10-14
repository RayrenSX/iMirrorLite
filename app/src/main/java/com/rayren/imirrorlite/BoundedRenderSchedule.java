package com.rayren.imirrorlite;

/** Preserve small decode bursts without growing an unbounded Surface queue. */
final class BoundedRenderSchedule {
    static final long PERIOD_NS = 16_666_667L;
    static final long MAX_FUTURE_NS = 40_000_000L;
    private long last, first;
    private long periodNs=PERIOD_NS;
    void setFrameRate(int fps) {
        if(fps<1) throw new IllegalArgumentException("Invalid frame rate");
        // Buffers already submitted to Surface retain their presentation times.
        // Live FPS/resolution changes must continue after the last such buffer;
        // only closing/rebuilding a decoder starts a new presentation epoch.
        periodNs=1_000_000_000L/fps;
    }

    long maxFutureNs() { return Math.max(MAX_FUTURE_NS,2*periodNs); }

    int plan(long now, int available) {
        if(available<1 || available>2) throw new IllegalArgumentException("Expected one or two outputs");
        first=last==0 || now-last>50_000_000L ? now+12_000_000L : Math.max(now,last+periodNs);
        // Two slots need more than 40 ms at 24/30 fps. Bound by both time and
        // frame periods so coalesced USB reads do not halve the requested FPS.
        long room=now+maxFutureNs()-first;
        int count=room<0 ? 0 : Math.min(available,1+(int)(room/periodNs));
        if(count>0) last=first+(count-1)*periodNs;
        return count;
    }

    long time(int index) { return first+index*periodNs; }
    void reset() { last=first=0; }
}
