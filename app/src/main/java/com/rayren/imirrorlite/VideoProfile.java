package com.rayren.imirrorlite;

/** Immutable requested output; source dimensions and aspect ratio remain authoritative. */
final class VideoProfile {
    static final int[] HEIGHTS={0,540,720,1080,1440,2160,2880};
    static final int[] FRAME_RATES={24,30,60,120};
    final int height, fps;
    final String decoder;

    VideoProfile(int height,int fps,String decoder,boolean wireless) {
        if(!contains(HEIGHTS,height) || (wireless && height==0) || !contains(FRAME_RATES,fps))
            throw new IllegalArgumentException("Unsupported video profile");
        this.height=height; this.fps=fps;
        this.decoder=decoder==null || decoder.isEmpty()?"auto":decoder;
    }
    static boolean contains(int[] values,int value) {
        for(int candidate:values) if(candidate==value) return true;
        return false;
    }
    int canvasWidth() { return height*16/9; }
    int[] renderSize(int sourceWidth,int sourceHeight) {
        if(sourceWidth<1 || sourceHeight<1) throw new IllegalArgumentException("Invalid source size");
        if(height==0) return new int[]{sourceWidth,sourceHeight};
        int maxWidth=sourceWidth>=sourceHeight?canvasWidth():height;
        int maxHeight=sourceWidth>=sourceHeight?height:canvasWidth();
        double scale=Math.min(1,Math.min((double)maxWidth/sourceWidth,(double)maxHeight/sourceHeight));
        return new int[]{Math.max(1,(int)Math.round(sourceWidth*scale)),Math.max(1,(int)Math.round(sourceHeight*scale))};
    }
    String resolutionLabel() { return height==0?"原始分辨率":height+"p（"+canvasWidth()+" × "+height+"）"; }
    String summary() { return resolutionLabel()+" · "+fps+" fps"; }
}
