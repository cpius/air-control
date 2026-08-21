#!/usr/bin/env python3
"""One correct way to run a plate solve, because there are two ways to get a
confidently wrong answer out of the Air's solver and both are silent.

TRAP 1 — a solve that never finishes. `start_solve` returns 0 immediately and
the result arrives as an async `PlateSolve` event. In twilight, through cloud,
or with the camera off the OTA, the solver reaches `state: "solving"` and simply
stays there. Nothing times out, so a caller that waits for `complete` waits
forever. Every solve must be individually capped -- 20 s is the number that has
held up on sky; a solve that has not converged by then is not going to.

TRAP 2 — the stale result, which is worse, because it looks like success.
`get_last_solve_result` is a *last* result, not a *this* result. When the
current solve fails or is still running it returns the PREVIOUS solve verbatim,
`state: "complete"` and all. Poll it after a failed solve and you get a
plausible RA/Dec from some earlier pointing, and any offset computed from it is
garbage that looks authoritative. Observed live 2026-08-21: two solves 15
minutes and one goto apart returned byte-identical `ra_dec`, `fov` and
`star_number`, distinguishable only by `image_id` being unchanged at 7.

`image_id` is the discriminator. Read it BEFORE starting, and reject any result
that comes back carrying the same one.

    from solving import solve
    r = solve(air, timeout=20.0)
    if r is None:
        ...        # no solve -- do not fall back on get_last_solve_result
    else:
        ra_h, dec_deg = r["ra_dec"]
"""

import json
import time

from airlog import get_logger

log = get_logger("solve")

DEFAULT_TIMEOUT = 20.0


def _call(air, method, params=None, timeout=10):
    r = air.call(method, params or [], timeout=timeout)
    return r.get("result", r.get("error"))


def last_result(air):
    r = _call(air, "get_last_solve_result")
    return r if isinstance(r, dict) else None


def solve(air, timeout=DEFAULT_TIMEOUT, keepalive=4.0):
    """Run one plate solve. Return the result dict, or None if it did not solve.

    Never returns a stale result: the `image_id` of whatever was sitting in
    `get_last_solve_result` beforehand is remembered and rejected.
    """
    prev = last_result(air)
    prev_id = prev.get("image_id") if prev else None
    log.debug("previous solve image_id=%s", prev_id)

    air.drain_events()
    _call(air, "start_solve")
    t0 = time.time()
    last_poke = t0
    state = None

    while time.time() - t0 < timeout:
        for e in air.drain_events():
            if e.get("Event") == "PlateSolve":
                state = e.get("state")
                log.debug("PlateSolve %s", json.dumps(e, ensure_ascii=False)[:160])
        if state in ("complete", "fail", "error"):
            break
        # The Air drops an idle 4700 socket at ~15 s, which is INSIDE this
        # window -- without a poke the solve "fails" as a disconnect.
        if time.time() - last_poke > keepalive:
            try:
                _call(air, "get_camera_state")
            except Exception:
                pass
            last_poke = time.time()
        time.sleep(0.3)

    elapsed = time.time() - t0
    if state != "complete":
        log.warn("plate solve did not complete in %.1fs (state=%s) — aborting",
                 elapsed, state)
        try:
            _call(air, "stop_solve")
        except Exception:
            pass
        return None

    r = last_result(air)
    if not isinstance(r, dict) or r.get("state") != "complete":
        log.warn("solve reported complete but no usable result: %r", r)
        return None
    if prev_id is not None and r.get("image_id") == prev_id:
        # The event said complete but the result did not advance. Trust the id.
        log.warn("STALE solve result (image_id still %s) — discarding", prev_id)
        return None

    log.info("solved in %.1fs: RA %.5fh Dec %+.5f  fov %.1f'x%.1f'  FL %.1fmm  %s stars",
             elapsed, r["ra_dec"][0], r["ra_dec"][1],
             r["fov"][0] * 60, r["fov"][1] * 60, r.get("focal_len", 0),
             r.get("star_number"))
    return r
