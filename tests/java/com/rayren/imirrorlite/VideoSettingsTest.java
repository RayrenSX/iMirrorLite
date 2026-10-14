package com.rayren.imirrorlite;

public final class VideoSettingsTest {
    private static void require(boolean ok,String message) { if(!ok) throw new AssertionError(message); }
    public static void main(String[] args) {
        VideoProfile nativeSize=new VideoProfile(0,60,"auto",false);
        require(nativeSize.renderSize(1170,2532)[1]==2532,"Native was resized");
        VideoProfile small=new VideoProfile(720,30,"auto",false);
        int[] portrait=small.renderSize(1170,2532), landscape=small.renderSize(2532,1170);
        require(portrait[0]==591 && portrait[1]==1280,"Portrait box or aspect is incorrect");
        require(landscape[0]==1280 && landscape[1]==591,"Rotation changed the selected cap");
        require(small.renderSize(320,480)[0]==320,"Small source was upscaled");
        try { new VideoProfile(0,60,"auto",true); throw new AssertionError("Wireless native profile accepted"); }
        catch(IllegalArgumentException expected) { }
        for(int target:new int[]{24,30,60,120}) {
            for(int source:new int[]{30,60,120}) {
                FrameRateGate gate=new FrameRateGate(); gate.configure(target);
                int accepted=0;
                for(int n=0;n<source*10;n++) {
                    long jitter=n%2==0?0:500;
                    if(gate.allow(1_000_000L+n*1_000_000L/source+jitter)) accepted++;
                }
                require(Math.abs(accepted-10*Math.min(source,target))<=1,"FPS cap inaccurate: "+source+" -> "+target+" = "+accepted);
                require(gate.allow(99_000_000L),"Stall did not rebase FPS cap");
                require(gate.allow(1),"New timestamp epoch did not reset FPS cap");
            }
        }
        SourceRenderSchedule schedule=new SourceRenderSchedule(); schedule.setFrameRate(120);
        BoundedRenderSchedule usbSchedule=new BoundedRenderSchedule(); usbSchedule.setFrameRate(120);
        long last=0;
        for(int i=0;i<120;i++) {
            long ns=1_000_000_000L+i*1_000_000_000L/120;
            long render=schedule.time(ns+8_000_000L,ns/1000,ns/1000);
            require(render>last,"120 FPS sender cadence lost or reordered a frame"); last=render;
            require(usbSchedule.plan(ns,1)==1,"120 FPS USB pacing lost a frame");
        }
        schedule.setFrameRate(30);
        int kept=0; long received=0;
        for(int i=0;i<300;i++) {
            long source=2_000_000_000L+i*33_333_333L;
            long jitter=new long[]{0,40_000_000,5_000_000,60_000_000,0}[i%5];
            received=Math.max(received,source+jitter);
            long output=received+15_000_000;
            long render=schedule.time(output,received/1000,source/1000);
            if(render>=0) { kept++; require(render<=output+66_666_666L,"30 FPS queue grew unbounded"); }
        }
        require(kept>=270,"30 FPS jitter caused excessive drops: "+kept);
        System.out.println("VIDEO_SETTINGS_OK: aspect, orientation, no upscale, 24/30/60/120 caps, source pacing");
    }
}
