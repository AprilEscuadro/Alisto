# Deploying the ALISTO controller to the Pi

The controller is `alisto_main.py`, run by the `alisto-emergency` systemd
service. Voice detection is whisper.cpp (Tagalog mode, base model) followed by
a wake-word check ("Alisto") and a keyword search that picks the emergency
category. The older template-matching detector (`enroll.py`, `calibrate.py`,
`dtw.py`, `features.py`, `templates_lib.py`, `detector.py`, `trigger.py`) is
no longer used and needs no setup.

Pi: Raspberry Pi 4 Model B. From the home WiFi: `ssh alisto@192.168.1.72`
(if that address changes, look the Pi up by its MAC in the router; with no known
WiFi it opens the "Alisto-Setup" hotspot, then `ssh alisto@10.42.0.1`).

## 1. What lives where on the Pi

```
/home/alisto/
  alisto_main.py  alisto_sms.py  firestore_backend.py  gps.py  outbox.py
  reverse_geocode.py  load_alert.py  wifi_forget.py  rtc_sync.py
  wifi_auto_fallback.py  wifi_provision.py  wifi_setup_server.py
  data/cebu_city_barangays.json
  voice_detector/            audio_io  vad  whisper_asr  tts  categorize
                             phrase_clean  keyword_map  config  + test tools
  device_config.json                 {"serial": "<this unit's serial>"}
  firebase_service_account.json      secret, NOT in git: copy it by hand
  whisper.cpp/                       build/bin/whisper-cli and
                                     models/ggml-base-q5_1.bin
  piper_voices/en_US-ryan-low.onnx   the announcement voice (Piper TTS)
  alisto_audio/                      generated announcement audio
```

Python packages used: `numpy`, `sounddevice`, `webrtcvad`, `pyserial`,
`piper-tts`, `firebase-admin`. (`voice_detector/requirements.txt` only lists
the first three and is out of date.)

Ports: ESP32 at `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`, the SIM7600
modem's AT port at `/dev/serial/by-id/usb-SimTech_..._if02-port0`.

## 2. Update the code (the usual job)

From PowerShell on the Windows PC. Copy only what changed; the password is
asked each time.

```powershell
scp "alisto-iot/pi/alisto_main.py" alisto@192.168.1.72:/home/alisto/
scp "alisto-iot/pi/firestore_backend.py" alisto@192.168.1.72:/home/alisto/
scp "alisto-iot/pi/voice_detector/categorize.py" alisto@192.168.1.72:/home/alisto/voice_detector/
```

Let each copy finish (a `100%` line). A half-copied file can stop the service
from starting. Then restart and watch:

```bash
sudo systemctl restart alisto-emergency.service
systemctl is-active alisto-emergency.service        # should print: active
journalctl -u alisto-emergency -f -n 0              # Ctrl+C to leave
```

It is ready when the log shows `ALISTO EMERGENCY SYSTEM READY` (about 40 s
after the restart; whisper and the Piper voice load first). The listeners for
the app's commands and for new family links appear a minute later
(`Device commands: listening...`, `Family link: listening...`).

## 3. First-time install of the service

```bash
sudo cp /home/alisto/systemd/alisto-emergency.service /etc/systemd/system/
sudo cp /home/alisto/systemd/alisto-wifi-autofallback.service /etc/systemd/system/
sudo cp /home/alisto/systemd/alisto-wifi-provision.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alisto-emergency.service
```

`alisto-emergency` restarts itself after a crash and, through the systemd
watchdog (`WatchdogSec=120`), after a hang. It needs no network to start.

Disable WiFi power-save (it makes an audible "knock" in the speaker):

```bash
sudo cp /home/alisto/network/99-wifi-powersave-off.conf /etc/NetworkManager/conf.d/
sudo systemctl restart NetworkManager
iw dev wlan0 get power_save      # should print: Power save: off
```

## 4. Keep the logs after a reboot (journald)

By default the Pi's log is kept in memory and is gone after a reboot or power
cut, which is exactly when it is needed. Keep it on the SD card, capped:

```bash
sudo mkdir -p /var/log/journal
sudo sed -i 's/^#\?Storage=.*/Storage=persistent/; s/^#\?SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
journalctl --list-boots           # after the next reboot: more than one line
```

## 5. Test hooks (no real emergency needed)

| Command (on the Pi) | What it does |
|---|---|
| `echo ALL > /home/alisto/test_sms_failure.flag` | speaks every announcement once (17 messages) |
| `echo MODEM > /home/alisto/test_sms_failure.flag` | one SMS-failure message: `SIM`, `SIGNAL`, `LOAD` or `MODEM` |
| `touch /home/alisto/range_test.flag` | a heard "Alisto" is only logged, no alert (remove the file after) |
| `rm -f /home/alisto/.device_paired` + restart | plays the "Congratulations" pairing announcement again |

Voice tests with the service stopped (the mic must be free):

```bash
sudo systemctl stop alisto-emergency.service
cd /home/alisto/voice_detector
python3 phrase_test.py --file=phrases_manual30.txt --repeat=3 --verify --delay=40
sudo systemctl start alisto-emergency.service     # do not forget this
```

`--verify` is for an honest run: do not add keywords from its output.

## 6. How the device behaves (for checking)

- **Trigger:** SOS button, or "Alisto" + anything. The keywords only choose the
  category (FIRE, INTRUDER, MEDICATION, FALL_INJURY, MEDICAL_SYMPTOM,
  GENERAL_HELP); no match gives the generic "UNKNOWN" alert.
- **Cancel:** the Cancel button works only for the first 5 seconds. After that
  it is ignored (the LED stays on) until the blue listening LED is back. A Cancel
  pressed right after the trigger still counts.
- **SMS:** category, location, and for a spoken alert a last line
  `Heard: "..."` (kept inside one 160-character SMS; left out if it does not
  fit). Each SMS costs one load credit per contact.
- **App / website:** the alert shows the category. The spoken words are not
  shown there.
- **Offline:** alerts are written to disk first (`outbox.py`) and uploaded when
  the internet is back; the SMS needs only the SIM.
- **Voice messages:** emergency (per category), SMS failed (SIM / signal / load /
  modem), low SIM load (below PHP 10, not 9 pm to 7 am, repeated every 12 h),
  low battery, WiFi connected, WiFi removed, device paired (once), a new family
  member linked (every time), medicine reminders.
- **App commands:** "Forget WiFi" (Device Information) arrives through
  Firestore (`device_command/<serial>`); the Pi removes its saved WiFi and
  opens "Alisto-Setup".

## 7. Clock (DS3231 on the ESP32)

At boot the Pi sets its clock from the RTC (`System clock synced from RTC`).
While it has internet it also writes the correct time back to the RTC every
6 hours (`RTC: ... -> wrote ...`). To check after a power cut:

```bash
journalctl -u alisto-emergency -b --no-pager | grep -i "rtc"
```

A year before 2024 in the RTC reading means the RTC lost its power or its coin
cell is dead; the Pi then refuses to use it.

## 8. If something is wrong

| Symptom | Look at |
|---|---|
| service not `active` | `journalctl -u alisto-emergency -n 40 --no-pager` (a copy that stopped half-way, or a syntax error) |
| no SMS | `journalctl -u alisto-emergency --no-pager \| grep -i "sms\|CMGS"`; load is 0 most often (the device says "Check your sim load balance") |
| no wake word | `[gate] REJECTED` lines are the noise gate; try `range_test.flag` and the 3 m test |
| wrong or missing category | the category is only keywords; see `voice_detector/categorize.py`, and run `phrase_test.py` (section 5) |
