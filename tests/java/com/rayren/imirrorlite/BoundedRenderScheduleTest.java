package com.rayren.imirrorlite;

/** Regression: 60 frames arriving in pairs must retain 60 presentation slots. */
public final class BoundedRenderScheduleTest {
    private static void require(boolean ok,String message) {
        if(!ok) throw new AssertionError(message);
    }
    public static void main(String[] args) {
        BoundedRenderSchedule schedule=new BoundedRenderSchedule();
        long start=1_000_000_000L, previous=0;
        int submitted=0;
        for(int burst=0;burst<30;burst++) {
            long now=start+burst*2*BoundedRenderSchedule.PERIOD_NS;
            int count=schedule.plan(now,2);
            require(count==2,"Two-frame burst lost a frame");
            for(int i=0;i<count;i++) {
                long stamp=schedule.time(i);
                require(stamp>previous,"Presentation order regressed");
                require(stamp-now<=BoundedRenderSchedule.MAX_FUTURE_NS,"Future queue grew");
                previous=stamp; submitted++;
            }
        }
        require(submitted==60,"Batched 60 fps stream became 30 fps");
        require(schedule.plan(start+2_000_000_000L,2)==2,"Stall did not rebase");
        schedule.reset();
        require(schedule.plan(start,2)==2,"Reconnection retained old schedule");
        previous=0;
        for(int burst=0;burst<100;burst++) {
            long now=start+burst*1_000_000L;
            int count=schedule.plan(now,2);
            for(int i=0;i<count;i++) {
                long stamp=schedule.time(i);
                require(stamp>previous,"Overload reordered frames");
                require(stamp<=now+BoundedRenderSchedule.MAX_FUTURE_NS,"Overload grew future queue");
                previous=stamp;
            }
        }
        schedule.reset(); previous=0;
        for(int frame=0;frame<60;frame++) {
            long now=start+frame*BoundedRenderSchedule.PERIOD_NS;
            require(schedule.plan(now,1)==1,"Steady 60 fps lost frame");
            require(schedule.time(0)>previous,"Steady 60 fps regressed");
            previous=schedule.time(0);
        }
        System.out.println("BOUNDED_RENDER_SCHEDULE_OK: pairs, overload, stalls, reconnect, steady 60 fps");
    }
}
