#!/usr/bin/env python3
"""Hold a planet centred with a closed loop, then sweep focus on it.

Written 2026-09-05 on Saturn. It works -- it locked on and centred four times
(546->225 px, 404->259 px) -- but the night was lost to drift during the gaps
between runs, not to the control law. Read the rules below before changing it;
each one cost an hour.

THE RULES, all learned the hard way:

1. NEVER act on a frame you have not proved is new. 4800 will re-serve one
   cached image forever. Gate on NEW frames/sec, not frames/sec: a cadence test
   read "8 frames in 12.2 s" while only ONE was new, and the loop happily
   slewed the mount against a frozen picture (~36' of blind motion).

2. Open the 4800 image socket AFTER start_exposure. Opened before, it idles
   through the page/exposure setup and every grab then takes ~70 s. Opened in
   the right order a frame arrives in 0.7 s. This one masqueraded as a wedged
   Air for hours.

3. "No planet" is AMBIGUOUS -- cloud or mispointing. Do not guess: take a 0.5 s
   frame and read the background. Measured 2026-09-05 (bin2, gain 250,
   bias floor 3968): CLEAR ~0-290 ADU/s, CLOUD lit by city glow ~2300 ADU/s.
   That single measurement settles in 3 s what cost most of a night of guessing.

4. Detection floor must sit well clear of the noise. Measured Saturn peaks:
   15824 clear, 5088 through thin cloud, and an EMPTY-frame floor of 700-900.
   At a ~900 threshold the median-of-noise centroid FABRICATES a planet at the
   frame centre in every single frame, with a plausible-looking drift rate.
   2000 is the floor here. Area discriminates a clipped planet from a hot pixel.

5. The two mount axes can map to nearly the SAME image direction on this mount
   (measured J[ra]=[16,34], J[dec]=[5.1,10.7] -- both ~(0.43,0.90)). A plain
   2x2 solve is then near-singular and demands huge opposing RA/Dec pulses:
   the loop oscillated 125->117->162 px. Use the damped least squares below.

6. scope_move is NOT symmetric or trustworthy open-loop: 8 west pulses moved
   the register ~1 degree, 8 IDENTICAL east pulses moved it ~4'. Never search
   blind with it -- correct only with the planet visible in a fresh frame.

7. Re-assert tracking every pass. It drops silently after scope_move, and a
   stale in-flight move makes the mount report tracking off AND refuse
   scope_set_track_state with 207. Clear it with scope_move("none") +
   scope_abort_slew first.

8. Drift with the mount unaligned is ~1.3'/min (~5 deg polar error), which
   empties the 14.4'x8.1' bin2 focus page in about 6 minutes. Any step that
   takes more than ~20 s without correcting loses the planet -- including your
   own edit-and-restart cycle.

    python3 planet_hold.py
"""
import math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session import Session
SCR=os.environ.get('PLANET_HOLD_LOG', os.path.dirname(os.path.abspath(__file__)))
OUT=open(SCR+'/sess.txt','w',buffering=1)
def p(*a): OUT.write(" ".join(str(x) for x in a)+"\n")
HOST=os.environ.get('ASIAIR_HOST','192.168.1.35'); KEY=os.path.join(os.path.dirname(os.path.abspath(__file__)),'embedded_key.pem')
EXP,GAIN=0.02,250
LO,HI=35000,45000
DEAD=22.0
SCALE=0.588
GAIN_P=0.85   # proportional gain: <1 so a mis-estimated J cannot overshoot
POS=[40915,41215,41515,41815,42115]; ROUNDS=4

