"""
firestore_backend.py

Connects the Pi directly to Firestore using a service account (Admin SDK)
-- NOT through the app's normal user login, since the Pi needs to look
itself up and write data (alerts, location) that isn't scoped to any one
logged-in user's session.

SCHEMA (matches the app's lib/services/firestore_service.dart, which in
turn matches a companion Flask backend "app.py" field-for-field -- see
that file's doc comment for the authoritative version):

  device/{serial}
    serial_number, elder_id, is_registered, status ("Pending"|"Online"|
    "Offline"), simNumber, gps, registeredAt
    -> Devices are PRE-SEEDED (by an admin/manufacturing step, doc ID =
       the serial itself) before a customer ever registers one. Claiming
       through the app only ever UPDATES this existing doc (sets
       is_registered, elder_id) -- it's never created by the app.

  elder_profile/{elderId}
    full_name, date_of_birth, address, sex, created_at, deleted_at,
    is_archived
    -> The actual person ALISTO is protecting (not the family member's
       own account -- that's a separate `user_account` doc). Created once,
       the first time a device is claimed.

  device_location/{autoId}
    device_id (= the serial), gps_lat, gps_long, location_address,
    recorded_at
    -> One doc per ping is fine; the app sorts client-side and only reads
       the most recent. No delete/cleanup needed for this to work, though
       a real deployment would want to prune old ones eventually.

  The collections below match the capstone paper's ERD (entity names
  lowercased/snake_cased for Firestore's own naming convention; Firestore
  has no real foreign keys, so every "*_id" field below is just the
  referenced doc's ID, same as elder_id/device_id already were):

  emergency_alert/{alertId}
    elder_id, device_id, location_id (-> device_location doc, nullable),
    phrase_id (-> distress_phrase doc, nullable), trigger_type
    ('VOICE'|'BUTTON'), alert_status ('PENDING'|'SENT'|'RESOLVED'|
    'CANCELLED'), title, lines (list of strings -- extra, not in the ERD,
    kept because the app's alert cards render from it), date_triggered,
    deleted_at, is_archived. The BHW/family website additionally sets
    acknowledged/acknowledged_by/acknowledged_at/resolved_by/
    resolution_note once someone acts on it -- the device never writes
    those. date_triggered is REQUIRED -- the app orders by it (and
    Firestore silently drops docs missing a field being ordered on in
    some query shapes).

  distress_phrase/{phraseId}
    phrase_text, phrase_category, status ('ACTIVE'|'INACTIVE'),
    created_at, updated_at
    -> get_or_create_phrase() finds-or-creates by exact phrase_text so
       repeated phrases share one doc, same idea as a normalized lookup
       table in a relational schema.

  voice_detection_log/{logId}
    device_id, alert_id, detected_phrase, confidence_score, noise_level,
    recognition_timestamp, is_emergency
    -> confidence_score/noise_level are written as null: the current
       whisper.cpp CLI integration and vad.py don't surface either value
       per utterance today. Left in place (rather than omitted) so the
       website's schema doesn't have to special-case a missing field,
       and as a documented hook for a future enhancement.

  alert_notification/{notificationId}
    alert_id, contact_id, sms_status ('PENDING'|'SENT'|'FAILED'),
    date_sent, failure_reason
    -> One per (alert, contact) pair -- who was actually notified about
       this specific alert.

  sms_delivery_log/{logId}
    notification_id, status ('QUEUED'|'SENT'|'FAILED'|'RETRIED'),
    queued_at, sent_at, delivery_time, retry_count, failure_reason
    -> One per send ATTEMPT, so every retry alisto_sms.py makes is
       individually auditable, not just the final outcome.

  emergency_status_history/{historyId}
    alert_id, status ('CREATED'|'SMS_SENT'|'ACKNOWLEDGED'|'RESOLVED'|
    'CLOSED'), changed_at, remarks
    -> The device only ever appends 'CREATED' and 'SMS_SENT' entries; the
       BHW/family website appends the rest when someone acts on the alert.

  medication_reminder/{reminderId}
    elder_id, medicine_name, reminder_time ("HH:MM"), status
    ("Upcoming"|"Notified"), is_active, created_at, updated_at
    -> Added through the app; the device reads these instead of a
       hardcoded schedule, and writes status back to "Notified" once it
       actually speaks the reminder.

  emergency_contact/{contactId}
    elder_id, full_name, relationship, contact_num, contact_type
    ("family"|"bhw")
    -> SMS recipients. Replaces the single hardcoded RECIPIENT number in
       alisto_sms.py.

find_owner_by_serial()-style cross-user lookups are no longer needed --
`device/{serial}` is now a direct top-level lookup by the serial itself,
no collection_group query or its index required.
"""

