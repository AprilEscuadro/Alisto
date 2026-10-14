"""Store-and-forward for emergency alerts.

The SMS goes out over the SIM7600's own cellular link, so an emergency
works with no internet at all -- but the alert's record (the app's History
screen, the website's Emergency Logs/map, the push notification) lives in
Firestore. Previously, if the internet was down at that moment, those
writes gave up after Firestore's ~60s of retries and the alert was simply
never recorded, even once the internet came back.

Now every alert is written to a small JSON file in OUTBOX_DIR the instant it
starts (local disk, no network), updated as it progresses, and marked
`complete` when the flow finishes. A background uploader (see
process_pending(), run from alisto_main.outbox_uploader_loop()) sends each
complete record to Firestore as ONE atomic, idempotent batch (fixed document
IDs, so a retry can never create duplicates) and deletes the file only after
that succeeds. Offline, files just wait; the moment the internet is back
they upload and disappear.

Timestamps: the Pi has no working RTC battery, so offline its wall clock can
be wrong. Each record stores the monotonic clock + boot id at trigger time;
if the Pi hasn't rebooted by upload time, the real event time is recovered
as (now - seconds elapsed since the event), using the by-then-corrected
clock. After a reboot it falls back to the recorded wall time.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

OUTBOX_DIR = "/home/alisto/outbox"

# A record that never got marked `complete` (the process died mid-emergency,
# power loss) is uploaded as-is after this long, rather than stuck forever.
INCOMPLETE_AFTER_S = 600
# The push makes the family's phone ring an alarm, so it must only fire while
# the emergency is still current. After a longer outage the SMS (which needs
# no internet) has already told the contacts, and an alarm for something
# that happened minutes or days ago would only scare people -- the alert
# still lands in the app's History with its real timestamp, silently.
PUSH_MAX_AGE_S = 120

_lock = threading.RLock()
# Set to make the uploader run right away instead of waiting out its timer.
wake = threading.Event()


def _path(alert_id):
    return os.path.join(OUTBOX_DIR, f"{alert_id}.json")


def local_alert_id():
    """Fallback alert ID if Firestore's client couldn't even be created --
    any string is a valid document ID, this one just looks like Firestore's."""
    return uuid.uuid4().hex[:20]


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def save(rec):
    """Atomic write (temp file + rename) so a power cut mid-write can never
    leave a half-written, unreadable record."""
    with _lock:
        os.makedirs(OUTBOX_DIR, exist_ok=True)
        path = _path(rec["alert_id"])
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def new_record(alert_id, **fields):
    rec = {
        "alert_id": alert_id,
        "t_wall": time.time(),
        "t_mono": time.monotonic(),
        "boot_id": _boot_id(),
        "status": "PENDING",
        "complete": False,
        "uploaded": False,
        "early_created": False,
        "push_sent": False,
    }
    rec.update(fields)
    save(rec)
    return rec


def _read(alert_id):
    try:
        with open(_path(alert_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def update(alert_id, **fields):
    """Read-modify-write one record. No-op (returns None) if it's gone."""
    with _lock:
        rec = _read(alert_id)
        if rec is None:
            return None
        rec.update(fields)
        save(rec)
        return rec


def remove(alert_id):
    with _lock:
        try:
            os.remove(_path(alert_id))
        except OSError:
            pass


def load_all():
    with _lock:
        try:
            names = [n for n in os.listdir(OUTBOX_DIR) if n.endswith(".json")]
        except OSError:
            return []
        records = [r for r in (_read(n[:-5]) for n in names) if r]
    return sorted(records, key=lambda r: r.get("t_wall", 0))


def event_time(rec, now_wall=None, now_mono=None):
    """When the emergency really happened, as a timezone-aware datetime."""
    now_wall = time.time() if now_wall is None else now_wall
    now_mono = time.monotonic() if now_mono is None else now_mono
    if rec.get("boot_id") and rec.get("boot_id") == _boot_id() and "t_mono" in rec:
        ts = now_wall - (now_mono - rec["t_mono"])
    else:
        ts = rec.get("t_wall", now_wall)
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def process_pending(upload_fn, push_fn, now_wall=None, now_mono=None):
    """One pass over the outbox. `upload_fn(rec, event_time)` must write the
    whole record to Firestore idempotently and raise on any failure;
    `push_fn(rec)` returns True if a push notification went out. A record
    stays on disk until it's fully handled, so any failure just means it's
    tried again next pass. Returns how many records were finished."""
    now_wall = time.time() if now_wall is None else now_wall
    finished = 0
    for rec in load_all():
        alert_id = rec["alert_id"]
        when = event_time(rec, now_wall, now_mono)
        age_s = now_wall - when.timestamp()

        if not rec.get("complete"):
            if age_s < INCOMPLETE_AFTER_S:
                continue
            rec = update(alert_id, complete=True) or rec

        try:
            if not rec.get("uploaded"):
                upload_fn(rec, when)
                rec = update(alert_id, uploaded=True) or rec

            needs_push = (
                rec.get("status") != "CANCELLED"
                and not rec.get("push_sent")
                and age_s < PUSH_MAX_AGE_S
            )
            if needs_push:
                if not push_fn(rec):
                    continue  # uploaded fine, but push still pending -- retry
                update(alert_id, push_sent=True)

            remove(alert_id)
            finished += 1
        except Exception as e:
            print(f"outbox: {alert_id} not uploaded yet ({type(e).__name__}: {e}) -- will retry")
    return finished
