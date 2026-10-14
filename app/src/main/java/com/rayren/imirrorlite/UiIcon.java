package com.rayren.imirrorlite;

import android.graphics.Canvas;
import android.graphics.ColorFilter;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.drawable.Drawable;

/** A shared, resolution-independent line icon set for the mirror controls. */
final class UiIcon extends Drawable {
    private final String name;
    private final int size;
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);

    UiIcon(String name, int color, int size) {
        this.name = name;
        this.size = size;
        paint.setColor(color);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(1.7f);
        paint.setStrokeCap(Paint.Cap.ROUND);
        paint.setStrokeJoin(Paint.Join.ROUND);
        setBounds(0, 0, size, size);
    }

    @Override public void draw(Canvas canvas) {
        Rect b = getBounds();
        canvas.save();
        canvas.translate(b.left, b.top);
        canvas.scale(b.width() / 24f, b.height() / 24f);
        switch (name) {
            case "brand":
                canvas.drawRoundRect(3, 4, 14, 20, 3, 3, paint);
                canvas.drawRoundRect(10, 7, 21, 18, 2, 2, paint);
                line(canvas, 7, 17, 10, 17); break;
            case "device":
                canvas.drawRoundRect(6, 2, 18, 22, 3, 3, paint);
                line(canvas, 10, 5, 14, 5); line(canvas, 10, 19, 14, 19); break;
            case "help":
                canvas.drawCircle(12, 12, 9, paint);
                canvas.drawArc(9, 6, 15, 12, 195, 240, false, paint);
                line(canvas, 12, 12, 12, 14); line(canvas, 12, 17, 12, 17.1f); break;
            case "home": canvas.drawCircle(12, 12, 7, paint); break;
            case "app_switcher":
                canvas.drawRoundRect(7, 7, 20, 20, 2, 2, paint);
                path(canvas, 4, 16, 4, 4, 16, 4); break;
            case "none": path(canvas, 14, 5, 7, 12, 14, 19); break;
            case "control_center":
                line(canvas, 5, 4, 5, 20); line(canvas, 12, 4, 12, 20); line(canvas, 19, 4, 19, 20);
                knob(canvas, 5, 9); knob(canvas, 12, 15); knob(canvas, 19, 8); break;
            case "notification_center":
                path(canvas, 5, 17, 7, 14, 7, 9);
                canvas.drawArc(7, 4, 17, 14, 180, 180, false, paint);
                path(canvas, 17, 9, 17, 14, 19, 17, 5, 17);
                canvas.drawArc(10, 17, 14, 21, 0, 180, false, paint); break;
            case "volume_down": case "volume_up":
                path(canvas, 4, 9, 8, 9, 13, 5, 13, 19, 8, 15, 4, 15, 4, 9);
                line(canvas, 17, 12, 22, 12);
                if (name.equals("volume_up")) line(canvas, 19.5f, 9.5f, 19.5f, 14.5f); break;
            case "siri":
                line(canvas, 4, 10, 4, 14); line(canvas, 8, 6, 8, 18);
                line(canvas, 12, 3, 12, 21); line(canvas, 16, 7, 16, 17); line(canvas, 20, 10, 20, 14); break;
            case "lock":
                canvas.drawRoundRect(5, 10, 19, 21, 2, 2, paint);
                canvas.drawArc(8, 2, 16, 14, 180, 180, false, paint);
                line(canvas, 12, 14, 12, 17); break;
            case "fullscreen":
                path(canvas, 9, 4, 4, 4, 4, 9); path(canvas, 15, 4, 20, 4, 20, 9);
                path(canvas, 4, 15, 4, 20, 9, 20); path(canvas, 15, 20, 20, 20, 20, 15); break;
            case "exit_fullscreen":
                path(canvas, 4, 9, 9, 9, 9, 4); path(canvas, 15, 4, 15, 9, 20, 9);
                path(canvas, 4, 15, 9, 15, 9, 20); path(canvas, 15, 20, 15, 15, 20, 15); break;
        }
        canvas.restore();
    }

    private void knob(Canvas canvas, float x, float y) {
        paint.setStyle(Paint.Style.FILL); canvas.drawCircle(x, y, 2.5f, paint);
        paint.setStyle(Paint.Style.STROKE);
    }
    private void line(Canvas canvas, float x1, float y1, float x2, float y2) {
        canvas.drawLine(x1, y1, x2, y2, paint);
    }
    private void path(Canvas canvas, float... points) {
        Path path = new Path(); path.moveTo(points[0], points[1]);
        for (int i = 2; i < points.length; i += 2) path.lineTo(points[i], points[i + 1]);
        canvas.drawPath(path, paint);
    }
    @Override public void setAlpha(int alpha) { paint.setAlpha(alpha); invalidateSelf(); }
    @Override public void setColorFilter(ColorFilter filter) { paint.setColorFilter(filter); invalidateSelf(); }
    @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
    @Override public int getIntrinsicWidth() { return size; }
    @Override public int getIntrinsicHeight() { return size; }
}
