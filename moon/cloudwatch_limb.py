#!/usr/bin/env python3
"""Watch the Moon's limb for clear sky, then measure how sharp the image really is.

    ASIAIR_HOST=192.168.1.36 python3 -u moon/cloudwatch_limb.py --every 60 --clear-glow 0.05 --max-minutes 90

Thin cloud shows up at this scale as a halo: on 2026-09-27 the sky 60" beside the
limb sat at 20-30% of the disc under cloud, against ~0 on a clear sky. Each cycle
takes a focus-page bin-1 frame (limb in the crop, disc toward the top), reads the
glow at 15/30/60/90" beyond the limb (distance transform, so the limb may run any way
across the crop) as a fraction of the disc level 20-60" inside it, keeps the limb in the field with 0.5' RA gotos (lunar tracking leaves
~0.2-0.65'/min of drift), and prints one line. Once the glow at 60" stays under
--clear-glow for --clear-cycles cycles in a row it runs an interleaved limb-width
check at --focus-pos (2 rounds x 2 frames) and exits 0 with the verdict; exits 3
after --max-minutes without clear sky. Frames go to the dashboard's preview pane.
Touches the focuser (the check) and the mount (re-centring gotos) -- nothing else.
"""
import argparse, json, math, os, sys, time
import numpy as np
from scipy import ndimage
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import daypipes
from daypipes import host, Pipes, log, save_png, focuser_pos, move_to
from mount import Mount
try:
    from recorder import rec
except Exception:
    rec = None

