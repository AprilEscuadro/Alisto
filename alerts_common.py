"""
Emergency alerts — shared helpers.

WHAT THE DEVICE WRITES
-----------------------
One document per alert in the `emergency_alert` collection -- matches the
capstone paper's ERD (EMERGENCY_ALERT), with Firestore's document IDs
standing in for the paper's integer primary/foreign keys:

    elder_id        str    required. The elder_profile document ID.
    device_id       str    the device document ID (its serial number).
    location_id     str    optional. -> a device_location doc.
    phrase_id       str    optional. -> a distress_phrase doc.
    trigger_type    str    'VOICE' | 'BUTTON'.
    alert_status    str    'PENDING' at creation, 'SENT' once an SMS goes
                           out to at least one contact. The website moves
                           it on to 'RESOLVED' or 'CANCELLED' (false
                           alarm) -- see row_status() below for why
                           'ACKNOWLEDGED' isn't a fifth value here.
    title           str    e.g. "Fall Injury emergency".
    lines           list   short strings shown as the alert's body.
    date_triggered  time   required. Use firestore.SERVER_TIMESTAMP.
    deleted_at      time   optional.
    is_archived     bool   optional.

Everything else (barangay, the elder's name/age, response time, GPS
coordinates, SMS delivery counts) is worked out by the website from
related collections -- the device does not send it directly on the alert
doc.

THE BHW PAGES FILL IN THE REST, on the same emergency_alert doc:
    acknowledged / acknowledged_at / acknowledged_by   set on Acknowledge
    resolved_at / resolved_by / resolution_note        set on Resolve

These four fields aren't in the paper's EMERGENCY_ALERT table -- the paper
tracks who-did-what purely through emergency_status_history's audit trail.
They're kept here too as a denormalized "current status" cache on the
alert itself, which is a completely ordinary NoSQL pattern (Firestore has
no joins, so a list page would otherwise need a separate query per alert
just to show who acknowledged it). write_status_history() below still
appends the real audit-trail entry every time, so both views agree.

Related collections (also paper-ERD-aligned; see firestore_backend.py on
the Pi for the authoritative field list):
    distress_phrase, voice_detection_log, device_location,
    alert_notification, sms_delivery_log, emergency_status_history
"""
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore

ALERT_COLLECTION = 'emergency_alert'
STATUS_HISTORY_COLLECTION = 'emergency_status_history'
NOTIFICATION_COLLECTION = 'alert_notification'
LOCATION_COLLECTION = 'device_location'
PHRASE_COLLECTION = 'distress_phrase'

# type -> (label, icon, colour class used in the templates). The device
# only ever writes real emergencies (no per-category type field exists in
# the paper's EMERGENCY_ALERT table), so 'emergency' is the only key any
# real alert row resolves to -- the rest are kept so the lookup never
# KeyErrors on old/manually-edited data.
ALERT_TYPES = {
    'emergency': ('Emergency', 'siren', 'red'),
    'fall': ('Fall Detected', 'person-standing', 'red'),
    'help': ('Help Request', 'hand', 'orange'),
    'medication': ('Medication Reminder', 'bell', 'orange'),
    'health_check': ('Health Check-in', 'heart-pulse', 'blue'),
    'false_alarm': ('False Alarm', 'flag', 'gray'),
}

# Display status (the lowercase vocabulary the templates were already
# built around) -> (label, badge class).
ALERT_STATUSES = {
    'active': ('Active', 'alert-active'),
    'acknowledged': ('Responded', 'alert-acknowledged'),
    'resolved': ('Resolved', 'alert-resolved'),
    'closed': ('Closed', 'alert-closed'),
    'delivered': ('Delivered', 'alert-delivered'),
    'false_alarm': ('False Alarm', 'alert-closed'),
}

# display-status values that still need someone to act.
OPEN_STATUSES = ('active',)
# display-status values that count as handled, for the Reports page.
RESOLVED_STATUSES = ('resolved', 'closed', 'false_alarm')

PH_TIME = timezone(timedelta(hours=8))


def local(value):
    """Firestore stores UTC; the barangay reads Philippine time."""
    if not hasattr(value, 'strftime'):
        return None
    return value.astimezone(PH_TIME) if getattr(value, 'tzinfo', None) else value


def fmt_time(value):
    """'Today, 10:24 AM' / 'Yesterday, 6:30 PM' / 'May 22, 2026 10:24 AM'."""
    moment = local(value)
    if not moment:
        return '—'
    today = datetime.now(PH_TIME).date()
    clock = moment.strftime('%I:%M %p').lstrip('0')
    if moment.date() == today:
        return f'Today, {clock}'
    if moment.date() == today - timedelta(days=1):
        return f'Yesterday, {clock}'
    # Drop the year for dates in the current year, to keep table columns narrow.
    day = moment.strftime(
        '%b %d') if moment.year == today.year else moment.strftime('%b %d, %Y')
    return f'{day}, {clock}'


