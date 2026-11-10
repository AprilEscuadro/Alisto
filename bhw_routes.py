"""
ALISTO BHW portal.

Registered in app.py with:
    from bhw_routes import bhw_bp
    app.register_blueprint(bhw_bp)

Every route lives under /bhw and only works for approved BHW accounts.
The portal reuses the admin layout styles (admin.css) with a BHW theme on top
(bhw_css/bhw.css), and the same admin.js for menus, tables, and the details window.

The alert fields this file expects are documented in alerts_common.py.
"""
import csv
import io
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Blueprint, render_template, session, redirect, url_for,
                   flash, request, g, Response, jsonify)
from firebase_admin import firestore
from werkzeug.security import check_password_hash, generate_password_hash

import account_profile
import alerts_common as ac
import device_info
from barangays import same_barangay, canonical
from database import db

bhw_bp = Blueprint('bhw', __name__, url_prefix='/bhw')

# A device counts as online if it checked in within this window.
ONLINE_WINDOW = timedelta(minutes=10)
# Below this signal percentage the map shows "Weak Signal".
WEAK_SIGNAL_BELOW = 40


# ---------- ACCESS GUARD ----------

def bhw_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'error')
            return redirect(url_for('login'))
        if session.get('role') != 'bhw':
            flash('That page is only for Barangay Health Workers.', 'error')
            return redirect(url_for('dashboard'))

        doc = db.collection('user_account').document(session['user_id']).get()
        user = doc.to_dict() if doc.exists else None
        if not user or user.get('deleted_at') or user.get('approval_status') != 'approved':
            session.clear()
            flash(
                'Your BHW account is not active. Please contact the system administrator.', 'error')
            return redirect(url_for('login'))

        g.bhw_user = user
        g.bhw_profile = _bhw_profile(session['user_id'])
        g.barangay = g.bhw_profile.get('barangay_assigned') or ''
        return view(*args, **kwargs)
    return wrapped


# ---------- SMALL HELPERS ----------

def _bhw_profile(user_id):
    matches = list(db.collection('bhw_profile').where(
        'user_id', '==', user_id).limit(1).stream())
    return (matches[0].to_dict() or {}) if matches else {}


def _initials(name):
    parts = (name or '').split()
    return ''.join(p[0] for p in parts[:2]).upper() or '?'


def _fmt_date(value):
    moment = ac.local(value)
    return moment.strftime('%b %d, %Y') if moment else '—'


def _fmt_datetime(value):
    moment = ac.local(value)
    return moment.strftime('%b %d, %Y %I:%M %p') if moment else '—'


def _age(date_of_birth):
    try:
        born = datetime.strptime(date_of_birth or '', '%Y-%m-%d').date()
    except ValueError:
        return None
    today = datetime.now().date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


# An elder's address may be saved either as one string
#   "12, Purok 1, Sudlon II, Cebu City, Cebu, 6000"
# or as a map of separate fields
#   {house_no: "12", street: "Purok 1", barangay: "Sudlon II", ...}
# depending on which part of the system created the record, so every function
# below goes through _address_parts() instead of splitting the value directly.
ADDRESS_FIELD_ORDER = ('house_no', 'house_number', 'unit', 'street', 'purok',
                       'barangay', 'city', 'municipality', 'province', 'zip_code', 'zip')


def _address_parts(address):
    """Return the address as a list of parts, whatever shape it was stored in."""
    if not address:
        return []
    if isinstance(address, str):
        return [p.strip() for p in address.split(',') if p.strip()]
    if isinstance(address, dict):
        parts, used = [], set()
        for key in ADDRESS_FIELD_ORDER:
            value = address.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                parts.append(str(value).strip())
                used.add(key)
        # Anything the list above doesn't know about, in the order Firestore gave it.
        for key, value in address.items():
            if key not in used and isinstance(value, (str, int)) and str(value).strip():
                parts.append(str(value).strip())
        return parts
    if isinstance(address, (list, tuple)):
        return [str(p).strip() for p in address if str(p).strip()]
    return [str(address).strip()]


def _address_text(address):
    return ', '.join(_address_parts(address)) or '—'


def _in_barangay(address, barangay):
    """Compare whole parts so 'Sudlon I' doesn't match 'Sudlon II'."""
    if not address or not barangay:
        return False
    return any(same_barangay(p, barangay) for p in _address_parts(address))


def _short_address(address, parts_wanted=3):
    """'12, Purok 1, Sudlon II, Cebu City, ...' -> 'Purok 1, Sudlon II, Cebu City'."""
    parts = _address_parts(address)
    if len(parts) > 3:
        return ', '.join(parts[1:1 + parts_wanted])
    return ', '.join(parts) or '—'


def _device_state(device):
    """Return (status, last_seen) where status is 'online', 'weak', or 'offline'."""
    last_seen = device.get('last_seen')

    if device.get('status') in ('online', 'offline'):
        online = device['status'] == 'online'
    elif 'is_online' in device:
        online = bool(device['is_online'])
    elif hasattr(last_seen, 'timestamp'):
        seen = last_seen if last_seen.tzinfo else last_seen.replace(
            tzinfo=timezone.utc)
        online = datetime.now(timezone.utc) - seen < ONLINE_WINDOW
    else:
        online = False

    if not online:
        return 'offline', last_seen

    signal = device.get('signal_strength')
    if isinstance(signal, (int, float)) and signal < WEAK_SIGNAL_BELOW:
        return 'weak', last_seen
    return 'online', last_seen


# ---------- READING FIRESTORE ----------