def planet(im):
    """Threshold set from measured numbers, not guessed:
       clear Saturn peak 15824, through thin cloud 5088,
       EMPTY-SKY floor 688-880 (hot pixels + noise).
    2000 sits an order of magnitude above the floor and still catches a
    3x dimmed planet. Do NOT loosen this: at ~900 the median-of-noise
    centroid fabricates a planet at the frame centre in every frame."""
    bg=int(np.median(im[::7,::7])); pk=int(im.max())
    if pk-bg<2000: return None,"faint (peak %d, floor ~900)"%(pk-bg)
    thr=bg+0.5*(pk-bg); ys,xs=np.nonzero(im>=thr)
    if len(xs)<60: return None,"small (%d px, peak %d)"%(len(xs),pk-bg)
    mx,my=np.median(xs),np.median(ys)
    m=(np.abs(xs-mx)<200)&(np.abs(ys-my)<200); xs,ys=xs[m],ys[m]
    if len(xs)<60: return None,"small after cluster"
    wt=(im[ys,xs]-bg).astype(float)
    return (float((xs*wt).sum()/wt.sum()),float((ys*wt).sum()/wt.sum())),"peak %d area %d"%(pk-bg,len(xs))

def sharp(im,q,half=110):
    h,w=im.shape; cx,cy=int(q[0]),int(q[1])
    x0,x1=max(0,cx-half),min(w,cx+half); y0,y1=max(0,cy-half),min(h,cy+half)
    roi=im[y0:y1,x0:x1].astype(np.float64)
    if roi.size<400: return None
    bg=float(np.median(im[::7,::7])); s=roi-bg; flux=float(s[s>0].sum())
    if flux<=0: return None
    gx=np.diff(roi,axis=1)[:-1,:]; gy=np.diff(roi,axis=0)[:,:-1]
    return float((gx*gx+gy*gy).sum())/(flux*flux)*1e6

s=Session(HOST,KEY,with_mount=True)
J={"ra":np.array([16.0,34.0]),"dec":None}
def grab():
    v,w,h=s.fresh(1)
    a=np.frombuffer(v,dtype=np.uint8)
    im=a.reshape(h,w) if a.size==w*h else a.view(np.uint16).reshape(h,w)
    return im.astype(np.int32),w,h
def track_ok(tag):
    try:
        m=s.mount()
        if not m.state().get("is_enable_track"):
            m._r("scope_move",["none"]); m._r("scope_abort_slew"); time.sleep(0.5)
            m._r("scope_set_track_state",[True]); p("  %s TRACKING WAS OFF -> re-enabled"%tag)
    except Exception as e: p("  %s track check: %s"%(tag,e))
def pulse(d,secs):
    secs=max(0.0,min(secs,2.0))
    if secs<0.12: return 0.0
    m=s.mount()
    try:
        m._r("scope_move",[d]); time.sleep(secs)
    finally:
        m._r("scope_move",["none"])
    return secs
def solve(err, d):
    """Damped least squares (Levenberg). The two mount axes can map to nearly
    the SAME image direction on this mount, which makes a plain 2x2 solve
    near-singular: it then demands huge opposing RA/Dec pulses and the loop
    oscillates instead of converging (observed 125->117->162 px). Damping
    yields the minimum-norm correction instead of exploding, and the gain
    keeps it from overshooting."""
    a=J["ra"]; b=J["dec"]
    if b is None:
        return {"ra": GAIN_P*float(np.dot(err,a)/np.dot(a,a))}
    M=np.array([[a[0],b[0]],[a[1],b[1]]], dtype=float)
    lam=0.05*float(np.trace(M.T@M))+1e-6
    t=np.linalg.solve(M.T@M+lam*np.eye(2), M.T@np.array(err,dtype=float))
    t*=GAIN_P
    cap=0.5 if d<60 else 2.0   # small cap only when ALREADY close
    t=np.clip(t,-cap,cap)
    return {"ra":float(t[0]),"dec":float(t[1])}

def apply(pl):
    done={}
    for ax,v in pl.items():
        d=("east","west") if ax=="ra" else ("north","south")
        t=pulse(d[0] if v>0 else d[1],abs(v))
        if t: done[ax]=math.copysign(t,v)
    return done
