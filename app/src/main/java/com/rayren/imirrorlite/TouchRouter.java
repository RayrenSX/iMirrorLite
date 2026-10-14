package com.rayren.imirrorlite;

import android.view.MotionEvent;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.util.HashSet;
import java.util.Set;
import java.util.function.Consumer;

/** Stable Android pointer IDs, normalized within the actual video rectangle. */
final class TouchRouter {
    private final Consumer<String> send;
    private final Set<Integer> active = new HashSet<>();
    private int capacity;
    TouchRouter(Consumer<String> sender) { send = sender; }
    void setCapacity(int value) { cancel(); capacity = Math.max(0, Math.min(5, value)); }

    boolean onTouch(MotionEvent e, int width, int height) {
        if (capacity == 0 || width <= 0 || height <= 0) return true;
        int action = e.getActionMasked(), changed = e.getActionIndex();
        if (action == MotionEvent.ACTION_CANCEL) { cancel(); return true; }
        boolean down = action == MotionEvent.ACTION_DOWN || action == MotionEvent.ACTION_POINTER_DOWN;
        boolean up = action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_POINTER_UP;
        if (action == MotionEvent.ACTION_DOWN) cancel();
        int changedId = e.getPointerId(changed);
        if (down && active.size() < capacity) active.add(changedId);
        JSONArray points = new JSONArray();
        try {
            for (int i = 0; i < e.getPointerCount(); i++) {
                int id = e.getPointerId(i);
                if (!active.contains(id)) continue;
                JSONObject p = new JSONObject();
                p.put("id", id);
                p.put("x", Math.max(0, Math.min(1, e.getX(i) / width)));
                p.put("y", Math.max(0, Math.min(1, e.getY(i) / height)));
                p.put("action", i == changed && down ? "down" : i == changed && up ? "up" : "move");
                points.put(p);
            }
            if (points.length() > 0) send.accept(new JSONObject().put("points", points).toString());
        } catch (JSONException ignored) { cancel(); }
        if (up) active.remove(changedId);
        return true;
    }

    void cancel() {
        boolean hadContacts = !active.isEmpty();
        active.clear();
        if (hadContacts) send.accept("{\"cancel\":true}");
    }
}
