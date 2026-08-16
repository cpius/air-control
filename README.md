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
| `joystick.py` | Directional mount control on 4400 (`scope_move`) + slew-rate calibration. |
| `starhunt.py` | Joystick-driven star search, for when plate solving isn't available. |
| `demo_slew.py` | Small self-returning demonstration slew — a safe first move. |
| `extract_key.py` | Pull the RSA interop key out of an ASIAIR APK/XAPK into `embedded_key.pem`. |
| `handshake.py` | Standalone RSA handshake; proves it by reading `get_device_state`. |
| `find_methods.py` | Enumerate implemented RPC methods (silence / `103`-vs-reply oracle). |
| `smoke_test.py` | End-to-end Alpaca capture: connect, subframe, expose, read pixels. |
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