_ELDERS_CACHE = {}          # {barangay: (time read, elders)}
ELDERS_CACHE_SECONDS = 300         # new or edited residents show up within a minute


def _elders_in_barangay(barangay):
    """{elder_id: elder dict} for every non-deleted elder in this barangay.
    Kept for a minute so the alert poll doesn't re-read every elder
    profile every 15 seconds."""
    import time
    hit = _ELDERS_CACHE.get(barangay)
    if hit and time.time() - hit[0] < ELDERS_CACHE_SECONDS:
        return dict(hit[1])
    elders = {}
    for doc in db.collection('elder_profile').stream():
        elder = doc.to_dict() or {}
        if not elder.get('deleted_at') and _in_barangay(elder.get('address'), barangay):
            elders[doc.id] = elder
    _ELDERS_CACHE[barangay] = (time.time(), elders)
    return dict(elders)


def _devices_by_elder(elder_ids):
    """Devices of these residents only, not every device in the system."""
    by_elder = {}
    ids = list(elder_ids)
    for i in range(0, len(ids), 30):            # Firestore 'in' allows 30 at a time
        for doc in (db.collection('device')
                    .where('elder_id', 'in', ids[i:i + 30]).stream()):
            device = doc.to_dict() or {}
            if device.get('elder_id'):
                by_elder.setdefault(device['elder_id'],
                                    []).append((doc.id, device))
    return by_elder


def _open_alerts(elders):
    """Open alerts for these elders, newest first. Served from memory."""
    if not elders:
        return []
    out = []
    for alert_id, a in ac.open_alerts(db).items():
        if a.get('elder_id') in elders and ac.row_status(a) in ac.OPEN_STATUSES:
            a['id'] = alert_id
            out.append(a)
    oldest = datetime.min.replace(tzinfo=ac.PH_TIME)
    out.sort(key=lambda a: ac.local(
        a.get('date_triggered')) or oldest, reverse=True)
    return out


def _alerts_for(elder_ids, since=None, limit=None):
    """Raw alert documents belonging to these elders, newest first.

    since= and limit= are applied INSIDE the Firestore query, so only the
    alerts actually shown are read (and paid for). Uses the emergency_alert
    index (elder_id + date_triggered desc)."""
    if not elder_ids:
        return []
    ids = list(elder_ids)
    out = []
    try:
        for i in range(0, len(ids), 30):        # Firestore 'in' allows 30 at a time
            q = (db.collection(ac.ALERT_COLLECTION)
                 .where('elder_id', 'in', ids[i:i + 30]))
            if since:
                q = q.where('date_triggered', '>=', since)
            q = q.order_by('date_triggered',
                           direction=firestore.Query.DESCENDING)
            if limit:
                q = q.limit(limit)
            for doc in q.stream():
                alert = doc.to_dict() or {}
                alert['id'] = doc.id
                out.append(alert)
    except Exception as e:
        # Missing index -> the error has a link that creates it in one click.
        print(f'[bhw] CREATE THIS INDEX (elder_id + date_triggered desc): {e}')
        return _alerts_for_unindexed(ids, since, limit)

    oldest = datetime.min.replace(tzinfo=ac.PH_TIME)
    out.sort(key=lambda a: ac.local(
        a.get('date_triggered')) or oldest, reverse=True)
    return out[:limit] if limit else out


def _alerts_for_unindexed(ids, since=None, limit=None):
    """Old way (reads every alert). Only used if the index is missing."""
    out = []
    for i in range(0, len(ids), 30):
        for doc in (db.collection(ac.ALERT_COLLECTION)
                    .where('elder_id', 'in', ids[i:i + 30]).stream()):
            alert = doc.to_dict() or {}
            created = ac.local(alert.get('date_triggered'))
            if since and created and created < since:
                continue
            alert['id'] = doc.id
            out.append(alert)
    oldest = datetime.min.replace(tzinfo=ac.PH_TIME)
    out.sort(key=lambda a: ac.local(
        a.get('date_triggered')) or oldest, reverse=True)
    return out[:limit] if limit else out


def _alert_rows(alerts, elders, lookups=True):
    """Turn raw alert documents into what the templates need. Batches the
    location/phrase/sms-count lookups once for the whole list instead of
    3 Firestore round trips PER alert -- that N+1 pattern was clearly
    noticeable once there was more than a handful of alerts."""
    if lookups:
        locations = ac.alert_locations_bulk(
            db, (a.get('location_id') for a in alerts))
        phrases = ac.alert_phrases_bulk(
            db, (a.get('phrase_id') for a in alerts))
        sms_counts = ac.sms_sent_counts_bulk(db, (a.get('id') for a in alerts))
    else:
        # Reports and CSVs never show location, phrase or SMS count,
        # so those 3 extra reads per alert are skipped there.
        locations, phrases, sms_counts = {}, {}, {}

    rows = []
    for alert in alerts:
        elder = elders.get(alert.get('elder_id'), {})
        name = elder.get('full_name') or 'Unknown resident'
        status = ac.row_status(alert)
        # The device only ever writes real emergencies -- the paper's
        # EMERGENCY_ALERT table has no per-category type column at all.
        type_key = 'emergency'
        lat, lng, _address = locations.get(
            alert.get('location_id'), (None, None, None))
        trigger = (alert.get('trigger_type') or 'VOICE').lower()

        rows.append({
            'id': alert['id'],
            'elder_id': alert.get('elder_id'),
            'name': name,
            'initials': _initials(name),
            'age': _age(elder.get('date_of_birth')),
            'type': type_key,
            'type_label': ac.type_label(type_key),
            'type_icon': ac.type_icon(type_key),
            'type_colour': ac.type_colour(type_key),
            'status': status,
            'status_label': ac.status_label(status),
            'status_class': ac.status_class(status),
            'created_at': alert.get('date_triggered'),
            'time': ac.fmt_time(alert.get('date_triggered')),
            'full_time': _fmt_datetime(alert.get('date_triggered')),
            'address': _short_address(elder.get('address')),
            # Shorter form for the alerts table, where the column is narrow.
            'location': _short_address(elder.get('address'), parts_wanted=2),
            'device': alert.get('device_id') or '—',
            'trigger': trigger,
            'trigger_phrase': phrases.get(alert.get('phrase_id'), ''),
            'gps_verified': lat is not None,
            'sms_sent_count': sms_counts.get(alert.get('id'), 0),
            'notes': alert.get('resolution_note') or '',
            'response_minutes': ac.response_minutes(alert),
            'is_open': status in ac.OPEN_STATUSES,
            'latitude': lat,
            'longitude': lng,
            'admin_flagged': bool(alert.get('admin_flagged')),
            'admin_flagged_by': alert.get('admin_flagged_by') or '',
        })
    return rows


