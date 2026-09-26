"""
wifi_setup_server.py

Runs on the Raspberry Pi. While the Pi is in "setup mode" (broadcasting its
own hotspot called "Alisto-Setup"), this small web server listens for the
phone app to send home WiFi credentials, then tells the Pi to connect to
that network using NetworkManager (nmcli).

Requirements on the Pi:
    sudo apt update
    sudo apt install network-manager
    pip3 install flask

Run it with:
    sudo python3 wifi_setup_server.py

(sudo is required because nmcli network changes need root privileges.)

CLAUDE'S PATCH #1: nmcli reporting success only means the Pi joined the WiFi
network locally -- it does NOT mean there's actually working internet
(wrong-but-accepted password on some routers, an ISP outage, etc. can all
still return success here). connect_to_wifi() now also confirms real
internet access before reporting success back to the app, so the customer
doesn't get told "Connected!" when the Pi actually still can't reach
Firebase. Endpoint/request/response shape is unchanged -- the app doesn't
need any changes for this.

CLAUDE'S PATCH #2: real on-device testing found the Pi's WiFi chip CANNOT
scan for other networks while it's actively broadcasting the Alisto-Setup
hotspot -- `nmcli device wifi list` while hotspot mode is active only ever
sees the hotspot's own SSID, no matter how close the real target network
is. Every connect attempt was therefore failing with "No network with SSID
'...' found", even sitting right next to the router. Fixed by explicitly
bringing the hotspot down, forcing a fresh scan, THEN attempting to join
the target network -- and bringing the hotspot back up if that fails, so
the customer can retry from the app without needing SSH.
"""

import os
import socket
import subprocess
import time

from flask import Flask, request, jsonify

app = Flask(__name__)

# Name of the connection profile NetworkManager will create/use.
HOME_WIFI_CONNECTION_NAME = "alisto-home-wifi"
# nmcli's default name for a hotspot started without an explicit
# "con-name" (which is how start_setup_hotspot.sh starts it).
HOTSPOT_CONNECTION_NAME = "Hotspot"

INTERNET_CHECK_HOST = ("8.8.8.8", 53)
INTERNET_CHECK_TIMEOUT_S = 3

# CLAUDE'S ADDITION: this script runs as root, standalone -- it has no
# access to the ESP32/speaker (that's owned exclusively by the
# alisto-emergency service, running as the "alisto" user, and two
# processes writing to the same serial port at once would corrupt both).
# So instead of talking to the speaker directly, this just drops a flag
# file; alisto_main.py polls for it and does the actual spoken
# announcement through its own already-open, already-safe audio pipeline.
WIFI_CONNECTED_FLAG = "/home/alisto/wifi_just_connected.flag"


def _signal_wifi_connected():
    try:
        with open(WIFI_CONNECTED_FLAG, "w"):
            pass
        # wifi_setup_server.py runs via sudo (root); alisto_main.py runs as
        # the unprivileged "alisto" user and needs to be able to read AND
        # delete this file.
        os.chmod(WIFI_CONNECTED_FLAG, 0o666)
    except Exception as e:
        print(f"wifi_setup_server: could not write connected-flag: {e}")


def has_internet() -> bool:
    try:
        socket.setdefaulttimeout(INTERNET_CHECK_TIMEOUT_S)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(INTERNET_CHECK_HOST)
        return True
    except OSError:
        return False


def _restart_hotspot():
    """Brings Alisto-Setup back up after a failed connect attempt, so the
    customer can retry from the app without needing physical/SSH access
    again. Best-effort -- if this itself fails there's nothing more useful
    to do than log it."""
    result = subprocess.run(
        ["nmcli", "connection", "up", HOTSPOT_CONNECTION_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"wifi_setup_server: failed to restart hotspot: {result.stderr.strip()}")


def connect_to_wifi(ssid: str, password: str) -> tuple[bool, str]:
    """
    Uses nmcli to connect the Pi to the given WiFi network.
    Returns (success, message).
    """
    try:
        # Remove any old profile with the same name first, so re-running
        # setup with new credentials doesn't fail on a duplicate name.
        subprocess.run(
            ["nmcli", "connection", "delete", HOME_WIFI_CONNECTION_NAME],
            capture_output=True,
            text=True,
        )

        # Must leave AP mode before the radio can actually see other
        # networks -- see CLAUDE'S PATCH #2 above.
        subprocess.run(
            ["nmcli", "connection", "down", HOTSPOT_CONNECTION_NAME],
            capture_output=True, text=True,
        )
        time.sleep(2)  # let the radio actually finish leaving AP mode
        subprocess.run(["nmcli", "device", "wifi", "rescan"], capture_output=True, text=True)
        time.sleep(3)  # give the scan time to actually populate results

        result = subprocess.run(
            [
                "nmcli", "device", "wifi", "connect", ssid,
                "password", password,
                "name", HOME_WIFI_CONNECTION_NAME,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            # nmcli prints useful error text to stderr, e.g. wrong password
            # or SSID not found. Restore the hotspot so the customer can
            # try again from the app.
            _restart_hotspot()
            return False, result.stderr.strip() or "Failed to connect. Check the WiFi password."

        # nmcli says it joined -- confirm there's actually real internet
        # before telling the app it worked.
        if not has_internet():
            _restart_hotspot()
            return False, (
                f"Joined {ssid} but there's no internet on that network. "
                "Check the router's internet connection and try again."
            )

        _signal_wifi_connected()
        return True, f"Connected to {ssid} successfully."

    except subprocess.TimeoutExpired:
        _restart_hotspot()
        return False, "Connection attempt timed out. Please check the WiFi name and try again."
    except Exception as e:
        _restart_hotspot()
        return False, f"Unexpected error: {e}"


@app.route("/connect-wifi", methods=["POST"])
def connect_wifi_endpoint():
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")

    if not ssid:
        return jsonify({"success": False, "message": "WiFi name (SSID) is required."}), 400
    if not password:
        return jsonify({"success": False, "message": "WiFi password is required."}), 400

    success, message = connect_to_wifi(ssid, password)
    return jsonify({"success": success, "message": message})


@app.route("/status", methods=["GET"])
def status_endpoint():
    """Simple health check the app can call to confirm it's talking to the Pi."""
    return jsonify({"status": "ok", "device": "Alisto"})


if __name__ == "__main__":
    # 0.0.0.0 so it's reachable from the phone while on the Pi's hotspot.
    app.run(host="0.0.0.0", port=5000)
