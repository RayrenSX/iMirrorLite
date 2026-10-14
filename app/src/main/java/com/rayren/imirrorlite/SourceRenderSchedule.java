package com.rayren.imirrorlite;

/** A small wireless jitter buffer driven by sender cadence, not USB's 60 Hz clock. */
final class SourceRenderSchedule {
    private static final long BUFFER_NS=32_000_000L, MAX_AHEAD_NS=50_000_000L;
    private static final long PERIOD_NS=16_666_667L;
    private long offsetNs, lastSourceUs, lastReleaseNs;
    private boolean anchored;
    private long periodNs=PERIOD_NS;
    void setFrameRate(int fps) { periodNs=1_000_000_000L/fps; reset(); }

    long time(long now,long receivedUs,long sourceUs) {
        long receivedNs=receivedUs*1000L;
        long stepUs=sourceUs-lastSourceUs;
        long desired=receivedNs+BUFFER_NS;
        if(sourceUs>0) {
            long sourceNs=sourceUs*1000L;
            long observedOffset=receivedNs-sourceNs;
            if(!anchored || stepUs<0 || stepUs>1_000_000L) {
                offsetNs=observedOffset; anchored=true;
            } else {
                // The sender and Android have different monotonic epochs. A
                // minimum arrival offset removes transient network lateness;
                // it is not a measurement of one-way network latency.
                offsetNs=Math.min(offsetNs,observedOffset);
            }
            desired=sourceNs+offsetNs+BUFFER_NS;
            lastSourceUs=sourceUs;
        }
        // A cold decoder or changed network delay can miss the jitter target.
        // Present late frames as soon as a bounded slot is available; dropping
        // them all or adding another buffer would amplify that upstream stall.
        long spacing=stepUs>=4_000 && stepUs<=50_000 ? Math.min(periodNs,stepUs*1000L) : periodNs;
        long target=Math.max(now,desired);
        if(lastReleaseNs>0 && lastReleaseNs>=now-periodNs)
            target=Math.max(target,lastReleaseNs+spacing);
        // At 24/30 fps two presentation slots need more than the 50 ms used
        // for 60 fps. Keep the queue bounded in frames as well as wall time.
        if(target>now+Math.max(MAX_AHEAD_NS,2*periodNs)) return -1;
        lastReleaseNs=target;
        return target;
    }

    void reset() { anchored=false; offsetNs=lastSourceUs=lastReleaseNs=0; }
}
