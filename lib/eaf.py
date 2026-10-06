"""EAF backlash: plan every focuser move so it ARRIVES GOING DOWN.

The EAF has ~125-150 steps of backlash and its own compensation is 0
(get_focuser_info "backlash": 0, EAFN fw 3.8.1). Measured 2026-10-06 on Altair at
f/15.7: 44760 reached from above HFD 16.5 px, from below 25.8 px, 8/8 separated --
so a sweep that alternates directions mixes two focus curves ~130 steps apart.

legs() returns the positions to visit: upward moves, short reversals and an
unknown approach go OVERSHOOT past the target and come back down. Each tool
drives the legs with its own move-and-wait loop:

    for leg in eaf.legs(here, pos):
        here = go(leg)

EAF_OVERSHOOT=0 in the environment restores plain one-leg moves.
"""
import os

OVERSHOOT = int(os.environ.get("EAF_OVERSHOOT", 250))
MAX_STEP = 100000          # get_focuser_info max_step
_from_above = False        # the last planned move in this process ended going down


def legs(start, pos, hi=MAX_STEP, overshoot=None):
    """Positions to move through, in order, to reach pos from above.

    Call once per move and visit every leg: it records that the EAF now sits
    approached from above. Returns [] when it already does and pos == start."""
    global _from_above
    start, pos = int(start), int(pos)
    ov = OVERSHOOT if overshoot is None else int(overshoot)
    if ov <= 0:
        _from_above = pos < start or (pos == start and _from_above)
        return [] if pos == start else [pos]
    # Going down lands from above only if the slack is already taken up that
    # way, or the move is longer than the backlash.
    if pos <= start and (_from_above or start - pos >= ov):
        _from_above = True
        return [] if pos == start else [pos]
    over = min(pos + ov, int(hi))
    if over <= pos:                 # at the top of the range: nothing above to come from
        _from_above = False
        return [] if pos == start else [pos]
    _from_above = True
    return [over, pos]