def type_label(type_key):
    return ALERT_TYPES.get(type_key, ALERT_TYPES['emergency'])[0]


def type_icon(type_key):
    return ALERT_TYPES.get(type_key, ALERT_TYPES['emergency'])[1]


def type_colour(type_key):
    return ALERT_TYPES.get(type_key, ALERT_TYPES['emergency'])[2]


def status_label(status_key):
    return ALERT_STATUSES.get(status_key, ALERT_STATUSES['active'])[0]


def status_class(status_key):
    return ALERT_STATUSES.get(status_key, ALERT_STATUSES['active'])[1]


_ROW_STATUS_BY_ALERT_STATUS = {
    'PENDING': 'active',
    'SENT': 'active',
    'RESOLVED': 'resolved',
    'CANCELLED': 'false_alarm',
}


def row_status(alert):
    """Translates the raw alert_status ('PENDING'|'SENT'|'RESOLVED'|
    'CANCELLED', the paper's 4-value enum) plus the website-only
    `acknowledged` flag into the lowercase display status the templates
    expect ('active'|'acknowledged'|'resolved'|'false_alarm').
    'acknowledged' isn't one of the paper's alert_status values -- an
    acknowledged-but-unresolved alert is still alert_status 'SENT'
    underneath, tracked separately so a BHW can acknowledge without
    prematurely closing the alert out."""
    raw = (alert.get('alert_status') or 'PENDING').upper()
    if raw in ('RESOLVED', 'CANCELLED'):
        return _ROW_STATUS_BY_ALERT_STATUS[raw]
    return 'acknowledged' if alert.get('acknowledged') else _ROW_STATUS_BY_ALERT_STATUS.get(raw, 'active')


def response_minutes(alert):
    """Minutes between the alert arriving and a BHW acknowledging it."""
    created, answered = local(alert.get('date_triggered')), local(
        alert.get('acknowledged_at'))
    if not created or not answered:
        return None
    minutes = (answered - created).total_seconds() / 60
    return minutes if minutes >= 0 else None


def sms_sent_count(db, alert_id):
    """How many contacts were actually texted for this alert -- counted
    from alert_notification.sms_status == 'SENT', which the Pi's
    write_sms_delivery_log() keeps in sync with the real per-attempt log
    in sms_delivery_log. One query per alert (fine at capstone-demo
    scale); returns 0 on any failure rather than raising, since this is
    just a display count."""
    if not alert_id:
        return 0
    try:
        docs = (
            db.collection(NOTIFICATION_COLLECTION)
            .where('alert_id', '==', alert_id)
            .where('sms_status', '==', 'SENT')
            .get()
        )
        return len(docs)
    except Exception:
        return 0


def alert_location(db, location_id):
    """(latitude, longitude, address) for an alert's location_id, or
    (None, None, None) if it has none or the lookup fails."""
    if not location_id:
        return None, None, None
    try:
        doc = db.collection(LOCATION_COLLECTION).document(location_id).get()
        if not doc.exists:
            return None, None, None
        data = doc.to_dict() or {}
        return data.get('gps_lat'), data.get('gps_long'), data.get('location_address')
    except Exception:
        return None, None, None


