"""
Emergency alerts — shared helpers.

WHAT THE DEVICE / RASPBERRY PI MUST WRITE
-----------------------------------------
One document per alert in the `emergency_alert` collection:

    elder_id        str    required. The elder_profile document ID.
    device_serial   str    the device document ID (its serial number).
    type            str    one of ALERT_TYPES below. Default 'emergency'.
    status          str    one of ALERT_STATUSES. New alerts start as 'active'
                           ('delivered' for medication reminders).
    trigger         str    'voice' | 'button' | 'sensor' | 'schedule'
    trigger_phrase  str    optional. What the elder said, e.g. "Tabang!"
    latitude        float  optional. Where the device was at the time.
    longitude       float  optional.
    gps_verified    bool   optional. True if the coordinates came from a GPS fix.
    notes           str    optional, free text.
    sms_sent_count  int    optional. How many contacts were texted.
    created_at      time   required. Use firestore.SERVER_TIMESTAMP.

Everything else (barangay, the elder's name and age, response time) is worked
out by the website, so the device does not send it.

The BHW pages fill in the rest:
    acknowledged_at / acknowledged_by   set when a BHW presses Acknowledge
    resolved_at / resolved_by / resolution_note   set when a BHW resolves it
"""
from datetime import datetime, timedelta, timezone

ALERT_COLLECTION = 'emergency_alert'

# type -> (label, icon, colour class used in the templates)
ALERT_TYPES = {
    'emergency': ('Emergency', 'siren', 'red'),
    'fall': ('Fall Detected', 'person-standing', 'red'),
    'help': ('Help Request', 'hand', 'orange'),
    'medication': ('Medication Reminder', 'bell', 'orange'),
    'health_check': ('Health Check-in', 'heart-pulse', 'blue'),
    'false_alarm': ('False Alarm', 'flag', 'gray'),
}

# status -> (label, badge class)
ALERT_STATUSES = {
    'active': ('Active', 'alert-active'),
    'acknowledged': ('Responded', 'alert-acknowledged'),
    'resolved': ('Resolved', 'alert-resolved'),
    'closed': ('Closed', 'alert-closed'),
    'delivered': ('Delivered', 'alert-delivered'),
    'false_alarm': ('False Alarm', 'alert-closed'),
}

# Statuses that still need someone to act.
OPEN_STATUSES = ('active',)
# Statuses that count as handled, for the Reports page.
RESOLVED_STATUSES = ('resolved', 'closed')

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
    day = moment.strftime('%b %d') if moment.year == today.year else moment.strftime('%b %d, %Y')
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


def response_minutes(alert):
    """Minutes between the alert arriving and a BHW acknowledging it."""
    created, answered = local(alert.get('created_at')), local(alert.get('acknowledged_at'))
    if not created or not answered:
        return None
    minutes = (answered - created).total_seconds() / 60
    return minutes if minutes >= 0 else None