def _open_alerts_by_elder(elders):
    """{elder_id: alert row} for alerts that still need attention."""
    open_alerts = _open_alerts(elders)
    # Reversed so the newest open alert wins when a resident has more than one.
    return {r['elder_id']: r for r in reversed(_alert_rows(open_alerts, elders))}


def _elderly_rows(elders, open_alerts_by_elder=None, family_counts=None):
    """Residents in this barangay with their device status and alert state.
    Pass family_counts (device_info.family_counts) to include how many
    family accounts are connected; only the Elderly Users page needs it."""
    devices_by_elder = _devices_by_elder(elders)
    open_alerts_by_elder = open_alerts_by_elder or {}
    family_counts = family_counts or {}
    order = ('online', 'weak', 'offline')

    rows = []
    for elder_id, elder in elders.items():
        devices = devices_by_elder.get(elder_id, [])
        if devices:
            best = min(((_device_state(d), d) for _, d in devices),
                       key=lambda pair: order.index(pair[0][0]))
            (device_status, last_seen), device = best
        else:
            device_status, last_seen, device = 'none', None, {}

        name = elder.get('full_name') or 'Unnamed'
        open_alert = open_alerts_by_elder.get(elder_id)
        sim = device_info.sim_number(device)
        load = device_info.load_info(device)
        accounts_text = device_info.accounts_text(
            family_counts.get(elder_id, 0))

        if open_alert:
            current_status = 'emergency'
        elif device_status == 'none':
            current_status = 'none'
        elif device_status == 'offline':
            current_status = 'offline'
        else:
            current_status = 'safe'

        rows.append({
            'id': elder_id,
            'name': name,
            'initials': _initials(name),
            'age': _age(elder.get('date_of_birth')),
            'address': _address_text(elder.get('address')),
            'short_address': _short_address(elder.get('address')),
            'device_status': device_status,
            'current_status': current_status,
            'last_update': _fmt_datetime(last_seen),
            'last_time': ac.fmt_time(last_seen),
            'device_ids': [d_id for d_id, _ in devices],
            'latitude': device.get('latitude'),
            'longitude': device.get('longitude'),
            'open_alert': open_alert,
            'sim_number': sim,
            'load': load,
            'accounts_text': accounts_text,
            'details': [
                ['Full Name', name],
                ['Age', _age(elder.get('date_of_birth'))
                 if elder.get('date_of_birth') else '—'],
                ['Date of Birth', elder.get('date_of_birth') or '—'],
                ['Address', _address_text(elder.get('address'))],
                ['Device ID', ', '.join(
                    d_id for d_id, _ in devices) or 'No device'],
                ['Device Number', sim or 'Not reported'],
                ['Load', f"{load['text']} (checked {load['checked']})"
                 if load else 'Not checked yet'],
                ['Connected Accounts', accounts_text],
                ['Last Update', _fmt_datetime(last_seen)],
            ],
        })

    # Residents needing attention first, then alphabetical.
    rows.sort(key=lambda r: (r['current_status'] !=
              'emergency', r['name'].casefold()))
    return rows


@bhw_bp.context_processor
def _bhw_layout_context():
    """Values every BHW page needs for the sidebar and top bar."""
    user = getattr(g, 'bhw_user', None)
    if not user:
        return {}
    # Only the COUNT stays here (sidebar "Alerts" badge). The bell loads its
    # own items from /rbell (role_notifications.py).
    open_count = len(_open_alerts(_elders_in_barangay(g.barangay)))
    return {
        'bhw_name': user.get('full_name') or 'BHW',
        'bhw_initials': _initials(user.get('full_name')),
        'bhw_photo': user.get('photoBase64') or None,
        'bhw_barangay': g.barangay,
        'bhw_phone': user.get('contact_number') or '',
        'active_alert_count': open_count,
        'now_year': datetime.now().year,
    }


# ---------- ACCOUNT APPROVED (shown once, on the first sign-in after approval) ----------

@bhw_bp.route('/account-approved')
@bhw_required
def account_approved():
    return render_template('bhw/account_approved.html')


# ---------- DASHBOARD ----------