def latest_device_location(db, device_id):
    """(latitude, longitude, address, recorded_at) for a device's most
    recent device_location ping, or (None, None, None, None) if it has
    none or the lookup fails. This is where the Pi's write_location()
    actually writes live GPS -- a device doc's own fields are NEVER
    updated with location by any current code path (real testing found
    the admin map reading device.latitude/device.longitude directly,
    which either doesn't exist at all for a device that's only ever
    reported through this collection, or is frozen at whatever a much
    older write happened to leave there -- either way, not the resident's
    real, current location)."""
    if not device_id:
        return None, None, None, None
    try:
        docs = list(
            db.collection(LOCATION_COLLECTION)
            .where('device_id', '==', device_id)
            .order_by('recorded_at', direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
    except Exception:
        docs = None

    if docs is None:
        # Usually a missing composite index -- fall back to an unordered
        # read and sort in Python instead of failing the whole lookup.
        try:
            docs = list(db.collection(LOCATION_COLLECTION)
                        .where('device_id', '==', device_id).stream())
            docs.sort(key=lambda d: getattr(d.to_dict().get('recorded_at'), 'timestamp', lambda: 0)(),
                      reverse=True)
        except Exception:
            return None, None, None, None

    if not docs:
        return None, None, None, None
    data = docs[0].to_dict() or {}
    return data.get('gps_lat'), data.get('gps_long'), data.get('location_address'), data.get('recorded_at')


def alert_phrase_text(db, phrase_id):
    """The distress_phrase.phrase_text an alert's phrase_id points to, or
    '' if it has none or the lookup fails."""
    if not phrase_id:
        return ''
    try:
        doc = db.collection(PHRASE_COLLECTION).document(phrase_id).get()
        if not doc.exists:
            return ''
        return (doc.to_dict() or {}).get('phrase_text') or ''
    except Exception:
        return ''


def alert_locations_bulk(db, location_ids):
    """{location_id: (lat, lng, address)} for a batch of location_ids in
    ONE round trip (db.get_all), instead of one .get() per alert. Use this
    (not alert_location()) whenever building a LIST of alert rows --
    calling the single-lookup version once per row turned an N-alert page
    into 3N extra Firestore round trips, noticeably slow past a handful
    of alerts."""
    ids = sorted({i for i in location_ids if i})
    if not ids:
        return {}
    try:
        refs = [db.collection(LOCATION_COLLECTION).document(i) for i in ids]
        result = {}
        for doc in db.get_all(refs):
            if doc.exists:
                data = doc.to_dict() or {}
                result[doc.id] = (data.get('gps_lat'), data.get(
                    'gps_long'), data.get('location_address'))
        return result
    except Exception:
        return {}


def alert_phrases_bulk(db, phrase_ids):
    """{phrase_id: phrase_text} for a batch of phrase_ids in ONE round
    trip. See alert_locations_bulk()'s docstring -- same reasoning."""
    ids = sorted({i for i in phrase_ids if i})
    if not ids:
        return {}
    try:
        refs = [db.collection(PHRASE_COLLECTION).document(i) for i in ids]
        result = {}
        for doc in db.get_all(refs):
            if doc.exists:
                result[doc.id] = (doc.to_dict() or {}).get('phrase_text') or ''
        return result
    except Exception:
        return {}


def sms_sent_counts_bulk(db, alert_ids):
    """{alert_id: count of SENT notifications} for a batch of alert_ids.
    Firestore 'in' queries cap at 30 values, so this chunks into groups of
    30 -- still a small constant number of round trips rather than one
    query per alert."""
    ids = [i for i in dict.fromkeys(alert_ids) if i]  # de-dup, keep order
    counts = {i: 0 for i in ids}
    if not ids:
        return counts
    try:
        for start in range(0, len(ids), 30):
            chunk = ids[start:start + 30]
            docs = (
                db.collection(NOTIFICATION_COLLECTION)
                .where('alert_id', 'in', chunk)
                .where('sms_status', '==', 'SENT')
                .get()
            )
            for d in docs:
                aid = (d.to_dict() or {}).get('alert_id')
                if aid in counts:
                    counts[aid] += 1
        return counts
    except Exception:
        return counts


def write_status_history(db, alert_id, status, changed_by_user_id=None, remarks=None):
    """Appends one emergency_status_history entry -- the website's half of
    the same audit trail the Pi writes 'CREATED'/'SMS_SENT' entries to.
    Best-effort: a failure here should never block the actual status
    update on the alert doc, which the caller writes separately."""
    try:
        db.collection(STATUS_HISTORY_COLLECTION).add({
            'alert_id': alert_id,
            'status': status,
            'changed_at': firestore.SERVER_TIMESTAMP,
            'changed_by_user_id': changed_by_user_id,
            'remarks': remarks,
        })
    except Exception as e:
        print(f'[alerts_common] failed to write status history: {e}')


# ---------------------------------------------------------------------
# SHARED WRITES -- both the family dashboard (app.py) and the BHW portal
# (bhw_routes.py) let a logged-in user act on an alert; both call these
# instead of duplicating the same update a third time.
# ---------------------------------------------------------------------

def acknowledge(db, ref, alert_id, user_id):
    """Marks an alert acknowledged ("Responded") without closing it out.
    alert_status itself is left alone (the paper's alert_status enum has
    no 'ACKNOWLEDGED' value -- see row_status()). Returns False if the
    alert was already acknowledged/resolved/cancelled, True on success.

    Also acknowledges every OTHER still-open alert for the same elder --
    a real panic can mean more than one button/voice trigger before anyone
    responds (the Pi logs a separate alert doc per press, no debounce), so
    one BHW/family member seeing ONE of them counts as having seen the
    whole episode instead of leaving the rest open (and their admin-map
    marker stuck) until someone happens to acknowledge each press on its
    own. Mirrors FirestoreService.acknowledgeAlert() on the mobile app,
    which writes straight to Firestore and never goes through this
    function."""
    alert = ref.get().to_dict() or {}
    if row_status(alert) != 'active':
        return False
    ref.update({
        'acknowledged': True,
        'acknowledged_at': firestore.SERVER_TIMESTAMP,
        'acknowledged_by': user_id,
    })
    write_status_history(db, alert_id, 'ACKNOWLEDGED', user_id)

    elder_id = alert.get('elder_id')
    if elder_id:
        try:
            siblings = (
                db.collection(ALERT_COLLECTION)
                .where('elder_id', '==', elder_id)
                .where('alert_status', 'in', ['PENDING', 'SENT'])
                .stream()
            )
            for doc in siblings:
                if doc.id == alert_id:
                    continue
                sibling = doc.to_dict() or {}
                if sibling.get('acknowledged'):
                    continue
                doc.reference.update({
                    'acknowledged': True,
                    'acknowledged_at': firestore.SERVER_TIMESTAMP,
                    'acknowledged_by': user_id,
                })
                write_status_history(
                    db, doc.id, 'ACKNOWLEDGED', user_id,
                    remarks=f'Auto-acknowledged together with alert {alert_id}',
                )
        except Exception as e:
            print(
                f'[alerts_common] failed to bulk-acknowledge sibling alerts: {e}')
    return True


def resolve(db, ref, alert_id, user_id, false_alarm=False, notes=None):
    """Marks an alert 'RESOLVED', or 'CANCELLED' if [false_alarm]. Backfills
    acknowledged/acknowledged_at/by if nobody pressed Acknowledge first, so
    response-time reporting still has a timestamp to work with. Returns the
    new alert_status."""
    alert = ref.get().to_dict() or {}
    new_status = 'CANCELLED' if false_alarm else 'RESOLVED'
    resolution_note = (notes or '').strip()
    updates = {
        'alert_status': new_status,
        'resolved_at': firestore.SERVER_TIMESTAMP,
        'resolved_by': user_id,
        'resolution_note': resolution_note,
    }
    if not alert.get('acknowledged_at'):
        updates['acknowledged'] = True
        updates['acknowledged_at'] = firestore.SERVER_TIMESTAMP
        updates['acknowledged_by'] = user_id
    ref.update(updates)
    write_status_history(db, alert_id, new_status, user_id,
                         remarks=resolution_note or None)
    return new_status


# The Flutter app's own alarm (EmergencyAlarmService.DURATION_MS) auto-stops
# after this long, so an unattended household stops getting any louder about
# it right around here too -- past this point nobody at home is being
# reminded, so the BHW needs to find out some other way.
AUTO_ESCALATE_AFTER_MINUTES = 3
AUTO_ESCALATE_LABEL = 'Auto (3-min no response)'


def auto_escalate_stale_alerts(db):
    """Flags every alert still unacknowledged after AUTO_ESCALATE_AFTER_MINUTES
    the same way flag_alert_for_bhw() does manually (admin_flagged/_by/_at),
    so it shows up on the BHW's Alerts page as "Flagged" without an admin
    having to notice and click Notify BHW themselves. admin_flagged_by is
    set to AUTO_ESCALATE_LABEL rather than a real name so the two origins
    are never confused; auto_escalated guards against re-flagging (and
    re-notifying) the same alert on every sweep. Meant to be called
    periodically by a background loop -- best-effort throughout, since a
    bad sweep should never take the caller down. Returns how many alerts
    were newly flagged."""
    cutoff = datetime.now(timezone.utc) - \
        timedelta(minutes=AUTO_ESCALATE_AFTER_MINUTES)
    flagged = 0
    try:
        docs = db.collection(ALERT_COLLECTION).where(
            'alert_status', 'in', ['PENDING', 'SENT']).stream()
    except Exception as e:
        print(f'[alerts_common] auto_escalate_stale_alerts query failed: {e}')
        return 0

    for doc in docs:
        try:
            alert = doc.to_dict() or {}
            if alert.get('acknowledged') or alert.get('admin_flagged') or alert.get('auto_escalated'):
                continue
            triggered = alert.get('date_triggered')
            if not hasattr(triggered, 'astimezone') or triggered.astimezone(timezone.utc) > cutoff:
                continue
            doc.reference.update({
                'admin_flagged': True,
                'admin_flagged_by': AUTO_ESCALATE_LABEL,
                'admin_flagged_at': firestore.SERVER_TIMESTAMP,
                'auto_escalated': True,
            })
            write_status_history(
                db, doc.id, 'FLAGGED', None,
                remarks=f'Auto-flagged for BHW after {AUTO_ESCALATE_AFTER_MINUTES} min with no response',
            )
            flagged += 1
        except Exception as e:
            print(
                f'[alerts_common] auto_escalate_stale_alerts failed on {doc.id}: {e}')
    return flagged
