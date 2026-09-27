#!/usr/bin/env python3
"""Is a planet visible right now, or is it behind cloud? Check, and re-check
every --every seconds while it is covered.

"No planet in the frame" on its own is AMBIGUOUS -- cloud or mispointing --
and guessing wrong costs a night (2026-09-05). So each check reads the sky
RATE from a 0.5 s + 2 s pair on the preview page, bin 2 gain 250: clear sky
over Copenhagen is 0-290 ADU/s, cloud lit by the city ~2300 ADU/s (see the
sky-brightness-cloud-meter note). A saturated blob of >= 30 superpixels is
the planet; a hot pixel is 1-4.

Pointing: with a badly aligned mount the register can be degrees off at a
target far from the last sync, and a planet's field is too sparse to solve.
`pointing_model.py` predicts the offset; pass it as --pred-dra/--pred-ddec
(arcmin, the amount to ADD to the target when commanding the goto) and the
search grid is walked outward from that prediction, field-sized steps.

Verdicts, one line each, grep-able on "VERDICT":
  VISIBLE    planet blob found -> centred with tonight's Jacobian, register
             synced on the ephemeris position, exit 0
  COVERED    no planet and the sky is bright -> wait and repeat
  UNCERTAIN  no planet and the sky is dark -> pointing recovery first
             (optional solves, then the grid search), and only then wait

    ASIAIR_HOST=192.168.1.35 python3 -u cloudcheck.py --ra 0.8545 --dec 2.575 --name Saturn \
        --pred-dra 27 --pred-ddec 181 --search-ra 42 --search-dec 60 --no-solve --every 300
"""
import argparse, math, os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daypipes import host, Pipes, log, save_png
from findstar import blobs
from mount import Mount
from solving import solve

ap = argparse.ArgumentParser()
ap.add_argument("--ra", type=float, required=True, help="JNow hours (Horizons 'a-app')")
ap.add_argument("--dec", type=float, required=True, help="JNow degrees")
ap.add_argument("--name", default="planet")
ap.add_argument("--every", type=float, default=300, help="seconds between checks while covered")
ap.add_argument("--max-checks", type=int, default=24)
ap.add_argument("--cloud-rate", type=float, default=800, help="ADU/s (bin2 g250) above which the sky is cloud")
ap.add_argument("--jacobian", default="-85.3,-19.7,25.9,-90.4", help="px/arcmin bin2: dx/dRA,dx/dDec,dy/dRA,dy/dDec")
ap.add_argument("--jacobian-pier", default="east", help="pier_side the Jacobian was measured on; it is negated on the other side")
ap.add_argument("--pred-dra", type=float, default=0.0, help="arcmin to ADD to the target RA when commanding (from pointing_model.py)")
ap.add_argument("--pred-ddec", type=float, default=0.0, help="arcmin to ADD to the target Dec when commanding")
ap.add_argument("--search-ra", type=float, default=28.0, help="half-width of the search grid, arcmin")
ap.add_argument("--search-dec", type=float, default=20.0, help="half-height of the search grid, arcmin")
ap.add_argument("--step-ra", type=float, default=14.0, help="grid step in RA, arcmin (frame is 18.8' wide)")
ap.add_argument("--step-dec", type=float, default=9.0, help="grid step in Dec, arcmin (frame is 10.6' tall)")
ap.add_argument("--no-recover", action="store_true", help="never search, just read the sky")
ap.add_argument("--no-solve", action="store_true", help="skip the solve attempts in the recovery, go straight to the search")
ap.add_argument("--no-sync", action="store_true")
ap.add_argument("--solve-cap", type=float, default=20.0)
ap.add_argument("--outdir", default="/Users/madsdorup/ASICAP/telemetry/cloudcheck")
ap.add_argument("--planet-peak", type=int, default=60000, help="min peak (16-bit) of the planet blob; lower it (~8000) for a grossly defocused donut")
ap.add_argument("--planet-area", type=int, default=30, help="min area of the planet blob in superpixels")
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
J0 = np.array([float(v) for v in a.jacobian.split(",")]).reshape(2, 2)
PLANET_PEAK, PLANET_AREA = a.planet_peak, a.planet_area

M = None
def mount():
    global M
    try:
        M.state()
    except Exception:
        M = Mount(host())
    return M

def goto(ra, dec, timeout=90):
    m = mount()
    st = m.state()
    if not st.get("is_enable_track"):
        log("tracking was OFF -> on: %s" % m.set_tracking(True))
    m.goto(ra, dec, wait=True, timeout=timeout)
    return mount().state()

def cmd(dra_m=0.0, ddec_m=0.0):
    """Register coordinates to command: target + model prediction + search offset (arcmin)."""
    dra = (a.pred_dra + dra_m) / (60.0 * 15.0 * math.cos(math.radians(a.dec)))
    return a.ra + dra, a.dec + (a.pred_ddec + ddec_m) / 60.0

def set_exp(p, e):
    p.c("set_control_value", ["Exposure", int(round(e * 1_000_000))]); p.exp = e

