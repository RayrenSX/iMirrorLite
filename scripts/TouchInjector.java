import android.os.SystemClock;
import android.view.InputEvent;
import android.view.InputDevice;
import android.view.MotionEvent;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/** Development-only shell input. Run only with the iPhone touch observer visible. */
public final class TouchInjector {
    private static Object manager;
    private static Method inject;
    private static long downTime;
    private static float left, top, width, height;
    private static final List<Integer> ids = new ArrayList<>();
    private static final float[] ys = {.45f,.45f,.45f,.45f,.45f,.45f};

    private static void event(int action) throws Exception {
        int count=ids.size();
        MotionEvent.PointerProperties[] props=new MotionEvent.PointerProperties[count];
        MotionEvent.PointerCoords[] coords=new MotionEvent.PointerCoords[count];
        for(int i=0;i<count;i++) {
            int id=ids.get(i);
            props[i]=new MotionEvent.PointerProperties(); props[i].id=id+10; props[i].toolType=MotionEvent.TOOL_TYPE_FINGER;
            coords[i]=new MotionEvent.PointerCoords(); coords[i].x=left+width*(.18f+id*.13f);
            coords[i].y=top+height*ys[id]; coords[i].pressure=1; coords[i].size=.1f;
        }
        MotionEvent e=MotionEvent.obtain(downTime,SystemClock.uptimeMillis(),action,count,props,coords,0,0,1,1,0,0,InputDevice.SOURCE_TOUCHSCREEN,0);
        try {
            if(!Boolean.TRUE.equals(inject.invoke(manager,e,2))) throw new IllegalStateException("Input injection failed");
        } finally { e.recycle(); }
    }
    private static void down(int id) throws Exception {
        ids.add(id);
        event(ids.size()==1?MotionEvent.ACTION_DOWN:MotionEvent.ACTION_POINTER_DOWN|((ids.size()-1)<<8));
    }
    private static void up(int id) throws Exception {
        int index=ids.indexOf(id); if(index<0) return;
        event(ids.size()==1?MotionEvent.ACTION_UP:MotionEvent.ACTION_POINTER_UP|(index<<8)); ids.remove(index);
    }
    public static void main(String[] args) throws Exception {
        if(args.length!=4) throw new IllegalArgumentException("Pass actual video left top width height");
        left=Float.parseFloat(args[0]); top=Float.parseFloat(args[1]); width=Float.parseFloat(args[2]); height=Float.parseFloat(args[3]);
        Class<?> cls=Class.forName("android.hardware.input.InputManager");
        manager=cls.getMethod("getInstance").invoke(null);
        inject=cls.getMethod("injectInputEvent",InputEvent.class,int.class);
        downTime=SystemClock.uptimeMillis();
        try {
            for(int i=0;i<5;i++) { down(i); Thread.sleep(200); }
            System.out.println("STAGE five down and held"); Thread.sleep(1500);
            ys[2]=.6f; event(MotionEvent.ACTION_MOVE);
            System.out.println("STAGE one moves four held"); Thread.sleep(800);
            down(5); System.out.println("STAGE sixth rejected"); Thread.sleep(800);
            up(2); System.out.println("STAGE independent third up"); Thread.sleep(800);
            ys[5]=.65f; event(MotionEvent.ACTION_MOVE); Thread.sleep(800);
            up(5); down(5); System.out.println("STAGE sixth re-down reuses slot"); Thread.sleep(800);
            event(MotionEvent.ACTION_CANCEL); ids.clear();
            System.out.println("STAGE cancel releases all"); Thread.sleep(800);
        } finally {
            if(!ids.isEmpty()) { event(MotionEvent.ACTION_CANCEL); ids.clear(); }
        }
    }
}
