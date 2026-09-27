#!/usr/bin/env python3
"""Regenerate HORIZON.md and horizon-mask.json from the survey datasets.

Both artifacts are derived, never hand-edited: a table typed by hand drifts from
the JSONL behind it, and this file is consulted precisely when something has gone
wrong and trust matters. Re-run after any survey.
"""
import json, os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import survey_report as sr

HOME = os.path.expanduser("~/ASICAP")
SETS = {
    "west": (os.path.join(HOME, "skysurvey-west.jsonl"), "2026-08-26/27"),
    "east": (os.path.join(HOME, "skysurvey-east.jsonl"), "2026-08-25/26"),
}

def load(path):
    return [json.loads(l) for l in open(path) if l.strip()]

def profile(recs):
    """az -> (opens_above_lo, opens_above_hi, kind). The boundary is the same
    quantity for a low roofline and for a tall wall: the altitude above which
    sky is open."""
    out = {}
    for az, (lo, hi) in sorted(sr.roofline(recs).items()):
        if lo is None and hi is None:
            continue
        if lo is not None and hi is not None:
            out[az] = (min(lo, hi), max(lo, hi), "bracketed")
        elif lo is not None:
            out[az] = (None, lo, "open-only")          # never found the bottom
        else:
            out[az] = (hi, None, "blocked-only")       # never found sky
    return out

def md_table(prof):
    rows = ["| az | | sky opens above | |", "|---|---|---|---|"]
    for az, (lo, hi, kind) in sorted(prof.items(), key=lambda t: t[0]):
        if kind == "bracketed":
            val, note = f"**{(lo+hi)/2:.1f}°**", f"{lo:.1f}–{hi:.1f}"
        elif kind == "open-only":
            val, note = f"≤ {hi:.1f}°", "no blockage found below"
        else:
            val, note = f"> {lo:.1f}°", "**no sky found up to here**"
        rows.append(f"| {az}° | {sr.compass(az)} | {val} | {note} |")
    return "\n".join(rows)

data, mask = {}, {}
for name, (path, when) in SETS.items():
    recs = load(path)
    prof = profile(recs)
    data[name] = (recs, prof, when)
    mask[name] = {
        "surveyed": when,
        "records": len(recs),
        "verdicts": dict(collections.Counter(r.get("verdict") for r in recs)),
        "profile": [
            {"az": az,
             "opens_above_deg": None if kind != "bracketed" else round((lo+hi)/2, 2),
             "bracket_lo": None if lo is None else round(lo, 2),
             "bracket_hi": None if hi is None else round(hi, 2),
             "kind": kind}
            for az, (lo, hi, kind) in sorted(prof.items())
        ],
    }

json.dump({
    "schema_version": 2,
    "generated_by": "air-control/horizon/make_horizon.py -- do not hand-edit",
    "site": {"name": "Copenhagen flat, two balconies",
             "lat": 55.689444, "lon": 12.555278, "elevation_m": 20},
    "frame": {
        "azimuth": "TRUE azimuth, degrees, N=0 E=90 S=180 W=270",
        "altitude": "degrees above the true horizon",
        "note": ("Positions come from PLATE SOLVES, not the mount register, so this "
                 "survives a re-home. Blocked points have no solve and carry the "
                 "commanded azimuth; each is bracketed by a solve above it."),
    },
    "semantics": {
        "opens_above_deg": "sky is open ABOVE this altitude at this azimuth",
        "kind": {
            "bracketed": "boundary pinned between a blocked and an open shot",
            "open-only": "open at the lowest altitude tried; no blockage found below",
            "blocked-only": "blocked at every altitude tried; SKY MAY EXIST ABOVE",
        },
        "warning": "A missing azimuth is UNKNOWN, never open.",
    },
    "balconies": mask,
}, open(os.path.join(HOME, "horizon-mask.json"), "w"), indent=1)

west_recs, west_prof, west_when = data["west"]
east_recs, east_prof, east_when = data["east"]
def counts(recs):
    c = collections.Counter(r.get("verdict") for r in recs)
    return f"{len(recs)} shots — {c['OPEN']} open, {c['BLOCKED']} blocked, {c['AMBIGUOUS']} ambiguous"

