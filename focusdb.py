#!/usr/bin/env python3
"""Known-good focuser positions, so a night never starts by guessing.

Recall is the easy part. The discipline around it is what stops the database
being worse than nothing:

  * **Only `quality: good` entries drive a suggestion.** A single-pass,
    single-star sweep measures the seeing, not the focuser — one can land a
    couple of hundred steps from the truth and look perfectly convincing.
    Average one of those in with a careful interleaved measurement and you
    manufacture a temperature coefficient out of two numbers that differ for
    an entirely different reason. Record sloppy runs as `rough`: kept for the
    record, never used.

  * **No temperature extrapolation.** A coefficient fitted to two noisy points
    will put you a thousand steps out with total confidence. `suggest` reports
    the temperature span its points actually cover and applies no correction;
    fit one only when the data genuinely resolves it.

  * **The suggestion is a starting point, not an answer.** Verify it —
    `focuscompare.py` does it in about two minutes — then record what you
    found.

Positions do not transfer across optical trains: a reducer moves focus by
thousands of steps, and the plate solver reports the focal length you are
actually on, so `focal_len_mm` is the key that keeps two trains from
contaminating each other.

The database is JSON at `./focus-points.json`, or wherever `FOCUS_DB` points.
It is your own observing data — keep it out of version control.
"""
import argparse
import json
import os
import sys

DB_PATH = os.environ.get("FOCUS_DB", "focus-points.json")


def load(path):
    if not os.path.exists(path):
        return {"points": []}
    with open(path) as f:
        return json.load(f)


def save(db, path):
    with open(path, "w") as f:
        json.dump(db, f, indent=2)
        f.write("\n")


def matches(p, fl=None, filt=None, fl_tol=0.08):
    """Same optical train? Focal length within a few percent, same filter."""
    if fl is not None and p.get("focal_len_mm"):
        if abs(p["focal_len_mm"] - fl) / float(fl) > fl_tol:
            return False
    if filt is not None and (p.get("filter") or None) != (filt or None):
        return False
    return True


def suggest(db, fl=None, filt=None):
    good = [p for p in db.get("points", []) if p.get("quality") == "good"]
    cand = [p for p in good if matches(p, fl, filt)]
    fallback = False
    if not cand and good:
        cand, fallback = good, True
    if not cand:
        return None
    cand.sort(key=lambda p: p.get("date", ""), reverse=True)
    latest = cand[0]
    agree = [p for p in cand if p["position"] == latest["position"]]
    lo = max((p["plateau"][0] for p in cand if p.get("plateau")), default=None)
    hi = min((p["plateau"][1] for p in cand if p.get("plateau")), default=None)
    temps = sorted(p["temp_c"] for p in cand if p.get("temp_c") is not None)
    return {
        "position": latest["position"],
        "from": latest.get("date", "?"),
        "n_matching": len(cand),
        "n_agreeing": len(agree),
        "plateau": [lo, hi] if lo is not None and hi is not None and lo <= hi else None,
        "fallback_train": fallback,
        "temp_note": ("no temperature correction applied: the matching points span "
                      "%.1f-%.1f degC and do not resolve a thermal term"
                      % (temps[0], temps[-1])) if len(temps) > 1 else
                     "only one temperature recorded; no thermal term derivable",
    }


def fmt(s, span=250):
    if not s:
        return ("no usable points yet — measure one with focuscompare.py and "
                "record it with `focusdb.py record`")
    head = "start at %d  (from %s" % (s["position"], s["from"])
    if s["n_agreeing"] > 1:
        head += ", %d of %d good points agree" % (s["n_agreeing"], s["n_matching"])
    lines = [head + ")"]
    if s["plateau"]:
        lines.append("  flat between: %d - %d" % tuple(s["plateau"]))
    if s["fallback_train"]:
        lines.append("  !! no point recorded for this optical train — this is from a "
                     "DIFFERENT one and may be far out")
    lines.append("  " + s["temp_note"])
    lines.append("  verify before trusting it:")
    lines.append("    python3 focuscompare.py --pos %d,%d,%d"
                 % (s["position"] - span, s["position"], s["position"] + span))
    return "\n".join(lines)


def cmd_list(a):
    db = load(a.db)
    pts = db.get("points", [])
    if not pts:
        print("%s is empty" % a.db)
        return
    print("%-11s %8s %-14s %8s %7s %-8s %s"
          % ("date", "pos", "plateau", "FL mm", "temp", "quality", "metric"))
    for p in sorted(pts, key=lambda p: p.get("date", "")):
        pl = ("%d-%d" % tuple(p["plateau"])) if p.get("plateau") else "-"
        print("%-11s %8d %-14s %8s %7s %-8s %s"
              % (p.get("date", "?"), p["position"], pl, p.get("focal_len_mm", "-"),
                 p.get("temp_c", "-"), p.get("quality", "?"), p.get("metric", "")))
        if p.get("note"):
            print("            %s" % p["note"])


def cmd_suggest(a):
    print(fmt(suggest(load(a.db), fl=a.fl, filt=a.filter)))


def cmd_record(a):
    db = load(a.db)
    if a.quality == "good" and not a.metric:
        sys.exit("a 'good' point needs --metric describing how it was measured — "
                 "that field is what stops a sloppy number being reused as fact")
    db.setdefault("points", []).append({
        "date": a.date,
        "position": a.position,
        "plateau": list(a.plateau) if a.plateau else None,
        "train": a.train,
        "focal_len_mm": a.fl,
        "camera": a.camera,
        "filter": a.filter,
        "temp_c": a.temp,
        "temp_source": a.temp_source,
        "metric": a.metric,
        "value": a.value,
        "value_unit": a.unit,
        "quality": a.quality,
        "note": a.note,
    })
    save(db, a.db)
    print("recorded %d for %s (%s) in %s" % (a.position, a.date, a.quality, a.db))
    print(fmt(suggest(db, fl=a.fl, filt=a.filter)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB_PATH, help="default: $FOCUS_DB or ./focus-points.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="show every recorded point")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("suggest", help="best starting position for tonight")
    s.add_argument("--fl", type=float, help="focal length in mm — a plate solve reports it")
    s.add_argument("--filter", default=None)
    s.set_defaults(func=cmd_suggest)

    s = sub.add_parser("record", help="add a measured point")
    s.add_argument("date", help="YYYY-MM-DD")
    s.add_argument("position", type=int)
    s.add_argument("--plateau", type=int, nargs=2, metavar=("LO", "HI"))
    s.add_argument("--train", default=None, help="free text, e.g. 'SCT + 0.63x reducer'")
    s.add_argument("--fl", type=float)
    s.add_argument("--camera", default=None)
    s.add_argument("--filter", default=None)
    s.add_argument("--temp", type=float)
    s.add_argument("--temp-source", default="ambient")
    s.add_argument("--metric", help="HOW it was measured — required for --quality good")
    s.add_argument("--value", type=float)
    s.add_argument("--unit", default="px HFD")
    s.add_argument("--quality", choices=["good", "rough"], default="rough",
                   help="'good' = interleaved, several stars. Anything single-pass "
                        "or single-star is 'rough' and never drives a suggestion.")
    s.add_argument("--note", default=None)
    s.set_defaults(func=cmd_record)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