def find_planet(img):
    _, _, bl = blobs(img, nsig=8.0, top=10)
    pl = [b for b in bl if b["peak"] >= PLANET_PEAK and b["area_spx"] >= PLANET_AREA]
    return pl[0] if pl else None

def sky(p, tag):
    """0.5 s + 2 s frames at the current pointing: rate, star count, planet blob."""
    set_exp(p, 0.5); img05, w, h, _ = p.grab(timeout=30)
    set_exp(p, 2.0); img2, w, h, _ = p.grab(timeout=40)
    bg05, bg2 = float(np.median(img05)), float(np.median(img2))
    rate = (bg2 - bg05) / 1.5
    _, _, bl2 = blobs(img2, nsig=5.0, top=80)
    stars = [b for b in bl2 if b["area_spx"] >= 3 and b["peak"] < PLANET_PEAK]
    planet = find_planet(img05) or find_planet(img2)
    fn = save_png(img2, "%s/%s_%s.png" % (a.outdir, time.strftime("%H%M%S"), tag), shrink=2)
    log("sky @%s: bg 0.5s %.0f / 2s %.0f -> rate %.0f ADU/s (clear 0-290, cloud ~2300) ; %d stars ; max05 %.0f ; %s ; %s" % (
        tag, bg05, bg2, rate, len(stars), float(img05.max()),
        ("PLANET x=%.0f y=%.0f peak %.0f area %d" % (planet["x"], planet["y"], planet["peak"], planet["area_spx"])) if planet else "no planet blob", fn))
    return dict(rate=rate, stars=len(stars), planet=planet, png=fn)

def try_solve(p, tag, exp=15.0, gain=300):
    m = mount(); st = m.state()
    log("solve attempt @%s: %.0fs g%d frame, cap %.0fs (register RA %.4f Dec %.4f)" % (tag, exp, gain, a.solve_cap, st["RA"], st["Dec"]))
    p.setup("preview", exp, gain, 2)
    p.grab(timeout=exp + 40)
    t0 = time.time()
    r = solve(p.s.air, timeout=a.solve_cap)
    p.setup("preview", 0.5, 250, 2)
    if r is None:
        log("  no solve in %.1fs" % (time.time() - t0)); return None
    ra, dec = r["ra_dec"]
    dra = (ra - st["RA"]) * 15 * math.cos(math.radians(dec)) * 60; ddec = (dec - st["Dec"]) * 60
    st2 = mount().state()
    log("  SOLVED in %.1fs: RA %.4f Dec %+.4f, %s stars ; register was off by %+.1f' RA %+.1f' Dec ; register now RA %.4f Dec %.4f" % (
        time.time() - t0, ra, dec, r.get("star_number"), dra, ddec, st2["RA"], st2["Dec"]))
    return r

def centre(p, planet, cra, cdec):
    """Corrective gotos with the Jacobian from the register position (cra, cdec) where the planet was seen;
    flips the Jacobian if the first move goes the wrong way (other pier side)."""
    J = J0.copy(); cur = np.array([planet["x"], planet["y"]])
    side = mount().state().get("pier_side")
    if side and side != a.jacobian_pier:
        J = -J
        log("  pier side %s (Jacobian measured on %s) -> Jacobian negated" % (side, a.jacobian_pier))
    for it in range(4):
        need = np.array([960.0, 540.0]) - cur
        if np.hypot(*need) < 40:
            break
        corr = np.linalg.solve(J, need)
        cra += corr[0] / (60.0 * 15.0 * math.cos(math.radians(cdec))); cdec += corr[1] / 60.0
        goto(cra, cdec)
        set_exp(p, 0.5); img, w, h, _ = p.grab(timeout=30)
        pl = find_planet(img)
        if not pl:
            log("  centring: lost the planet after RA %+.2f' Dec %+.2f'" % (corr[0], corr[1])); return None
        new = np.array([pl["x"], pl["y"]])
        r_old, r_new = np.hypot(*need), np.hypot(960 - new[0], 540 - new[1])
        log("  centring %d: RA %+.2f' Dec %+.2f' -> x=%.0f y=%.0f (residual %.0f -> %.0f px)" % (it, corr[0], corr[1], new[0], new[1], r_old, r_new))
        if r_new > r_old * 1.2:
            log("  moved the wrong way -> flipping the Jacobian (pier side)"); J = -J
        cur = new
    return cur

