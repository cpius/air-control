#!/usr/bin/env python3
"""A few well-exposed frames of the Moon with the state of the rig written next to
them: mount register, track mode, EAF position/temperature. Auto-exposes on the
99.5th percentile, saves every frame as 16-bit .npy + PNG, and prints per-frame
levels, the lunar-surface / sky fractions and a texture metric.

    ASIAIR_HOST=192.168.1.36 python3 -u moon/moonlook.py --page preview --bin 2 --exp-ms 10 --gain 100 --frames 2 --tag moon
    ASIAIR_HOST=192.168.1.36 python3 -u moon/moonlook.py --page focus --bin 1 --exp-ms 10 --gain 100 --frames 5 --every 0 --tag adc
    ASIAIR_HOST=192.168.1.36 python3 -u moon/moonlook.py --page preview --bin 2 --frames 10 --every 10 --tag drift   # timed series

--every S spaces the frames S seconds apart (wall clock), for drift series.
"""
import argparse, json, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from daypipes import host, Pipes, log, save_png, focuser_pos, metrics
from mount import Mount

ap = argparse.ArgumentParser()
ap.add_argument("--page", default="preview", choices=["preview", "focus"])
ap.add_argument("--bin", type=int, default=2)
ap.add_argument("--exp-ms", type=float, default=10.0); ap.add_argument("--gain", type=int, default=100)
ap.add_argument("--target-lo", type=float, default=22000, help="p99.5 lower bound (16-bit ADU)")
ap.add_argument("--target-hi", type=float, default=50000, help="p99.5 upper bound")
ap.add_argument("--exp-min-ms", type=float, default=0.1); ap.add_argument("--exp-max-ms", type=float, default=500)
ap.add_argument("--no-auto", action="store_true")
ap.add_argument("--frames", type=int, default=2); ap.add_argument("--every", type=float, default=0.0)
ap.add_argument("--tag", default="moon")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "moonlook"))
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
BIAS = 3925.0 if a.bin == 2 else None       # bin-2 preview offset measured 2026-09-22; bin 1 measured from the frame's floor

def rig_state():
    st = {}
    try:
        m = Mount(host()); s = m.state(); m.close()
        st.update(RA=s["RA"], Dec=s["Dec"], Alt=s["Alt"], Az=s["Az"], track=s["is_enable_track"], pier=s.get("pier_side"),
                  track_mode=s.get("track_mode", s.get("track_mode_index")), rate_idx=s.get("slew_rate_index"), mV=s.get("input_voltage"))
    except Exception as e:
        st["mount_error"] = str(e)
    return st

def describe(img, bias):
    p = np.percentile(img, [2, 50, 90, 99.5, 99.99])
    sat = float((img >= 65000).mean())
    surf = p[2] - bias; sky = p[0] - bias
    frac_surface = float(((img - bias) > 0.5 * max(surf, 1.0)).mean())
    return dict(p2=float(p[0] - bias), p50=float(p[1] - bias), p90=float(p[2] - bias), p995=float(p[3] - bias), p9999=float(p[4] - bias),
                max=float(img.max()), sat_frac=sat, sky_over_bias=float(sky), surface_over_bias=float(surf), surface_frac=frac_surface)

p = Pipes()
try:
    st = rig_state()
    try:
        fi = p.c("get_focuser_info"); st["eaf"] = fi.get("position"); st["eaf_temp"] = fi.get("temperature")
    except Exception as e:
        st["eaf_error"] = str(e)
    log("rig: " + json.dumps(st))
    exp = a.exp_ms / 1000.0
    p.setup(a.page, exp, a.gain, a.bin)
    p.grab(timeout=40)                                     # discard: may predate the setup
    # auto-exposure on p99.5
    if not a.no_auto:
        for it in range(8):
            img, w, h, info = p.grab(timeout=40)
            bias = BIAS if BIAS is not None else float(np.percentile(img, 0.05))
            p995 = float(np.percentile(img, 99.5))
            log("auto %d: %.2f ms g%d -> p99.5 %.0f (max %.0f)" % (it, exp * 1000, a.gain, p995, img.max()))
            if a.target_lo <= p995 <= a.target_hi:
                break
            want = 0.5 * (a.target_lo + a.target_hi)
            scale = (want - bias) / max(p995 - bias, 50.0)
            if p995 >= 65000: scale = min(scale, 0.4)
            new = float(np.clip(exp * scale, a.exp_min_ms / 1000.0, a.exp_max_ms / 1000.0))
            if abs(new - exp) / exp < 0.05:
                break
            exp = new
            p.c("set_control_value", ["Exposure", int(round(exp * 1_000_000))]); p.exp = exp
            p.s.air.drain_events(); time.sleep(0.3)
            p.grab(timeout=40)                             # discard one after an exposure change
    rows = []
    t0 = time.time()
    for i in range(a.frames):
        if a.every > 0 and i > 0:
            wait = t0 + i * a.every - time.time()
            if wait > 0:
                time.sleep(wait)
        tw = time.time()
        img, w, h, info = p.grab(timeout=40)
        bias = BIAS if BIAS is not None else float(np.percentile(img, 0.05))
        d = describe(img, bias)
        try:
            m = metrics(img); d.update(tenengrad=m["tenengrad"], texture=m["texture"], ratio=m["ratio"])
        except Exception as e:
            d.update(tenengrad=float("nan"), texture=float("nan"), ratio=float("nan"))
        stamp = time.strftime("%H%M%S", time.localtime(tw))
        base = os.path.join(a.outdir, "%s_%s_%s_bin%d_%gms_g%d" % (stamp, a.tag, a.page, a.bin, round(exp * 1000, 2), a.gain))
        np.save(base + ".npy", img.astype(np.uint16))
        png = save_png(img, base + ".png", shrink=2 if a.bin == 1 and a.page == "preview" else 1)
        row = dict(i=i, t_wall=tw, t_rel=tw - t0, w=w, h=h, exp_ms=exp * 1000, gain=a.gain, fresh=info.get("fresh"), stale=info.get("stale_reads"),
                   dt=info.get("dt"), npy=base + ".npy", **d)
        rows.append(row)
        log("frame %d t=%5.1fs %dx%d %s bin%d %.2fms g%d fresh=%s (%.1fs): sky %+.0f surf %+.0f p99.5 %.0f max %.0f sat %.4f surface-frac %.3f | ten %.3f tex %.2f | %s" % (
            i, row["t_rel"], w, h, a.page, a.bin, exp * 1000, a.gain, info.get("fresh"), info.get("dt", 0),
            d["sky_over_bias"], d["surface_over_bias"], d["p995"], d["max"], d["sat_frac"], d["surface_frac"], d["tenengrad"], d["texture"], os.path.basename(png)))
    json.dump(dict(rig=st, args=vars(a), rows=rows), open(os.path.join(a.outdir, "%s_%s.json" % (time.strftime("%H%M%S"), a.tag)), "w"), indent=1, default=str)
    log("saved %d frames to %s" % (len(rows), os.path.relpath(a.outdir)))
finally:
    try: p.close()
    except Exception: pass
