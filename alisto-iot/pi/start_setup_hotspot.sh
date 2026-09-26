#!/bin/bash
# start_setup_hotspot.sh
#
# Puts the Raspberry Pi into "setup mode" by creating its own temporary
# WiFi hotspot named "Alisto-Setup". Run this when the Pi has no saved
# home WiFi to connect to (e.g. first boot, or after a factory reset).
#
# Requirements:
#   sudo apt install network-manager
#
# Usage:
#   chmod +x start_setup_hotspot.sh
#   sudo ./start_setup_hotspot.sh

HOTSPOT_NAME="Alisto-Setup"
HOTSPOT_PASSWORD="alisto123"   # must be 8+ characters for WPA2
WIFI_INTERFACE="wlan0"          # check with `nmcli device status` if different

echo "Starting Alisto setup hotspot..."

nmcli device wifi hotspot \
  ifname "$WIFI_INTERFACE" \
  ssid "$HOTSPOT_NAME" \
  password "$HOTSPOT_PASSWORD"

if [ $? -eq 0 ]; then
    echo "Hotspot '$HOTSPOT_NAME' is now active."
    echo "Connect your phone to it, then use the Alisto app to send your home WiFi details."
else
    echo "Failed to start hotspot. Run 'nmcli device status' to check your WiFi interface name."
fi