@bhw_bp.route('/')
@bhw_bp.route('/dashboard')
@bhw_required
def dashboard():
    elders = _elders_in_barangay(g.barangay)
    # Only the 3 alerts the "Recent Alerts" card shows are read; the active
    # count comes from the open alerts already kept in memory (0 reads).
    alert_rows = _alert_rows(_alerts_for(set(elders), limit=3), elders)
    rows = _elderly_rows(elders, _open_alerts_by_elder(elders))
    with_device = [r for r in rows if r['device_status'] != 'none']

    stats = {
        'active_alerts': len(_open_alerts(elders)),
        'elderly_monitored': len(rows),
        'active_devices': sum(1 for r in with_device if r['device_status'] in ('online', 'weak')),
        'offline_devices': sum(1 for r in with_device if r['device_status'] == 'offline'),
    }

    return render_template(
        'bhw/dashboard.html', active_page='dashboard',
        stats=stats, recent_alerts=alert_rows[:3],
        map_points=[r for r in rows if r['latitude'] and r['longitude']],
    )


# ---------- ALERTS ----------

BHW_ALERTS_LIMIT = 30   # newest alerts read for the Alerts page


@bhw_bp.route('/alerts')
@bhw_required
def alerts():
    elders = _elders_in_barangay(g.barangay)
    # Newest BHW_ALERTS_LIMIT only. Every open alert is still shown: they are
    # served from memory and added on top if they fall outside that window.
    rows = _alert_rows(_alerts_for(
        set(elders), limit=BHW_ALERTS_LIMIT), elders)
    shown = {r['id'] for r in rows}
    missing_open = [a for a in _open_alerts(elders) if a['id'] not in shown]
    if missing_open:
        rows = _alert_rows(missing_open, elders) + rows

    stats = {
        'active': sum(1 for r in rows if r['is_open']),
        'resolved': sum(1 for r in rows if r['status'] in ac.RESOLVED_STATUSES),
        'medication': sum(1 for r in rows if r['type'] == 'medication'),
        'false_alarm': sum(1 for r in rows if 'false_alarm' in (r['status'], r['type'])),
    }

    # Open the alert asked for in the URL, otherwise the first one needing attention.
    selected = request.args.get('alert')
    detail = (next((r for r in rows if r['id'] == selected), None)
              or next((r for r in rows if r['is_open']), None)
              or (rows[0] if rows else None))

    return render_template(
        'bhw/alerts.html', active_page='alerts',
        rows=rows, stats=stats, detail=detail,
    )


@bhw_bp.route('/alerts/poll')
@bhw_required
def alerts_poll():
    """Unanswered emergencies in this BHW's barangay. bhw_alert_watch.js asks
    for this every few seconds on every BHW page to ring the alarm."""
    elders = _elders_in_barangay(g.barangay)
    open_alerts = _open_alerts(elders)
    return jsonify(success=True, count=len(open_alerts), alerts=[{
        'id': a['id'],
        'resident': (elders.get(a.get('elder_id')) or {}).get('full_name') or 'A resident',
        'time': ac.fmt_time(a.get('date_triggered')),
        'trigger': (a.get('trigger_type') or 'VOICE').lower(),
        'url': url_for('bhw.alerts', alert=a['id']),
    } for a in open_alerts[:20]])


@bhw_bp.route('/alerts/<alert_id>/<action>', methods=['POST'])
@bhw_required
def update_alert(alert_id, action):
    if action not in ('acknowledge', 'resolve', 'false-alarm'):
        flash('Unknown action.', 'error')
        return redirect(url_for('bhw.alerts'))

    ref = db.collection(ac.ALERT_COLLECTION).document(alert_id)
    doc = ref.get()
    alert = doc.to_dict() if doc.exists else None

    # A BHW may only act on alerts from their own barangay.
    if not alert or alert.get('elder_id') not in _elders_in_barangay(g.barangay):
        flash('That alert is not in your barangay.', 'error')
        return redirect(url_for('bhw.alerts'))

    if action == 'acknowledge':
        if not ac.acknowledge(db, ref, alert_id, session['user_id']):
            flash('That alert was already handled.', 'info')
            return redirect(url_for('bhw.alerts', alert=alert_id))
        flash('Alert acknowledged. You can now contact the resident.', 'success')
    else:
        ac.resolve(
            db, ref, alert_id, session['user_id'],
            false_alarm=(action == 'false-alarm'),
            notes=request.form.get('resolution_note'),
        )
        flash('Alert marked as a false alarm.' if action ==
              'false-alarm' else 'Alert resolved.', 'success')

    return redirect(url_for('bhw.alerts', alert=alert_id))


@bhw_bp.route('/alerts/bulk', methods=['POST'])
@bhw_required
def bulk_update_alerts():
    """Responding / Resolved / False Alarm for every ticked row at once."""
    action = request.form.get('action')
    ids = list(dict.fromkeys(request.form.getlist('alert_ids')))[:50]
    if action not in ('acknowledge', 'resolve', 'false-alarm') or not ids:
        flash('Tick at least one alert first.', 'error')
        return redirect(url_for('bhw.alerts'))

    mine = _elders_in_barangay(g.barangay)
    done = skipped = 0
    acked_elders = set()   # acknowledge() also acknowledges that elder's other open alerts

    for alert_id in ids:
        ref = db.collection(ac.ALERT_COLLECTION).document(alert_id)
        doc = ref.get()
        alert = doc.to_dict() if doc.exists else None
        # A BHW may only act on alerts from their own barangay.
        if not alert or alert.get('elder_id') not in mine:
            skipped += 1
            continue

        status = ac.row_status(alert)
        if action == 'acknowledge':
            ok = status == 'active' and ac.acknowledge(
                db, ref, alert_id, session['user_id'])
            if ok:
                acked_elders.add(alert.get('elder_id'))
            elif status == 'acknowledged' and alert.get('elder_id') in acked_elders:
                ok = True   # already covered by another ticked alert of the same resident
        else:
            ok = status in ('active', 'acknowledged')
            if ok:
                ac.resolve(db, ref, alert_id, session['user_id'],
                           false_alarm=(action == 'false-alarm'))

        if ok:
            done += 1
        else:
            skipped += 1

    label = {'acknowledge': 'Responding', 'resolve': 'Resolved',
             'false-alarm': 'False Alarm'}[action]
    msg = f"{done} alert{'s' if done != 1 else ''} marked {label}."
    if skipped:
        msg += f" {skipped} skipped (already handled)."
    flash(msg, 'success' if done else 'info')
    return redirect(url_for('bhw.alerts'))

