// SPDX-License-Identifier: Apache-2.0
#include "../NvtTouchCore.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct nvt_touch_core core;
static uint32_t rng=12345;
static int noise(int amp){rng=rng*1664525u+1013904223u;return (int)((rng>>16)%(unsigned)(2*amp+1))-amp;}
static void base_frame(int16_t *m,int amp){for(int i=0;i<NVT_NODES;++i)m[i]=(int16_t)(5000+(i%7)*3+noise(amp));}
static void blob(int16_t *m,double row,double col,double amplitude,double sigma){
    for(int r=0;r<NVT_ROWS;++r)for(int c=0;c<NVT_COLUMNS;++c){
        double d2=(r-row)*(r-row)+(c-col)*(c-col);
        m[r*NVT_COLUMNS+c]=(int16_t)(m[r*NVT_COLUMNS+c]+(int)lround(amplitude*exp(-d2/(2*sigma*sigma))));
    }
}
static void settle(void){
    int16_t frame[NVT_NODES];nvt_touch_reset(&core);
    for(unsigned i=0;i<NVT_STARTUP_FRAMES;++i){
        base_frame(frame,2);struct nvt_contact c[4];
        int n=nvt_touch_process(&core,frame,4,c,4);assert(n==-1);
    }
    assert(core.have_reference);
}
static int feed(int16_t *m,struct nvt_contact *out,int max){return nvt_touch_process(&core,m,4,out,max);}
// A lift: the finger is held for NVT_HOLD_FRAMES empty frames, then gone.
static void lift(void){
    int16_t m[NVT_NODES];struct nvt_contact out[12];
    for(int k=0;k<NVT_HOLD_FRAMES;++k){base_frame(m,2);(void)feed(m,out,12);}
    base_frame(m,2);assert(feed(m,out,12)==0);
}

