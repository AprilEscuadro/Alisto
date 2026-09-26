"""
wifi_auto_fallback.py

Runs ONCE at every boot (see its systemd service). Solves exactly the
problem hit in the field: the device was carried to a new location with no
known WiFi in range, and there was no way to reach it at all -- no
monitor/keyboard, no SSH (needs network to work in the first place) -- to
even START the manual WiFi setup flow (start_setup_hotspot.sh +
wifi_setup_server.py had to be triggered by hand over SSH).

Logic:
  1. Give NetworkManager a grace period to reconnect to a saved network on
     its own (normal case: device is still at home, or was moved back to
     a place with a network it already knows).
  2. If there's still no internet after that -- new location, or first
     boot ever with no WiFi configured yet -- automatically run the SAME
     hotspot + credential server the app's WiFi setup screen already
     knows how to talk to (Alisto-Setup / 10.42.0.1:5000/connect-wifi),
     with zero manual intervention needed.

Deliberately runs ONLY ONCE per boot, not on a repeating timer -- this is
for the "device just started up with no network at all" case, not for
reacting to a brief WiFi drop during normal operation (which should NOT
kick the device into hotspot mode and disrupt whatever it's doing).

NOTE: while this service is enabled, don't ALSO manually run
wifi_setup_server.py by hand for testing without stopping this service
first -- both would fight over port 5000 (the exact "Address already in
use" bug real testing hit earlier from leftover manual test processes).
"""

import socket
import subprocess
import time

BOOT_GRACE_PERIOD_S = 25  # let NetworkManager try its own reconnect first
INTERNET_CHECK_HOST = ("8.8.8.8", 53)
INTERNET_CHECK_TIMEOUT_S = 3

HOTSPOT_SCRIPT = "/home/alisto/start_setup_hotspot.sh"
WIFI_SERVER_SCRIPT = "/home/alisto/wifi_setup_server.py"


def has_internet():
    try:
        socket.setdefaulttimeout(INTERNET_CHECK_TIMEOUT_S)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(INTERNET_CHECK_HOST)
        return True
    except OSError:
        return False


def main():
    print(f"wifi_auto_fallback: waiting {BOOT_GRACE_PERIOD_S}s for a normal WiFi reconnect...")
    time.sleep(BOOT_GRACE_PERIOD_S)

    if has_internet():
        print("wifi_auto_fallback: already online -- nothing to do.")
        return

    print(
        "wifi_auto_fallback: no internet after the grace period -- "
        "starting the setup hotspot automatically (no SSH needed)."
    )
    # Already running as root (see the systemd service's User=root) --
    # no sudo needed for these.
    subprocess.run(["bash", HOTSPOT_SCRIPT])
    # wifi_setup_server.py blocks forever serving Flask requests -- this
    # process just sits here running it, same as the manual
    # "sudo python3 wifi_setup_server.py" command used during development.
    subprocess.run(["python3", WIFI_SERVER_SCRIPT])


if __name__ == "__main__":
    main()
