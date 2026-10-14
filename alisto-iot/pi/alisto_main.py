"""ALISTO unified Pi controller.

Replaces alisto_emergency.py + alisto_voice.py with one process that:
  - owns a single persistent ESP32 serial connection (fixes the old bug where
    alisto_audio.py opened a second competing connection to the same port)
  - handles the physical SOS/CANCEL buttons via the ESP32's real protocol
    (confirmed from firmware ALISTO_ESP32_V8.6.ino: EVENT:SOS / EVENT:CANCEL /
    EVENT:EMERGENCY_ACTIVE / AUDIO_READY / AUDIO_DONE, 921600 baud)
  - handles voice-triggered emergencies via whisper.cpp (tiny, quantized,
    --audio-ctx 768 -bs 1) transcribing each isolated utterance and checking
    it for the "Alisto"/"Tabang" phrase -- replaces the earlier MFCC+DTW
    template matcher, which was fast but too brittle across sessions/voices;
    whisper.cpp's own STT model generalizes far better and, tuned this way,
    runs in ~4-5s per utterance instead of the original untuned Whisper
    setup's ~60s
  - routes both trigger sources through one shared 5-second cancel window +
    SMS flow, reusing the existing (working, response-checked) send_sms()
    from alisto_sms.py
  - speaks scheduled medicine reminders (name + dosage) through the same
    speaker/TTS pipeline, using the Pi's clock (kept accurate by the DS3231
    RTC synced at startup) -- see MEDICINE_REMINDERS below

Run as the alisto-emergency systemd service (see systemd/alisto-emergency.service).
"""

import math
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

import serial

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "voice_detector"))
import audio_io      # noqa: E402
import categorize     # noqa: E402
import config          # noqa: E402
import tts               # noqa: E402
import vad                # noqa: E402
import whisper_asr         # noqa: E402

sys.path.insert(0, "/home/alisto")
from alisto_sms import send_sms, read_own_sim_number, read_sim_health, sms_failure_reason  # noqa: E402  (existing, working, own serial port)
import gps  # noqa: E402  (SIM7600's built-in GNSS -- shares that same port,
            # but never used at the same time as send_sms(), see below)
import reverse_geocode  # noqa: E402  (offline lat/lon -> barangay name)
import firestore_backend  # noqa: E402  (Admin SDK -- finds who claimed this device)
import outbox  # noqa: E402  (offline store-and-forward for emergency alerts)

try:
    from alisto_sms import DEFAULT_MESSAGE  # noqa: E402
except ImportError:
    # Fallback only -- alisto_sms.py on the Pi defines the real one. This
    # just keeps trigger_emergency() from crashing if that ever changes.
    DEFAULT_MESSAGE = "ALISTO ALERT! Emergency detected. Please check immediately."

ESP32_PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
ESP32_BAUD = 921600

AUDIO_CHUNK_SIZE = 4096
AUDIO_READY_TIMEOUT_S = 3
AUDIO_DONE_TIMEOUT_S = 5
AUDIO_BYTES_PER_SEC = 32000  # 16kHz * 2 bytes/sample (matches the I2S config)
AUDIO_LEAD_S = 0.2  # how far ahead of real-time playback stream_audio() may run

WHISPER_TEMP_WAV = "/home/alisto/voice_detector/whisper_in.wav"

CANCEL_WINDOW_S = 5

# Real testing hit a one-off "+CMS ERROR" send failure with everything else
# (SIM, signal, message content/length) confirmed fine seconds later --
# almost certainly a transient modem/network hiccup, plausibly the GNSS and
# cellular antennas sitting right next to each other on the same HAT. A
# single such hiccup should never be the reason an emergency SMS never goes
# out, so retry a few times with a short gap before giving up.
SMS_RETRY_ATTEMPTS = 3
SMS_RETRY_DELAY_S = 8  # widened from 3 -- a modem still finishing an
# in-flight SMS transmission needs real recovery time before it'll answer
# a fresh AT+CMGS. Retrying too soon just piled another command onto a
# still-busy modem, which left it unresponsive (not even answering a
# plain "AT") for the rest of the emergency in real testing. Paired with
# send_sms()'s own AT-ping guard, which now bails immediately instead of
# hammering an unresponsive modem with more commands.

REMINDER_CHECK_INTERVAL_S = 20  # granularity of the schedule check -- fine
                                 # enough to reliably catch every "HH:MM"
                                 # minute boundary without busy-looping
# The schedule itself is no longer hardcoded -- see medicine_reminder_loop()
# below, which reads it live from Firestore's medication_reminder
# collection (added through the app), scoped to this device's elder_id.

cancel_event = threading.Event()
audio_ready_event = threading.Event()
audio_done_event = threading.Event()
rtc_time_event = threading.Event()
_rtc_time_value = None

RTC_TIMEOUT_S = 3

_emergency_lock = threading.Lock()
_emergency_in_progress = False

# Set once in voice_listen_loop() at startup, then shared with
# trigger_emergency() below -- both the button path (spawned independently
# from esp32_reader) and the voice path need to pause/resume the SAME mic
# stream, not two different ones.
frames = None

# Real testing crashed the whole process (PortAudio SIGABRT) when the
# medicine reminder thread called frames.pause() at the same moment the
# main voice thread was already pausing it for whisper transcription --
# sounddevice/PortAudio is not safe to stop()/start() concurrently from two
# threads. Every pause/resume of `frames`, from any thread (transcription,
# an emergency, or a reminder), must hold this lock for its whole "paused"
# window so only one such operation is ever in flight system-wide.
_audio_state_lock = threading.Lock()

# Resolved once this unit's serial is claimed in the app (see
# device_pairing_watcher_loop) -- needed by everything Firestore-related
# beyond the initial pairing itself: writing alerts, syncing GPS location,
# reading medicine reminders, and looking up emergency contacts are all
# scoped to this elder_id, not to any particular user account.
_elder_id = None

# _elder_id lives in memory only, so a Pi that boots with NO internet used to
# come up not knowing who it protects: no contacts lookup (SMS only to the
# hardcoded default number) and no alert record. Remembered on disk the last
# time it resolved, and loaded back at startup -- the pairing watcher still
# re-checks it against Firestore whenever the internet is available.
ELDER_ID_CACHE_PATH = "/home/alisto/.elder_id_cache"


def _load_cached_elder_id():
    try:
        with open(ELDER_ID_CACHE_PATH) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _save_cached_elder_id(elder_id):
    try:
        with open(ELDER_ID_CACHE_PATH, "w") as f:
            f.write(elder_id)
    except OSError as e:
        print(f"could not cache elder_id: {e}")


def open_esp32():
    ser = serial.Serial(ESP32_PORT, ESP32_BAUD, timeout=0.1)
    time.sleep(2)  # let the board finish its boot reset
    return ser


def send_command(ser, cmd):
    ser.write((cmd + "\n").encode())
    ser.flush()


def esp32_reader(ser):
    """The ONLY thing that reads from `ser`. Everything else only writes."""
    global _rtc_time_value

    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        print("ESP32:", line)

        if line == "EVENT:CANCEL":
            cancel_event.set()
        elif line == "EVENT:SOS":
            threading.Thread(
                target=trigger_emergency, args=(ser, "button"), daemon=True
            ).start()
        elif line == "AUDIO_READY":
            audio_ready_event.set()
        elif line == "AUDIO_DONE":
            audio_done_event.set()
        elif line.startswith("TIME:"):
            _rtc_time_value = line[len("TIME:"):]
            rtc_time_event.set()
        # EVENT:EMERGENCY_ACTIVE and the boot banner are informational only