# ---------- ELDERLY USERS ----------


@bhw_bp.route('/elderly')
@bhw_required
def elderly_users():
    elders = _elders_in_barangay(g.barangay)
    return render_template(
        'bhw/elderly_users.html', active_page='elderly_users',
        rows=_elderly_rows(elders, _open_alerts_by_elder(elders),
                           device_info.family_counts(db)),
    )


@bhw_bp.route('/elderly/new')
@bhw_required
def add_elderly():
    # Old link: just open the Elderly Users page with the Add window open.
    return redirect(url_for('bhw.elderly_users', add=1))


# ---------- ADD ELDERLY IN COMMUNITY ----------

# Relationships that are saved as contact_type FAMILY (everything else: OTHER).
FAMILY_RELATIONS = {'Son/Daughter', 'Spouse',
                    'Sibling', 'Grandchild', 'Relative'}


def _clean_device_id(raw):
    return re.sub(r'\s+', '', raw or '').upper()


def _clean_ph_mobile(raw):
    """'0917 123 4567' / '+639171234567' -> '09171234567', else None."""
    digits = ''.join(ch for ch in (raw or '') if ch.isdigit())
    if digits.startswith('63') and len(digits) == 12:
        digits = '0' + digits[2:]
    return digits if len(digits) == 11 and digits.startswith('09') else None


def _dob_error(dob):
    try:
        born = datetime.strptime(dob, '%Y-%m-%d').date()
    except ValueError:
        return 'Enter a valid date of birth.'
    if born > datetime.now(timezone(timedelta(hours=8))).date():
        return 'Date of birth cannot be in the future.'
    if born.year < 1900:
        return 'Enter a valid date of birth.'
    return None


@bhw_bp.route('/elderly/check-device', methods=['POST'])
@bhw_required
def check_elderly_device():
    """Live check for the Device ID box in the Add Elderly window."""
    data = request.get_json(silent=True) or {}
    serial = _clean_device_id(data.get('device_id'))
    if not serial:
        return jsonify(valid=False, message='Enter the Device ID.'), 400
    doc = db.collection('device').document(serial).get()
    if not doc.exists:
        return jsonify(valid=False,
                       message='No matching Device ID found. Check the sticker or QR on the device.')
    if (doc.to_dict() or {}).get('is_registered'):
        return jsonify(valid=False,
                       message='This device is already linked to another resident.')
    return jsonify(valid=True, message='Device ID verified.')


