# Controlling a ZWO ASIAIR over the network

A small, dependency-free toolkit for driving a ZWO **ASIAIR** from any computer —
whether a standalone ASIAIR (Mini / Plus / Pro) or a camera with ASIAIR built in
(the ASI585MC Air, ASI2600MC Air and their siblings). It talks to the Air the
same way the ASIAIR phone app does. Everything is stdlib-only — `python3 <script>`
just works — with one optional exception noted below.

The Air exposes several network services; this toolkit uses the ones the app
actually relies on. Two need no authentication; the third — the main camera
channel — uses ZWO's RSA challenge handshake.

## The services this uses

| Port | Auth | What lives here |
|---|---|---|
| UDP **32227** | none | ASCOM Alpaca discovery (broadcast to find the Air) |
| Alpaca **TCP** | none | ASCOM Alpaca REST — **camera control**. Port is discovered (it's `32323` on the built-in Air units) |
| **4700** | RSA handshake | Native ASIAIR RPC — camera, focuser, filter wheel, plate solve, sequences, settings, telemetry |
| **4400** | none | Native ASIAIR RPC — **mount + guiding** (`scope_*` and guide commands) |
| **4800** | none | MainImageSocket — pull the last captured frame down natively (`main_image.py`) |
| **4500** | none | Guide image stream — frames produced by `loop` on 4400 |

`4700` and `4400` are ASIAIR firmware ports and are the same across the product
line; the Alpaca port can vary, so discover it. The Air may not answer ICMP (mine
doesn't), so don't rely on `ping` — use Alpaca discovery or a TCP scan.

## 1. Find your Air

```bash
python3 discover.py                    # Alpaca UDP + mDNS + TCP sweep of your subnet
python3 discover.py --subnet 10.0.0    # if you joined the Air's own Wi-Fi AP
python3 discover.py --host <air-ip>    # fingerprint a known address
```

Note the Air's IP, and its Alpaca port if you'll use Alpaca. Substitute your IP
for `<air-ip>` throughout — or set it once and drop `--host` from every command:

```bash
export ASIAIR_HOST=<air-ip>            # every script reads this; --host overrides it
```

## 2. Camera — ASCOM Alpaca (works now, no key)

Alpaca is ZWO's documented REST API, so it won't break on a firmware bump. It
covers the **camera only**: connect, gain/offset, binning, subframe, expose, read
pixels, and the cooler on cooled cameras. Each connected camera is its own Alpaca
device — on the built-in units that's the main sensor plus the onboard guide
sensor; on a standalone ASIAIR it's whatever cameras you've plugged in.

```bash
python3 alpaca.py --host <air-ip> info                         # devices + camera info
python3 alpaca.py --host <air-ip> get camera 0 ccdtemperature
python3 alpaca.py --host <air-ip> put camera 0 gain Gain=252
python3 alpaca.py --host <air-ip> put camera 0 setccdtemperature SetCCDTemperature=-10
python3 alpaca.py --host <air-ip> put camera 0 cooleron CoolerOn=True
python3 alpaca.py --host <air-ip> expose 5 --gain 252 --out frame.json
```

`alpaca.py` defaults to port `32323` (the built-in Air units); pass `--port` for a
standalone ASIAIR, or use the port `discover.py` reported. `imagearray` returns
JSON, so a full frame is millions of numbers and is slow — use binning or a
subframe for anything interactive; for real captures the driver also serves the
binary `imagebytes` form.

## Planetary video — use the Air's own recorder

The firmware records AVI to eMMC and supports a readout ROI. The commands are
`start_record_avi` / `stop_record_avi` (**no parameters**) and `set_subframe` /
`get_subframe` (`{x, y, width, height}` in sensor pixels), on page `"video"`.

```bash
python3 video.py --host <air-ip> --key embedded_key.pem \
    --seconds 30 --exposure-ms 8 --gain 250 --roi 800x800
```

A small ROI is the whole point: `set_subframe` crops the **readout**, so the
frames are full sensor resolution *and* fast, straight to eMMC. Pull the AVI
afterwards from the SMB share (`//<air-ip>/EMMC Images`).

Also on the same page: `start_planet_stack` / `stop_planet_stack` (on-device
planetary stacking) and `set_rtmp_config` + `start_avi_rtmp` (live streaming).
Progress arrives as `AviRecord` events carrying
`{is_working, lapse_sec, fps, write_file_fps}`.

**Do not probe for method names.** These were all missed by probing —
`start_video`, `start_recording`, `start_video_record`, `set_roi` all return
`103 method not found`, which looks exactly like the feature being absent. The
app ships its own command table; `CMD_METHODS.tsv` in this repo is that table
(289 commands, extracted from `com.zwoasi.kit.cmd.CmdMethod`). A `103` only
tells you your guess was wrong.

`video_preview_ser.py` (via `video.py --preview-ser`) is the fallback that
records the 4800 preview to a SER client-side. Its ceiling is low — the preview
is a fixed ~1472 px subsample of the sensor at ~1 fps on 2.4 GHz — so prefer
the native recorder.

`focus_monitor.py` is the focus companion: a passive live focus score
(flux-weighted RMS radius, which unlike peak brightness does not lie when you
change exposure or clip the core) that runs alongside the ASIAIR app.

```bash
python3 focus_monitor.py --host <air-ip> --key embedded_key.pem --arcsec-per-px 2.462
```

## 3. Native RPC on 4700 — needs the RSA key

Port 4700 is the channel the app uses for the main camera, focuser, filter wheel,
plate solving, sequences, and all the settings and telemetry. Firmware 7.18+ gates
it: on connect it answers only `test_connection` and `get_verify_str`, and
**silently drops** everything else until the client authenticates.

The handshake: `get_verify_str` returns a challenge; you sign it **RSA PKCS#1 v1.5
/ SHA-1** and call `verify_client([signature, challenge])` (two positional
strings), then `pi_is_verified` reads true. Verification is **per connection**.

The catch is the key: signing needs an RSA private key that ZWO ships inside the
ASIAIR app, not on the device. `seestar_alp` implements the same handshake but
ships no key — each user supplies one extracted from the app they own, which ZWO's
own config frames under the DMCA interoperability exemption (17 U.S.C. § 1201(f)).
It lives as a PEM blob in the app's bundled `libopenssllib.so`; `extract_key.py`
pulls it out of an APK/XAPK for you.

Grab the ASIAIR app package first. If you don't have the app installed on an
Android device to pull the APK from, a mirror such as
[apkpure.com](https://apkpure.com/asiair/com.zwoasi.asiair) serves it as an
`.xapk` bundle — download that and point the script at it:

```bash
python3 extract_key.py ASIAIR_x.y.z.xapk        # -> embedded_key.pem (git-ignored)
```

Then use it (the key stays local — `embedded_key.pem` is git-ignored):

```bash
python3 handshake.py --host <air-ip> --key embedded_key.pem      # prove the handshake
python3 air_rpc.py  --host <air-ip> --key embedded_key.pem probe # see what's implemented
python3 air_rpc.py  --host <air-ip> --key embedded_key.pem call get_device_state
python3 air_rpc.py  --host <air-ip> --key embedded_key.pem console
```

Signing needs the `cryptography` package — the only non-stdlib dependency, used
only for 4700 auth, and imported lazily so everything else stays dependency-free.

## 4. Mount + guiding on 4400 — no key

Mount and guiding are a **separate service on port 4400 with no authentication**.
This is easy to miss: mount methods return `103 method-not-found` on 4700 not
because they're gated, but because they're on the wrong port. Connect to 4400 and
call directly. Coordinates are **RA in hours, Dec/Alt/Az in degrees**.

```bash
python3 mount.py --host <air-ip> info                  # model / firmware / driver
python3 mount.py --host <air-ip> coord                 # live RA/Dec/Alt/Az + tracking
python3 mount.py --host <air-ip> track on              # sidereal tracking
python3 mount.py --host <air-ip> goto 20.016 35.365    # slew to RA(h) Dec(deg)
python3 mount.py --host <air-ip> sync 20.016 35.365
python3 mount.py --host <air-ip> park

python3 guide.py --host <air-ip> state                 # Idle / Looping / Guiding / …
python3 guide.py --host <air-ip> connect on
python3 guide.py --host <air-ip> expose 1000
python3 guide.py --host <air-ip> loop
```

Plate-solve-and-center uses both channels — the native `start_auto_goto` on 4700
(needs the key) exposes, solves, and nudges the mount until centered:

```bash
python3 solve_center.py --host <air-ip> 20.016 35.365  # refuses below-horizon targets
```

## 5. Power telemetry — is it dead, or just flat?

A battery running out looks exactly like a crashed Air from the network side:
no reply to ping, ARP `(incomplete)`, every port closed, subnet sweep empty. Two
signals precede it, and `telemetry.py` appends both to a CSV so the failure can
be seen coming — and so the moment of loss is itself recorded, as a row with an
empty voltage and `note=unreachable`.

```bash
python3 telemetry.py --host <air-ip>                       # voltage only, 60s rows
python3 telemetry.py --host <air-ip> --key embedded_key.pem  # + undervolt/temp
python3 telemetry.py --host <air-ip> --once                # single reading
```

`scope_get_info.input_voltage` (4400, millivolts) is the mount's supply;
`PiStatus.is_undervolt` (4700, needs the key) is the Air complaining about its
own. Whether voltage is a usable fuel gauge depends on the supply: a regulated
output holds flat and then collapses, giving little notice, while an unregulated
one sags gradually. One full run-to-flat tells you which you have.

## 6. Frames are not saved unless you run a sequence

Worth knowing before a long night: everything driven through the **preview** page
is transient. `start_exposure` there exposes, and the frame can be pulled down
off port 4800 —

```bash
python3 main_image.py --host <air-ip> --out frame    # last captured frame
```

— but the Air never writes it to its own storage, and **no dithering happens**.
Both come from the Air's sequence runner, which is a different page and a
different call:

```
set_page(["autosave"]) -> set_sequence([...]) -> set_sequence_setting([...])
-> start_exposure(["light"])
```

That is the only path that saves to eMMC and dithers between frames. The full
shape, and the four details that each break it silently, are in
[`RPC_METHODS.md`](RPC_METHODS.md) under *Autorun*.

## 7. A camera the Air cannot see — Canon CCAPI over Wi-Fi

ZWO removed DSLR support from the ASIAIR and never had mirrorless, so a Canon
body is not a device the Air can be asked about. It is a **second imager on the
same network**, driven directly. Everything above still applies unchanged — the
mount, guiding, plate solving and telemetry do not care which sensor is taking
the picture. The one part that does not carry over is the Air's own sequence
runner (§6): it only drives ZWO sensors, so the exposure loop and the dithering
have to be run client-side instead.

`ccapi.py` speaks Canon's **Camera Control API** — plain HTTP, JSON in and out,
no authentication, no SDK, stdlib only.

```bash
python3 ccapi.py discover                        # sweep the subnet
python3 ccapi.py --host <cam-ip> probe           # endpoint map + capabilities
python3 ccapi.py --host <cam-ip> info
python3 ccapi.py --host <cam-ip> settings
python3 ccapi.py --host <cam-ip> set iso 1600
python3 ccapi.py --host <cam-ip> shoot --download frames/
python3 ccapi.py --host <cam-ip> bulb 120 --download frames/
```

**Endpoints are discovered, not hardcoded.** `GET /ccapi` returns the complete
endpoint map for the connected body and firmware. Which endpoints exist — and
which API version each lives under — varies by model: the same call can be
`ver100` on one body and `ver110` on another. The client reads the map on
connect and resolves every request through it, keyed on the path suffix. So
asking for something the body does not implement fails with a clear message
instead of a bare 404, and `probe` prints what this particular camera can do.

**CCAPI ships disabled**, behind a free developer registration: update the
camera to the latest firmware, register at Canon's developer community, run
their desktop activation tool to write an *enabler* file to the SD card, then
connect the camera from the CCAPI entry that appears in its Wi-Fi menu. The
camera displays its own URL once active. Nothing here can do that step for you.

**Exposures past 30s need bulb**, which is `shutterbutton/manual` — full_press,
wait, release — and needs the mode dial physically on M with `tv` set to bulb.
Whether a body advertises that endpoint is exactly what `probe` answers; `bulb`
checks for it, and preflights the dial and `tv`, rather than half-working. Bulb
timing is host-side, so it carries network jitter — tens of milliseconds, which
is irrelevant against a 120s sub. Under 30s, set a real `tv` and use `shoot`.

A note on Wi-Fi bands: the EOS R50 is 2.4 GHz-only (802.11b/g/n), which is the
same constraint the Air has — so both land on the same SSID and no extra network
plumbing is needed. Set the camera's auto power off to **Disable** first. A body
that sleeps drops its Wi-Fi association, and from the host side that is
indistinguishable from a crash.

## 8. Logging — a line a second through anything slow

Every tool in here talks to hardware over Wi-Fi, and the failures all look the
same from the outside: **a long silence**. A goto that is still slewing, a
socket the Air dropped, a camera another client is holding, a battery that just
died — all of them present as nothing happening. So nothing here is allowed to
go quiet. Any operation that can take more than a second reports itself **every
second while it runs**, with live values, through `airlog.py`.

```
00:25:15  +3.0s info  mount:  waiting for ScopeGoto (timeout 120s, keepalive every 5s)
00:25:17  +5.0s info  mount:  ... ScopeGoto  2.0s  working  RA=20.0200 Dec=35.1000  1 event(s)
00:25:18  +6.0s info  mount:  ... ScopeGoto  3.0s  working  RA=20.0400 Dec=35.2000  2 event(s)
00:25:24 +12.2s info  mount:  ScopeGoto complete after 9.2s (9 progress event(s))
```

The progress line comes from a background thread, deliberately: most of these
waits are blocked in `socket.recv` or `time.sleep`, where the calling code
cannot emit anything at all. Nothing prints until an operation has actually
been slow (1 s), so the fast path stays quiet and **every tick line in the log
means something really did take a second**.

Log output goes to **stderr**, so `mount.py coord > coords.json` still works.

| Flag | Effect |
|---|---|
| *(default)* | Per-second progress, state changes, warnings, errors. |
| `-v` | Adds every RPC call, every frame, every retry and reconnect. |
| `-vv` | Adds the wire: request and reply bodies. |
| `-q` | Warnings and errors only — silences the per-second ticks. |
| `--log-file PATH` | Append the full log to a file as well as stderr. |

`AIRLOG=debug` and `AIRLOG_FILE=path` set the same things from the environment,
which is what you want for an unattended overnight run. The flags work on either
side of a subcommand: `mount.py -v goto ...` and `mount.py goto ... -v` both
parse.

What is instrumented, roughly: every RPC call on 4400/4700 that outlives a
second; slews and homing, with live RA/Dec; the site-location hold; focuser
moves, with the position counting down; frame downloads on 4800, with
throughput; exposures, counting the shutter and then the readout separately;
guide calibration, by state; the pure-Python pixel work in `focus.py` and
`starhunt.py`, by row; subnet sweeps and mDNS browses, by host; Canon bulb
exposures, counting the open shutter down; and CCAPI and Alpaca HTTP requests.

## 9. Focus frames are written as they are taken

`focus.py` commits **every frame to disk at the step**, not at the end of the
run. Each step writes three files immediately:

```
focus-frames/20260821-003119/
  step007.raw    the 16-bit row-major buffer, exactly as measured
  step007.json   frame geometry, focuser position, peak, star width, reject reason
  step007.png    a 240x240 closeup of the tracked star
```

This used to accumulate frame buffers in memory and render them after the
sweep, which was wrong twice over. It held ~75 MB of buffers for no benefit,
and — the real problem — it produced **nothing at all** when a run did not
reach the end. A sweep that lost the star at step 6, or that you killed, is
precisely the sweep whose frames you want, and those runs left an empty
directory behind. Frames rejected by the identity lock are written too, with
the reason in the filename, because a rejected frame is the most useful thing
in the directory.

Verified by SIGKILL mid-sweep: three complete steps, raw buffers intact.

`--images DIR` picks the directory, `--no-raw` keeps the PNGs but drops the
2.4 MB raw dumps, `--no-images` turns it off entirely.

## Files

| File | What it does |
|---|---|
| `discover.py` | Find the Air: Alpaca UDP discovery, mDNS, TCP sweep. |
| `alpaca.py` | ASCOM Alpaca camera client + CLI; importable as `from alpaca import Alpaca`. |
| `air_rpc.py` | Native 4700 RPC: `probe`, `call`, `console`, `listen`. `--key` runs the RSA handshake. |
| `mount.py` | Mount control on 4400: `info`, `coord`, `track`, `goto`, `sync`, `park`. |
| `guide.py` | Guiding on 4400: `state`, `connect`, `expose`, `loop`, `start`, `stop`. |
| `solve_center.py` | Plate-solve-and-center via native `start_auto_goto` (horizon-guarded). |
| `telemetry.py` | Log supply voltage + the Air's undervolt flag to CSV, so a flat battery is distinguishable from a crash. |
| `main_image.py` | Native MainImageSocket client on 4800: pull the last captured frame. |
| `guide_image.py` | Watch the guide camera live on 4500, without disturbing the main camera. |
| `joystick.py` | Directional mount control on 4400 (`scope_move`) + slew-rate calibration. |
| `starhunt.py` | Joystick-driven star search, for when plate solving isn't available. |
| `demo_slew.py` | Small self-returning demonstration slew — a safe first move. |
| `extract_key.py` | Pull the RSA interop key out of an ASIAIR APK/XAPK into `embedded_key.pem`. |
| `handshake.py` | Standalone RSA handshake; proves it by reading `get_device_state`. |
| `find_methods.py` | Enumerate implemented RPC methods (silence / `103`-vs-reply oracle). |
| `smoke_test.py` | End-to-end Alpaca capture: connect, subframe, expose, read pixels. |
| `ccapi.py` | Canon CCAPI client: control an EOS body over Wi-Fi (`discover`, `probe`, `settings`, `shoot`, `bulb`). Endpoint map is discovered, not hardcoded. |
| `airlog.py` | Logging core: levelled logger + the per-second progress ticker every slow operation uses. |
| `RPC_METHODS.md` | Full method map for 4700 and 4400, extracted from the app. |

## Safety notes

- `air_rpc.py` keeps read-only methods (`PROBE_METHODS`) separate from
  state-changing ones (`WRITE_METHODS`); `probe` only ever sends the former.
- Mount `goto`/`sync`/`park` and guide calibration physically move the mount.
  `solve_center.py` refuses targets below the horizon — check before you slew.
- The RSA key is yours and stays local: `embedded_key.pem` is git-ignored, never
  commit it.

## License

Released under the [MIT License](LICENSE) © 2026 Mads Dørup.

The RSA-key handling is for interoperability with your own device, using an app
you are licensed to use, under the DMCA exemption at 17 U.S.C. § 1201(f). No key
material is included in this repository.