import json
import threading

import firebase_admin
from firebase_admin import credentials, firestore, messaging

SERVICE_ACCOUNT_PATH = "/home/alisto/firebase_service_account.json"
DEVICE_CONFIG_PATH = "/home/alisto/device_config.json"
MEDICINE_CACHE_PATH = "/home/alisto/.medicine_reminders_cache.json"
EMERGENCY_CONTACTS_CACHE_PATH = "/home/alisto/.emergency_contacts_cache.json"

_db = None
# Several background threads (heartbeat_loop, sync_sim_number,
# device_pairing_watcher_loop, location_sync_loop, medicine_reminder_loop)
# all call _get_db() within moments of each other at startup. Without a
# lock, two threads could both see `_db is None` and both call
# firebase_admin.initialize_app() -- the second one raises "The default
# Firebase app already exists," crashing whichever thread loses the race.
# Real testing hit exactly this once more threads were added. Standard
# double-checked locking: the cheap unlocked check avoids taking the lock
# on every call once _db is already set, which is the common case.
_db_lock = threading.Lock()


def _get_db():
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
                try:
                    firebase_admin.initialize_app(cred)
                except ValueError:
                    pass  # another thread won the race and already did this
                _db = firestore.client()
    return _db


def get_own_serial():
    """Reads this specific unit's serial number from its local config --
    kept out of the code itself so each physical device can have its own
    without editing Python."""
    with open(DEVICE_CONFIG_PATH) as f:
        return json.load(f)["serial"]


# ---------------------------------------------------------------------
# DEVICE / ELDER RESOLUTION
# ---------------------------------------------------------------------

def resolve_elder_id(serial):
    """Returns the elder_id this serial is claimed under, or None if the
    device doc doesn't exist yet or hasn't been claimed by anyone in the
    app yet."""
    db = _get_db()
    doc = db.collection("device").document(serial).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if data.get("is_registered") is not True:
        return None
    return data.get("elder_id")


def get_elder_name(elder_id):
    """Returns the elder's full_name, or None if the profile is missing
    (shouldn't normally happen once elder_id is non-null, but never let a
    lookup failure crash a spoken announcement)."""
    db = _get_db()
    doc = db.collection("elder_profile").document(elder_id).get()
    if not doc.exists:
        return None
    return doc.to_dict().get("full_name")