@bhw_bp.route('/elderly/add', methods=['POST'])
@bhw_required
def add_elderly_submit():
    f = request.form
    full_name = f.get('full_name', '').strip()
    dob = f.get('date_of_birth', '').strip()
    occupation = f.get('occupation', '').strip()[:80]
    house_no = f.get('house_no', '').strip()[:40]
    street = f.get('street', '').strip()[:80]
    city = f.get('city', '').strip()[:60] or 'Cebu City'
    province = f.get('province', '').strip()[:60] or 'Cebu'
    zip_code = f.get('zip_code', '').strip()[:10]
    serial = _clean_device_id(f.get('device_id'))

    c_name = f.get('contact_name', '').strip()[:80]
    c_number_raw = f.get('contact_num', '').strip()
    c_rel = f.get('contact_relationship', '').strip()
    if c_rel == 'Other':
        c_rel = f.get('contact_relationship_other', '').strip()
    c_rel = c_rel[:60]

    if len(full_name) < 2:
        return jsonify(success=False, message="Enter the resident's full name."), 400
    if not dob:
        return jsonify(success=False, message='Enter the date of birth.'), 400
    error = _dob_error(dob)
    if error:
        return jsonify(success=False, message=error), 400

    contact = None
    if c_name or c_number_raw:
        number = _clean_ph_mobile(c_number_raw)
        if not c_name or not number:
            return jsonify(success=False, message='For the emergency contact, enter a name '
                           'and a valid mobile number (09XXXXXXXXX).'), 400
        contact = {'full_name': c_name,
                   'relationship': c_rel, 'contact_num': number}

    # The resident is always saved under THIS BHW's barangay, so they show up here.
    barangay = canonical(g.barangay) or g.barangay
    address = ', '.join(
        filter(None, [house_no, street, barangay, city, province, zip_code]))

    # Same person already registered here?
    for elder in _elders_in_barangay(g.barangay).values():
        if ((elder.get('full_name') or '').strip().casefold() == full_name.casefold()
                and (elder.get('date_of_birth') or '') == dob):
            return jsonify(success=False,
                           message=f'{full_name} is already registered in this barangay.'), 409

    try:
        device_data = {}
        if serial:
            device_doc = db.collection('device').document(serial).get()
            if not device_doc.exists:
                return jsonify(success=False,
                               message='No matching Device ID found. Check it, or leave it blank.'), 400
            device_data = device_doc.to_dict() or {}
            if device_data.get('is_registered'):
                return jsonify(success=False,
                               message='This device is already linked to another resident.'), 409

        elder_ref = db.collection('elder_profile').document()
        elder_ref.set({
            'full_name': full_name,
            'date_of_birth': dob,
            'address': address,
            'occupation': occupation or None,
            'photo_url': None,
            'created_at': firestore.SERVER_TIMESTAMP,
            'created_by': session['user_id'],
            'deleted_at': None,
            'is_archived': 0,
        })
        elder_id = elder_ref.id

        if serial:
            db.collection('device').document(serial).set({
                'elder_id': elder_id,
                'is_registered': True,
                'registered_at': firestore.SERVER_TIMESTAMP,
                'registered_by': session['user_id'],
                'serial_number': serial,
                'gps': device_data.get('gps', 'Inactive'),
                'sim_number': device_data.get('sim_number'),
                'status': device_data.get('status', 'Offline'),
            }, merge=True)

        if contact:
            db.collection('emergency_contact').add({
                **contact,
                'email_add': '',
                'contact_type': 'FAMILY' if contact['relationship'] in FAMILY_RELATIONS else 'OTHER',
                'elder_id': elder_id,
                'status': 'ACTIVE',
                'created_by': session['user_id'],
                'created_at': firestore.SERVER_TIMESTAMP,
                'updated_at': firestore.SERVER_TIMESTAMP,
                'deleted_at': None,
                'is_archived': False,
            })

        # History row (best effort: never blocks the registration)
        try:
            db.collection('activity_log').add({
                'elder_id': elder_id, 'type': 'device',
                'title': f'{full_name} was registered by the Barangay Health Worker',
                'detail': f'Device {serial} linked' if serial else 'No device linked yet',
                'badge': 'Linked' if serial else 'Pending',
                'badge_class': 'success' if serial else 'warning',
                'location_address': address,
                'device_id': serial or None,
                'actor_user_id': session['user_id'],
                'ref_id': None,
                'created_at': firestore.SERVER_TIMESTAMP,
            })
        except Exception as e:
            print(f'[bhw] could not log activity: {e}')

        # so the table shows them right away
        _ELDERS_CACHE.pop(g.barangay, None)
        return jsonify(success=True, message=f'{full_name} has been added to the community.')

    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500


# ---------- MAP VIEW ----------

@bhw_bp.route('/map')
@bhw_required
def map_view():
    elders = _elders_in_barangay(g.barangay)
    rows = _elderly_rows(elders, _open_alerts_by_elder(elders))

    points = [{
        'id': r['id'],
        'name': r['name'],
        'initials': r['initials'],
        'age': r['age'],
        'address': r['short_address'],
        'latitude': r['latitude'],
        'longitude': r['longitude'],
        'status': 'alert' if r['current_status'] == 'emergency' else r['device_status'],
        'time': r['last_time'],
        'alert_label': r['open_alert']['type_label'] if r['open_alert'] else None,
    } for r in rows]

    located = [p for p in points if p['latitude'] and p['longitude']]

    return render_template(
        'bhw/map_view.html', active_page='map_view',
        rows=rows, points=located, missing_location=len(points) - len(located),
    )


# ---------- REPORTS ----------

REPORT_RANGES = {
    'this_month': 'This Month',
    'last_30': 'Last 30 Days',
    'last_7': 'Last 7 Days',
    'all': 'All Time',
}

REPORT_FILES = {
    'alerts': 'Emergency Alert Summary',
    'response': 'Response Time Analysis',
    'residents': 'Elderly Monitoring Report',
}


def _range_start(key):
    now = datetime.now(ac.PH_TIME)
    if key == 'last_7':
        return now - timedelta(days=7)
    if key == 'last_30':
        return now - timedelta(days=30)
    if key == 'all':
        return None
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _report_data(range_key):
    elders = _elders_in_barangay(g.barangay)
    start = _range_start(range_key)
    rows = _alert_rows(_alerts_for(set(elders), since=start), elders,
                       lookups=False)
    residents = _elderly_rows(elders, _open_alerts_by_elder(elders))

    answered = [r['response_minutes']
                for r in rows if r['response_minutes'] is not None]
    avg_minutes = round(sum(answered) / len(answered)) if answered else None

    # Four buckets across the selected range, oldest first.
    now = datetime.now(ac.PH_TIME)
    window_start = start or (now - timedelta(days=28))
    span = max((now - window_start).total_seconds(), 1) / 4
    weeks = [0, 0, 0, 0]
    for row in rows:
        moment = ac.local(row['created_at'])
        if not moment:
            continue
        index = int((moment - window_start).total_seconds() / span)
        weeks[min(max(index, 0), 3)] += 1

    counts = Counter(r['current_status'] for r in residents)

    return {
        'rows': rows,
        'residents': residents,
        'stats': {
            'total_alerts': len(rows),
            'resolved': sum(1 for r in rows if r['status'] in ac.RESOLVED_STATUSES),
            'elderly_monitored': len(residents),
            'avg_minutes': avg_minutes,
        },
        # 'counts'/'peak', not 'values'/'max': in a Jinja template {{ trend.values }}
        # would resolve to Python's dict.values method instead of this data.
        'trend': {
            'labels': ['Week 1', 'Week 2', 'Week 3', 'Week 4'],
            'counts': weeks,
            'peak': max(weeks + [1]),
        },
        'overview': {
            'safe': counts.get('safe', 0),
            'alert': counts.get('emergency', 0),
            'offline': counts.get('offline', 0) + counts.get('none', 0),
        },
    }


