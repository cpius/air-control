#!/usr/bin/env python3
"""EAF backlash planning (lib/eaf.py) and the movers that use it, against a fake
focuser -- nothing touches the Air.

    python3 -u tests/test_eaf.py

The regression: 2026-10-06 on Altair, 44760 reached from below read HFD 25.8 px and
from above 16.5 px, so every sweep mixed two focus curves ~130 steps apart and the
first fine sweep picked 44885. Every move must now END GOING DOWN.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, os.path.join(HERE, "..", "focus"))
os.environ.pop("EAF_OVERSHOOT", None)
import eaf, daypipes, focuscompare

daypipes.log = lambda m: None
focuscompare.time.sleep = lambda s: None


class FakeEAF:
    def __init__(self, pos):
        self.pos, self.moves = pos, []

    def c(self, m, p=None, *a, **k):
        if m == "get_focuser_position":
            return self.pos
        if m == "get_focuser_state":
            return {"state": "idle"}
        if m == "move_focuser":
            self.moves.append(p[0]); self.pos = p[0]; return 0
        raise AssertionError(m)


HI = 54956
LEGS = [  # name, start, target, from_above before, overshoot, legs, from_above after
    ("up",                     44510, 44760, False, None, [45010, 44760], True),
    ("long down",              45010, 44760, False, None, [44760], True),
    ("short down, unknown",    44800, 44760, False, None, [45010, 44760], True),
    ("short down, from above", 44800, 44760, True,  None, [44760], True),
    ("stay, unknown",          44760, 44760, False, None, [45010, 44760], True),
    ("stay, from above",       44760, 44760, True,  None, [], True),
    ("near the top",           HI - 300, HI - 100, False, None, [HI, HI - 100], True),
    ("at the top",             HI - 300, HI, False, None, [HI], False),
    ("overshoot off, up",      44510, 44760, True,  0, [44760], False),
    ("overshoot off, down",    45010, 44760, False, 0, [44760], True),
]

fails = 0
for name, start, pos, fa, ov, want, fa_after in LEGS:
    eaf._from_above = fa
    got = eaf.legs(start, pos, hi=HI, overshoot=ov)
    ok = got == want and eaf._from_above == fa_after
    fails += not ok
    print("%s legs  %-24s %s -> %s  from_above=%s" % ("ok  " if ok else "FAIL", name, start, got, eaf._from_above))

# The movers walk every leg and end at the target, going down.
for label, mover, wrap in (("daypipes.move_to", daypipes.move_to, lambda f: f),
                           ("focuscompare.move_to", focuscompare.move_to, lambda f: f)):
    for start, pos, want in ((44510, 44760, [45010, 44760]), (45010, 44760, [44760])):
        eaf._from_above = False
        f = FakeEAF(start)
        mover(wrap(f), pos)
        ok = f.moves == want and f.pos == pos
        fails += not ok
        print("%s %-21s %d -> %d  moves %s" % ("ok  " if ok else "FAIL", label, start, pos, f.moves))

print("ALL OK" if not fails else "%d FAILED" % fails)
sys.exit(1 if fails else 0)