def send_heartbeat(serial):
    """Best-effort periodic 'I'm alive' timestamp on device/{serial}.
    last_seen -- lets the app distinguish a genuinely offline device (Pi
    crashed/lost power, stopped writing entirely) from one that's just
    paired but otherwise idle. 'status' alone can't do this: it only
    gets set to 'Online' once, at pairing time (see
    device_pairing_watcher_loop() in alisto_main.py), and nothing here
    ever flips it back -- so a dead Pi would show as 'Online' in the app
    forever without this. The app computes actual online/offline status
    by comparing this timestamp's age, not by trusting 'status' alone
    (see app_models.dart's effectiveDeviceStatus())."""
    try:
        db = _get_db()
        db.collection("device").document(serial).update({
            "last_seen": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f"firestore: failed to send heartbeat: {e}")


def set_device_sim_number(serial, sim_number):
    """Best-effort push of an auto-detected SIM number (see
    alisto_sms.read_own_sim_number() for why this often can't be
    detected at all -- most prepaid SIMs never have their own number
    programmed onto them). A failure here just means the family sets it
    manually in the app's Device Information screen instead."""
    try:
        db = _get_db()
        db.collection("device").document(
            serial).update({"simNumber": sim_number})
    except Exception as e:
        print(f"firestore: failed to set SIM number: {e}")


def set_device_status(serial, status):
    """Updates device/{serial}.status ('Pending' | 'Online' | 'Offline') --
    drives the colored badge on the app's Home screen. Best-effort: a
    status update failing should never be allowed to crash anything else
    (voice detection, emergencies, etc. all keep working regardless)."""
    try:
        db = _get_db()
        db.collection("device").document(serial).update({"status": status})
    except Exception as e:
        print(f"firestore: failed to set device status to {status!r}: {e}")


# ---------------------------------------------------------------------
# LOCATION
# ---------------------------------------------------------------------

def write_location(serial, lat, lon, address=None):
    """Adds one device_location ping. The app only ever reads the most
    recent one (sorted client-side), so old pings are simply left behind
    -- no update-in-place, no cleanup needed for correctness. Returns the
    new ping's doc ID so trigger_emergency() can reference it as an
    emergency_alert's location_id, or None if the write failed."""
    try:
        db = _get_db()
        _, doc_ref = db.collection("device_location").add({
            "device_id": serial,
            "gps_lat": lat,
            "gps_long": lon,
            "location_address": address,
            "recorded_at": firestore.SERVER_TIMESTAMP,
        })
        return doc_ref.id
    except Exception as e:
        print(f"firestore: failed to write location: {e}")
        return None


# ---------------------------------------------------------------------
# EMERGENCY ALERTS
# (emergency_alert + distress_phrase + voice_detection_log +
#  alert_notification + sms_delivery_log + emergency_status_history --
#  matches the paper's ERD, see the module docstring above)
# ---------------------------------------------------------------------

def get_or_create_phrase(phrase_text, category):
    """Finds the distress_phrase doc for this exact phrase text, or
    creates one. Firestore has no real foreign keys, so an emergency_alert
    references this by ID the same way phrase_id does in the paper's ERD.
    Returns the doc ID, or None if phrase_text is empty or the lookup/
    write failed."""
    text = (phrase_text or "").strip()
    if not text:
        return None
    try:
        db = _get_db()
        existing = (
            db.collection("distress_phrase")
            .where("phrase_text", "==", text)
            .limit(1)
            .get()
        )
        for doc in existing:
            return doc.id
        _, doc_ref = db.collection("distress_phrase").add({
            "phrase_text": text,
            "phrase_category": category or "GENERAL_HELP",
            "status": "ACTIVE",
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        return doc_ref.id
    except Exception as e:
        print(f"firestore: failed to resolve distress phrase: {e}")
        return None


def write_voice_detection_log(device_id, alert_id, detected_phrase, is_emergency,
                              confidence_score=None, noise_level=None):
    """One row per recognized utterance. confidence_score/noise_level are
    passed through as-is (currently always None -- see module docstring)
    rather than faked, so the field is honest about what's actually
    measured today."""
    try:
        db = _get_db()
        db.collection("voice_detection_log").add({
            "device_id": device_id,
            "alert_id": alert_id,
            "detected_phrase": detected_phrase,
            "confidence_score": confidence_score,
            "noise_level": noise_level,
            "recognition_timestamp": firestore.SERVER_TIMESTAMP,
            "is_emergency": is_emergency,
        })
    except Exception as e:
        print(f"firestore: failed to write voice detection log: {e}")


def write_status_history(alert_id, status, remarks=None):
    """Appends one emergency_status_history entry. Best-effort and
    fire-and-forget -- a failure here should never affect the alert
    itself, which has already been written by the time this is called."""
    if not alert_id:
        return
    try:
        db = _get_db()
        db.collection("emergency_status_history").add({
            "alert_id": alert_id,
            "status": status,
            "changed_at": firestore.SERVER_TIMESTAMP,
            "remarks": remarks,
        })
    except Exception as e:
        print(f"firestore: failed to write status history: {e}")


def write_emergency_alert(elder_id, device_id, title, lines, trigger_type="VOICE",
                          location_id=None, phrase_id=None, alert_status="PENDING",
                          resolution_note=None):
    """Logs one emergency into emergency_alert so it shows up in the app's
    History screen, the Home screen's alert count/last-alert card, and
    the BHW/family website. `lines` is a list of short strings shown as
    the alert's body -- kept even though the paper's ERD doesn't have it,
    since the app's alert cards render straight from it. alert_status
    defaults to 'PENDING' for a real emergency; mark_alert_sms_sent()
    flips it to 'SENT' once at least one recipient's SMS goes through,
    and the website moves it on from there. Pass alert_status='CANCELLED'
    (with a resolution_note) instead for a false alarm -- the elder
    cancelled within the device's 5-second window -- so the website's
    False Alarms tab has something to count; see trigger_emergency()'s
    cancelled branch. Returns the new alert's doc ID (so a push
    notification -- see send_emergency_push() -- and the SMS loop's
    per-contact logging can reference it), or None if the write failed."""
    try:
        db = _get_db()
        _, doc_ref = db.collection("emergency_alert").add({
            "elder_id": elder_id,
            "device_id": device_id,
            "location_id": location_id,
            "phrase_id": phrase_id,
            "trigger_type": trigger_type,
            "alert_status": alert_status,
            "title": title,
            "lines": lines,
            "date_triggered": firestore.SERVER_TIMESTAMP,
            "deleted_at": None,
            "is_archived": False,
            "resolution_note": resolution_note,
        })
        write_status_history(
            doc_ref.id,
            "CANCELLED" if alert_status == "CANCELLED" else "CREATED",
            remarks=resolution_note,
        )
        return doc_ref.id
    except Exception as e:
        print(f"firestore: failed to write alert: {e}")
        return None


def update_emergency_alert(alert_id, **fields):
    """Best-effort partial update to an already-created emergency_alert doc.
    trigger_emergency() now calls write_emergency_alert() immediately when
    an emergency starts (before the TTS announcement and 5-second cancel
    window even run) so the website/app see it right away, then fills in
    the rest -- final message with location, location_id, phrase_id, or
    CANCELLED -- on this SAME doc once those steps finish, instead of a
    second doc appearing later. Never raises; a failed update just leaves
    the doc as it was."""
    if not alert_id:
        return
    try:
        db = _get_db()
        db.collection("emergency_alert").document(alert_id).update(fields)
        if fields.get("alert_status") == "CANCELLED":
            write_status_history(alert_id, "CANCELLED",
                                 remarks=fields.get("resolution_note"))
    except Exception as e:
        print(f"firestore: failed to update alert {alert_id}: {e}")


def mark_alert_sms_sent(alert_id):
    """Flips emergency_alert.alert_status to 'SENT' once at least one
    recipient's SMS went through, and logs the status-history entry.
    Best-effort: never allowed to affect the actual SMS sending."""
    if not alert_id:
        return
    try:
        db = _get_db()
        db.collection("emergency_alert").document(alert_id).update({
            "alert_status": "SENT",
        })
        write_status_history(alert_id, "SMS_SENT")
    except Exception as e:
        print(f"firestore: failed to mark alert {alert_id} as sent: {e}")


def write_alert_notification(alert_id, contact_id):
    """One alert_notification per (alert, contact) pair -- who was
    actually notified about this specific alert (paper ERD). Returns the
    new notification's doc ID so the SMS loop can attach sms_delivery_log
    rows to it, or None if alert_id/contact_id is missing or the write
    failed."""
    if not alert_id or not contact_id:
        return None
    try:
        db = _get_db()
        _, doc_ref = db.collection("alert_notification").add({
            "alert_id": alert_id,
            "contact_id": contact_id,
            "sms_status": "PENDING",
            "date_sent": None,
            "failure_reason": None,
        })
        return doc_ref.id
    except Exception as e:
        print(f"firestore: failed to write alert notification: {e}")
        return None


def write_sms_delivery_log(notification_id, status, retry_count, failure_reason=None):
    """One sms_delivery_log row per send ATTEMPT (paper ERD), so every
    retry alisto_sms.py makes is individually auditable. Also updates the
    parent alert_notification's sms_status/date_sent/failure_reason so
    the website's alert list can read just that one doc instead of
    joining against the log. No-op if notification_id is None (happens
    when the contact has no contact_id, e.g. the no-contacts-found
    fallback) -- there's nothing to attach the log to in that case."""
    if not notification_id:
        return
    try:
        db = _get_db()
        sent = status == "SENT"
        db.collection("sms_delivery_log").add({
            "notification_id": notification_id,
            "status": status,
            "queued_at": firestore.SERVER_TIMESTAMP,
            "sent_at": firestore.SERVER_TIMESTAMP if sent else None,
            "delivery_time": firestore.SERVER_TIMESTAMP if sent else None,
            "retry_count": retry_count,
            "failure_reason": failure_reason,
        })
        db.collection("alert_notification").document(notification_id).update({
            "sms_status": "SENT" if sent else "FAILED",
            "date_sent": firestore.SERVER_TIMESTAMP if sent else None,
            "failure_reason": failure_reason,
        })
    except Exception as e:
        print(f"firestore: failed to write SMS delivery log: {e}")


# ---------------------------------------------------------------------
# MEDICINE REMINDERS
# ---------------------------------------------------------------------

def get_active_medicine_reminders(elder_id):
    """Returns a list of {id, medicine_name, reminder_time} for every
    active ('is_active': True) reminder under this elder -- the device's
    real schedule, added through the app, replacing any hardcoded list.

    medicine_reminder_loop() calls this fresh every ~20s rather than
    reading a schedule loaded once at startup, so reminders added/edited/
    deleted through the app take effect on the next check. That design
    means a live Firestore read is normally required at the EXACT minute
    a reminder is due -- if WiFi happens to be down right then, the
    reminder would otherwise be silently skipped even though it was
    successfully fetched plenty of times earlier. To guard against that,
    every successful read is cached to disk, and a failed read falls back
    to that cache instead of returning nothing."""
    try:
        db = _get_db()
        docs = (
            db.collection("medication_reminder")
            .where("elder_id", "==", elder_id)
            .where("is_active", "==", True)
            .get()
        )
        reminders = [
            {
                "id": d.id,
                "medicine_name": d.to_dict().get("medicine_name", ""),
                "reminder_time": d.to_dict().get("reminder_time", ""),
                # schedule_type: 'daily' (default) | 'every_hours' |
                # 'every_days' -- see alisto_main.py's _should_fire_now()
                # for how each is interpreted.
                "schedule_type": d.to_dict().get("schedule_type") or "daily",
                "interval_value": d.to_dict().get("interval_value"),
                "start_date": d.to_dict().get("start_date"),
            }
            for d in docs
        ]
        try:
            with open(MEDICINE_CACHE_PATH, "w") as f:
                json.dump(reminders, f)
        except OSError as e:
            print(
                f"firestore: failed to cache medicine reminders locally: {e}")
        return reminders
    except Exception as e:
        print(
            f"firestore: failed to read medicine reminders ({e}) -- falling back to local cache")
        try:
            with open(MEDICINE_CACHE_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []


def mark_medicine_notified(reminder_id):
    """Called right after the device actually speaks a reminder, so the
    app's UI reflects that it fired (matches the 'status' StatusPill on
    the Medicines screen)."""
    try:
        db = _get_db()
        db.collection("medication_reminder").document(reminder_id).update({
            "status": "Notified",
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(
            f"firestore: failed to mark reminder {reminder_id} notified: {e}")


# ---------------------------------------------------------------------
# EMERGENCY CONTACTS
# ---------------------------------------------------------------------

def get_family_fcm_tokens(elder_id):
    """Returns the FCM push token for every family account linked to this
    elder (via family_elder_link -> user_account.fcm_token) -- who the app
    should push an emergency alert to. Skips accounts with no saved token
    (haven't opened the updated app yet, or denied notification
    permission) rather than failing the whole batch.

    Deduplicated -- real testing found several family_elder_link rows
    (different test accounts created during development) all sharing the
    SAME physical phone's token. Without deduping, send_emergency_push()
    would push once per link to that one phone, stacking N separate
    EmergencyAlertDialogs on top of each other -- needing "OK, I saw
    this" pressed N times to actually clear the screen, for what a real
    family member would experience as a single alert."""
    try:
        db = _get_db()
        links = (
            db.collection("family_elder_link")
            .where("elder_id", "==", elder_id)
            .get()
        )
        seen = set()
        tokens = []
        for link in links:
            family_user_id = link.to_dict().get("family_user_id")
            if not family_user_id:
                continue
            user_doc = db.collection(
                "user_account").document(family_user_id).get()
            if user_doc.exists:
                token = user_doc.to_dict().get("fcm_token")
                if token and token not in seen:
                    seen.add(token)
                    tokens.append(token)
        return tokens
    except Exception as e:
        print(f"firestore: failed to read family FCM tokens: {e}")
        return []


def send_emergency_push(elder_id, title, body, alert_id=None):
    """Pushes a notification to every family account linked to this elder,
    via the Admin SDK -- doesn't need each device's token individually
    passed in like a normal server would; firebase_admin already has full
    project access through the same service account used everywhere else
    in this file. Best-effort per-token: one stale/invalid token failing
    to send never blocks the others.

    Deliberately sends DATA-ONLY (no top-level `notification=` field).
    With a `notification` payload, Android auto-displays a system-tray
    entry itself whenever the app is backgrounded/killed and never hands
    the message to the app's own code at all -- which meant there was no
    way to run a real vibrate-and-buzz-for-3-minutes alarm in that state,
    only whatever one-shot sound/vibration the notification channel
    itself was configured with. Data-only messages always reach the
    app's own handlers instead (onMessage in the foreground,
    firebaseMessagingBackgroundHandler otherwise -- see
    notification_service.dart), which is what starts
    EmergencyAlarmService.kt's real alarm loop in every app state."""
    tokens = get_family_fcm_tokens(elder_id)
    if not tokens:
        print("firestore: no family FCM tokens to push to (nobody has opened the app with notifications yet)")
        return

    data = {"type": "emergency", "title": title, "body": body}
    if alert_id:
        data["alert_id"] = alert_id

    for token in tokens:
        try:
            message = messaging.Message(
                token=token,
                data=data,
                android=messaging.AndroidConfig(priority="high"),
            )
            messaging.send(message)
        except Exception as e:
            print(f"firestore: push failed for one token ({e})")


def _normalize_ph_number(num):
    """The app lets contacts be typed in local format ('09XXXXXXXXX'), but
    the SIM7600's AT+CMGS has only ever been confirmed working here with
    international format ('+639XXXXXXXX', see alisto_sms.py's RECIPIENT).
    Converts local-format numbers to international; leaves anything
    already starting with '+' (or any other unrecognized shape) alone
    rather than guessing."""
    num = num.strip()
    if num.startswith("+"):
        return num
    if num.startswith("0") and len(num) == 11:
        return "+63" + num[1:]
    return num


def get_emergency_contacts(elder_id):
    """Returns [{'contact_id':..., 'contact_num':...}, ...] for every
    contact (family + BHW) linked to this elder -- the real SMS recipient
    list, replacing the single hardcoded RECIPIENT in alisto_sms.py.
    contact_id is included (not just the bare number) so the SMS loop can
    log a proper alert_notification/sms_delivery_log row per contact,
    matching the paper's ERD.

    Same offline-resilience reasoning as get_active_medicine_reminders():
    the SMS send itself already works fully offline (SIM7600 modem, no
    WiFi needed) -- the only way an emergency would fail to reach every
    contact is if THIS list itself couldn't be fetched from Firestore at
    the exact moment of the emergency. Caches the last successful read so
    an offline trigger still has a recipient list to use."""
    try:
        db = _get_db()
        docs = (
            db.collection("emergency_contact")
            .where("elder_id", "==", elder_id)
            .get()
        )
        contacts = [
            {"contact_id": d.id, "contact_num": _normalize_ph_number(
                d.to_dict().get("contact_num"))}
            for d in docs
            if d.to_dict().get("contact_num")
            # Contacts removed in the web app are only archived (soft delete).
            and not d.to_dict().get("deleted_at")
            and d.to_dict().get("status") != "INACTIVE"
        ]
        try:
            with open(EMERGENCY_CONTACTS_CACHE_PATH, "w") as f:
                json.dump(contacts, f)
        except OSError as e:
            print(
                f"firestore: failed to cache emergency contacts locally: {e}")
        return contacts
    except Exception as e:
        print(
            f"firestore: failed to read emergency contacts ({e}) -- falling back to local cache")
        try:
            with open(EMERGENCY_CONTACTS_CACHE_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []
