package com.rayren.imirrorlite;

/** USB receipt timestamps are deliberately absent: a read can merge multiple source frames. */
public final class UsbRenderScheduleTest {
    private static void require(boolean ok,String message) { if(!ok) throw new AssertionError(message); }

    private static int simulate(int sourceFps,int targetFps,int readFrames,boolean batchedOutputs) {
        BoundedRenderSchedule schedule=new BoundedRenderSchedule(); schedule.setFrameRate(targetFps);
        long previous=0, period=1_000_000_000L/targetFps;
        int submitted=0;
        for(int frame=0;frame<sourceFps*10;frame+=readFrames) {
            long now=1_000_000_000L+frame*1_000_000_000L/sourceFps;
            int calls=batchedOutputs?1:readFrames;
            for(int call=0;call<calls;call++) {
                int count=schedule.plan(now,batchedOutputs?Math.min(2,readFrames):1);
                for(int i=0;i<count;i++) {
                    long render=schedule.time(i);
                    require(previous==0 || render-previous>=period,
                        "Output exceeded "+targetFps+" fps for source "+sourceFps+", read="+readFrames);
                    require(render>=now && render<=now+schedule.maxFutureNs(),"USB queue grew unbounded");
                    previous=render; submitted++;
                }
            }
        }
        require(submitted<=targetFps*10+3,"Output count exceeded cap: "+submitted);
        return submitted;
    }

    public static void main(String[] args) {
        liveOptions();
        for(int target:new int[]{24,30,60,120}) {
            for(int source:new int[]{24,30,60,120}) {
                for(int read:new int[]{1,2,4}) {
                    for(boolean batched:new boolean[]{false,true}) {
                        int submitted=simulate(source,target,read,batched);
                        // Two slots per read are guaranteed to fit. Larger,
                        // slower bursts can exceed the bounded latency window.
                        int available=Math.min(source,Math.min(2,read)*source/read);
                        if(available>=target)
                            require(Math.abs(submitted-target*10)<=3,"Requested cadence was lost: "
                                +source+" -> "+target+", read="+read+", batched="+batched+", kept="+submitted);
                        else if(target>=source && read<=2)
                            require(submitted==source*10,"Source frames were unnecessarily dropped");
                    }
                }
            }
        }
        int combined30=simulate(60,30,4,false);
        int decodedBurst30=simulate(60,30,4,true);
        int limited60=simulate(120,60,1,false);
        require(combined30==300 && decodedBurst30==300,"Four-frame reads halved 30 fps");
        require(limited60>=599 && limited60<=603,"120 -> 60 fps limit failed");

        BoundedRenderSchedule schedule=new BoundedRenderSchedule(); schedule.setFrameRate(30);
        require(schedule.plan(1_000_000_000L,2)==2,"30 fps cannot fit two presentation slots");
        require(schedule.plan(3_000_000_000L,2)==2,"Stall did not rebase USB slots");
        schedule.setFrameRate(24);
        require(schedule.plan(3_100_000_000L,2)==2,"Changing FPS retained an old schedule");
        require(schedule.time(1)-schedule.time(0)==1_000_000_000L/24,"FPS change did not update spacing");
        schedule.reset();
        require(schedule.plan(1_000_000_000L,2)==2,"New connection retained the old clock");

        System.out.println("USB_RENDER_SCHEDULE_OK: 60 fps / four-frame reads -> "+combined30
            +" slots at 30 fps; 120 -> 60 fps = "+limited60+" slots / 10 s; 24/30/60/120 caps");
    }

    private static void liveOptions() {
        for(int oldFps:new int[]{24,30,60,120}) {
            for(int newFps:new int[]{24,30,60,120}) {
                BoundedRenderSchedule schedule=new BoundedRenderSchedule(); schedule.setFrameRate(oldFps);
                long start=1_000_000_000L;
                int pending=schedule.plan(start,2);
                require(pending>0,"No initial frame");
                long previous=schedule.time(pending-1), newPeriod=1_000_000_000L/newFps;
                // Surface already owns these pending buffers. Even changing
                // only resolution calls setFrameRate with the same value.
                schedule.setFrameRate(newFps);
                int accepted=0;
                for(int attempt=0;attempt<100;attempt++) {
                    long now=start+1_000_000L+attempt*5_000_000L;
                    int count=schedule.plan(now,2);
                    for(int i=0;i<count;i++) {
                        long render=schedule.time(i);
                        require(render-previous>=newPeriod,"Live options reordered pending frames: "
                            +oldFps+" -> "+newFps);
                        require(render<=now+schedule.maxFutureNs(),"Live change grew presentation queue");
                        previous=render; accepted++;
                    }
                }
                require(accepted>=newFps/4,"Live change never resumed rendering");
                schedule.reset();
                require(schedule.plan(start,1)==1 && schedule.time(0)<previous,
                    "New session did not reset its own clock");
            }
        }
        System.out.println("USB_LIVE_OPTIONS_OK: pending frames keep order through all FPS changes and same-FPS resize");
    }
}