ap = argparse.ArgumentParser()
ap.add_argument("--every", type=float, default=60.0, help="seconds between cycles")
ap.add_argument("--clear-glow", type=float, default=0.05, help="glow 60\" beyond the limb, as a fraction of the disc, that counts as clear")
ap.add_argument("--clear-cycles", type=int, default=2)
ap.add_argument("--max-minutes", type=float, default=90.0)
ap.add_argument("--exp-ms", type=float, default=10.0); ap.add_argument("--gain", type=int, default=220)
ap.add_argument("--scale", type=float, default=0.1866, help="bin-1 arcsec/px (driftscale, 2026-09-27)")
ap.add_argument("--focus-pos", default="44510,44760,45010", help="EAF positions for the sharpness check; the middle one is the current focus")
ap.add_argument("--recentre-step", type=float, default=0.5, help="arcmin per re-centring goto")
ap.add_argument("--limits", default="30000,70000")
ap.add_argument("--stop-file", default="/tmp/cloudwatch_limb.stop")
ap.add_argument("--outdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "telemetry", time.strftime("%Y-%m-%d"), "cloudwatch_limb"))
a = ap.parse_args()
daypipes.LO, daypipes.HI = [int(v) for v in a.limits.split(",")]
os.makedirs(a.outdir, exist_ok=True)
BIAS = 1500.0                       # focus page bin 1, 16-bit
SP = 2 * a.scale                    # arcsec per green superpixel


def green(img):
    img = img.astype(np.float32) - BIAS
    return 0.5 * (img[0::2, 1::2] + img[1::2, 0::2])


def limb_profile(g):
    """Glow beside the limb, independent of the limb's direction in the crop: every pixel's
    distance from the half-level contour (distance transform of the disc mask), glow(d) =
    median outside at distance d / median inside at 20-60". status: ok | no_sky (nothing
    dark: all disc, or a mare edge -- step west) | no_disc (step east)."""
    gs = ndimage.gaussian_filter(g, 2.0)
    top = float(np.percentile(gs, 97))
    if top < 3000:
        return dict(status="no_disc")
    mask = gs > 0.5 * top
    frac = float(mask.mean())
    if frac < 0.05:
        return dict(status="no_disc", disc_frac=frac)
    din = ndimage.distance_transform_edt(mask) * SP            # arcsec inside the limb
    dout = ndimage.distance_transform_edt(~mask) * SP          # arcsec outside it
    ring_in = gs[(din >= 20) & (din < 60)]
    disc = float(np.median(ring_in)) if ring_in.size > 200 else top
    if float(np.percentile(gs, 0.5)) > 0.30 * disc:            # mare stays >= ~40% of highland; cloud-lit sky falls below
        return dict(status="no_sky", disc_frac=frac)
    glow = {}
    for d in (15, 30, 60, 90):
        sel = (dout >= d - 4) & (dout < d + 4)
        if sel.sum() > 200:
            glow[d] = float(np.median(gs[sel])) / disc
    return dict(status="ok", disc=disc, glow=glow, disc_frac=frac)


def limb_width(g):
    """limbfocus.py's metric: contrast / |grad| at the first half-level crossing from the sky side."""
    gs = ndimage.gaussian_filter(g, 1.0)
    sky = float(np.percentile(gs, 2)); plat = float(np.percentile(gs, 97)); C = plat - sky
    if C < 2000 or sky > 0.10 * plat:
        return None
    thr = sky + 0.5 * C; skylim = sky + 0.1 * C
    gy, gx = np.gradient(gs); gm = np.hypot(gx, gy)
    ws = []
    for L, GM in ((gs, gm), (gs.T, gm.T)):
        for i, prof in enumerate(L):
            for rev in (False, True):
                pr = prof[::-1] if rev else prof
                if pr[0] > skylim or pr[1] > skylim:
                    continue
                k = int(np.argmax(pr > thr))
                if k == 0 or pr[k] <= thr or k > len(pr) - 3:
                    continue
                j = len(pr) - 1 - k if rev else k
                if GM[i, j] > 0:
                    ws.append(C / GM[i, j])
    return float(np.median(ws)) * SP if len(ws) >= 20 else None


p = Pipes(); m = Mount(host())

def keepalive():
    """4400 and 4700 both drop a socket idle for ~15 s; ping both, reconnect on failure."""
    global p, m
    try: m.state()
    except Exception as e:
        log("  mount socket lost (%s) -- reconnecting" % e)
        try: m.close()
        except Exception: pass
        m = Mount(host())
    try: p.c("get_focuser_position")
    except Exception as e:
        log("  camera socket lost (%s) -- reconnecting" % e)
        try: p.close()
        except Exception: pass
        p = Pipes(); p.setup("focus", a.exp_ms / 1000.0, a.gain, 1); p.grab()

t0 = time.time(); clear_run = 0; cycle = 0; result = None
try:
    s = m.state()
    log("start: register RA %.4f Dec %+.4f alt %.1f track %s mode %s; EAF %d; clear = glow@60\" < %.0f%% for %d cycles; max %.0f min" %
        (s["RA"], s["Dec"], s["Alt"], s["is_enable_track"], m._r("scope_get_track_mode").get("index"), focuser_pos(p), 100 * a.clear_glow, a.clear_cycles, a.max_minutes))
    if rec: rec.note("cloudwatch_limb started", device="main camera")
    p.setup("focus", a.exp_ms / 1000.0, a.gain, 1); p.grab()
    while True:
        cycle += 1; tc = time.time()
        if os.path.exists(a.stop_file):
            log("stop file %s -- ending" % a.stop_file); os.remove(a.stop_file); result = "stopped"; break
        if (tc - t0) / 60 > a.max_minutes:
            log("no clear sky in %.0f min -- giving up" % a.max_minutes); result = "timeout"; break
        s = m.state()
        if not s["is_enable_track"]:
            log("  tracking was OFF -- re-enabling (Lunar)"); m._r("scope_set_track_state", [True]); m._r("scope_set_track_mode", ["Lunar"])
        img = p.grab()[0]; g = green(img)
        lp = limb_profile(g)
        fn = save_png(img, os.path.join(a.outdir, "%s_cycle%03d.png" % (time.strftime("%H%M%S"), cycle)), shrink=2)
        if rec:
            try: rec.artifact("preview", fn, device="main camera", meta={"tool": "cloudwatch_limb", "cycle": cycle})
            except Exception: pass
        if lp["status"] != "ok":
            sgn = -1 if lp["status"] == "no_sky" else +1
            log("[%3d %5.1f min] %s -- stepping %s %.1f'" % (cycle, (tc - t0) / 60, {"no_sky": "no dark sky in the crop (all disc)", "no_disc": "no disc in the crop",
                "limb_at_top": "limb at the top edge"}[lp["status"]], "west" if sgn < 0 else "east", a.recentre_step))
            m.goto(s["RA"] + sgn * a.recentre_step / 60 / 15 / math.cos(math.radians(s["Dec"])), s["Dec"], timeout=60); clear_run = 0
        else:
            gl = lp["glow"]; g60 = gl.get(60, float("nan"))
            clear = g60 < a.clear_glow                           # NaN (no sky 60" out) is not clear
            clear_run = clear_run + 1 if clear else (clear_run if math.isnan(g60) else 0)
            log("[%3d %5.1f min] alt %.1f  disc %6.0f  glow beside limb: 15\" %4.1f%%  30\" %4.1f%%  60\" %4.1f%%  90\" %4.1f%%  %s  (disc %.0f%% of crop)" %
                (cycle, (tc - t0) / 60, s["Alt"], lp["disc"], 100 * gl.get(15, float("nan")), 100 * gl.get(30, float("nan")), 100 * g60, 100 * gl.get(90, float("nan")),
                 "CLEAR %d/%d" % (clear_run, a.clear_cycles) if clear else ("no room for 60\"" if math.isnan(g60) else "cloud"), 100 * lp["disc_frac"]))
            fr = lp["disc_frac"]
            if fr < 0.2 or fr > 0.5 or math.isnan(g60):          # need >=90" of sky beside the limb; pointing east brings more disc in
                sgn = +1 if fr < 0.2 else -1
                m.goto(s["RA"] + sgn * a.recentre_step / 60 / 15 / math.cos(math.radians(s["Dec"])), s["Dec"], timeout=60)
                log("  re-centred %s %.1f' (disc filled %.0f%% of the crop)" % ("east" if sgn > 0 else "west", a.recentre_step, 100 * fr))
            if clear_run >= a.clear_cycles:
                log("CLEAR for %d cycles -- sharpness check at EAF %s" % (clear_run, a.focus_pos))
                if rec: rec.note("clear sky -- limb sharpness check", device="focuser")
                pos = [int(v) for v in a.focus_pos.split(",")]
                seq = pos + pos[::-1]
                res = {q: [] for q in pos}
                for q in seq:
                    move_to(p, q); p.grab()
                    for _ in range(2):
                        gg = green(p.grab()[0]); w = limb_width(gg); lp2 = limb_profile(gg)
                        if w: res[q].append(w)
                        log("  EAF %d: limb width %s  glow@60\" %s" % (q, "%.1f\"" % w if w else "n/a", "%.1f%%" % (100 * lp2["glow"].get(60, float("nan"))) if lp2.get("status") == "ok" else lp2.get("status")))
                mid = pos[len(pos) // 2]
                summ = {q: (float(np.median(v)) if v else None) for q, v in res.items()}
                best = min((q for q in summ if summ[q]), key=lambda q: summ[q], default=None)
                move_to(p, best if best else mid)
                log("RESULT sharpness: " + "  ".join("EAF %d %.1f\"" % (q, summ[q]) for q in pos if summ[q]) +
                    "  -> best %s, EAF left at %d  (pre-flip clear-sky reference: 5\" at 45000)" % (best, focuser_pos(p)))
                result = dict(widths=summ, best=best, cycle=cycle)
                json.dump(result, open(os.path.join(a.outdir, time.strftime("%H%M%S") + "_result.json"), "w"), indent=1)
                break
        # wait, pinging both sockets every 5 s, heartbeat every 15 s
        last_hb = time.time()
        while time.time() - tc < a.every:
            time.sleep(min(5.0, max(a.every - (time.time() - tc), 0.0)))
            keepalive()
            if time.time() - last_hb >= 15 and time.time() - tc < a.every - 1:
                log("  ... next check in %.0f s" % (a.every - (time.time() - tc))); last_hb = time.time()
finally:
    try: p.close()
    except Exception: pass
    m.close()
sys.exit(0 if isinstance(result, dict) else (3 if result == "timeout" else 1))