int main(void){
    // Startup baseline: -1 for 72 frames, then a quiet frame yields no contacts.
    settle();
    int16_t f[NVT_NODES];struct nvt_contact c[12];
    base_frame(f,2);assert(feed(f,c,12)==0);
    // Reference is the per-node median of the startup frames (within the injected noise).
    for(int i=0;i<NVT_NODES;++i)assert(abs(core.reference[i]-(5000+(i%7)*3))<=2);
    // A frame with the wrong type is ignored.
    assert(nvt_touch_process(&core,f,3,c,12)==0);
    // One finger: reported from the second frame on, near the expected super-resolution centroid, same id.
    int id=0;
    for(int frame=0;frame<4;++frame){
        base_frame(f,2);blob(f,20.0,30.0,900,1.3);int n=feed(f,c,12);
        if(frame==0)assert(n==0);
        else{assert(n==1);assert(abs(c[0].x-nvt_touch_column_center(30)*NVT_SUPER_RESOLUTION)<120);
            assert(abs(c[0].y-nvt_touch_row_center(20)*NVT_SUPER_RESOLUTION)<120);
            if(!id)id=c[0].tracking_id;assert(c[0].tracking_id==id&&c[0].slot>=0);}
    }
    // Sub-pixel position: a blob between columns 30 and 31 lands between their centres once the smoothing has settled.
    int n=0;for(int frame=0;frame<40;++frame){base_frame(f,2);blob(f,20.0,30.5,900,1.3);n=feed(f,c,12);}assert(n==1);
    int lo=nvt_touch_column_center(30)*NVT_SUPER_RESOLUTION,hi=nvt_touch_column_center(31)*NVT_SUPER_RESOLUTION;
    assert(c[0].x>lo+100&&c[0].x<hi-100&&c[0].tracking_id==id);
    // Lift, then a new touch is a new contact (not reported on its first frame).
    lift();
    base_frame(f,2);blob(f,10.0,10.0,900,1.3);assert(feed(f,c,12)==0);
    base_frame(f,2);blob(f,10.0,10.0,900,1.3);assert(feed(f,c,12)==1&&c[0].tracking_id!=id);
    // Two fingers far apart: two contacts, distinct ids and slots.
    settle();
    int ids[2]={0,0};
    for(int frame=0;frame<3;++frame){
        base_frame(f,2);blob(f,8.0,12.0,800,1.3);blob(f,30.0,48.0,700,1.3);n=feed(f,c,12);
        if(frame>=1){assert(n==2&&c[0].tracking_id!=c[1].tracking_id&&c[0].slot!=c[1].slot);
            if(!ids[0]){ids[0]=c[0].tracking_id;ids[1]=c[1].tracking_id;}
            assert((c[0].tracking_id==ids[0]&&c[1].tracking_id==ids[1])||(c[0].tracking_id==ids[1]&&c[1].tracking_id==ids[0]));}
    }
    // Faint blobs (below the noise/peak thresholds) never start a contact.
    settle();
    for(int frame=0;frame<4;++frame){base_frame(f,2);blob(f,20,30,80,1.3);assert(feed(f,c,12)==0);}
    // A blob whose peak sits between the up and down thresholds does not start a contact...
    for(int frame=0;frame<4;++frame){base_frame(f,2);blob(f,20,30,260,1.3);assert(feed(f,c,12)==0);}
    // ...but keeps an established one alive.
    for(int frame=0;frame<3;++frame){base_frame(f,2);blob(f,20,30,900,1.3);feed(f,c,12);}
    base_frame(f,2);blob(f,20,30,260,1.3);assert(feed(f,c,12)==1);
    // Palm-sized blobs are not reported.
    settle();
    for(int frame=0;frame<4;++frame){base_frame(f,2);blob(f,20,30,900,6.0);assert(feed(f,c,12)==0);}
    // The output limit is honoured.
    settle();
    for(int frame=0;frame<3;++frame){base_frame(f,2);blob(f,5,8,800,1.2);blob(f,5,30,800,1.2);blob(f,5,52,800,1.2);blob(f,30,8,800,1.2);
        n=feed(f,c,2);if(frame>=1)assert(n==2);}
    // Edge mapping formula and monotonicity.
    assert(nvt_touch_map_edge_axis(300,NVT_LONG_AXIS_MAX,0)==600-483);
    assert(nvt_touch_map_edge_axis(300,NVT_SHORT_AXIS_MAX,1)==600-483-1);
    assert(nvt_touch_map_edge_axis(10000,NVT_LONG_AXIS_MAX,0)==10000);
    int previous=-1;for(int v=0;v<NVT_LONG_AXIS_MAX;v+=7){int m=nvt_touch_map_edge_axis(v,NVT_LONG_AXIS_MAX,0);assert(m>=previous-1);previous=m;}
    assert(nvt_touch_map_edge_axis(NVT_LONG_AXIS_MAX-1,NVT_LONG_AXIS_MAX,0)>NVT_LONG_AXIS_MAX-600);
    // A baseline frame of type 2 sets the reference immediately.
    nvt_touch_reset(&core);base_frame(f,0);assert(nvt_touch_process(&core,f,2,c,12)==-1&&core.have_reference);
    // Identifiers are reused once their finger is up: 100 separate strokes never go past the panel's slot count (int-267: an ever
    // growing identifier reached backboardd's path limit after about 33 strokes and every later touch was dropped).
    settle();
    int top=0;
    for(int stroke=0;stroke<100;++stroke){
        for(int frame=0;frame<4;++frame){
            base_frame(f,2);blob(f,10.0+(stroke%5)*5,10.0+(stroke%7)*5,900,1.3);n=feed(f,c,12);
            for(int k=0;k<n;++k)if(c[k].tracking_id>top)top=c[k].tracking_id;
        }
        lift();
    }
    assert(top>=1&&top<=NVT_SLOTS);
    // A resting finger whose centroid wanders by up to 0.15 node (8 px, the noise int-271 measured) reports steps of two pixels at most.
    settle();
    int prev_x=-1,prev_y=-1;
    for(int frame=0;frame<60;++frame){
        base_frame(f,2);blob(f,20.0+0.15*((frame%3)-1),30.0+0.1*(((frame*7)%5)-2),900,1.3);n=feed(f,c,12);
        if(frame<2)continue;
        assert(n==1);
        if(frame>=20)assert(abs(c[0].x-prev_x)<=20&&abs(c[0].y-prev_y)<=20);
        prev_x=c[0].x;prev_y=c[0].y;
    }
    assert(core.jitter_frames>0&&core.smoothed_sum<core.jitter_sum/4);
    // A slow drag (1.25 px a frame, 150 px/s) moves the report every frame and never in jumps (int-273: a rest lock held it, then let
    // it go 20 px at once); it ends within a few pixels of the finger.
    {
        int moved=0;
        for(int frame=0;frame<120;++frame){
            base_frame(f,2);blob(f,20.0,30.0+frame*0.025,900,1.3);n=feed(f,c,12);assert(n==1);
            const int step=c[0].x-prev_x;
            assert(step>=-5&&step<=40);
            if(frame>=40&&step>0)++moved;
            prev_x=c[0].x;
        }
        assert(moved>=70);
        const int truth=(int)((nvt_touch_column_center(32)+0.975*(nvt_touch_column_center(33)-nvt_touch_column_center(32)))*NVT_SUPER_RESOLUTION);
        assert(abs(c[0].x-truth)<80);
    }
    // A fast swipe (half a node a frame, 3000 px/s) is followed within about a frame.
    for(int frame=0;frame<16;++frame){base_frame(f,2);blob(f,20.0,35.0+frame*0.5,900,1.3);n=feed(f,c,12);assert(n==1);}
    {
        const int truth=(nvt_touch_column_center(42)+nvt_touch_column_center(43))*NVT_SUPER_RESOLUTION/2;
        assert(c[0].x<truth&&truth-c[0].x<400);
    }
    {   // it stops there and settles on the finger; lifting off, the blob fades below the down threshold and slides, and the reported
        // position does not follow
        for(int frame=0;frame<60;++frame){base_frame(f,2);blob(f,20.0,42.5,900,1.3);n=feed(f,c,12);assert(n==1);}
        const int still_x=c[0].x;
        assert(abs(still_x-(nvt_touch_column_center(42)+nvt_touch_column_center(43))*NVT_SUPER_RESOLUTION/2)<60);
        for(int frame=0;frame<3;++frame){base_frame(f,2);blob(f,20.0+frame*0.4,42.5+frame*0.6,250-frame*10,1.3);n=feed(f,c,12);
            assert(n==1&&c[0].x==still_x);}
    }
    // One finger with two maxima two nodes apart is one contact between them; fingers four nodes apart stay two.
    settle();
    for(int frame=0;frame<3;++frame){base_frame(f,2);blob(f,20.0,30.0,900,0.8);blob(f,20.0,32.0,800,0.8);n=feed(f,c,12);}
    {
        const int left=nvt_touch_column_center(30)*NVT_SUPER_RESOLUTION,right=nvt_touch_column_center(32)*NVT_SUPER_RESOLUTION;
        assert(n==1&&core.merges>0&&c[0].x>left&&c[0].x<right);
    }
    settle();
    for(int frame=0;frame<3;++frame){base_frame(f,2);blob(f,20.0,30.0,900,0.8);blob(f,20.0,34.0,800,0.8);n=feed(f,c,12);}
    assert(n==2&&core.merges==0);
    // A finger missing for up to NVT_HOLD_FRAMES frames is held where it was, with its id, and continues when it reappears; one missing
    // longer is lifted, and the reason (no blob at all) is counted.
    settle();
    for(int frame=0;frame<3;++frame){base_frame(f,2);blob(f,20.0,30.0,900,1.3);n=feed(f,c,12);}
    assert(n==1);
    {
        const struct nvt_contact before=c[0];
        for(int k=0;k<NVT_HOLD_FRAMES;++k){
            base_frame(f,2);n=feed(f,c,12);
            assert(n==1&&c[0].tracking_id==before.tracking_id&&c[0].x==before.x&&c[0].y==before.y);
        }
        base_frame(f,2);blob(f,20.0,30.0,900,1.3);n=feed(f,c,12);
        assert(n==1&&c[0].tracking_id==before.tracking_id&&core.holds_bridged==1);
        lift();
        assert(core.ends[NVT_MISS_NONE]==1);
    }
    // A finger whose blob turns palm-sized is held, then lifted with the palm reason.
    for(int frame=0;frame<3;++frame){base_frame(f,2);blob(f,20.0,30.0,900,1.3);feed(f,c,12);}
    for(int k=0;k<=NVT_HOLD_FRAMES;++k){base_frame(f,2);blob(f,20.0,30.0,900,6.0);n=feed(f,c,12);}
    assert(n==0&&core.ends[NVT_MISS_PALM]==1);
    // Thirty faint maxima above the noise floor (more than the peak table holds) in the rows before two fingers: both fingers are found.
    settle();
    for(int frame=0;frame<3;++frame){
        base_frame(f,2);
        for(int k=0;k<30;++k)blob(f,2.0+(k/10)*4,3.0+(k%10)*5,160,0.5);
        blob(f,30.0,15.0,900,1.3);blob(f,34.0,45.0,900,1.3);n=feed(f,c,12);
    }
    assert(n==2&&core.peak_overflows>0&&core.peaks_max>=24);
    // A panel whose raw values drift up by 400 over a few thousand frames keeps no maxima: the reference follows (int-274), and a
    // finger is still found afterwards.
    settle();
    for(int frame=0;frame<4000;++frame){
        base_frame(f,2);for(int i=0;i<NVT_NODES;++i)f[i]=(int16_t)(f[i]+frame/10+((i*13)%17==0 ? frame/20 : 0));
        n=feed(f,c,12);assert(n==0);
    }
    {
        const unsigned long before=core.peaks_max;
        base_frame(f,2);for(int i=0;i<NVT_NODES;++i)f[i]=(int16_t)(f[i]+400+((i*13)%17==0 ? 200 : 0));n=feed(f,c,12);
        assert(n==0&&core.reference_frames>3000);(void)before;
        core.peaks_max=0;
        for(int frame=0;frame<3;++frame){base_frame(f,2);for(int i=0;i<NVT_NODES;++i)f[i]=(int16_t)(f[i]+400+((i*13)%17==0 ? 200 : 0));
            blob(f,20.0,30.0,900,1.3);n=feed(f,c,12);}
        assert(n==1&&core.peaks_max<=2);
    }
    // A swipe up from the bottom edge that speeds up to five nodes a frame (about 300 mm/s more than the tracking radius allows from
    // the last position) keeps its finger: tracking looks where the finger is heading (int-275: 13 of 125 strokes ended "far").
    settle();
    {
        double row=38.0;int id=-1;
        for(int frame=0;frame<9;++frame){
            base_frame(f,2);blob(f,row,30.0,900,1.3);n=feed(f,c,12);
            if(frame>=1){assert(n==1);if(id<0)id=c[0].tracking_id;assert(c[0].tracking_id==id);}
            row-=frame<5 ? frame+1.0 : 5.0;
            if(row<2.0)break;
        }
        assert(core.ends[NVT_MISS_FAR]==0);
    }
    lift();
    // Two fingers four nodes apart sliding sideways by two nodes a frame keep their identifiers: the closest pair is matched first, so
    // the leading finger's slot does not take the trailing finger's blob.
    settle();
    {
        int left_id=-1,right_id=-1;
        for(int frame=0;frame<12;++frame){
            base_frame(f,2);blob(f,20.0,10.0+frame*2.0,900,1.3);blob(f,20.0,14.0+frame*2.0,900,1.3);n=feed(f,c,12);
            if(frame<1)continue;
            assert(n==2);
            const int l=c[0].x<c[1].x ? 0 : 1;
            if(left_id<0){left_id=c[l].tracking_id;right_id=c[1-l].tracking_id;}
            assert(c[l].tracking_id==left_id&&c[1-l].tracking_id==right_id);
        }
        assert(core.ends[NVT_MISS_TAKEN]==0&&core.ends[NVT_MISS_FAR]==0);
    }
    puts("PASS: NvtTouchCore baseline median, thresholds, centroid, tracking ids, palm/limit/edge rules, adaptive smoothing, lift freeze, hold, split peaks, peak table overflow, reference drift, fast edge swipe, close fingers; synthetic frames, no hardware");
    return 0;
}
