"""Forget the Pi's saved WiFi networks and bring up the setup hotspot.

Triggered from the app (Device Information -> "Forget WiFi Network"): the app
writes device_command/{serial} in Firestore, alisto_main.py's
device_command_watcher_loop() listens to that one document and calls into here.

What "forget" does, in order:
  1. Preflight: the hotspot/setup scripts must exist and the fallback service
     must be installed. If anything is missing NOTHING is deleted -- forgetting
     the WiFi with no way to set a new one up would strand the device.
  2. Delete every saved WiFi profile in NetworkManager (except the setup
     hotspot's own profile).
  3. Restart alisto-wifi-autofallback.service. With no WiFi left, it sees no
     internet after its short grace period and starts the "Alisto-Setup"
     hotspot plus the credential server -- the same one the app's "Change WiFi
     Network" flow already talks to (10.42.0.1:5000/connect-wifi).

The emergency path (voice/button -> SMS over the SIM) never uses WiFi, so it
keeps working throughout.

Pure helpers (parsing, "is this command still valid") live here too so they can
be tested without hardware.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone

# The setup hotspot's own NetworkManager profile(s) -- never deleted.
HOTSPOT_PROFILES = {"Hotspot", "Alisto-Setup"}
FALLBACK_SERVICE = "alisto-wifi-autofallback.service"
REQUIRED_FILES = (
    "/home/alisto/wifi_auto_fallback.py",
    "/home/alisto/start_setup_hotspot.sh",
    "/home/alisto/wifi_setup_server.py",
)
STATE_PATH = "/home/alisto/.last_device_command.json"
MAX_COMMAND_AGE_S = 10 * 60  # an old command must never fire later (e.g. after
# the Pi was offline for days)


# ---------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------

def parse_wifi_profiles(nmcli_output):
    """Parses `nmcli -t -f NAME,UUID,TYPE connection show`. Terse mode
    separates fields with ':' and writes a ':' inside a value as '\\:'.
    Returns [(name, uuid)] for saved WiFi profiles, skipping the setup
    hotspot's own profile."""
    profiles = []
    for line in nmcli_output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"(?<!\\):", line)
        if len(parts) != 3:
            continue
        name = parts[0].replace("\\:", ":").replace("\\\\", "\\")
        uuid, ctype = parts[1], parts[2]
        if ctype not in ("802-11-wireless", "wifi"):
            continue
        if name in HOTSPOT_PROFILES:
            continue
        profiles.append((name, uuid))
    return profiles


def command_is_actionable(data, now, last_request_id, max_age_s=MAX_COMMAND_AGE_S):
    """Should this device_command document be acted on?
    Returns (ok, reason). `now` is a timezone-aware datetime."""
    if not isinstance(data, dict):
        return False, "not a command"
    if data.get("action") != "forget_wifi":
        return False, "unknown action"
    request_id = data.get("request_id")
    if not request_id:
        return False, "no request_id"
    if request_id == last_request_id:
        return False, "already handled"
    at = data.get("requested_at")
    if not isinstance(at, datetime):
        return False, "no timestamp"
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    age = (now - at).total_seconds()
    if age > max_age_s:
        return False, f"stale ({int(age)}s old)"
    if age < -120:
        return False, "timestamp in the future"
    return True, "ok"


def load_last_request_id(path=STATE_PATH):
    try:
        with open(path) as f:
            return json.load(f).get("request_id")
    except Exception:
        return None


def save_last_request_id(request_id, path=STATE_PATH):
    try:
        with open(path, "w") as f:
            json.dump({"request_id": request_id}, f)
    except Exception as e:
        print(f"wifi_forget: could not save last request id: {e}")


# ---------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def forget_saved_wifi(run=_run, exists=os.path.exists):
    """Returns {"ok": bool, "deleted": [names], "failed": [names],
    "hotspot_started": bool, "error": str|None}. `run` and `exists` are
    injectable for tests. Needs passwordless sudo (the service runs as the
    unprivileged "alisto" user); `sudo -n` makes a missing permission fail
    at once instead of hanging on a password prompt."""
    result = {"ok": False, "deleted": [], "failed": [],
              "hotspot_started": False, "error": None}

    # 1. Preflight -- never strand the device without a setup path.
    missing = [p for p in REQUIRED_FILES if not exists(p)]
    if missing:
        result["error"] = f"setup scripts missing: {', '.join(missing)}"
        return result
    unit = run(["systemctl", "cat", FALLBACK_SERVICE])
    if unit.returncode != 0:
        result["error"] = f"{FALLBACK_SERVICE} is not installed"
        return result

    # 2. Delete the saved WiFi profiles.
    listing = run(["sudo", "-n", "nmcli", "-t", "-f", "NAME,UUID,TYPE",
                   "connection", "show"])
    if listing.returncode != 0:
        result["error"] = f"could not list connections: {listing.stderr.strip()}"
        return result
    for name, uuid in parse_wifi_profiles(listing.stdout):
        r = run(["sudo", "-n", "nmcli", "connection", "delete", "uuid", uuid])
        (result["deleted"] if r.returncode == 0 else result["failed"]).append(name)

    # 3. Bring up the setup hotspot (no wait: the service has its own grace period).
    started = run(["sudo", "-n", "systemctl", "restart", "--no-block", FALLBACK_SERVICE])
    result["hotspot_started"] = started.returncode == 0
    if not result["hotspot_started"]:
        result["error"] = f"could not start {FALLBACK_SERVICE}: {started.stderr.strip()}"
    result["ok"] = result["hotspot_started"] and not result["failed"]
    return result
