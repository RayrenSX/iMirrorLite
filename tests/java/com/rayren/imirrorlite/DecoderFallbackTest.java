package com.rayren.imirrorlite;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

public final class DecoderFallbackTest {
    private static void require(boolean ok,String message) { if(!ok) throw new AssertionError(message); }

    public static void main(String[] args) throws IOException {
        List<String> attempts=new ArrayList<>();
        boolean fallback=DecoderFallback.start(
            () -> attempts.add("software"),
            () -> { throw new IOException("Default hardware allocation is unavailable"); });
        require(!fallback && attempts.equals(List.of("software")),
            "Working software selection depended on default hardware creation");

        for(boolean allocationFailure:new boolean[]{false,true}) {
            attempts.clear();
            fallback=DecoderFallback.start(
                () -> {
                    attempts.add("selected");
                    if(allocationFailure) throw new IllegalStateException("Selected decoder cannot be allocated");
                    throw new IOException("Selected decoder cannot start this stream");
                },
                () -> attempts.add("automatic"));
            require(fallback && attempts.equals(List.of("selected","automatic")),
                "Selected creation/start failure did not reach automatic fallback");
        }

        attempts.clear();
        fallback=DecoderFallback.start(null,() -> attempts.add("automatic"));
        require(!fallback && attempts.equals(List.of("automatic")),"Automatic selection was skipped or duplicated");

        IOException selectedError=new IOException("Selected decoder failed");
        IllegalStateException automaticError=new IllegalStateException("Automatic decoder failed");
        try {
            DecoderFallback.start(() -> { throw selectedError; },() -> { throw automaticError; });
            throw new AssertionError("Both failures were swallowed");
        } catch(IllegalStateException error) {
            require(error==automaticError && error.getSuppressed().length==1
                && error.getSuppressed()[0]==selectedError,"Fallback lost the original failure");
        }
        System.out.println("DECODER_FALLBACK_OK: software first, allocation/start failure, automatic, error retention");
    }
}
