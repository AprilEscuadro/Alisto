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