try:
    s.c("open_focuser",[0])
    st=s.mount().state()
    p("start: track %s rate %s pier %s focuser %s"%(st.get("is_enable_track"),
      st.get("slew_rate_index"),st.get("pier_side"),s.c("get_focuser_position")))
    track_ok("init")
    s.mount()._r("scope_set_slew_rate",[0])
    s.page("focus",EXP,GAIN,binning=2); time.sleep(1.2)
    p(">>> WAITING for Saturn -- re-centre whenever you like, I lock on automatically <<<")
    target=None; t0=time.time(); got=None; prevq=None; nwait=0
    def skycheck():
        """0.5s exposure: sky background separates CLOUD from BAD POINTING.
        Measured 2026-09-05: clear ~250 ADU/s, cloud lit by city glow ~2300 ADU/s."""
        try:
            s.page("focus",0.5,GAIN,binning=2); time.sleep(1.7)
            v,w,h=s.fresh(1)
            a=np.frombuffer(v,dtype=np.uint8)
            im=(a.reshape(h,w) if a.size==w*h else a.view(np.uint16).reshape(h,w)).astype(np.int32)
            bg=int(np.median(im[::7,::7])); rate=(bg-3968)/0.5
            p("  SKY: bg %d = %.0f ADU/s -> %s"%(bg,rate,
              "CLOUD over the target" if rate>800 else "CLEAR (so it is not in the frame)"))
        except Exception as e:
            p("  skycheck failed %r"%(e,))
        finally:
            try: s.page("focus",EXP,GAIN,binning=2); time.sleep(1.0)
            except Exception: pass
    while time.time()-t0<1800:
        try: im,w,h=grab()
        except Exception as e:
            p("%s grab %r"%(time.strftime("%H:%M:%S"),e)); time.sleep(1.0); continue
        target=(w/2.0,h/2.0); q,info=planet(im)
        if q:
            if prevq is not None and math.hypot(q[0]-prevq[0],q[1]-prevq[1])<80:
                p("%s *** LOCKED *** (%.0f,%.0f) off(%+.0f,%+.0f) %s"%(time.strftime("%H:%M:%S"),
                  q[0],q[1],q[0]-target[0],q[1]-target[1],info)); got=q; break
            p("%s candidate (%.0f,%.0f) %s -- need a consistent second sighting"%(
                time.strftime("%H:%M:%S"),q[0],q[1],info))
            prevq=q; continue
        prevq=None
        nwait+=1
        if nwait%30==0: skycheck()
        elif nwait%5==0: p("%s waiting... %s"%(time.strftime("%H:%M:%S"),info))
    if got is None: p("nothing appeared"); raise SystemExit(1)

    def centre(tag,maxit=3):
        track_ok(tag)
        last=None
        for _ in range(maxit):
            try: im,w,h=grab()
            except Exception as e:
                p("  %s grab %r -- NOT pulsing"%(tag,e)); return None
            q,info=planet(im)
            if q is None: p("  %s planet gone (%s)"%(tag,info)); return None
            err=(target[0]-q[0],target[1]-q[1]); d=math.hypot(*err)
            if d<=DEAD:
                p("  %s off %.0f px (%.0f\") ok  %s"%(tag,d,d*SCALE,info)); return q
            lq=q
            done=apply({"dec":0.6}) if J["dec"] is None else apply(solve(err,d))
            p("  %s off %.0f px (%.0f\") -> %s"%(tag,d,d*SCALE,{k:round(v,2) for k,v in done.items()}))
            try: im2,_,_=grab()
            except Exception: continue
            q2,_=planet(im2)
            if q2 and done:
                mv=np.array([q2[0]-lq[0],q2[1]-lq[1]])
                for ax,v in done.items():
                    if abs(v)>=0.25 and np.linalg.norm(mv)>4:
                        o=mv/v
                        if J[ax] is None:
                            J[ax]=o; p("    learned J[%s]=%s"%(ax,np.round(o,1)))
                        elif np.linalg.norm(o) < 4*np.linalg.norm(J[ax]) and np.linalg.norm(o) > 0.25*np.linalg.norm(J[ax]):
                            J[ax]=0.7*J[ax]+0.3*o; p("    J[%s]=%s"%(ax,np.round(J[ax],1)))
                        else:
                            p("    J[%s] update rejected (|obs| %.0f vs |J| %.0f)"%(ax,np.linalg.norm(o),np.linalg.norm(J[ax])))
        return None
    p("--- centring: waits out cloud, gives up on nothing ---")
    # Cloud blanks the planet for tens of seconds at a time (measured: sky is
    # ~9x brighter than clear right now). A frame with no planet is NOT a
    # failure and must not count against us -- just wait for the next gap.
    ok=False; t0=time.time(); tries=0; clouded=0
    while time.time()-t0 < 900:
        q=centre("c%d"%tries,maxit=1)
        if q is None:
            clouded+=1
            if clouded%15==0: p("  ...waiting out cloud (%d frames, %.0f s)"%(clouded,time.time()-t0))
            continue
        tries+=1
        if math.hypot(target[0]-q[0],target[1]-q[1])<60:
            ok=True; p("CENTRED to <60 px (%d real passes, %d cloud frames)"%(tries,clouded)); break
    if not ok:
        p("gave up centring after 15 min (%d cloud frames)"%clouded); raise SystemExit(1)
    p("--- stability check: 45 s hold before touching focus ---")
    t0=time.time(); seen=0; lost=0
    while time.time()-t0<45:
        q=centre("hold",maxit=1)
        if q is None: lost+=1
        else: seen+=1
    p("stability: %d held, %d cloud-missed"%(seen,lost))
    if seen<2:
        p("never saw it during the hold -- not starting focus"); raise SystemExit(1)
    p("--- focus sweep ---")
    acc={f:[] for f in POS}
    for rnd in range(ROUNDS):
        for fp in (POS if rnd%2==0 else POS[::-1]):
            fp=max(LO,min(HI,fp))
            tc=time.time()
            while time.time()-tc < 120:
                q=centre("r%d"%rnd,maxit=1)
                if q is not None and math.hypot(target[0]-q[0],target[1]-q[1])<60: break
            s.c("move_focuser",[int(fp)]); time.sleep(2.0)
            try: grab()          # frame exposed during the move
            except Exception: pass
            # Cloud keeps blanking the planet for a few seconds at a time. Do
            # NOT abandon a focus position on the first miss -- keep sampling
            # until two good measurements land, or the attempts run out.
            vals=[]; peaks=[]; miss=0
            tstart=time.time()
            while len(vals)<2 and time.time()-tstart < 90:
                try: im,w,h=grab()
                except Exception: continue
                q,info=planet(im)
                if q is None:
                    miss+=1; continue
                v=sharp(im,q)
                if v:
                    vals.append(v)
                    peaks.append(int(im.max()-np.median(im[::7,::7])))
            if vals:
                m=float(np.median(vals)); acc[fp].append(m)
                p("  r%d %6d sharp %.4f  (n=%d, %d cloud misses, peak %s)"%(
                    rnd,fp,m,len(vals),miss,peaks))
            else:
                p("  r%d %6d NO MEASUREMENT after %d cloud misses"%(rnd,fp,miss))
    p("--- focus summary (higher = sharper) ---")
    best,bv=None,-1
    for fp in POS:
        if acc[fp]:
            m=float(np.median(acc[fp])); p("  %6d  %.4f  %s"%(fp,m,["%.4f"%x for x in acc[fp]]))
            if m>bv: best,bv=fp,m
    if best:
        p("BEST %d (%.4f)"%(best,bv)); s.c("move_focuser",[int(best)]); time.sleep(3)
        p("focuser now %s"%s.c("get_focuser_position"))
    p("--- holding ---")
    t0=time.time()
    while time.time()-t0<1200: centre("hold",maxit=2)
    p("DONE")
finally:
    try: s.mount()._r("scope_move",["none"])
    except Exception: pass
    try: s.mount()._r("scope_set_slew_rate",[4])
    except Exception: pass
    try: s.c("stop_exposure")
    except Exception: pass
    s.close()
