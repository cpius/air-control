"""A focus run as the dashboard's Focus pane reads it (dashboard.py read_focus_run).

One folder per run, ~/ASICAP/telemetry/<date>/focus/<tag>/, holding a stepNN.json (+ stepNN.png) per measured
position and a summary.json at the end. Star sweeps write star_width_px (smaller is better); planet sweeps write
a generic `score` with `higher_is_better`, so the pane can plot sharpness (maximise) as well as size or a moon's
FWHM (minimise).

Writing never raises: a full disk or a missing recorder must not stop a focus run.

    run = FocusRun("satfocus3", "sharp", log=log); run.announce()
    run.step("r0", 55731, 12.15, png=path, peak=176, size_px=99.8, frames=12, exposure_s=0.025, gain=400)
    run.summary(vertex=55731, best_sample=55750, parked=55731)
"""
import glob, json, math, os, shutil, time

ROOT = os.path.expanduser("~/ASICAP/telemetry")
# metric key -> (label, higher is better, unit)
METRICS = {"sharp": ("sharpness", True, ""), "size": ("planet size", False, "px"),
           "moon": ("moon FWHM", False, "px")}


def _clean(v):
    """NaN/inf -> None: the dashboard ships this to the browser as JSON, where NaN does not parse."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if hasattr(v, "item"):                       # numpy scalars
        return _clean(v.item())
    return v


class FocusRun:
    def __init__(self, tag, metric, date=None, root=ROOT, fresh=True, log=print):
        self.dir = os.path.join(root, date or time.strftime("%Y-%m-%d"), "focus", tag)
        self.metric, self.log, self.n = metric, log, 0
        self.label, self.higher, self.unit = METRICS.get(metric, (metric, True, ""))
        try:
            os.makedirs(self.dir, exist_ok=True)
            if fresh:                                # a re-run with the same tag must not mix in old steps
                for f in glob.glob(os.path.join(self.dir, "step*")) + glob.glob(os.path.join(self.dir, "summary.json")):
                    os.remove(f)
        except Exception as e:
            log("  (focus run folder %s: %s)" % (self.dir, e))

    def _dump(self, name, obj):
        try:
            with open(os.path.join(self.dir, name), "w") as fh:
                json.dump({k: _clean(v) for k, v in obj.items()}, fh, indent=1)
        except Exception as e:
            self.log("  (focus run file %s not written: %s)" % (name, e))

    def announce(self):
        """make this the run the dashboard's Focus pane shows"""
        try:
            from recorder import rec
            rec.artifact("focus_run", self.dir, metric=self.label)
        except Exception as e:
            self.log("  (dashboard not told about the focus run: %s)" % e)

    def step(self, label, focuser, score, png=None, rejected=None, t=None, **extra):
        self.n += 1
        self._dump("step%02d.json" % self.n, dict(
            step=self.n, label=label, focuser=focuser, requested=focuser, metric=self.label, metric_key=self.metric,
            score=score, higher_is_better=self.higher, unit=self.unit, rejected=rejected,
            time=t or time.strftime("%Y-%m-%dT%H:%M:%S"), **extra))
        if png and os.path.exists(png):
            try:
                shutil.copyfile(png, os.path.join(self.dir, "step%02d.png" % self.n))
            except Exception as e:
                self.log("  (focus run image not copied: %s)" % e)

    def summary(self, **kw):
        self._dump("summary.json", dict(metric=self.label, metric_key=self.metric, higher_is_better=self.higher,
                                        unit=self.unit, steps=self.n, **kw))
