# Deploying the unified ALISTO controller to the Pi

## 1. Copy files to the Pi

From PowerShell on this Windows machine (adjust the local path if you moved
this project folder):

```bash
scp -r "alisto-iot/pi/voice_detector" alisto@192.168.1.71:/home/alisto/
scp -r "alisto-iot/pi/network" alisto@192.168.1.71:/home/alisto/
scp "alisto-iot/pi/alisto_main.py" alisto@192.168.1.71:/home/alisto/
```

## 2. On the Pi: stop the old service, install new dependencies

```bash
sudo systemctl stop alisto-emergency.service

sudo apt-get install -y libportaudio2
pip install --break-system-packages -r /home/alisto/voice_detector/requirements.txt
```

## 3. Free up space (remove the old whisper.cpp setup)

Only do this once you're happy the new detector works (step 5) — no rush.

```bash
rm -rf /home/alisto/whisper.cpp
rm -f /home/alisto/alisto_voice_before_*.py
rm -f /home/alisto/alisto_voice.py.save
rm -f /home/alisto/alisto_audio.py.save
rm -f /home/alisto/*_backup.py
sudo apt-get clean
df -h   # confirm space was freed
```

Keep `alisto_sms.py` (reused by `alisto_main.py`) and `alisto_audio.py` (no
longer used, but harmless to leave for reference).

## 4. Enroll and calibrate the voice phrase

```bash
cd /home/alisto/voice_detector
python enroll.py       # record 5-8 samples of "Alisto Tabang"
python calibrate.py    # say the phrase + other things, tune DETECTION_THRESHOLD in config.py
```

## 5. Test the unified controller manually first

```bash
cd /home/alisto
python3 alisto_main.py
```

Try both: press the physical SOS button, and say the phrase. Confirm the LED,
audio announcement, 5s cancel window (physical cancel button), and SMS all
behave correctly for both trigger paths. Ctrl+C to stop.

## 6. Install it as the systemd service

```bash
sudo cp /home/alisto/voice_detector/../systemd/alisto-emergency.service /etc/systemd/system/alisto-emergency.service
sudo systemctl daemon-reload
sudo systemctl restart alisto-emergency.service
sudo systemctl status alisto-emergency.service --no-pager
```

(If you didn't scp the `systemd/` folder over, just copy the contents of
`alisto-iot/pi/systemd/alisto-emergency.service` into
`/etc/systemd/system/alisto-emergency.service` on the Pi directly with
`sudo nano`.)

## 7. Watch it run

```bash
journalctl -u alisto-emergency.service -f
```

## 8. Disable WiFi power-save (fixes the audible "knock" through the speaker)

The WiFi radio's periodic sleep/wake cycle under power-save mode draws brief
current spikes that couple into the I2S audio line as a knock -- most
noticeable during quiet reminder audio and around WiFi (re)connection. This
is a NetworkManager config change, not a firmware/hardware fix:

```bash
sudo cp /home/alisto/network/99-wifi-powersave-off.conf /etc/NetworkManager/conf.d/
sudo systemctl restart NetworkManager
```

Confirm it took effect:

```bash
iw dev wlan0 get power_save   # should print "Power save: off"
```
