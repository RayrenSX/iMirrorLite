The Android instrumentation assets `decoder-csd.bin` and `decoder-frame.bin`
contain a locally generated 640x960 solid red H.264 baseline image. They contain
no captured device content. Generate the Annex B source with:

    ffmpeg -f lavfi -i color=c=red:s=640x960:r=1 -frames:v 1 -c:v libx264 -profile:v baseline -preset ultrafast -tune zerolatency -pix_fmt yuv420p -f h264 decoder-red.h264

Keep NAL types 7/8 in the configuration and type 5 in the frame, each prefixed
with a four-byte Annex B start code. The test uses the production MediaCodec
decoder and a real SurfaceView, and uses PixelCopy to check the rendered pixel is red.
The regular frame size also runs on hardware decoders which reject 64x96 video.

`quicktime-decoder.bin` wraps the same configuration and red IDR in a synthetic
QuickTime sample buffer (640x960, avc1, avcC and sdat). Android instrumentation
feeds it in 7-byte fragments and checks the exact Annex B frame, configuration,
dimensions and monotonic timestamp. `quicktime-cvrp.bin` is the existing captured
handshake fixture from `cvrp-request`; its license is in `tests/fixtures/LICENSE`.