@bhw_bp.route('/reports')
@bhw_required
def reports():
    range_key = request.args.get('range', 'this_month')
    if range_key not in REPORT_RANGES:
        range_key = 'this_month'

    data = _report_data(range_key)
    overview = data['overview']
    total = sum(overview.values()) or 1

    return render_template(
        'bhw/reports.html', active_page='reports',
        range_key=range_key, ranges=REPORT_RANGES, report_files=REPORT_FILES, **data,
        donut={
            'safe_pct': overview['safe'] / total * 100,
            'alert_pct': (overview['safe'] + overview['alert']) / total * 100,
            'empty': sum(overview.values()) == 0,
        },
    )


@bhw_bp.route('/reports/<report>.csv')
@bhw_required
def download_report(report):
    if report not in REPORT_FILES:
        flash('Unknown report.', 'error')
        return redirect(url_for('bhw.reports'))

    range_key = request.args.get('range', 'this_month')
    data = _report_data(
        range_key if range_key in REPORT_RANGES else 'this_month')

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    if report == 'alerts':
        writer.writerow(['Resident', 'Age', 'Alert Type', 'Status', 'Date & Time',
                         'Location', 'Device', 'Notes'])
        for r in data['rows']:
            writer.writerow([r['name'], r['age'] or '', r['type_label'], r['status_label'],
                             r['full_time'], r['address'], r['device'], r['notes']])
    elif report == 'response':
        writer.writerow(['Resident', 'Alert Type', 'Date & Time',
                        'Status', 'Response Time (minutes)'])
        for r in data['rows']:
            writer.writerow([r['name'], r['type_label'], r['full_time'], r['status_label'],
                             '' if r['response_minutes'] is None else round(r['response_minutes'])])
    else:
        writer.writerow(['Resident', 'Age', 'Address', 'Device Status', 'Current Status',
                         'Last Update', 'Device ID'])
        for r in data['residents']:
            writer.writerow([r['name'], r['age'] or '', r['address'], r['device_status'].title(),
                             r['current_status'].title(), r['last_update'],
                             ', '.join(r['device_ids']) or 'No device'])

    filename = f"alisto-{report}-{datetime.now(ac.PH_TIME).strftime('%Y-%m-%d')}.csv"
    return Response(
        '\ufeff' + buffer.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


# ---------- SETTINGS ----------

SETTING_KEYS = ('emergency_alerts', 'resident_status_alerts',
                'daily_summary', 'map_updates')


@bhw_bp.route('/inbox')
@bhw_required
def inbox():
    """Messages with families in this barangay and with ALISTO Support,
    plus announcements. The page talks to the JSON API in inbox_routes.py."""
    return render_template('bhw/inbox.html', active_page='inbox')


@bhw_bp.route('/settings')
@bhw_required
def settings():
    saved = g.bhw_user.get('notification_settings') or {}
    return render_template(
        'bhw/settings.html', active_page='settings',
        settings={key: bool(saved.get(key, True)) for key in SETTING_KEYS},
        user_email=g.bhw_user.get('email') or '',
        user_full_name=g.bhw_user.get('full_name') or '',
    )


@bhw_bp.route('/settings', methods=['POST'])
@bhw_required
def save_settings():
    db.collection('user_account').document(session['user_id']).update({
        'notification_settings': {key: request.form.get(key) == 'on' for key in SETTING_KEYS},
        'settings_updated_at': firestore.SERVER_TIMESTAMP,
    })
    flash('Your settings have been saved.', 'success')
    return redirect(url_for('bhw.settings'))


# ---------- MY PROFILE ----------

PASSWORD_RULES = [
    (lambda p: len(p) >= 8, 'at least 8 characters'),
    (lambda p: re.search(
        r'[a-z]', p) and re.search(r'[A-Z]', p), 'uppercase and lowercase letters'),
    (lambda p: re.search(r'\d', p), 'at least one number'),
    (lambda p: re.search(r'[^A-Za-z0-9]', p),
     'at least one special character'),
]


@bhw_bp.route('/profile')
@bhw_required
def profile():
    """Same behaviour as the family My Profile page (see account_profile.py).
    Barangay, health center, and BHW ID stay read-only: the admin approved
    them, and the barangay decides which residents this BHW sees."""
    p = g.bhw_profile
    barangay = p.get('barangay_assigned') or '—'
    if p.get('barangay_assigned'):
        barangay += ', Cebu City'
    return render_template(
        'bhw/profile.html', active_page='profile',
        me=account_profile.view(g.bhw_user),
        role_label='Barangay Health Worker (BHW)',
        member_since=_fmt_date(g.bhw_user.get('created_at')),
        details=[
            ('Role', 'Barangay Health Worker (BHW)'),
            ('Barangay', barangay),
            ('Health Center', p.get('health_center') or '—'),
            ('BHW ID Number', p.get('bhw_id_number') or '—'),
            ('Account Status', 'Active'),
            ('Last Login', session.get('previous_login') or 'First sign-in'),
            ('Two-Factor Authentication', 'Disabled'),
        ],
        urls={
            'update': url_for('bhw.edit_profile'),
            'password': url_for('bhw.change_password'),
            'photo': url_for('bhw.upload_photo'),
            'photo_remove': url_for('bhw.remove_photo'),
        },
    )


@bhw_bp.route('/profile/password', methods=['POST'])
@bhw_required
def change_password():
    ok, message = account_profile.change_password(
        session['user_id'], request.form)
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('bhw.profile'))


