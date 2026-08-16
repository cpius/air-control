# ASIAIR 4700 RPC — real method names

Extracted from the ASIAIR Android app (`com.zwoasi.asiair` 3.0.0, `classes6.dex`)
on 2026-08-09, then validated live against firmware 43.97. These are the names
the app sends over the port-4700 JSON-RPC channel.

## ✅ The mount is on a DIFFERENT PORT: 4400, unauthenticated

The thing that cost the most time: **mount and guiding commands are not on 4700
at all.** The app builds its `MountGateway` on the `AirGuide4400Gateway`
(`ServiceInitRepository`: `mountGateway = new MountGateway(airGuide4400Gateway)`),
so `scope_*` / `get_mount_*` go to **TCP 4400**. That port answers
`test_connection` but has **no RSA handshake** (`get_verify_str` → 103 there), so
mount control needs **no key** — just connect to 4400 and call. Every mount
method returning `103` on 4700 was simply the wrong port, not an auth or session
gate. (An earlier note here claimed session-scoped access-gating — that was
wrong; it's a port split.) `mount.py` uses 4400; `air_rpc.py --key` is 4700 only.

| Port | Auth | Serves |
|---|---|---|
| 4700 | RSA handshake (`--key`) | camera, focuser, wheel, solve, plan, stack, polar, settings, telemetry |
| 4400 | none | **mount (`scope_*`) + guiding (unprefixed guide methods)** |

Live-validated on 4400 (firmware 43.97 / AM5N fw 1.8.6, 2026-08-09):
`get_connected_mount_info`→`{"model":"ZWO AM5N",...}`, `get_mount_list`→driver
list (index 1 = ZWO AM3/AM5/AM7), `scope_get_ra_dec`→`[ra_h, dec_d, sidereal]`,
`scope_get_info`→full state, `scope_get_cap`→capability list.

Param shapes below marked ✓ are confirmed from the app's `MountGateway`.

Why this was needed: the seestar_alp `scope_*` / `iscope_*` names all return
`103 method-not-found` on ASIAIR, and the mount does **not** use the
`open_<device>` connect path (that's camera/focuser/wheel only). The mount has
its own vocabulary and connects via **`set_connected([{"mount": true}])`** —
the same call that connects the guide camera, with a different field.
(An earlier note here said `set_mount`; that name returns `103` on both ports.
Corrected 2026-08-15 after connecting the mount this way live.)

All mount methods below are on **port 4400** (no auth). Coordinates: **RA in
hours, Dec/Alt/Az in degrees.**

## Mount connect / select (4400)

| Method | Params | Purpose |
|---|---|---|
| `set_connected` | `[{"mount": true}]` ✓ | **Connect the mount.** The way back after an Air restart |
| `scope_get_location` / `scope_set_location` | — / `[lat, lon]` ✓ | Site position. **Wiped to `[0.0,-0.0]` by a restart** |
| `scope_get_time` / `scope_set_time` | — / `[str]` ✓ | Clock; survives a restart |
| `mount_scan_port` | — ✓ | Scan serial ports (`/dev/ttyACM0` for the AM5N over USB) |
| `get_mount_list` | — ✓ | Driver list; the index maps to a mount model |
| `select_mount_list_index` | `[index]` | Pick the mount driver (index 1 = ZWO AM3/AM5/AM7) |
| `select_serial_dev` | `[dev]` | Choose the serial device |
| `scope_set_connection_mode` / `scope_set_connection_para` | mode/param | Set connection mode (USB/serial/BLE) |
| `get_mount_index` | — ✓ | Currently-selected driver index |
| `get_connected_mount_info` | — ✓ | `{model, fw_ver, sn, ble_name}` |
| `scope_set_mount_info` | | Push mount config |

## Pointing — read (4400)

| Method | Returns |
|---|---|
| `scope_get_ra_dec` ✓ | `[ra_h, dec_deg, sidereal_h]` |
| `scope_get_info` ✓ | Full state: RA/Dec/Az/Alt/tracking/park/slew_rate/voltage/caps/… |
| `scope_get_horiz_coord` ✓ | `[alt_deg, az_deg]` |
| `scope_get_track_state` ✓ | bool |
| `scope_get_cap` ✓ | capability strings (`goto`, `sync`, `park`, `move`, …) |
| `scope_get_slew_rate` / `scope_get_track_mode` / `scope_get_guide_rate` | rate/mode |

## Goto / slew / sync / park (4400 — **moves hardware**)

| Method | Params ✓ | Purpose |
|---|---|---|
| `scope_goto` | `[ra_h, dec_deg]` | Slew to coordinates |
| `scope_sync` | `[ra_h, dec_deg]` | Sync pointing model |
| `scope_set_track_state` | `[bool]` | Sidereal tracking on/off |
| `scope_move` | `[dir]` or `[dir, speed]` | Directional slew |
| `scope_move_left_by_angle` | `[obj]` | Slew by angle |
| `scope_park` | — | Park |
| `scope_abort_slew` | — | Stop a slew / unpark move |
| `scope_set_track_mode` / `scope_set_slew_rate` | `[index]` | Track mode / slew rate (list index) |
| `scope_set_guide_rate` | `[rate]` | Pulse-guide rate, a **float** ×sidereal (e.g. `0.5`) |

## Solve-and-center (these ARE on 4700, main channel)

`start_auto_goto` (`[float,…]`) / `start_auto_goto_pixel` / `stop_auto_goto` —
plate-solve-and-center, orchestrated from 4700 using the mount underneath.

## Plate solve

`start_solve` · `stop_solve` · `get_solve_result` · `get_last_solve_result` · `set_solved`

## Guiding (4400 — unprefixed guide methods)

Guiding shares the 4400 channel with the mount but the guide methods are
**unprefixed** (they'd collide with 4700 names, but it's a separate service).
Names + params from the app's `GuideCameraGateway`, live-validated 2026‑08‑09.

**Entering the guide tab / opening the sensor.** There is no `set_page("guide")`
— the guide tab (`GuiderFragment`) talks to 4400 directly. To bring the guide
camera online so `get_camera_info` / `get_exposure` / `get_gain` return real
values instead of `318`:

```
set_camera_idx([<id>])             # id from get_connected_cameras (e.g. 0)
set_connected([{"camera": true}])  # connect the sensor (Camera bean {camera:bool})
loop()                             # optional — start streaming; frames go to TCP 4500
```

`set_connected([{"camera": false}])` disconnects it. **Reading the settings needs
only the connect** — `loop` (live frames) additionally needs a consumer on the
guide image stream, **TCP 4500** (`GuideImageSocket`), and returns
`303 could not start looping` if the sensor isn't connected first. (An earlier
revision of this doc claimed the sensor only opens with the app's engine running
— that was wrong; the `set_connected` object shape was.)

### Camera / session
| Method | Params | Purpose |
|---|---|---|
| `get_connected_cameras` | — ✓ | List guide cameras `[{name,id,path}]` |
| `set_camera_idx` | `[index]` ✓ | Select the guide camera (id from the list above) |
| `set_connected` | `[{"camera": bool}]` ✓ | Connect/disconnect the guide sensor (`Camera` bean); the mount side is `[{... mount ...}]` |
| `get_connected` | — ✓ | `{camera:{name,path}, mount, mount_name, …}` (the `camera` field appears once connected) |
| `get_camera_info` / `get_camera_binning` | — ✓ | `{full_size:[w,h]}` / `{bin,max_bin}` once connected (else `318`) |
| `get_exposure` / `set_exposure` | — / `[ms]` | Guide exposure, ms (reads real once connected) |
| `get_gain` / `set_gain` / `get_gain_segment` | — / `[gain]` / — | Guide-camera gain (`{min,max,val}`) |
| `loop` / `stop_capture` | — ✓ | Start / stop streaming (frames on TCP **4500**); `loop` → `303` if the sensor isn't connected |
| `guide` | **bare** `{settle-obj}` | Start calibration + guiding |
| `get_app_state` | — ✓ | `Idle`/`Looping`/`Selected`/`Calibrating`/`Guiding`/`Paused`/`LostLock`/`Stopped` |

### Algorithm / tuning (the "am I on defaults?" params)
| Method | Params ✓ | Values |
|---|---|---|
| `get_algo_param` | `[axis, key]` | axis `"ra"`/`"dec"`, key `"aggression"`/`"period"`. A single arg → `105` |
| `set_algo_param` | `[axis, key, value]` | e.g. `["dec","aggression",0.7]` (aggression 0–1) |
| `get_dec_guide_mode` / `set_dec_guide_mode` | — / `[mode]` | `"Auto"`/`"North"`/`"South"`/`"Off"` |
| `get_search_region` / `set_search_region` | — / `[px]` | Star search box, px |
| `get_lock_position` / `set_lock_position` | — / `[x, y, lock]` | Guide lock position |
| `get_setting` / `set_setting` | — / `[obj]` | Guide settings blob (observed empty `{}` even when connected; likely populates only during guiding) |
| `get_beta_setting` / `set_beta_setting` | — / **bare** `{obj}` | Holds `disable_meridian_limit`; setter takes a **bare** object (list-wrapped → `107`) |

### Calibration / darks
| Method | Params | Purpose |
|---|---|---|
| `get_calibrated` / `clear_calibration` | — | Calibration state / clear |
| `get_auto_load_calibration` / `set_auto_load_calibration` | — / `[bool]` | Auto-load stored calibration |
| `get_flip_state` / `flip_calibrate` | — | Meridian-flip calibration |
| `start_create_dark` / `stop_create_dark` / `get_dark_info` | — | Guide dark library |
| `get_ra_dec_history` | — | Guide graph history (`321` when empty) |

**Param-shape gotchas on 4400** (they are not uniform): `scope_*` mount methods
and `set_algo_param` take a **list** (`["dec","aggression",0.7]`); the guide
config setters `set_beta_setting`/`guide` take a **bare object** (list-wrapping
them returns `107`), while `set_connected` takes a **list-wrapped** `Camera`/`Mount`
bean (`[{"camera": true}]`).
The mount's pulse-guide rate is `scope_set_guide_rate` — a `scope_*` mount
method taking a **float** (e.g. `0.5`), not one of these guide methods.
`get_dither`/`set_dither` live on **4700**, not here.

## Polar alignment

`start_polar_align` · `stop_polar_align` · `get_polar_align_image` ·
`set_polar_align_image` · `get_polar_axis`

These are on **4700**. `start_polar_align` will not run until you call
`set_page(["pa"])` first, and it takes a bare object — see
[Polar alignment needs `set_page`](#polar-alignment-needs-set_page) below for
the working sequence, the result shape, and the near-pole failure mode.

## Autorun — the only path that saves to eMMC and dithers (4700)

`set_page` · `set_sequence` · `set_sequence_setting` · `start_exposure` ·
`clear_sequence` · `reset_sequence_progress` · `get_target_sequences`

Everything driven through the preview page is **transient**: `start_exposure`
there exposes, the frame can be pulled off 4800, and the Air never writes it to
storage. Dithering does not happen either. Both come from the Air's own sequence
runner, and this is how to start it — captured from the app's traffic 2026-08-16:

```
set_page(["autosave"])
clear_sequence()                              # only if a previous run has progress
set_sequence([{ "id":1, "type":"light", "exp":180, "gain":-10000, "bin":1,
                "repeat":100, "filter":0, "suffix":"", "enable":true,
                "autoexp":false, "capture_index":1 }])
set_sequence_setting([{ "group_name":"M 101", "mount_end_action":"none",
                        "delay_first":0, "delay_between_frame":0,
                        "delay_between_sequence":0, "group_by_slot":true,
                        "focuser_go_home":false, "caa_go_home":false,
                        "shutdown_pi":false }])
start_exposure(["light"])
```

Four details, each of which silently breaks the whole thing on its own:

- **It is `set_sequence`, not `set_plan`.** `set_plan` is the separate
  multi-target planner and is a decoy: it accepts a plan object, reports
  `is_plan_started: true`, and never exposes a frame.
- **The page is `"autosave"`.** `set_page(["plan"])` is *also* accepted and even
  flips `capture.exposure_mode` to `"autosave"`, which looks right but does not
  arm the runner. `"autorun"` is rejected outright with `109`.
- **`start_exposure` takes `["light"]` here.** Called bare it returns `0` and
  does nothing at all.
- **`gain: -10000`** is a sentinel meaning "use the camera's current gain".

`mount_end_action: "none"` stops the mount parking itself when the run ends.

Progress arrives as events: `Sequence` (`start` / `frame_start` /
`frame_complete`, carrying `frame` and `total_frame`), `Dither`, and `Settle`
(`dither_settling` → `complete`); on 4400, `GuidingDithered {dx,dy}` and
`SettleDone {Status:0}`. The runner has **no quality gate** — it will happily
shoot into cloud or a rooftop until `repeat` is exhausted.

Editing a sequence that has already run some frames returns
`224 cannot edit sequence unless reset the progress`; `clear_sequence` and
`reset_sequence_progress` clear it, but both return `206 capture is active`
while a run is in progress, so stop it first.

## Also confirmed live (firmware 43.97), for reference

Device-open (global): `open_camera` / `close_camera`, `open_focuser` /
`close_focuser`, `open_wheel` / `close_wheel`. Reads: `get_device_state`,
`get_camera_state`, `get_camera_info`, `get_focuser_state/info/position/setting`,
`get_wheel_state/setting/position`, `get_control_value`, `get_controls`,
`get_disk_volume`, `get_image_save_path`, `get_img_name_field`,
`get_stack_info/setting`, `get_sequence_setting`, `get_plan`, `get_focal_length`,
`get_app_setting`, `get_setting`, `get_test_setting`, `get_svr_version`,
`pi_get_info`, `pi_station_state`. Exposure: `set_exposure`, `start_exposure`,
`stop_exposure`.

## Operational traps (learned on sky, 2026-08-10/15)

Each of these presents as a hardware fault and isn't one.

### Coordinates are JNow, not J2000

`scope_goto`, `start_auto_goto` and the plate solver all work in **apparent
coordinates of date**. The system is self-consistent, so feeding J2000 converges
beautifully onto the wrong patch of sky with nothing appearing wrong — verified
by sending Arcturus's J2000 position, watching `start_auto_goto` report the field
centre as exactly those numbers, and finding no Arcturus anywhere in the 30'x17'
frame. The JNow position put it 1.6' from centre. In 2026 the offset is ~18' in
RA, comparable to the whole field at 1260 mm.

`get_last_solve_result.ra_dec` is JNow too, and **the Air auto-syncs the mount to
every successful solve** — so a pointing error computed right after a solve reads
zero by construction.

### Exposures over ~10 s need a keepalive

The Air drops an **idle 4700 socket after ~15 s**. Fire `start_exposure`, wait in
silence, and you are disconnected before the `Exposure complete` event — the
failure looks like a camera problem but the camera is fine. Poke any cheap read
(`get_camera_state`) every ~4 s while waiting. Fixed in `starhunt.py`'s
`Camera.grab`; 30 s and 60 s subs verified.

### Polar alignment needs `set_page`

`start_polar_align` takes a **bare object** (list-wrapping → `107`), and returns
`300 internal error` until you first call `set_page(["pa"])`. Results come from
`get_polar_axis` → `{centre_deg:[az,alt], polar_deg:[az,alt], dist_arcsec, …}`.

Two live caveats: pointing near the pole (Dec +79) makes the RA-rotation geometry
degenerate and the solution **diverges** across successive reads (3.2° → 7.4° →
11.4°) — use Dec 30–50°. And a stall showing `pa.mount_move_ok: false` with no
exposures at all is still unexplained; check the **site location** first (below),
since at latitude 0 the routine's geometry is nonsense.

### `guide` wants a PHD2 settle object

```
guide  [{"pixels": 1.5, "time": 10, "timeout": 60}]
```
The app's `DitherConfig` (`enable`/`ra_only`/`amount`/`settle_arcsec`/…) is a
different bean and is rejected `102`.

### Recovering from an Air restart, without the phone

A restart drops every device and **silently wipes the site location**:

```
4400: set_connected([{"mount": true}])            # mount
4700: open_camera(["ZWO ASI585MC Air"])           # main cam, by NAME
4700: open_focuser([0])                           # by ID; the name gives 524
4400: set_camera_idx([0]) ; set_connected([{"camera": true}])   # retry: 207 first try
```

**The site location is the trap inside the trap.** Both a restart *and* attaching
the mount zero it to `[0.0, -0.0]`, and the wipe lands **asynchronously, after
`set_connected` has already returned** — so writing the location immediately just
gets overwritten. Worse, `scope_set_location([lat, lon])` can read back correctly
from both `scope_get_location` and `scope_get_info.Lat` and still revert ~20–25 s
later (measured 2026-08-15).

The only reliable pattern is write-then-verify, repeated while the attach
settles: `mount.py connect --lat <lat> --lon <lon>` does exactly that, and
`mount.py location` re-checks it. Setting it from the app is durable.

Sanity check: with Dec `+90`, `scope_get_info.Alt` must equal your latitude. At
lat 0 it reads `0.00` and every alt/az, horizon check and polar-align result is
quietly wrong — this is a prime suspect for the `mount_move_ok: false` stall.

### Error codes as a param-shape oracle

| Code | Meaning | What to change |
|---|---|---|
| `103` | method not found | wrong name, or wrong port (mount/guide live on 4400) |
| `102` | invalid params | wrapping is right, **fields** are wrong |
| `107` | expected object param | wrapping is wrong — try a bare object |
| `104` / `105` / `108` | expected int / string / float | scalar param, type named for you |

`102` vs `107` identifies a bean fast: `107` means stop list-wrapping, `102`
means keep the list and fix the keys.

Codes seen since: `109` unexpected param (a valid enum, wrong member — e.g.
`set_page(["autorun"])`), `224` cannot edit sequence unless reset the progress,
`206` capture is active, `207` fail to operate (often just needs a retry, or a
stuck exposure cleared), `238` exposure failed, `252` auto goto failed,
`305` could not set lock position, `312` cannot select if equipment are
connected, `318` device not connected.

### Probing `set_*` can apply a value

The oracle is safe for `get_*`. It is **not** safe for setters: a `set_*` probed
with junk can accept it and write. `scope_set_location(["__probe__"])` returns an
error and still wipes the site to `[0, 0]`. `set_plan([])` returns `0` outright.
Probe setters by name only if you are willing to have them take effect, and
snapshot the current value first.

### `start_*` methods FIRE when probed with no params

Worse than the setters, because some simply run. `start_auto_focuse` with an
empty param list returns `0` and **starts an autofocus** — it stopped an
exposure mid-frame and dropped guiding. Probe `start_*` names only when the rig
is idle and you would accept them running.

### `start_auto_focuse` leaves the camera faulted

After it runs, every subsequent exposure returns
`Exposure {state: "fail", error: "exposure failed", code: 238}` — indefinitely,
while `get_camera_state` still cheerfully reports `idle` with the camera open.
The fix is `close_camera` then `open_camera([name])`; nothing less clears it.

Related: with no params it does not sweep. It returns in ~16 s having taken a
single exposure, emits an `AutoFocus` event whose `result` is byte-identical
across runs (`x_scale`, `y_scale` unchanged), and moves the focuser to a
remembered position. Treat a bare `start_auto_focuse` as "replay the last
result", not "measure focus".

### `AutoGotoStep` failing with 252 on the first attempt is normal

`{"Event":"AutoGotoStep","state":"fail","count":1,"error":"auto goto failed",
"code":252}` is what a *healthy* solve-and-centre looks like: the routine
retries and completes on attempt 2. The app's own runs do this too. Do not
treat a single 252 as fatal — and note the target object keeps the stale
`code: 252` afterwards, so reading it later is misleading.

### The first `scope_get_info` on a fresh 4400 socket can be garbage

Immediately after connecting, the first read sometimes returns plausible but
wrong values — an RA about 4° off in one measured case, which then got written
into a FITS header, and a `tracking: false` while the mount was demonstrably
tracking. Subsequent reads on the same socket are correct. Discard the first
read, or re-read before recording anything.

### `SettleDone` with `TotalFrames` at the timeout is a failed settle

`{"Event":"SettleDone","Status":1,...}` means the settle never converged;
`Status: 0` is success. The frame count is the giveaway: at 4 s guide exposures
a timed-out settle reports `TotalFrames: 16` — the full 60 s
`settle_timeout_sec` — whereas a real settle completes in about 6. At 3 s
exposures the same timeout reads as 20-21 frames, so compare against the
exposure length, not the number.

A settle that keeps timing out is usually one of: a duration cap truncating
corrections (see below), a `settle_arcsec` tighter than the guiding can
achieve, or a stale calibration used at a different declination.

### Guide tuning lives in `set_setting` on 4400, not in the algo params

`get_algo_param_names(["dec"])` returns only
`["minMove", "fastSwitch", "aggression"]` (and `hysteresis` in place of
`fastSwitch` for `ra`) — the max pulse durations are **not** there. They are in
a separate write-only bag:

```
set_setting([{"max_dec_dur": 1500}])     # ms; default 500
set_setting([{"max_ra_dur":  1500}])
set_setting([{"focallength": 1263}])     # mm, the guider's own
```

`get_setting` on 4400 returns `{}` regardless of arguments, so there is no
read-back — verify by behaviour. At the 500 ms default with a 0.25x guide rate
a pulse can only deliver 1.88", so corrections needing more were truncated,
leaving a one-sided Dec bias and an `Alert` code `415` about Max Dec Duration.
Guide rate and duration cap interact: a slow rate demands a generous cap.

### Dithering only works from the sequence runner

Two ways to dither from outside it, both no-ops (measured by cross-correlating
consecutive subs — 0.0 px field shift, against a method validated on injected
shifts):

- `dither(<float>)` on 4400 is accepted and does nothing.
- Offsetting the lock with `set_lock_position` is reverted within seconds — the
  Air snaps it straight back.

The runner's own dither does work: `Dither` on 4700, then `LockPositionSet` and
`GuidingDithered {dx,dy}` on 4400. If you need dithering, drive an autorun.

## Extraction recipe

```bash
cd ASIAIR_3.0.0_APKPure/base-apk           # unpacked xapk → base apk
grep -aoE '[a-z][a-z0-9_]{3,40}' classes6.dex | sort -u   # all string tokens
# classes6.dex is the one containing test_connection / verify_client
```
