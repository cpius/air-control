#!/usr/bin/env python3
"""Focus metric for the GUIDE sensor, which is focused by a physical knob.

The guide sensor has no electronic focuser -- the knob on top of the camera body
is the only control -- so there is no V-curve sweep to run. What is useful is a
fast, repeatable number that responds while a hand turns the dial.

HFD (half-flux diameter) is that number: the diameter of the circle containing
half the star's flux. It is what ASIAIR and NINA display, it degrades smoothly
and symmetrically either side of focus (unlike peak, which is flat-bottomed and
noisy), and it is insensitive to transparency because it is a ratio of the
star's own flux to itself. LOWER IS BETTER.

Note the guide sensor is OFFSET from the main sensor, not concentric -- a star
centred by a main-camera plate solve lands near the top of the guide frame. The
mount->pixel Jacobian here is measured live rather than derived from the main
camera's solve angle, because the two sensors' orientations are not guaranteed
to match and a wrong sign sends the star off-frame.
"""
import math, sys, time

def background(v, w, h, cx, cy, r_in=60, r_out=90):
    vals = []
    for y in range(max(0, cy - r_out), min(h, cy + r_out)):
        dy = y - cy
        for x in range(max(0, cx - r_out), min(w, cx + r_out)):
            d = math.hypot(x - cx, dy)
            if r_in <= d <= r_out:
                vals.append(v[y * w + x])
    if not vals:
        return 0.0
    vals.sort()
    return float(vals[len(vals) // 2])

def hfd(v, w, h, cx, cy, ap=40):
    """Half-flux diameter in pixels, plus centroid, peak and flux."""
    bg = background(v, w, h, cx, cy)
    # flux-weighted centroid first -- HFD about the wrong centre reads high
    sx = sy = st = 0.0
    for y in range(max(0, cy - ap), min(h, cy + ap)):
        for x in range(max(0, cx - ap), min(w, cx + ap)):
            if math.hypot(x - cx, y - cy) > ap:
                continue
            f = v[y * w + x] - bg
            if f > 0:
                sx += f * x; sy += f * y; st += f
    if st <= 0:
        return None
    fx, fy = sx / st, sy / st
    # radial profile about the centroid
    prof = []
    total = 0.0
    peak = 0.0
    for y in range(max(0, int(fy) - ap), min(h, int(fy) + ap)):
        for x in range(max(0, int(fx) - ap), min(w, int(fx) + ap)):
            d = math.hypot(x - fx, y - fy)
            if d > ap:
                continue
            f = v[y * w + x] - bg
            peak = max(peak, f)
            if f > 0:
                prof.append((d, f)); total += f
    if total <= 0:
        return None
    prof.sort()
    half, acc = total / 2.0, 0.0
    r_half = ap
    for d, f in prof:
        acc += f
        if acc >= half:
            r_half = d
            break
    return {"hfd": 2.0 * r_half, "cx": fx, "cy": fy, "peak": peak,
            "flux": total, "bg": bg}

def brightest(v, w, h, margin=40):
    """Brightest pixel away from the frame edge."""
    best, bi = -1, None
    for y in range(margin, h - margin):
        row = y * w
        for x in range(margin, w - margin):
            val = v[row + x]
            if val > best:
                best, bi = val, (x, y)
    return bi, best


# --------------------------------------------------------------------------
# Vega saturates this sensor even at 10ms / gain 0 (mag 0 through 200mm), and a
# flat-topped star has no measurable HFD -- its "brightest pixel" wanders across
# the plateau. So focus is measured on the brightest UNSATURATED star in the
# same frame instead. Pointing still goes to the named target; only the metric
# star differs.
# --------------------------------------------------------------------------
import numpy as _np

def sources(v, w, h, sat=60000, margin=60, min_sep=25, top=12):
    """Local maxima above the sky, brightest first, saturated ones flagged."""
    a = _np.frombuffer(v, dtype=_np.uint16).reshape(h, w).astype(_np.float32) \
        if not isinstance(v, _np.ndarray) else v.astype(_np.float32)
    bg = float(_np.median(a))
    sig = float(_np.median(_np.abs(a - bg))) * 1.4826 or 1.0
    thr = bg + 8.0 * sig
    m = a[margin:h - margin, margin:w - margin]
    ys, xs = _np.where(m > thr)
    if len(xs) == 0:
        return [], bg, sig
    vals = m[ys, xs]
    order = _np.argsort(-vals)
    picked = []
    for i in order:
        x, y, val = int(xs[i]) + margin, int(ys[i]) + margin, float(vals[i])
        if any(abs(x - px) < min_sep and abs(y - py) < min_sep for px, py, _, _ in picked):
            continue
        picked.append((x, y, val, val >= sat))
        if len(picked) >= top:
            break
    return picked, bg, sig


def blob(a, w, h, sat=60000):
    """Extent of the defocused star. Returns diameter in px.

    A badly defocused SCT does not make a star, it makes a DONUT -- a ring with
    a hole where the secondary shadows it. HFD is meaningless on that (and the
    rim saturates on a bright star anyway, so the peak carries no information).
    What does vary monotonically with focus is the donut's SIZE, so measure the
    area above a sky threshold and report the equivalent diameter. This keeps
    working right down to focus, where it degenerates smoothly into a normal
    star's diameter.
    """
    bg = float(_np.median(a))
    sig = float(_np.median(_np.abs(a - bg))) * 1.4826 or 1.0
    mask = a > bg + 10.0 * sig
    ys, xs = _np.where(mask)
    if len(xs) < 10:
        return None
    # largest cluster: take the densest 200x200 window, then the pixels in it
    cx0, cy0 = int(_np.median(xs)), int(_np.median(ys))
    keep = (_np.abs(xs - cx0) < 150) & (_np.abs(ys - cy0) < 150)
    xs, ys = xs[keep], ys[keep]
    if len(xs) < 10:
        return None
    area = float(len(xs))
    cx, cy = float(xs.mean()), float(ys.mean())
    d_area = 2.0 * math.sqrt(area / math.pi)
    # radial extent as a cross-check (95th percentile radius)
    r = _np.hypot(xs - cx, ys - cy)
    d_r95 = 2.0 * float(_np.percentile(r, 95))
    sat_frac = float((a[ys, xs] >= sat).mean())
    return {"diam_area": d_area, "diam_r95": d_r95, "area_px": area,
            "cx": cx, "cy": cy, "bg": bg, "sat_frac": sat_frac}
