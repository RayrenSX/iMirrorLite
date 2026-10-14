package com.rayren.imirrorlite;

import android.content.Context;
import android.os.Build;
import android.util.Log;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardOpenOption;
import java.time.Instant;

/** Small app-private log for diagnosing OTG sessions after reconnecting USB. */
final class SessionLog {
    private final File file;
    SessionLog(Context context) {
        File directory = new File(context.getFilesDir(), "diagnostics");
        directory.mkdirs();
        file = new File(directory, "session.log");
        record("app", "iMirrorLite 0.1.0-dev; " + Build.MODEL + "; Android "
                + Build.VERSION.RELEASE + "; API " + Build.VERSION.SDK_INT);
    }
    synchronized void record(String kind, String message) {
        if (message == null) message = "unknown";
        // Device identifiers and pairing material do not belong in a copied log.
        String safe = message.replaceAll("(?i)\\b[0-9a-f]{8}-?[0-9a-f]{16}\\b|\\b[0-9a-f]{40}\\b", "[device]");
        if (safe.length() > 2000) safe = safe.substring(0, 2000);
        String line = Instant.now() + " [" + kind + "] " + safe + "\n";
        Log.i("iMirrorLite", line);
        try {
            if (file.length() > 128 * 1024) {
                Files.move(file.toPath(), new File(file.getParentFile(), "previous.log").toPath(),
                        java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            }
            Files.write(file.toPath(), line.getBytes(StandardCharsets.UTF_8),
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException error) { Log.w("iMirrorLite", "Cannot save diagnostics", error); }
    }
    synchronized String read() {
        try { return new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8); }
        catch (IOException error) { return "暂无诊断日志"; }
    }
}