doc = f"""# Horizon — what each balcony can actually see

Machine-readable companion: [`horizon-mask.json`](horizon-mask.json).
Hardware facts live in [`RIG.md`](RIG.md).
**Both files are generated** by `air-control/horizon/make_horizon.py` from the survey
JSONLs. Do not hand-edit them; re-run it instead.

## Status

| | |
|---|---|
| West balcony | **measured**, {len(west_prof)} azimuths, {west_when} |
| East balcony | **measured**, {len(east_prof)} azimuths, {east_when} |
| Method | plate solve as classifier — a solve IS the proof of open sky |

Positions come from solves, not the mount register, so this survives a re-home.
That was the blocker that kept this file unmeasured for weeks; solving instead
of star-counting removed it.

## The correction that matters most

The old version of this file said the east and south were blocked, full stop.
**They are blocked only up to a height.** There is open sky above the walls, and
it is the best sky on the site — 800–1700 stars a frame, because it is Cygnus
overhead. From the west balcony:

    az 180   blocked at 60.6°   open at 60.9°   (52 stars, then 265 by 65°)
    az 190   blocked at 60°     open at 65°     (859 stars at 80°)

Anything that reads "blocked" without an altitude is a statement about the
altitudes that were *tried*, not about the sky.

**The zenith is completely clear.** Alt 85° solved at az 0, 150, 170, 180, 190
and 270 — there is no balcony ceiling or soffit anywhere. A target transiting
high is reachable from either balcony regardless of azimuth.

## How to read the tables

Each row gives the altitude **above which sky is open** at that azimuth, with
the bracket that pins it. For the open arcs this is a roofline; for the walls it
is the top edge of the masonry. It is the same quantity either way.

* **bracketed** — a blocked shot below, a solved shot above. Trustworthy.
* **≤ x°** — open at the lowest altitude tried; the true roofline is lower.
* **> x°** — blocked everywhere tried. **Sky may exist above it.** Not a wall of
  infinite height, just an unfinished measurement.
* **A missing azimuth is unknown, never open.**

## West balcony — {counts(west_recs)}

{md_table(west_prof)}

### Shape

Three regimes:

* **Open arc, az 250°→30°** (WSW · W · NW · N · NNE). The working sky. Lowest
  horizon is **4.6–6.2° at due west**; 260°–310° is under 8° apart from a
  discrete ~6° spike at az 300° (chimney or stack — low sky either side of it).
* **South-west ramp, az 200°→240°.** A smooth climb 17°→22°, receding rooftops.
* **The wall, az 35°→195°.** Rises steeply off the north-east edge (28° at 35°,
  37° at 40°, 50° at 50°), runs level at **66–69° from az 100° to 120°**, eases
  to ~59–61° across the south-east, and ends abruptly between 195° and 200°.

The balcony is a **notch between two walls of different heights**, not a slot of
open sky. Both edges are sharp: az 30° opens below 14° while az 35° is blocked
to 28°; az 195° is wall to 51° while az 200° opens below 22°.

## East balcony — {counts(east_recs)}

{md_table(east_prof)}

### Caveats specific to the east data

Measured with the **older method** and it shows:

1. **It never looked above 60°.** The ladder probed 30/45/60 only, so every
   `> x°` row may have open sky above it — exactly the error later disproved on
   the west side. Do not read those as closed.
2. **10 s exposures throughout.** Too long for dark sky: it over-exposes and the
   solver grinds on crowded fields. Eleven shots came back ambiguous, against
   five on the west. Dark-sky values are 2 s above 40° altitude, 3 s from
   20–40°, 4 s below.
3. The **celestial pole (az 0°, alt 55.7°) is blocked**, consistent with polar
   alignment never having succeeded from this balcony.

Still solid from 2026-08-23, and independent of any mount register because the
Moon's position is ephemeris-exact:

| az | alt | verdict | evidence |
|---|---|---|---|
| 181.7° | 6.6° | open | observer confirmed visible |
| 199.4° | 4.7° | open | **photographed**, lunar surface at 44k ADU |
| 206.7° | 3.0° | open | observer confirmed visible |
| 208.4° | 2.5° | open | **photographed**, full disc |

## Method, and the three ways it lies to you

`air-control/horizon/ladder.py` finds a boundary by solve-verified bisection;
`refine.py` tightens a known bracket without re-deriving it; `skysurvey.py` takes
the individual shots. All write JSONL, and `survey_report.py` renders the map.

1. **A fast fail is masonry; a long grind is not.** A blocked frame fails in
   2–5 s with zero stars. A frame that grinds for 75 s has stars it cannot
   match — recorded as `AMBIGUOUS`, never as blocked. Reading those as wall is
   how the south got written off.
2. **Exposure cuts both ways.** Too short and extinction empties a low frame
   until open sky looks blocked. Too long and a crowded field defeats the
   solver. Az 35° was unsolvable at 10 s and bracketed to ±0.4° at 3 s.
3. **A blocked reading has no position of its own.** It has no solve, so it
   inherits the commanded azimuth. Every blocked point here is bracketed by a
   solve nearby; without that a stalled axis writes fiction. One shot on
   2026-08-26 was voided for exactly this — two clients on the mount at once, so
   the frame was taken 68° from where it was commanded.
"""
open(os.path.join(HOME, "HORIZON.md"), "w").write(doc)
print("wrote HORIZON.md and horizon-mask.json")
print("  west:", len(west_prof), "azimuths |  east:", len(east_prof), "azimuths")