def sync_clock_from_rtc(ser):
    """Sets the Pi's system clock from the DS3231 RTC (firmware V8.8+). The
    Pi has no onboard RTC and this system runs fully offline (no NTP), so
    its clock would otherwise be wrong after any reboot -- reads real time
    back from the battery-backed RTC instead. Requires passwordless sudo for
    /bin/date (see /etc/sudoers.d/alisto-date); never blocks startup if it
    fails for any reason -- an emergency device shouldn't refuse to run just
    because the clock might be wrong."""
    rtc_time_event.clear()
    send_command(ser, "CMD:GET_TIME")

    if not rtc_time_event.wait(timeout=RTC_TIMEOUT_S):
        print("WARNING: no response from RTC -- system clock not synced")
        return

    rtc_time = _rtc_time_value

    # Sanity check before trusting this: a DS3231 with a dead/missing backup
    # battery resets to its power-on default (2000-01-01 00:00:xx-ish) every
    # time it loses power, even briefly. Real testing hit exactly this --
    # applying a reading like that would silently corrupt the Pi's
    # otherwise-correct clock instead of fixing it. Anything before 2024 is
    # almost certainly a lost-battery reset, not a legitimately old date.
    try:
        rtc_year = int(rtc_time[:4])
    except (ValueError, TypeError):
        rtc_year = 0
    if rtc_year < 2024:
        print(
            f"WARNING: RTC returned implausible time ({rtc_time}) -- likely "
            "lost power / dead or missing backup battery. NOT syncing the "
            "clock from this reading. Check the DS3231's coin-cell battery."
        )
        return

    try:
        result = subprocess.run(
            ["sudo", "date", "-s", rtc_time],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print(f"System clock synced from RTC: {rtc_time}")
        else:
            print(f"WARNING: failed to set system clock: {result.stderr.strip()}")
    except Exception as e:
        print(f"WARNING: failed to set system clock: {e}")


def stream_audio(ser, path):
    if not os.path.exists(path):
        print("Audio file missing:", path)
        return

    size = os.path.getsize(path)
    audio_ready_event.clear()
    audio_done_event.clear()

    send_command(ser, f"AUDIO_START:{size}")

    if not audio_ready_event.wait(timeout=AUDIO_READY_TIMEOUT_S):
        print("WARNING: no AUDIO_READY from ESP32, streaming anyway")

    # Pace delivery to roughly MATCH the ESP32's actual I2S playback rate
    # (16kHz * 2 bytes/sample = 32000 B/s -> one 4096-byte chunk every
    # ~128ms) -- NOT faster (overflows the buffer) and not much slower
    # either. Paced against a monotonic clock (bytes sent so far vs. time
    # elapsed so far) rather than a blind fixed sleep per chunk: a fixed
    # sleep can't correct for a single slow iteration (a GC pause, another
    # thread's scheduling slice, etc.) -- that one iteration's overrun
    # just accumulates as permanent drift for every chunk after it. A
    # short announcement is over before drift adds up to anything
    # audible; a longer one (e.g. a repeated reminder, several times the
    # duration) has far more chunks for that same drift to accumulate
    # across, which real testing traced to audible clicks partway through
    # -- never reproduced on the single-utterance announcement. Catching
    # up against elapsed wall-clock time keeps the gap between "should
    # have sent" and "actually sent" from growing no matter how long the
    # stream runs.
    start = time.monotonic()
    sent = 0
    with open(path, "rb") as f:
        while True:
            if cancel_event.is_set():
                # The ESP32's CANCEL button handler has HIGHEST priority in
                # its own loop() and aborts playback (audioMode=false)
                # immediately, independent of the Pi. Without this check we
                # would keep writing the rest of the file, then wait up to
                # AUDIO_DONE_TIMEOUT_S for a confirmation the ESP32 will
                # never send (since it already gave up on this audio) --
                # exactly the multi-second "why is it slow to resume
                # listening after I cancel" delay real testing showed.
                print("Cancelled mid-announcement -- stopping audio stream early.")
                return
            chunk = f.read(AUDIO_CHUNK_SIZE)
            if not chunk:
                break
            ser.write(chunk)
            ser.flush()
            sent += len(chunk)
            # Stay AUDIO_LEAD_S ahead of real-time playback instead of
            # delivering each chunk exactly when the previous one finishes
            # playing. With zero lead any single Pi hiccup (a Firestore
            # thread, scheduling, GC) lets the ESP32's I2S buffer run dry
            # -> a gap of silence -> an audible pop/knock when audio resumes.
            # Real testing: the ~6s medicine reminder knocked every time while
            # the ~3s button announcement didn't, because the longer stream
            # has more chances to hit one. The lead has to stay within what
            # the ESP32 can buffer without dropping bytes (8192-byte serial
            # RX buffer + ~8192 bytes of I2S DMA = ~0.5s); 0.2s fits easily.
            remaining = (sent / AUDIO_BYTES_PER_SEC) - (time.monotonic() - start) - AUDIO_LEAD_S
            if remaining > 0:
                time.sleep(remaining)

    if cancel_event.is_set():
        return  # same reasoning -- no AUDIO_DONE coming, don't wait for it

    if not audio_done_event.wait(timeout=AUDIO_DONE_TIMEOUT_S):
        print("WARNING: no AUDIO_DONE confirmation from ESP32")


# How long to wait on Firestore for the emergency-contact list before using
# the local cache instead. The SMS goes out over the SIM7600's own cellular
# link, so it must never end up waiting on WiFi/Firestore -- real testing
# measured ~6s of Firestore calls (alert update, push, contacts) sitting in
# front of the SMS even with working internet, and an offline or slow
# connection would stretch that much further.
CONTACTS_FETCH_TIMEOUT_S = 3


def _run_in_background(fn, *args):
    """Runs a best-effort Firestore step off the emergency's critical path.
    Never raises into the caller -- a failure here must not affect the SMS."""
    def runner():
        try:
            fn(*args)
        except Exception as e:
            print(f"background step {fn.__name__} failed: {e}")
    threading.Thread(target=runner, daemon=True).start()


# OFFLINE STORE-AND-FORWARD (see outbox.py). Every emergency is saved to a
# local file the moment it starts and finished off there; the Firestore side
# (alert doc, location, phrase, logs, push) is uploaded by
# outbox_uploader_loop() whenever the internet allows, and the file is
# deleted once it's all safely uploaded. Nothing in the emergency flow itself
# waits on Firestore.

OUTBOX_RETRY_INTERVAL_S = 15
CONTACTS_REFRESH_INTERVAL_S = 30 * 60

_push_inflight = set()  # alert_ids whose immediate push is still running
_push_inflight_lock = threading.Lock()


def _outbox_update(alert_id, **fields):
    """outbox.update() that can never raise into the emergency flow (a full
    or failing disk must not stop the SMS)."""
    if not alert_id:
        return
    try:
        outbox.update(alert_id, **fields)
    except Exception as e:
        print(f"outbox: could not update {alert_id}: {e}")


def _bg_create_alert(alert_id, elder_id, own_serial, alert_title, lines, trigger_type):
    """The early emergency_alert write, off the critical path (see
    firestore_backend.new_alert_id()): purely so the website/app show the
    alert the instant it fires when internet is up. If it can't get through
    (offline), the outbox uploader writes the whole alert later."""
    if firestore_backend.write_emergency_alert(
        elder_id=elder_id,
        device_id=own_serial,
        title=alert_title,
        lines=lines,
        trigger_type=trigger_type,
        alert_id=alert_id,
    ):
        _outbox_update(alert_id, early_created=True)


def _send_push(elder_id, alert_id, alert_title, age_s=0.0):
    """`age_s` = how long ago the emergency happened. The push is dropped if
    it can't go out within outbox.PUSH_MAX_AGE_S of the emergency itself,
    even if the call only gets unstuck later (see send_emergency_push)."""
    return firestore_backend.send_emergency_push(
        elder_id=elder_id,
        title="ALISTO Emergency Alert",
        body=f"{alert_title}. Please check immediately.",
        alert_id=alert_id,
        deadline_mono=time.monotonic() + outbox.PUSH_MAX_AGE_S - age_s,
    )


def _bg_push(elder_id, alert_id, alert_title):
    """The immediate push, sent right when the SMS starts. If it fails
    (offline) the outbox uploader retries it after the alert is uploaded."""
    with _push_inflight_lock:
        _push_inflight.add(alert_id)
    try:
        if _send_push(elder_id, alert_id, alert_title):
            _outbox_update(alert_id, push_sent=True)
    finally:
        with _push_inflight_lock:
            _push_inflight.discard(alert_id)


def _outbox_push(rec):
    """push_fn for outbox.process_pending(): the delayed push, unless the
    immediate one is still in flight (would double-notify)."""
    with _push_inflight_lock:
        if rec["alert_id"] in _push_inflight:
            return False
    age_s = max(0.0, time.time() - outbox.event_time(rec).timestamp())
    return _send_push(rec["elder_id"], rec["alert_id"], rec["title"], age_s)


def outbox_uploader_loop():
    """Uploads finished alerts from the outbox to Firestore and deletes them
    once they're safely there. Runs once at startup (picks up anything left
    from before a reboot), then whenever an alert completes or every
    OUTBOX_RETRY_INTERVAL_S. Also keeps the SMS-recipient cache fresh so a
    later offline emergency still has the current contacts."""
    last_contacts_refresh = None
    while True:
        try:
            done = outbox.process_pending(firestore_backend.upload_alert_record, _outbox_push)
            if done:
                print(f"outbox: uploaded {done} alert(s) to Firestore")
        except Exception as e:
            print(f"outbox: uploader pass failed: {e}")

        if _elder_id is not None and (
            last_contacts_refresh is None
            or time.monotonic() - last_contacts_refresh >= CONTACTS_REFRESH_INTERVAL_S
        ):
            try:
                if firestore_backend.refresh_contacts_cache(_elder_id):
                    last_contacts_refresh = time.monotonic()
            except Exception as e:
                print(f"contacts cache refresh failed: {e}")

        outbox.wake.wait(OUTBOX_RETRY_INTERVAL_S)
        outbox.wake.clear()


def _announce_sms_failure(ser):
    """Speaks why the emergency SMS failed for every contact (no SIM, no
    signal, no load, modem not answering). Called from inside
    trigger_emergency(), which already holds _audio_state_lock with the mic
    paused, so this streams directly instead of taking those again.
    Best-effort -- must never raise into the emergency flow."""
    try:
        with gps.port_lock:
            health = read_sim_health()
        reason = sms_failure_reason(health)
        print(f"SMS FAILED for every contact -- SIM health: {health} -> {reason}")
        audio_path = tts.synthesize_sms_failure(reason)
        if audio_path:
            stream_audio(ser, audio_path)
    except Exception as e:
        print(f"SMS-failure announcement failed: {e}")


def trigger_emergency(ser, source, category="UNKNOWN", message=None):
    global _emergency_in_progress

    with _emergency_lock:
        if _emergency_in_progress:
            return  # already handling one, ignore duplicate/overlapping trigger
        _emergency_in_progress = True

    # Mic off + status LED off for the WHOLE emergency flow (announcement,
    # cancel window, SMS), regardless of trigger source. Previously this
    # only happened for voice-triggered emergencies (handled at the
    # voice_listen_loop call site); the physical SOS button spawns this
    # function independently from esp32_reader and was never covered, so
    # the LED stayed lit "listening" and the mic kept recording during a
    # button-triggered emergency too. Doing it here instead covers both.
    # _audio_state_lock held for the whole pause-through-resume window so a
    # concurrent transcription or medicine reminder can't touch `frames` at
    # the same time (real testing crashed the process doing exactly that).
    with _audio_state_lock:
        if frames is not None:
            frames.pause()
        send_command(ser, "CMD:STATUS_LED_OFF")

        try:
            print(f"\n*** EMERGENCY TRIGGERED (source={source}, category={category}) ***")
            cancel_event.clear()

            own_serial = firestore_backend.get_own_serial()
            alert_title = f"{category.replace('_', ' ').title()} emergency"
            trigger_type = "BUTTON" if source == "button" else "VOICE"

            # Written immediately -- before the TTS announcement even starts
            # streaming, let alone the 5-second cancel window -- so the
            # website/app show something the instant the button/keyword
            # fires. Previously the first Firestore write for a real
            # emergency didn't happen until AFTER announcement + cancel
            # window had both fully played out, which real testing measured
            # at 10-15+ seconds of the admin map showing nothing at all --
            # long enough that an admin pressed the button again out of
            # doubt, multiplying the very alerts this was meant to report.
            # location_id/phrase_id/the location-enriched message aren't
            # known yet at this point -- they're filled in on this same doc
            # via update_emergency_alert() below once the cancel window
            # clears, rather than waiting to create the doc until then.
            alert_id = None
            if _elder_id is not None:
                alert_id = firestore_backend.new_alert_id() or outbox.local_alert_id()
            if alert_id:
                # Saved to local disk FIRST (no network involved) -- this is
                # the copy that survives no internet and even a power cut;
                # see outbox.py. Then the early Firestore write happens in
                # the background -- with no internet it can block for ~60s on
                # Firestore's retries, and this used to sit in front of the
                # announcement, the cancel window, and the SMS.
                try:
                    outbox.new_record(
                        alert_id, elder_id=_elder_id, device_id=own_serial,
                        title=alert_title, trigger_type=trigger_type,
                        category=category, phrase_text=message,
                        lines=(message or DEFAULT_MESSAGE).split("\n"),
                    )
                except Exception as e:
                    print(f"outbox: could not save alert {alert_id} locally: {e}")
                _run_in_background(
                    _bg_create_alert, alert_id, _elder_id, own_serial,
                    alert_title, (message or DEFAULT_MESSAGE).split("\n"), trigger_type,
                )

            if source == "voice":
                # Button path already set the ESP32's own emergency state
                # (it detects the physical press itself); voice needs to
                # tell it.
                send_command(ser, "CMD:VOICE_EMERGENCY")

            # Announcement is synthesized live via Piper TTS (see tts.py)
            # instead of a pre-recorded file -- says which category was
            # detected, and costs no permanent storage. Falls back to no
            # announcement (silent, but cancel window + SMS still run
            # normally) if TTS fails for any reason -- same
            # graceful-degradation behavior as a missing file.
            audio_path = tts.synthesize_announcement(category)
            if audio_path:
                stream_audio(ser, audio_path)
            else:
                print("TTS announcement unavailable -- continuing without audio")

            cancelled = False
            for i in range(CANCEL_WINDOW_S, 0, -1):
                if cancel_event.is_set():
                    cancelled = True
                    break
                print("Sending SMS in", i)
                # wait(), not sleep(): returns the instant cancel_event is
                # set instead of finishing out the full second first, so a
                # cancel during the countdown takes effect immediately.
                if cancel_event.wait(timeout=1):
                    break
            if cancel_event.is_set():
                cancelled = True

            if cancelled:
                print("Emergency cancelled.")
                # ESP32 already turned its own LED off via cancelEmergency()
                # Flips the same doc written at trigger time to CANCELLED
                # (no push, no SMS -- nobody needs to be notified about a
                # false alarm) so the website's False Alarms tab has
                # something to count, instead of creating a second doc.
                # Background thread, not inline: this is two Firestore
                # writes (the alert + its status history), and the `finally`
                # below -- which turns the blue listening LED back on and
                # resumes the mic -- can't run until this returns. Real
                # testing: the LED took ~3s to come back after a cancel.
                if alert_id:
                    _outbox_update(
                        alert_id, status="CANCELLED",
                        resolution_note="Cancelled within 5 seconds", complete=True,
                    )
                    outbox.wake.set()
            else:
                final_message = message or DEFAULT_MESSAGE
                # Instant, in-memory read -- no serial I/O here at all (see
                # gps.py's design note: querying GPS live this close to an
                # SMS send made every send fail). This is whatever the
                # background poll loop (started in main()) most recently
                # cached, which is normally at most POLL_INTERVAL_S old.
                fix = gps.get_last_fix()
                fix_age_s = None  # None = a fresh fix; a number = stale "last known"
                if fix is None:
                    # No fresh fix (indoors, antenna just moved, first boot,
                    # ...). This device sits in one place -- an elder's home
                    # -- so the last known position is almost certainly still
                    # right; sending it, clearly labelled and aged, beats an
                    # SMS with no location at all.
                    known = gps.get_last_known()
                    if known:
                        fix, fix_age_s = known
                gps_args = None
                if fix:
                    lat, lon = fix
                    coords = gps.coords_text(fix)  # plain "lat,lon", not a
                    # clickable link -- see gps.coords_text() for why
                    # (carrier URL filtering).
                    barangay = reverse_geocode.barangay_for(lat, lon)
                    place = f"{barangay}, Cebu City ({coords})" if barangay else coords
                    if fix_age_s is None:
                        # Human-readable first, coordinates kept alongside
                        # as backup precision for anyone who does have a
                        # maps app -- not hardcoded, this is a live
                        # point-in-polygon lookup against the actual fix.
                        final_message += f"\nLocation: {place}"
                        # Only a fresh fix becomes a new device_location row
                        # -- a stale one would be recorded with a misleading
                        # "just now" timestamp.
                        gps_args = (lat, lon, barangay)
                    else:
                        final_message += f"\nLast known: {place} {gps.age_text(fix_age_s)}"
                    print(f"GPS: including {'cached' if fix_age_s is None else 'LAST KNOWN (' + gps.age_text(fix_age_s) + ')'} location {fix} -> {barangay}")
                else:
                    print("GPS: no fix at all (fresh or saved) -- sending without location")

                # Everything that needs Firestore (push, alert doc update,
                # location row, logs) now runs on background threads, started
                # BEFORE the SMS loop so the push still goes out immediately
                # but nothing here can delay or block the SMS. The SMS uses
                # the SIM7600's own cellular link and has to work even when
                # WiFi/Firestore is slow or down -- previously push, alert
                # update, location write, and contact lookup all ran in
                # series in front of it (~6s with working internet).
                if alert_id:
                    _outbox_update(
                        alert_id, lines=final_message.split("\n"),
                        gps=(
                            {"lat": gps_args[0], "lon": gps_args[1], "barangay": gps_args[2]}
                            if gps_args else None
                        ),
                    )
                    _run_in_background(_bg_push, _elder_id, alert_id, alert_title)

                # Real recipients (added through the app's Contacts screen)
                # instead of the single hardcoded number -- falls back to
                # alisto_sms.py's own default RECIPIENT only if no contact
                # could be resolved at all (not yet paired, or genuinely
                # zero contacts added). The Firestore read is capped at
                # CONTACTS_FETCH_TIMEOUT_S and falls back to the local cache.
                contacts = (
                    firestore_backend.get_emergency_contacts(
                        _elder_id, timeout_s=CONTACTS_FETCH_TIMEOUT_S)
                    if _elder_id is not None else []
                )
                if not contacts:
                    print("No emergency contacts found -- falling back to default recipient.")
                    contacts = [{"contact_id": None, "contact_num": None}]

                results = []  # (contact_id, ok, attempts) for the post-send log
                for contact in contacts:
                    recipient = contact["contact_num"]
                    label = recipient or "default recipient"
                    ok = False
                    attempt = 0
                    for attempt in range(1, SMS_RETRY_ATTEMPTS + 1):
                        print(f"Sending SMS to {label} (attempt {attempt}/{SMS_RETRY_ATTEMPTS})...")
                        # port_lock keeps this from ever overlapping the
                        # background GPS poll's own use of the same SIM port.
                        with gps.port_lock:
                            ok = send_sms(final_message, recipient=recipient)
                        if ok:
                            break
                        if attempt < SMS_RETRY_ATTEMPTS:
                            time.sleep(SMS_RETRY_DELAY_S)
                    results.append((contact["contact_id"], ok, attempt))
                    print(f"SMS to {label} sent." if ok else f"SMS to {label} FAILED after {SMS_RETRY_ATTEMPTS} attempts.")
                any_ok = any(ok for _, ok, _ in results)
                if alert_id:
                    _outbox_update(
                        alert_id,
                        status="SENT" if any_ok else "PENDING",
                        contacts=[
                            {"contact_id": cid, "ok": ok, "attempts": n}
                            for cid, ok, n in results
                        ],
                        complete=True,
                    )
                    outbox.wake.set()
                if not any_ok:
                    # Nothing got through to anyone -- say so out loud, with
                    # the likely reason, instead of failing in silence.
                    _announce_sms_failure(ser)
                send_command(ser, "CMD:CANCEL")  # reset ESP32 state for next trigger

        finally:
            # Stamp the watchdog BEFORE clearing the flag: the mic has been
            # paused for the whole emergency (possibly minutes), and the
            # moment the flag clears the watchdog judges by the last frame
            # time -- which would look like a long stall for a moment.
            _progress["t"] = time.monotonic()
            with _emergency_lock:
                _emergency_in_progress = False

            dropped = frames.resume() if frames is not None else 0
            _progress["t"] = time.monotonic()
            if dropped:
                print(f"(discarded {dropped} boundary frame(s) after emergency handling)")
            send_command(ser, "CMD:STATUS_LED_ON")  # back to solid = listening again


def _blink_status_led(ser, stop_event, interval_s=0.2):
    """Toggles the STATUS LED (separate physical LED on GPIO 27, firmware
    V8.7+) on/off until stop_event is set -- visual feedback that the device
    is actively transcribing, not just idly listening. Deliberately a
    different LED from the SOS/emergency one (CMD:LED_ON/CMD:LED_OFF) so the
    two never get visually confused with each other."""
    state = False
    while not stop_event.is_set():
        state = not state
        send_command(ser, "CMD:STATUS_LED_ON" if state else "CMD:STATUS_LED_OFF")
        stop_event.wait(interval_s)


def apply_capture_gain():
    """Sets the mic HAT's capture gain (see config.CAPTURE_PGA). Best-effort:
    a failure just leaves the card at whatever it was -- detection still
    works, only with less reach."""
    if config.CAPTURE_PGA is None:
        return
    try:
        result = subprocess.run(
            ["amixer", "-c", config.CAPTURE_CARD, "-q", "sset", "PGA", str(config.CAPTURE_PGA)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print(f"Mic capture gain set: PGA {config.CAPTURE_PGA} ({config.CAPTURE_PGA * 0.5:.1f} dB)")
        else:
            print(f"WARNING: could not set mic gain: {result.stderr.strip()}")
    except Exception as e:
        print(f"WARNING: could not set mic gain: {e}")


# Test hook: `touch /home/alisto/range_test.flag` makes a recognised wake word
# only get LOGGED ("RANGE TEST: heard it") instead of raising an emergency, so
# how far away the device can hear can be tested without an announcement and
# an SMS for every attempt. `rm` it to return to normal -- it is checked on
# every detection, so no restart is needed either way.
RANGE_TEST_FLAG = "/home/alisto/range_test.flag"


def voice_listen_loop(ser):
    """Continuously listens, isolates each utterance with VAD (same
    segmenter used by the old DTW system), and runs it through whisper.cpp.
    No separate wake-word stage -- say the whole phrase ("Alisto Tabang") in
    one breath, same as the very first Whisper-based version, just with a
    properly tuned/quantized model+flags instead of the ~60s-per-utterance
    setup that started this whole project.

    LED: solid ON while idly listening, blinking while transcribing, OFF
    while an emergency is actively being handled (trigger_emergency() owns
    that -- covers both voice and physical-button triggers uniformly), back
    to solid ON once done.

    The mic is paused entirely while transcribing (see audio_io.FrameStream)
    so nothing gets recorded/queued during that ~4-5s window -- otherwise
    speech captured while busy would only get processed AFTER the current
    one finishes, compounding delay if someone repeats the phrase while
    waiting. Matches the LED: blinking means genuinely not listening, not
    listening-but-slow.
    """
    global frames

    apply_capture_gain()
    print("Whisper-based voice detection ready. Listening...")
    send_command(ser, "CMD:STATUS_LED_ON")  # solid = ready/listening
    segmenter = vad.Segmenter()
    frames = audio_io.FrameStream()
    _progress["t"] = time.monotonic()
    _progress["listening"] = True

    # Starting the mic stream makes the same DC step/click as restarting it
    # after a pause, so the very first moments get the same settle window.
    if config.AUDIO_PREPROCESS:
        segmenter.restart()
    last_frame_t = time.monotonic()
    for frame in frames:
        now = time.monotonic()
        _progress["t"] = now  # watchdog heartbeat, see watchdog_loop()
        if config.AUDIO_PREPROCESS and now - last_frame_t > config.FRAME_GAP_RESTART_S:
            # The mic was paused (transcription, an emergency, a reminder...)
            # and has just restarted -- see Segmenter.restart().
            segmenter.restart()
        last_frame_t = now
        if _emergency_in_progress:
            continue  # don't try to match while already handling one

        utterance = segmenter.push(frame)
        if utterance is None:
            continue

        duration_s = len(utterance) / config.SAMPLE_RATE
        print(f"Utterance captured ({duration_s:.2f}s) -- transcribing...")

        audio_io.save_wav(WHISPER_TEMP_WAV, utterance)

        # Held for the whole pause-through-resume window -- shared with
        # trigger_emergency() and play_medicine_reminder() so only one of
        # transcription/emergency/reminder ever touches `frames` at a time
        # (real testing crashed the process when a reminder and a
        # transcription both tried to pause/resume it concurrently).
        with _audio_state_lock:
            frames.pause()  # mic off entirely while transcribing

            stop_blink = threading.Event()
            blink_thread = threading.Thread(
                target=_blink_status_led, args=(ser, stop_blink), daemon=True
            )
            blink_thread.start()

            start = time.monotonic()
            text = whisper_asr.transcribe(WHISPER_TEMP_WAV)
            elapsed_s = time.monotonic() - start

            stop_blink.set()
            blink_thread.join()

            dropped = frames.resume()  # mic back on, drop boundary stragglers
            if dropped:
                print(f"(discarded {dropped} boundary frame(s))")

        if whisper_asr.has_wake_word(text):
            # Wake word heard -- now figure out WHICH emergency phrase was
            # said (fire/intruder/medication/fall/medical/general) by
            # searching the same transcript. Falls back to a generic
            # UNKNOWN alert if no specific keyword matched -- an emergency
            # is never silently dropped just because the category isn't clear.
            category, message = categorize.categorize(text)
            print(f"WAKE WORD + '{category}': '{text[:200]}' ({elapsed_s:.1f}s)")
            if os.path.exists(RANGE_TEST_FLAG):
                # Test mode: report the detection but don't raise an emergency
                # (no announcement, no SMS), so the pickup range can be tested.
                print("RANGE TEST: heard it -- would have triggered (not triggering)")
                send_command(ser, "CMD:STATUS_LED_ON")
                continue
            # trigger_emergency() itself pauses the mic and turns the status
            # LED off for the whole emergency flow (cancel window, audio
            # announcement, SMS send), and restores both when done -- covers
            # the physical SOS button path too, not just this voice path.
            trigger_emergency(ser, "voice", category=category, message=message)
        else:
            print(f"no match: '{text[:80]}' ({elapsed_s:.1f}s)")
            send_command(ser, "CMD:STATUS_LED_ON")  # back to solid = listening again


def play_medicine_reminder(ser, medicine, dosage):
    """Speaks a medicine reminder through the same speaker/TTS pipeline as
    emergency announcements. Mic pauses + status LED goes off for the
    duration, same as an emergency, but this is NOT an emergency -- no
    _emergency_in_progress, no cancel window, no SMS, just the spoken
    reminder."""
    # Same _audio_state_lock as transcription/trigger_emergency -- prevents
    # the exact crash real testing hit (this reminder pausing `frames` at
    # the same moment a whisper transcription was already pausing it).
    with _audio_state_lock:
        if frames is not None:
            frames.pause()
        send_command(ser, "CMD:STATUS_LED_OFF")

        try:
            audio_path = tts.synthesize_reminder(medicine, dosage)
            if audio_path:
                stream_audio(ser, audio_path)
            else:
                print("Medicine reminder TTS unavailable -- skipping this reminder")
        finally:
            dropped = frames.resume() if frames is not None else 0
            if dropped:
                print(f"(discarded {dropped} boundary frame(s) after medicine reminder)")
            send_command(ser, "CMD:STATUS_LED_ON")


def _normalize_reminder_time(time_str):
    """The app saves reminder_time as 12-hour '%I:%M %p' (e.g. '2:30 AM'),
    not the 24-hour 'HH:MM' this file originally assumed -- comparing that
    directly against datetime.now().strftime('%H:%M') (e.g. '02:30') never
    matched, so every reminder silently never fired. Normalizes either
    format to 24-hour 'HH:MM' before comparing."""
    time_str = time_str.strip()
    try:
        return datetime.strptime(time_str, "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return time_str  # already "HH:MM", or unrecognized -- leave as-is


def _minutes_since_midnight(hhmm):
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def _should_fire_now(reminder, now):
    """Whether `reminder` is due at `now` (a datetime), per its
    schedule_type (see MedicineModel's doc comment on the app side for
    the same three shapes):
      - 'daily' (default/unrecognized): fires once, at reminder_time,
        every day.
      - 'every_hours': fires every interval_value hours, anchored to
        reminder_time as the first dose of the day -- e.g. anchor 02:30 +
        4h fires at 02:30, 06:30, 10:30, ..., wrapping across the 24h
        day.
      - 'every_days': fires at reminder_time, but only on days where
        (today - start_date).days is an exact multiple of interval_value
        (e.g. interval_value=2 -> every other day, counted from
        start_date)."""
    schedule_type = reminder.get("schedule_type") or "daily"
    anchor = _normalize_reminder_time(reminder.get("reminder_time", ""))
    current_time = now.strftime("%H:%M")

    if schedule_type == "every_hours":
        try:
            interval_min = int(reminder.get("interval_value") or 0) * 60
            anchor_min = _minutes_since_midnight(anchor)
            current_min = _minutes_since_midnight(current_time)
        except (ValueError, TypeError, IndexError):
            return False
        if interval_min <= 0:
            return False
        return (current_min - anchor_min) % interval_min == 0

    if schedule_type == "every_days":
        start_date_str = reminder.get("start_date")
        if not start_date_str:
            return current_time == anchor  # no anchor date -- treat as daily
        try:
            interval_value = int(reminder.get("interval_value") or 1)
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            days_since_start = (now.date() - start_date).days
        except (ValueError, TypeError):
            return current_time == anchor
        if interval_value <= 0 or days_since_start < 0:
            return current_time == anchor
        return days_since_start % interval_value == 0 and current_time == anchor

    return current_time == anchor  # 'daily'


def medicine_reminder_loop(ser):
    """Checks the schedule every REMINDER_CHECK_INTERVAL_S using the Pi's
    clock (kept accurate via the RTC sync at startup). The schedule itself
    is read live from Firestore each check (medication_reminder, scoped to
    this device's elder_id) -- reminders added/edited/deleted through the
    app take effect on the very next check, no restart needed. Does
    nothing until pairing resolves _elder_id (device_pairing_watcher_loop).

    Each reminder fires at most once per matching minute -- tracks which
    (date, reminder_id, time) triples already fired so it doesn't repeat
    every check within the same trigger minute. Note this key includes
    the specific time, not just the date: an 'every_hours' reminder is
    meant to fire several times a day, so keying on date alone (as this
    used to) would have blocked every firing after the first each day.
    Skips (and retries next tick, doesn't drop) a reminder that lands
    exactly during an active real emergency, so it never interrupts or
    gets lost. Marks the reminder 'Notified' in Firestore right after
    actually speaking it, so the app's UI reflects that it fired."""
    fired = set()

    while True:
        if _elder_id is None:
            time.sleep(REMINDER_CHECK_INTERVAL_S)
            continue

        now = datetime.now()
        current_time = now.strftime("%H:%M")
        today = now.date()

        reminders = firestore_backend.get_active_medicine_reminders(_elder_id)
        for reminder in reminders:
            key = (today, reminder["id"], current_time)
            if _should_fire_now(reminder, now) and key not in fired:
                if _emergency_in_progress:
                    continue  # retry next tick instead of skipping outright
                fired.add(key)
                medicine_name = reminder["medicine_name"]
                print(f"Medicine reminder: {medicine_name} at {current_time}")
                # No dosage field in this schema -- just the name.
                play_medicine_reminder(ser, medicine_name, None)
                firestore_backend.mark_medicine_notified(reminder["id"])

        time.sleep(REMINDER_CHECK_INTERVAL_S)


WIFI_CONNECTED_FLAG = "/home/alisto/wifi_just_connected.flag"
WIFI_FLAG_CHECK_INTERVAL_S = 5  # one-time post-setup event, not time-critical

DEVICE_PAIRED_MARKER = "/home/alisto/.device_paired"  # local marker so a
# restart doesn't repeat the "Congratulations" announcement once this unit
# has already been claimed once
DEVICE_PAIRING_CHECK_INTERVAL_S = 15


def play_device_paired_announcement(ser, name):
    """Same pause-mic/status-LED-off/resume pattern as every other
    announcement -- see play_medicine_reminder() for why _audio_state_lock
    is required."""
    with _audio_state_lock:
        if frames is not None:
            frames.pause()
        send_command(ser, "CMD:STATUS_LED_OFF")

        try:
            audio_path = tts.synthesize_device_paired(name)
            if audio_path:
                stream_audio(ser, audio_path)
            else:
                print("Device-paired TTS unavailable -- skipping announcement")
        finally:
            dropped = frames.resume() if frames is not None else 0
            if dropped:
                print(f"(discarded {dropped} boundary frame(s) after device-paired announcement)")
            send_command(ser, "CMD:STATUS_LED_ON")


def device_pairing_watcher_loop(ser):
    """Polls Firestore (via the Admin SDK -- see firestore_backend.py for
    why this needs admin credentials rather than a normal user login) for
    this unit's own serial being claimed (registered to an elder_id) by a
    customer through the app.

    Unlike a one-shot check, this keeps running for the life of the
    process: resolving _elder_id isn't just for the welcome announcement
    -- alerts, GPS sync, medicine reminders, and emergency contacts (see
    trigger_emergency(), location_sync_loop(), medicine_reminder_loop())
    all need it too, and won't work until it's set. Re-resolving on every
    check also means a device that gets unlinked and relinked to a
    different elder (e.g. sold/reset) picks up the change automatically.

    The welcome announcement itself still only ever fires once -- tracked
    locally (DEVICE_PAIRED_MARKER) so a later restart doesn't repeat it."""
    global _elder_id

    already_announced = os.path.exists(DEVICE_PAIRED_MARKER)

    try:
        serial = firestore_backend.get_own_serial()
    except Exception as e:
        print(f"Device pairing: could not read own serial ({e}) -- skipping.")
        return

    print(f"Device pairing: watching for serial {serial!r} to be claimed...")

    confirmed = False  # _elder_id may come from the on-disk cache at startup;
                       # the first real Firestore answer still has to do the
                       # "set Online" step even if the value is unchanged
    while True:
        try:
            elder_id = firestore_backend.resolve_elder_id(serial)
        except Exception as e:
            print(f"Device pairing: Firestore check failed ({e})")
            elder_id = None

        if elder_id and (elder_id != _elder_id or not confirmed):
            confirmed = True
            _elder_id = elder_id
            _save_cached_elder_id(elder_id)
            firestore_backend.set_device_status(serial, "Online")
            print(f"Device pairing: resolved elder_id={elder_id!r} -- status set to Online.")

        if elder_id and not already_announced:
            full_name = None
            try:
                full_name = firestore_backend.get_elder_name(elder_id)
            except Exception as e:
                print(f"Device pairing: could not read elder name ({e})")
            already_announced = True
            try:
                with open(DEVICE_PAIRED_MARKER, "w"):
                    pass
            except OSError:
                pass
            play_device_paired_announcement(ser, full_name or "there")

        time.sleep(DEVICE_PAIRING_CHECK_INTERVAL_S)


LOCATION_SYNC_INTERVAL_S = 30  # how often the cached fix is CHECKED -- matches
                                # gps.py's own background poll interval
# ...but a Firestore document is only WRITTEN when the device has actually
# moved, or this long has passed. Every check used to write one (2,880 docs a
# day per device, forever): the Firebase free tier allows 20,000 writes a day
# in total, so a handful of devices would exhaust it -- which is what took the
# website down with 429 errors -- and the collection grew without bound for a
# device that sits in one room. 5 minutes keeps the app's "X minutes ago" on
# the Home screen's GPS card looking fresh.
LOCATION_WRITE_MIN_MOVE_M = 25
LOCATION_WRITE_MAX_AGE_S = 300


def _distance_m(a, b):
    """Great-circle distance in metres between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def location_write_due(fix, last_fix, last_write_mono, now_mono):
    """Whether this fix is worth a Firestore write: the first one, a move of
    LOCATION_WRITE_MIN_MOVE_M or more since the last write, or the periodic
    refresh."""
    if last_fix is None or last_write_mono is None:
        return True
    if now_mono - last_write_mono >= LOCATION_WRITE_MAX_AGE_S:
        return True
    return _distance_m(fix, last_fix) >= LOCATION_WRITE_MIN_MOVE_M


def location_sync_loop():
    """Pushes the most recent cached GPS fix (see gps.py -- never queried
    live here, same reasoning as trigger_emergency()'s use of it) to
    Firestore's device_location collection, so the app's Home screen GPS
    card and "Find Alisto" feature have something to show. Does nothing
    until pairing resolves _elder_id, and skips silently if there's no
    fix yet -- both are normal, not errors. Only writes when
    location_write_due() says so (see the note above)."""
    last_fix = None
    last_write_mono = None
    while True:
        if _elder_id is not None:
            fix = gps.get_last_fix()
            if fix and location_write_due(fix, last_fix, last_write_mono, time.monotonic()):
                lat, lon = fix
                barangay = reverse_geocode.barangay_for(lat, lon)
                address = f"{barangay}, Cebu City" if barangay else None
                try:
                    serial = firestore_backend.get_own_serial()
                    # write_location() returns None when the write failed --
                    # then nothing is recorded as written, so the next check
                    # tries again instead of waiting out the full interval.
                    if firestore_backend.write_location(serial, lat, lon, address):
                        last_fix, last_write_mono = fix, time.monotonic()
                except Exception as e:
                    print(f"Location sync: failed ({e})")
        time.sleep(LOCATION_SYNC_INTERVAL_S)


def play_wifi_connected_announcement(ser):
    """Speaks the 'connected to home WiFi' confirmation. Same
    pause-mic/status-LED-off/resume pattern as every other announcement --
    see play_medicine_reminder() for why _audio_state_lock is required."""
    with _audio_state_lock:
        if frames is not None:
            frames.pause()
        send_command(ser, "CMD:STATUS_LED_OFF")

        try:
            audio_path = tts.synthesize_wifi_connected()
            if audio_path:
                stream_audio(ser, audio_path)
            else:
                print("WiFi-connected TTS unavailable -- skipping announcement")
        finally:
            dropped = frames.resume() if frames is not None else 0
            if dropped:
                print(f"(discarded {dropped} boundary frame(s) after WiFi announcement)")
            send_command(ser, "CMD:STATUS_LED_ON")


# Test hook: `echo SIM > /home/alisto/test_sms_failure.flag` (or SIGNAL / LOAD /
# MODEM, empty = LOAD) makes the device speak the matching "SMS failed"
# announcement once, so it can be heard without pulling the SIM or shutting
# anything down. Polled by wifi_connected_watcher_loop() below; the file is
# removed as soon as it's picked up.
SMS_FAILURE_TEST_FLAG = "/home/alisto/test_sms_failure.flag"


def play_sms_failure_test(ser, reason):
    """Same pause-mic/status-LED-off/resume pattern as every other
    announcement -- see play_medicine_reminder() for why
    _audio_state_lock is required."""
    with _audio_state_lock:
        if frames is not None:
            frames.pause()
        send_command(ser, "CMD:STATUS_LED_OFF")
        try:
            audio_path = tts.synthesize_sms_failure(reason)
            if audio_path:
                stream_audio(ser, audio_path)
        finally:
            dropped = frames.resume() if frames is not None else 0
            if dropped:
                print(f"(discarded {dropped} boundary frame(s) after SMS-failure test)")
            send_command(ser, "CMD:STATUS_LED_ON")


def wifi_connected_watcher_loop(ser):
    """wifi_setup_server.py runs standalone (as root, triggered manually
    during device setup) and has no access to the ESP32/speaker -- that
    connection is owned exclusively by this process. So instead of talking
    to the speaker directly, it just drops a flag file when it successfully
    connects the Pi to the customer's home WiFi; this polls for that file
    and does the actual spoken announcement through the already-open,
    already-safe audio pipeline. Removes the flag immediately so a stray
    leftover file can't cause a repeat announcement on a later restart."""
    while True:
        if os.path.exists(WIFI_CONNECTED_FLAG):
            try:
                os.remove(WIFI_CONNECTED_FLAG)
            except OSError:
                pass
            print("WiFi setup: detected successful connection -- announcing.")
            play_wifi_connected_announcement(ser)
        if os.path.exists(SMS_FAILURE_TEST_FLAG):
            reason = "LOAD"
            try:
                with open(SMS_FAILURE_TEST_FLAG) as f:
                    reason = f.read().strip().upper() or "LOAD"
                os.remove(SMS_FAILURE_TEST_FLAG)
            except OSError:
                pass
            print(f"SMS-failure announcement TEST ({reason}).")
            play_sms_failure_test(ser, reason)
        time.sleep(WIFI_FLAG_CHECK_INTERVAL_S)


HEARTBEAT_INTERVAL_S = 30  # the app treats a device "offline" once its
# last_seen is much older than this (see app_models.dart's
# kHeartbeatStaleAfterSeconds, 3x this value) -- keep frequent enough that
# a real crash/power-loss is noticed within a reasonable time.


def heartbeat_loop():
    """Writes an 'I'm alive' timestamp to Firestore every
    HEARTBEAT_INTERVAL_S, regardless of pairing status -- only needs this
    unit's own serial (known immediately at boot via device_config.json),
    not _elder_id. See firestore_backend.send_heartbeat()'s doc comment
    for why this exists: without it, a crashed/powered-off Pi would show
    as 'Online' in the app forever, since nothing else ever flips
    'status' back."""
    try:
        serial = firestore_backend.get_own_serial()
    except Exception as e:
        print(f"Heartbeat: could not read own serial ({e}) -- skipping.")
        return

    while True:
        firestore_backend.send_heartbeat(serial)
        time.sleep(HEARTBEAT_INTERVAL_S)


def sync_sim_number():
    """Best-effort, runs once at startup -- attempts to auto-detect this
    SIM's own number via AT+CNUM and push it to Firestore (see
    alisto_sms.read_own_sim_number()'s doc comment for why this often
    can't be detected at all: most prepaid SIMs never have their own
    number programmed onto them by the carrier). When it can't be
    detected, the family can still enter it manually in the app's Device
    Information screen -- this is purely a nice-to-have shortcut for
    the SIMs where it does work. Uses gps.port_lock like every other AT
    command on this shared port, so it never collides with the
    background GPS poll loop."""
    try:
        with gps.port_lock:
            sim_number = read_own_sim_number()
        if sim_number:
            serial_id = firestore_backend.get_own_serial()
            firestore_backend.set_device_sim_number(serial_id, sim_number)
            print(f"SIM number auto-detected and synced: {sim_number}")
        else:
            print(
                "SIM number could not be auto-detected (this SIM likely has "
                "no MSISDN stored on it) -- can be set manually in the app."
            )
    except Exception as e:
        print(f"SIM number auto-detect skipped: {e}")


BATTERY_CHECK_INTERVAL_S = 30
# How many consecutive low readings in a row before actually announcing --
# a single momentary dip (e.g. the speaker itself drawing a current spike
# while announcing something) shouldn't trigger a false "low battery" on
# its own; requiring a few in a row filters that out.
BATTERY_CONSECUTIVE_CHECKS_REQUIRED = 2


def _is_undervoltage():
    """Reads the Pi's OWN built-in undervoltage flag on its 5V input rail
    (bit 0 of vcgencmd get_throttled's "currently" bits) -- no dedicated
    battery/fuel-gauge sensor on this unit. This only works when running
    off something that can actually sag (a power bank going flat); it
    can't report a percentage, only "about to brown out" vs "fine", and
    typically only fires seconds before real shutdown, not minutes. See
    battery_monitor_loop()'s own doc comment for the honest limits of
    this approach. Returns False on any failure (missing vcgencmd, parse
    error) rather than raising -- a failed battery check should never
    affect the emergency system."""
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
        # e.g. "throttled=0x50005"
        value = int(out.split("=")[1], 16)
        return bool(value & 0x1)
    except Exception:
        return False


def play_low_battery_announcement(ser):
    """Same pause-mic/status-LED-off/resume pattern as every other
    announcement -- see play_medicine_reminder() for why _audio_state_lock
    is required."""
    with _audio_state_lock:
        if frames is not None:
            frames.pause()
        send_command(ser, "CMD:STATUS_LED_OFF")

        try:
            audio_path = tts.synthesize_low_battery()
            if audio_path:
                stream_audio(ser, audio_path)
            else:
                print("Low-battery TTS unavailable -- skipping announcement")
        finally:
            dropped = frames.resume() if frames is not None else 0
            if dropped:
                print(f"(discarded {dropped} boundary frame(s) after low-battery announcement)")
            send_command(ser, "CMD:STATUS_LED_ON")


def battery_monitor_loop(ser):
    """Announces 'Low battery, please charge' once per sustained
    under-voltage episode -- a software-only stopgap for running off a
    power bank with no fuel-gauge sensor to report an actual percentage.
    A power bank's USB output stays a steady 5V right up until it's
    nearly empty, so there's no gradual signal to read; this only catches
    it very close to the end (seconds of real warning, not minutes).
    Re-arms once voltage recovers (e.g. after charging), so the NEXT
    low-battery episode announces again instead of only ever firing
    once per boot."""
    consecutive = 0
    already_announced = False
    while True:
        if _is_undervoltage():
            consecutive += 1
        else:
            consecutive = 0
            already_announced = False

        if consecutive >= BATTERY_CONSECUTIVE_CHECKS_REQUIRED and not already_announced:
            already_announced = True
            print("Battery: sustained undervoltage detected -- announcing low battery.")
            play_low_battery_announcement(ser)

        time.sleep(BATTERY_CHECK_INTERVAL_S)


# WATCHDOG. systemd's Restart=always only covers a CRASH; a program that hangs
# (mic stream dead, a blocking call that never returns, the emergency flow
# stuck) just sits there looking alive while the device is deaf -- the
# worst failure for an emergency device, and invisible to the family. The
# voice loop stamps _progress on every mic frame (~33/s); watchdog_loop()
# restarts the process when that stops for too long. It ALSO pings systemd
# (WATCHDOG=1, see systemd/alisto-emergency.service) only while healthy, so
# if the whole interpreter freezes -- where this thread couldn't run either --
# systemd itself kills and restarts it.
_progress = {"t": time.monotonic(), "listening": False}
WATCHDOG_CHECK_INTERVAL_S = 10
# Normal operation: frames arrive constantly, and every pause (whisper <=7s,
# reminders, announcements) is far shorter than this.
WATCHDOG_STALL_S = 120
# While an emergency is being handled (or before the voice loop has started)
# the mic is paused by design: an emergency with several contacts can
# legitimately run SMS retries for minutes, and startup includes the ~15s
# voice-model load and the modem/GPS setup.
WATCHDOG_BUSY_STALL_S = 600


def watchdog_stalled(now, last_progress, listening, emergency):
    """True when the voice loop has made no progress for longer than it
    legitimately could."""
    limit = WATCHDOG_STALL_S if (listening and not emergency) else WATCHDOG_BUSY_STALL_S
    return now - last_progress > limit


def _sd_notify(message):
    """Minimal systemd sd_notify (no extra package): a datagram to the
    socket named in $NOTIFY_SOCKET. Silently does nothing when not running
    under a unit that has the watchdog enabled."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]  # abstract socket namespace
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
    except OSError as e:
        print(f"sd_notify failed: {e}")


def watchdog_loop():
    while True:
        now = time.monotonic()
        if watchdog_stalled(now, _progress["t"], _progress["listening"], _emergency_in_progress):
            print(
                f"WATCHDOG: voice loop made no progress for {now - _progress['t']:.0f}s "
                f"(listening={_progress['listening']}, emergency={_emergency_in_progress}) "
                "-- restarting the service."
            )
            sys.stdout.flush()
            os._exit(1)  # systemd (Restart=always) brings it back in ~5s
        _sd_notify("WATCHDOG=1")
        time.sleep(WATCHDOG_CHECK_INTERVAL_S)


def main():
    threading.Thread(target=watchdog_loop, daemon=True).start()

    ser = open_esp32()
    print("ESP32 connected.")

    threading.Thread(target=esp32_reader, args=(ser,), daemon=True).start()

    # Correct the Pi's clock from the battery-backed RTC on every boot --
    # needs the reader thread already running to catch the TIME: reply.
    sync_clock_from_rtc(ser)

    # Pays the Piper voice model's ~14s load cost once, here, instead of
    # during the very first real emergency trigger.
    tts.preload()

    # Started once and left running continuously (not per-emergency) so the
    # receiver has the most possible time to get and refine a fix before
    # it's actually needed -- see gps.py for why both AT+CGPS=1 and the
    # AT+CVAUXS antenna power feed are required every startup.
    gps.enable_gps()
    try:
        with gps.port_lock:
            print(f"SIM health at startup: {read_sim_health()}")
    except Exception as e:
        print(f"SIM health at startup: check failed ({e})")
    threading.Thread(target=gps.poll_loop, daemon=True).start()

    threading.Thread(target=medicine_reminder_loop, args=(ser,), daemon=True).start()
    threading.Thread(target=wifi_connected_watcher_loop, args=(ser,), daemon=True).start()
    threading.Thread(target=device_pairing_watcher_loop, args=(ser,), daemon=True).start()
    threading.Thread(target=location_sync_loop, daemon=True).start()
    threading.Thread(target=sync_sim_number, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=battery_monitor_loop, args=(ser,), daemon=True).start()

    global _elder_id
    if _elder_id is None:
        _elder_id = _load_cached_elder_id()
        if _elder_id:
            print(f"Using cached elder_id {_elder_id!r} until Firestore confirms it.")
    threading.Thread(target=outbox_uploader_loop, daemon=True).start()

    print("ALISTO EMERGENCY SYSTEM READY")
    voice_listen_loop(ser)  # runs forever


if __name__ == "__main__":
    main()
