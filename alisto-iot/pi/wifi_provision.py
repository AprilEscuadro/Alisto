"""Self-service WiFi setup, the way consumer IoT devices (smart bulbs,
Google Home, etc.) do it -- the customer has no keyboard/screen on the Pi
and can't SSH in, so the Pi has to make itself reachable first.

Flow:
  1. On every boot, check if the Pi already has working internet. If yes,
     do nothing and exit immediately -- this script is meant to be safe to
     run every boot without disturbing an already-configured device.
  2. If not connected: broadcast a temporary open WiFi hotspot
     (ALISTO-Setup-XXXX, last 4 hex digits of the Pi's own WiFi MAC so
     multiple units in the field don't collide) and run a tiny local web
     server on it.
  3. The Alisto app -- once the customer's phone joins that hotspot --
     POSTs the customer's real home WiFi name/password to this server.
  4. This applies those credentials via NetworkManager (nmcli, the default
     network stack on Raspberry Pi OS Bookworm), tears the hotspot down on
     success, and exits. The rest of the app's registration flow continues
     normally now that the Pi has real internet.

This is entirely independent of the core emergency system (voice/button ->
SMS): that path never touches WiFi at all and keeps working with or
without any of this ever succeeding.
"""

import re
import socket
import subprocess
import time

from flask import Flask, jsonify, request

WLAN_IFACE = "wlan0"
HOTSPOT_PASSWORD = None  # None = open network -- simplest for a customer
                          # to join during first-time setup; the server it
                          # exposes only accepts WiFi credentials, nothing
                          # sensitive reachable on it.
SERVER_PORT = 5000
CONNECT_TIMEOUT_S = 20      # how long to give nmcli to actually join the
                            # customer's real network before reporting failure
INTERNET_CHECK_HOST = ("8.8.8.8", 53)
INTERNET_CHECK_TIMEOUT_S = 3

app = Flask(__name__)


def has_internet():
    try:
        socket.setdefaulttimeout(INTERNET_CHECK_TIMEOUT_S)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(INTERNET_CHECK_HOST)
        return True
    except OSError:
        return False


def _run(cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def get_hotspot_ssid():
    """ALISTO-Setup-XXXX, suffixed with the last 4 hex digits of this
    unit's own WiFi MAC address so multiple devices in the field (a
    technician setting up several at once, or neighboring units) don't
    broadcast the exact same hotspot name."""
    try:
        result = _run(["cat", f"/sys/class/net/{WLAN_IFACE}/address"])
        mac = result.stdout.strip().replace(":", "").upper()
        suffix = mac[-4:] if len(mac) >= 4 else "0000"
    except Exception:
        suffix = "0000"
    return f"ALISTO-Setup-{suffix}"


def start_hotspot(ssid):
    cmd = ["sudo", "nmcli", "device", "wifi", "hotspot", "ifname", WLAN_IFACE, "ssid", ssid]
    if HOTSPOT_PASSWORD:
        cmd += ["password", HOTSPOT_PASSWORD]
    result = _run(cmd)
    if result.returncode != 0:
        print(f"WiFi setup: failed to start hotspot: {result.stderr.strip()}")
        return False
    print(f"WiFi setup: hotspot '{ssid}' broadcasting on {WLAN_IFACE}")
    return True


def stop_hotspot():
    # The hotspot lives as a NetworkManager connection profile named after
    # its own SSID by default ("Hotspot") -- bring the interface back down
    # from AP mode so it's free to join a real network.
    _run(["sudo", "nmcli", "connection", "down", "Hotspot"])


def connect_to_wifi(ssid, password):
    stop_hotspot()
    time.sleep(2)  # let the interface settle out of AP mode
    cmd = ["sudo", "nmcli", "device", "wifi", "connect", ssid, "ifname", WLAN_IFACE]
    if password:
        cmd += ["password", password]
    result = _run(cmd, timeout=CONNECT_TIMEOUT_S)
    if result.returncode != 0:
        print(f"WiFi setup: failed to connect to '{ssid}': {result.stderr.strip()}")
        return False
    # nmcli reporting success doesn't always mean real internet access
    # (e.g. wrong password on some routers still "connects" locally) --
    # confirm before declaring victory.
    for _ in range(10):
        if has_internet():
            print(f"WiFi setup: connected to '{ssid}' with working internet.")
            return True
        time.sleep(1)
    print(f"WiFi setup: joined '{ssid}' but no internet after waiting.")
    return False


_connect_result = {"done": False, "ok": False}


@app.route("/wifi", methods=["POST"])
def receive_wifi_credentials():
    data = request.get_json(silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip()

    if not ssid:
        return jsonify({"ok": False, "error": "ssid is required"}), 400
    if not re.match(r"^[\x20-\x7e]{1,32}$", ssid):
        return jsonify({"ok": False, "error": "invalid ssid"}), 400

    print(f"WiFi setup: received credentials for '{ssid}'")
    ok = connect_to_wifi(ssid, password)
    _connect_result["done"] = True
    _connect_result["ok"] = ok

    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "could not connect -- check the password and try again"}), 502


@app.route("/ping", methods=["GET"])
def ping():
    # Lets the app confirm it's actually talking to an Alisto device before
    # showing the WiFi form, rather than just assuming.
    return jsonify({"device": "alisto", "status": "awaiting_wifi"})


def main():
    if has_internet():
        print("WiFi setup: already have internet -- nothing to do.")
        return

    ssid = get_hotspot_ssid()
    if not start_hotspot(ssid):
        print("WiFi setup: could not start hotspot -- giving up for this boot.")
        return

    print(f"WiFi setup: waiting for the app to POST credentials to :{SERVER_PORT}/wifi ...")
    app.run(host="0.0.0.0", port=SERVER_PORT)


if __name__ == "__main__":
    main()