@bhw_bp.route('/profile/edit', methods=['POST'])
@bhw_required
def edit_profile():
    ok, message, name = account_profile.update_details(
        session['user_id'], request.form)
    if ok:
        session['full_name'] = name
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('bhw.profile'))


@bhw_bp.route('/profile/photo', methods=['POST'])
@bhw_required
def upload_photo():
    ok, message = account_profile.save_photo(
        session['user_id'], request.form.get('photo_base64'))
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('bhw.profile'))


@bhw_bp.route('/profile/photo/remove', methods=['POST'])
@bhw_required
def remove_photo():
    ok, message = account_profile.remove_photo(session['user_id'])
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('bhw.profile'))

# ---------- ELDERLY PROFILE + EDIT (BHW) ----------
# GET  -> everything the View window shows beyond the table row: occupation,
#         the connected family accounts (name, email, relationship) and the
#         emergency contacts.
# POST -> saves name, date of birth, occupation and address.
# A BHW can only open/edit residents whose address is in their own barangay,
# and the address must stay in that barangay (moving a resident to another
# barangay is the admin's job).


def _elder_accounts(elder_id):
    """Active family accounts linked to this elder, with how they are related."""
    rows, seen = [], set()
    for link in db.collection('family_elder_link').where('elder_id', '==', elder_id).stream():
        data = link.to_dict() or {}
        uid = data.get('family_user_id')
        if not uid or uid in seen:
            continue
        seen.add(uid)
        udoc = db.collection('user_account').document(uid).get()
        user = udoc.to_dict() if udoc.exists else None
        if not user or user.get('deleted_at') or user.get('role') != 'family':
            continue
        rows.append({'name': user.get('full_name') or 'Unnamed',
                     'email': user.get('email') or '',
                     'phone': user.get('contact_number') or '',
                     'relationship': data.get('relationship') or '',
                     'role': 'Family'})
    rows.sort(key=lambda r: r['name'].casefold())
    return rows


def _elder_contacts(elder_id):
    """Emergency contacts the ALISTO device will text, A to Z."""
    rows = []
    for doc in db.collection('emergency_contact').where('elder_id', '==', elder_id).stream():
        c = doc.to_dict() or {}
        if c.get('deleted_at') or c.get('is_archived') or c.get('status') == 'INACTIVE':
            continue
        rows.append({'full_name': c.get('full_name') or '',
                     'relationship': c.get('relationship') or '',
                     'contact_num': c.get('contact_num') or '',
                     'contact_type': c.get('contact_type') or 'OTHER'})
    rows.sort(key=lambda r: r['full_name'].casefold())
    return rows


@bhw_bp.route('/elderly/<elder_id>/edit', methods=['GET', 'POST'])
@bhw_required
def edit_elderly(elder_id):
    ref = db.collection('elder_profile').document(elder_id)
    doc = ref.get()
    elder = doc.to_dict() if doc.exists else None
    if (not elder or elder.get('deleted_at')
            or not _in_barangay(elder.get('address'), g.barangay)):
        return jsonify(success=False,
                       message='That resident could not be found in your barangay.'), 404

    barangay = canonical(g.barangay) or g.barangay
    current_address = _address_text(elder.get('address'))

    if request.method == 'GET':
        try:
            accounts = _elder_accounts(elder_id)
            contacts = _elder_contacts(elder_id)
        except Exception as e:
            return jsonify(success=False, message=f'Error: {e}'), 500
        return jsonify(success=True, resident={
            'full_name': elder.get('full_name') or '',
            'date_of_birth': str(elder.get('date_of_birth') or ''),
            'occupation': elder.get('occupation') or '',
            'address': current_address,
            'barangay': barangay,
        }, accounts=accounts, contacts=contacts)

    data = request.get_json(silent=True) or {}
    full_name = (data.get('full_name') or '').strip()[:100]
    dob = (data.get('date_of_birth') or '').strip()
    occupation = (data.get('occupation') or '').strip()[:80]
    address = (data.get('address') or '').strip()[:200]

    if len(full_name) < 2:
        return jsonify(success=False, message="Enter the resident's full name."), 400
    if not dob:
        return jsonify(success=False, message='Enter the date of birth.'), 400
    error = _dob_error(dob)
    if error:
        return jsonify(success=False, message=error), 400
    if not address:
        return jsonify(success=False, message='Enter the address.'), 400
    if not _in_barangay(address, g.barangay):
        return jsonify(success=False,
                       message=f'The address must stay in Barangay {barangay}. '
                       'To move a resident to another barangay, ask the admin.'), 400

    # Same person already registered here (not counting this resident)?
    _ELDERS_CACHE.pop(g.barangay, None)
    for other_id, other in _elders_in_barangay(g.barangay).items():
        if (other_id != elder_id
                and (other.get('full_name') or '').strip().casefold() == full_name.casefold()
                and (other.get('date_of_birth') or '') == dob):
            return jsonify(success=False,
                           message=f'{full_name} is already registered in this barangay.'), 409

    update = {
        'full_name': full_name,
        'date_of_birth': dob,
        'occupation': occupation or None,
        'updated_at': firestore.SERVER_TIMESTAMP,
        'updated_by': session['user_id'],
    }
    # Leave the stored address alone unless it was really changed.
    if address != current_address:
        update['address'] = address
    ref.update(update)
    _ELDERS_CACHE.pop(g.barangay, None)

    flash(f'{full_name} was updated.', 'success')
    return jsonify(success=True, message='Saved.')
