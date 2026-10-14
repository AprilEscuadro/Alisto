"""Reads the SIM7600's built-in GNSS receiver over the same AT command port
already used for SMS (alisto_sms.py) -- no separate GPS module or ESP32
wiring needed, since this HAT has a dedicated GNSS antenna port (confirmed
working: real fix acquired at ~10.32N 123.90E, Cebu City).

Two things had to be true before a fix was possible, discovered by real
testing on the actual hardware:
  1. GPS must be started with AT+CGPS=1 -- this does NOT survive a reboot or
     power cycle, so it has to be re-sent every time the process starts, not
     just once ever.
  2. The antenna is an ACTIVE antenna (has its own built-in amplifier, needs
     2.7-5.0V fed onto the coax line to work at all) -- SIMCOM modules keep
     that power feed OFF by default. Without AT+CVAUXS=1 / AT+CVAUXV=3050,
     the antenna sits there deaf no matter how long you wait or how clear
     the sky is. This was the actual root cause of ~10 minutes of "no fix"
     during testing, not a wiring or antenna problem.

DESIGN NOTE (learned from a real failure): the first version of this module
queried the GPS live, in a thread, during the emergency's cancel countdown,
then send_sms() opened the same port right after. That consistently made
every SMS attempt fail with "+CMS ERROR: Unknown error" -- an identical
message sent moments later with the GPS step skipped went through
immediately. So a GPS query and an SMS send too close together in time on
this modem's single AT port is unreliable, even with each side properly
closing its own handle first.

Fix: GPS is never queried live during an emergency at all. A background
thread (see poll_loop(), started once in alisto_main.main()) polls the
receiver every POLL_INTERVAL_S and keeps the most recent fix in memory.
trigger_emergency() just reads that cached value (get_last_fix() -- plain,
instant, no serial I/O), so nothing GPS-related touches the SIM port
anywhere near an SMS send. port_lock additionally serializes the poll loop
against send_sms() at the thread level within this process, so even the
periodic background poll can never overlap a send in time.
"""

import contextlib
import json
import re
import threading
import time

import serial

SIM_PORT = "/dev/serial/by-id/usb-SimTech__Incorporated_SimTech__Incorporated_0123456789ABCDEF-if02-port0"
GPS_BAUD = 115200

# Auxiliary voltage the module feeds onto the GNSS antenna line to power its
# amplifier. 3050 mV sits comfortably inside the antenna's printed
# 2.7-5.0V spec.
AUX_VOLTAGE_MV = 3050

POLL_INTERVAL_S = 30     # how often the background loop refreshes the fix
FIX_MAX_AGE_S = 120      # a fix older than this is treated as "no fix" --
                          # better to omit location than send a stale one

# Held by every function that opens SIM_PORT (enable_gps, the poll loop) so
# an SMS send from alisto_main.py -- see its `with gps.port_lock:` around
# send_sms() -- can never run at the same moment. Both sides use serial.Serial
# with their own open/close, but the actual USB-CDC modem port has shown it
# needs real separation in time, not just separate Python file handles.
port_lock = threading.Lock()

_last_fix = None       # (lat, lon) or None
_last_fix_time = 0.0

# Last good fix, kept on disk so it survives a reboot -- the in-memory
# _last_fix above is gone the moment the service restarts, and a device
# that boots indoors may not see the sky for a long time. This device sits
# in one place (an elder's home), so even a days-old fix is usually still
# the right place; trigger_emergency() uses it as a labelled "last known"
# fallback instead of sending an SMS with no location at all.
LAST_FIX_PATH = "/home/alisto/.last_gps_fix.json"
LAST_KNOWN_MAX_AGE_S = 7 * 24 * 3600
# SD cards wear out with constant writes -- only re-save when the position
# actually moved, or the saved copy is getting old.
_SAVE_MIN_MOVE_DEG = 0.0005     # ~55 m
_SAVE_MIN_INTERVAL_S = 900
_persisted = None      # (lat, lon, wall_clock_ts) as loaded/last written

_CGPSINFO_RE = re.compile(
    r"\+CGPSINFO:\s*"
    r"([\d.]*),([NS]?),"      # lat, N/S
    r"([\d.]*),([EW]?),"      # lon, E/W
    r"([\d]*),([\d.]*),"      # date, utc time
    r"([\d.-]*),"             # altitude
)


def _send(ser, cmd, wait_s=1.0):
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode())
    time.sleep(wait_s)
    return ser.read(ser.in_waiting or 1).decode(errors="ignore")


@contextlib.contextmanager
def _open_port():
    """Opens SIM_PORT for GPS use, holding port_lock for the whole
    operation -- shared by the only two places that talk to the modem for
    GPS purposes (enable_gps, _query_once), which previously each
    duplicated this same open/lock/close boilerplate."""
    with port_lock:
        ser = serial.Serial(SIM_PORT, GPS_BAUD, timeout=1)
        try:
            yield ser
        finally:
            ser.close()


