"""Slow, mount-safe pointing nudges for the planet hold -- measured on the Moon 2026-09-28 (joytest.py).

* 1x = 15"/s: the Wi-Fi round trip (0.1-0.2 s of jitter) then costs ~2-3" instead of the 30-60" it
  cost at 20x, which is what made the 09-27 Saturn hold overshoot on a busy link and do nothing on a
  quiet one. Moves above --fast-above arcsec use 4x (60"/s) so a big correction stays quick.
* Every move is sent as scope_move [dir, sec]: the mount stops by itself after `sec` WHOLE seconds
  (a dead-man timer -- the app's joystick sends [dir, 3] while held). We still send "none" at the wanted
  time; the timer only guarantees a lost "none" can never leave the mount running.
* Dec loses 6-9" on every reversal (backlash; none measurable in RA): the first Dec move after a change
  of direction is lengthened by `backlash`. Unknown history (first move of a process) gets no extra.
* Pier west: 'south' RAISES Dec, i.e. moves the pointing north ([[mount-direction-sign-flips]]); pass
  dec_up="north" on the other pier side.
"""
import math, time
from air_rpc import Air

RATE_AS = {0: 15.0, 2: 60.0}                   # "/s at slew-rate index 0 (1x) and 2 (4x), measured


class Nudger:
    def __init__(self, host, dec_up="south", backlash=8.0, min_as=3.0, fast_above=45.0, log=print):
        self.host, self.dec_up = host, dec_up
        self.dec_down = {"south": "north", "north": "south"}[dec_up]
        self.backlash, self.min_as, self.fast_above, self.log = backlash, min_as, fast_above, log
        self.last_dec = None                       # "+" (pointing moved north) / "-" of the last Dec move

    def _move(self, m, cmd, amount):
        rate = 0 if amount <= self.fast_above else 2
        secs = amount / RATE_AS[rate]
        m.call("scope_set_slew_rate", [rate])
        t0 = time.time()
        m.call("scope_move", [cmd, max(1, int(math.ceil(secs)))])
        rest = secs - (time.time() - t0)
        if rest > 0:
            time.sleep(rest)
        m.call("scope_move", ["none"])
        return rate, secs

    def nudge(self, east_as, north_as):
        """Move the POINTING by east_as, north_as arcsec. Returns [(cmd, arcsec asked incl. backlash, rate, s)]."""
        done = []
        m = Air(self.host, 4400, timeout=10)
        try:
            idx = m.call("scope_get_info", [])["result"]["slew_rate_index"]
            try:
                if abs(north_as) >= self.min_as:
                    sgn = "+" if north_as > 0 else "-"
                    amt = abs(north_as) + (self.backlash if self.last_dec not in (None, sgn) else 0.0)
                    cmd = self.dec_up if sgn == "+" else self.dec_down
                    rate, secs = self._move(m, cmd, amt); self.last_dec = sgn
                    done.append((cmd, amt, rate, secs))
                if abs(east_as) >= self.min_as:
                    cmd = "east" if east_as > 0 else "west"
                    rate, secs = self._move(m, cmd, abs(east_as))
                    done.append((cmd, abs(east_as), rate, secs))
            finally:
                m.call("scope_move", ["none"])
                m.call("scope_set_slew_rate", [idx])
        finally:
            m.close()
        return done