def grid():
    """Search offsets (arcmin) on a field-sized grid, nearest to the prediction first, (0,0) excluded."""
    pts = []
    nra, ndec = int(a.search_ra // a.step_ra), int(a.search_dec // a.step_dec)
    for i in range(-nra, nra + 1):
        for j in range(-ndec, ndec + 1):
            if i == 0 and j == 0:
                continue
            pts.append((i * a.step_ra, j * a.step_dec))
    pts.sort(key=lambda q: math.hypot(q[0], q[1]))
    return pts

def recover(p):
    """Dark sky, no planet: fix the pointing. Returns (planet blob, register ra, register dec) or None."""
    if not a.no_solve:
        if try_solve(p, "here"):
            goto(*cmd()); s = sky(p, "after-solve")
            if s["planet"]: return s["planet"], cmd()[0], cmd()[1]
        else:
            log("anchor: solving 15 deg north where the field is richer")
            goto(a.ra, a.dec + 15.0)
            if try_solve(p, "anchor"):
                goto(*cmd()); s = sky(p, "after-anchor")
                if s["planet"]: return s["planet"], cmd()[0], cmd()[1]
    pts = grid()
    log("search: %d grid points within +/-%.0f' RA x +/-%.0f' Dec of the prediction (%+.0f', %+.0f'), 0.5 s frames, ~%.0f s" % (
        len(pts), a.search_ra, a.search_dec, a.pred_dra, a.pred_ddec, len(pts) * 4.5))
    set_exp(p, 0.5)
    t0 = time.time()
    for k, (dra_m, ddec_m) in enumerate(pts, 1):
        ra_c, dec_c = cmd(dra_m, ddec_m)
        goto(ra_c, dec_c)
        img, w, h, _ = p.grab(timeout=30)
        pl = find_planet(img)
        _, _, top = blobs(img, nsig=8.0, top=1)
        topdesc = ("top blob x=%.0f y=%.0f peak %.0f area %d" % (top[0]["x"], top[0]["y"], top[0]["peak"], top[0]["area_spx"])) if top else "no blob"
        if float(img.max()) >= PLANET_PEAK:
            save_png(img, "%s/%s_search_ra%+.0f_dec%+.0f.png" % (a.outdir, time.strftime("%H%M%S"), dra_m, ddec_m), shrink=2)
        log("  %3d/%d offset RA %+4.0f' Dec %+4.0f': max %.0f, %s -> %s  (%.0fs)" % (
            k, len(pts), dra_m, ddec_m, float(img.max()), topdesc, ("PLANET x=%.0f y=%.0f" % (pl["x"], pl["y"])) if pl else "nothing", time.time() - t0))
        if pl:
            return pl, ra_c, dec_c
    return None

def wait(secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(min(30, max(0.1, secs - (time.time() - t0))))
        left = secs - (time.time() - t0)
        if left > 0:
            log("  ... waiting, next check in %.0f s" % left)

log("%s at JNow RA %.4fh Dec %+.4f ; commanding with prediction %+.0f' RA %+.0f' Dec ; %d checks max, every %.0f s while covered" % (
    a.name, a.ra, a.dec, a.pred_dra, a.pred_ddec, a.max_checks, a.every))
recoveries = 0
for n in range(1, a.max_checks + 1):
    p = None
    try:
        ra_c, dec_c = cmd()
        st = goto(ra_c, dec_c)
        log("check %d: register RA %.4f Dec %.4f Alt %.1f Az %.1f track=%s" % (n, st["RA"], st["Dec"], st["Alt"], st["Az"], st["is_enable_track"]))
        p = Pipes(); p.setup("preview", 0.5, 250, 2)
        s = sky(p, "check%d" % n)
        planet = s["planet"]
        if not planet and s["rate"] <= a.cloud_rate and not a.no_recover and recoveries < 2:
            recoveries += 1
            log("dark sky but no %s -> pointing recovery %d" % (a.name, recoveries))
            got = recover(p)
            if got:
                planet, ra_c, dec_c = got
        if planet:
            log("VERDICT VISIBLE (check %d): %s in the frame at x=%.0f y=%.0f (bin2) with the register at RA %.4f Dec %.4f, sky rate %.0f ADU/s, %d stars ; %s" % (
                n, a.name, planet["x"], planet["y"], ra_c, dec_c, s["rate"], s["stars"], s["png"]))
            pos = centre(p, planet, ra_c, dec_c)
            if pos is not None:
                log("%s centred at x=%.0f y=%.0f" % (a.name, pos[0], pos[1]))
                if not a.no_sync:
                    try:
                        log("scope_sync to %s ephemeris position -> %s" % (a.name, mount().sync(a.ra, a.dec)))
                    except Exception as e:
                        log("sync refused (%s) -- register left as is" % e)
            st = mount().state()
            log("EXIT: rig tracking on %s, register RA %.4f Dec %.4f Alt %.1f Az %.1f track=%s" % (a.name, st["RA"], st["Dec"], st["Alt"], st["Az"], st["is_enable_track"]))
            sys.exit(0)
        if s["rate"] > a.cloud_rate:
            log("VERDICT COVERED (check %d): sky rate %.0f ADU/s (clear 0-290, cloud ~2300), %d stars, no %s ; %s" % (n, s["rate"], s["stars"], a.name, s["png"]))
        else:
            log("VERDICT UNCERTAIN (check %d): sky dark (%.0f ADU/s, %d stars) but no %s found ; %s" % (n, s["rate"], s["stars"], a.name, s["png"]))
    except SystemExit:
        raise
    except Exception as e:
        log("VERDICT ERROR (check %d): %s" % (n, e))
    finally:
        if p is not None:
            try: p.close()
            except Exception: pass
        try:
            if M is not None: M.close()
        except Exception: pass
        M = None
    if n < a.max_checks:
        wait(a.every)
log("EXIT: %s not seen after %d checks" % (a.name, a.max_checks))
sys.exit(2)