def enable_gps():
    """Turns on the GNSS engine and the active-antenna power feed. Call once
    at startup. Best-effort -- an emergency device shouldn't fail to start
    just because GPS setup hit a snag; get_last_fix() simply stays None if
    the receiver never actually came up."""
    try:
        with _open_port() as ser:
            _send(ser, "AT+CVAUXS=1")
            _send(ser, f"AT+CVAUXV={AUX_VOLTAGE_MV}")
            resp = _send(ser, "AT+CGPS=1")
            # "ERROR" here almost always just means GPS was already left
            # on from a previous run -- not a real failure, so don't
            # treat it as one (a reboot is the only thing that turns it
            # back off).
            if "OK" in resp or "ERROR" in resp:
                print("GPS: enabled (or already was).")
            else:
                print(f"GPS: unexpected response enabling: {resp!r}")
    except Exception as e:
        print(f"GPS: failed to enable ({e}) -- continuing without GPS")


def _dm_to_decimal(value, is_lat):
    """Converts SIM7600's DDMM.MMMMMM (lat) / DDDMM.MMMMMM (lon) format to
    plain decimal degrees for a Google Maps link."""
    deg_digits = 2 if is_lat else 3
    degrees = int(value[:deg_digits])
    minutes = float(value[deg_digits:])
    return degrees + minutes / 60.0


def _parse_cgpsinfo(raw):
    match = _CGPSINFO_RE.search(raw)
    if not match:
        return None
    lat_raw, ns, lon_raw, ew, *_ = match.groups()
    if not lat_raw or not lon_raw:
        return None  # empty fields -- no fix yet
    lat = _dm_to_decimal(lat_raw, is_lat=True)
    lon = _dm_to_decimal(lon_raw, is_lat=False)
    if ns == "S":
        lat = -lat
    if ew == "W":
        lon = -lon
    return (lat, lon)


def _query_once():
    """One open-query-close cycle against the modem. Only ever called from
    poll_loop() -- never from anything emergency-path-related."""
    try:
        with _open_port() as ser:
            raw = _send(ser, "AT+CGPSINFO", wait_s=1.0)
            return _parse_cgpsinfo(raw)
    except Exception as e:
        print(f"GPS: poll failed ({e})")
        return None


def _load_persisted():
    global _persisted
    try:
        with open(LAST_FIX_PATH) as f:
            d = json.load(f)
        _persisted = (float(d["lat"]), float(d["lon"]), float(d["ts"]))
    except (OSError, ValueError, KeyError):
        _persisted = None


def _maybe_save(fix):
    """Writes `fix` to LAST_FIX_PATH if it's new/moved/stale enough -- see
    _SAVE_MIN_MOVE_DEG. Best-effort; a failed write never affects polling."""
    global _persisted
    now = time.time()
    if _persisted is not None:
        lat, lon, ts = _persisted
        moved = max(abs(fix[0] - lat), abs(fix[1] - lon)) >= _SAVE_MIN_MOVE_DEG
        if not moved and now - ts < _SAVE_MIN_INTERVAL_S:
            return
    try:
        with open(LAST_FIX_PATH, "w") as f:
            json.dump({"lat": fix[0], "lon": fix[1], "ts": now}, f)
        _persisted = (fix[0], fix[1], now)
    except OSError as e:
        print(f"GPS: failed to save last fix ({e})")


_load_persisted()


def poll_loop():
    """Runs forever in its own daemon thread (started once from
    alisto_main.main()), refreshing the cached fix every POLL_INTERVAL_S.
    This is the ONLY thing that queries the GPS receiver -- an emergency
    just reads whatever's already cached via get_last_fix()."""
    global _last_fix, _last_fix_time
    while True:
        fix = _query_once()
        if fix:
            _last_fix = fix
            _last_fix_time = time.monotonic()
            _maybe_save(fix)
        time.sleep(POLL_INTERVAL_S)


def get_last_fix(max_age_s=FIX_MAX_AGE_S):
    """Instant, in-memory read -- no serial I/O, safe to call from anywhere
    including mid-emergency. Returns (lat, lon) or None if there's never
    been a fix, or the most recent one is older than max_age_s (better to
    omit the location than send a stale one)."""
    if _last_fix is None:
        return None
    if time.monotonic() - _last_fix_time > max_age_s:
        return None
    return _last_fix


def get_last_known(max_age_s=LAST_KNOWN_MAX_AGE_S):
    """(fix, age_seconds) for the most recent fix we have -- this process's
    own (age measured on the monotonic clock, accurate) or, after a
    restart, the one saved on disk (age from the wall clock, only
    approximate: the Pi has no working RTC battery, so offline the clock
    can be off). None if there's never been a fix or it's older than
    max_age_s. Meant for the "last known location" fallback when
    get_last_fix() has nothing fresh."""
    if _last_fix is not None:
        age = time.monotonic() - _last_fix_time
        return (_last_fix, age) if age <= max_age_s else None
    if _persisted is not None:
        lat, lon, ts = _persisted
        age = max(0.0, time.time() - ts)
        return ((lat, lon), age) if age <= max_age_s else None
    return None


def age_text(age_s):
    """'just now' / '12 min ago' / '3 hr ago' / '2 days ago'."""
    minutes = int(age_s // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hr ago"
    return f"{hours // 24} days ago"


def coords_text(fix):
    """Plain 'lat,lon' text -- NOT a clickable maps.google.com link. Real
    testing found Philippine carriers silently filter outbound SMS that
    contain a URL (anti-smishing filtering), returning a generic
    "+CMS ERROR: Unknown error" -- the exact same message succeeded
    immediately once the link was replaced with bare coordinates like this.
    Most phones' messaging apps still recognize and let you tap coordinate
    text, or it can be pasted into Maps manually."""
    lat, lon = fix
    return f"{lat:.6f},{lon:.6f}"
