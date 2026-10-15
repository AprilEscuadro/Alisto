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
                   flash, request, g, Response)
from firebase_admin import firestore
from werkzeug.security import check_password_hash, generate_password_hash

import alerts_common as ac
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
    wanted = barangay.strip().casefold()
    return wanted in [p.casefold() for p in _address_parts(address)]


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

def _elders_in_barangay(barangay):
    """{elder_id: elder dict} for every non-deleted elder in this barangay."""
    elders = {}
    for doc in db.collection('elder_profile').stream():
        elder = doc.to_dict() or {}
        if not elder.get('deleted_at') and _in_barangay(elder.get('address'), barangay):
            elders[doc.id] = elder
    return elders


def _devices_by_elder():
    by_elder = {}
    for doc in db.collection('device').stream():
        device = doc.to_dict() or {}
        if device.get('elder_id'):
            by_elder.setdefault(device['elder_id'],
                                []).append((doc.id, device))
    return by_elder


def _alerts_for(elder_ids, since=None):
    """Raw alert documents belonging to these elders, newest first."""
    if not elder_ids:
        return []
    out = []
    for doc in db.collection(ac.ALERT_COLLECTION).stream():
        alert = doc.to_dict() or {}
        if alert.get('elder_id') not in elder_ids:
            continue
        created = ac.local(alert.get('date_triggered'))
        if since and created and created < since:
            continue
        alert['id'] = doc.id
        out.append(alert)

    oldest = datetime.min.replace(tzinfo=ac.PH_TIME)
    out.sort(key=lambda a: ac.local(
        a.get('date_triggered')) or oldest, reverse=True)
    return out


def _alert_rows(alerts, elders):
    """Turn raw alert documents into what the templates need. Batches the
    location/phrase/sms-count lookups once for the whole list instead of
    3 Firestore round trips PER alert -- that N+1 pattern was clearly
    noticeable once there was more than a handful of alerts."""
    locations = ac.alert_locations_bulk(
        db, (a.get('location_id') for a in alerts))
    phrases = ac.alert_phrases_bulk(db, (a.get('phrase_id') for a in alerts))
    sms_counts = ac.sms_sent_counts_bulk(db, (a.get('id') for a in alerts))

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
    open_alerts = [a for a in _alerts_for(set(elders))
                   if ac.row_status(a) in ac.OPEN_STATUSES]
    # Reversed so the newest open alert wins when a resident has more than one.
    return {r['elder_id']: r for r in reversed(_alert_rows(open_alerts, elders))}


def _elderly_rows(elders, open_alerts_by_elder=None):
    """Residents in this barangay with their device status and alert state."""
    devices_by_elder = _devices_by_elder()
    open_alerts_by_elder = open_alerts_by_elder or {}
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
            'details': [
                ['Full Name', name],
                ['Age', _age(elder.get('date_of_birth'))
                 if elder.get('date_of_birth') else '—'],
                ['Date of Birth', elder.get('date_of_birth') or '—'],
                ['Address', _address_text(elder.get('address'))],
                ['Device ID', ', '.join(
                    d_id for d_id, _ in devices) or 'No device'],
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
    elders = _elders_in_barangay(g.barangay)
    open_count = sum(1 for a in _alerts_for(set(elders))
                     if ac.row_status(a) in ac.OPEN_STATUSES)
    return {
        'bhw_name': user.get('full_name') or 'BHW',
        'bhw_initials': _initials(user.get('full_name')),
        'bhw_barangay': g.barangay,
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
    alert_rows = _alert_rows(_alerts_for(set(elders)), elders)
    rows = _elderly_rows(elders, _open_alerts_by_elder(elders))
    with_device = [r for r in rows if r['device_status'] != 'none']

    stats = {
        'active_alerts': sum(1 for a in alert_rows if a['is_open']),
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

@bhw_bp.route('/alerts')
@bhw_required
def alerts():
    elders = _elders_in_barangay(g.barangay)
    rows = _alert_rows(_alerts_for(set(elders)), elders)

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


# ---------- ELDERLY USERS ----------

@bhw_bp.route('/elderly')
@bhw_required
def elderly_users():
    elders = _elders_in_barangay(g.barangay)
    return render_template(
        'bhw/elderly_users.html', active_page='elderly_users',
        rows=_elderly_rows(elders, _open_alerts_by_elder(elders)),
    )


@bhw_bp.route('/elderly/new')
@bhw_required
def add_elderly():
    flash('Add Elderly in Community is not implemented yet.', 'info')
    return redirect(url_for('bhw.elderly_users'))


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
    rows = _alert_rows(_alerts_for(set(elders), since=start), elders)
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
    return render_template(
        'bhw/profile.html', active_page='profile',
        user=g.bhw_user, profile=g.bhw_profile,
        member_since=_fmt_date(g.bhw_user.get('created_at')),
        last_login=session.get('previous_login') or 'First sign-in',
    )


@bhw_bp.route('/profile/password', methods=['POST'])
@bhw_required
def change_password():
    current = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')

    if not check_password_hash(g.bhw_user.get('password_hash', ''), current):
        flash('Your current password is incorrect.', 'error')
    elif new != confirm:
        flash('New password and confirm password do not match.', 'error')
    elif new == current:
        flash('Your new password must be different from your current password.', 'error')
    else:
        missing = [label for rule, label in PASSWORD_RULES if not rule(new)]
        if missing:
            flash('New password needs ' + ', '.join(missing) + '.', 'error')
        else:
            db.collection('user_account').document(session['user_id']).update({
                'password_hash': generate_password_hash(new),
                'password_changed_at': firestore.SERVER_TIMESTAMP,
            })
            flash('Your password has been updated.', 'success')
    return redirect(url_for('bhw.profile'))


@bhw_bp.route('/profile/edit')
@bhw_required
def edit_profile():
    flash('Edit Profile is not implemented yet.', 'info')
    return redirect(url_for('bhw.profile'))
