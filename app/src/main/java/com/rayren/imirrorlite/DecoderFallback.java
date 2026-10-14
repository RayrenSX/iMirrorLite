package com.rayren.imirrorlite;

import java.io.IOException;

/** Start the selected decoder without allocating an automatic decoder first. */
final class DecoderFallback {
    interface Attempt { void start() throws IOException; }

    static boolean start(Attempt selected, Attempt automatic) throws IOException {
        Exception selectedError = null;
        if (selected != null) {
            try { selected.start(); return false; }
            catch (IOException | RuntimeException error) { selectedError = error; }
        }
        try { automatic.start(); }
        catch (IOException | RuntimeException error) {
            if (selectedError != null && selectedError != error) error.addSuppressed(selectedError);
            throw error;
        }
        return selectedError != null;
    }
}
