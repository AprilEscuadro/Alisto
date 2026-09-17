"""
ALISTO admin panel.

Registered in app.py with:
    from admin_routes import admin_bp
    app.register_blueprint(admin_bp)

Every route lives under /admin and only works for accounts with role == 'admin'.
Templates are in templates/admin/, styles in static/css/admin_css/admin.css,
and the page behavior (menus, filters, pagination) in static/js/admin.js.
"""
import csv
import io
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, render_template, session, redirect, url_for,
                   flash, request, send_from_directory, current_app, abort, Response)
from firebase_admin import firestore
from werkzeug.security import check_password_hash, generate_password_hash

import alerts_common as ac
# One source of truth for reading addresses and working out whether a device is
# online. Both portals show the same thing, so they share the same helpers.
from bhw_routes import (PASSWORD_RULES, _address_parts, _address_text,
                        _device_state, _short_address)
from database import db

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


# ---------- ACCESS GUARD ----------

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'error')
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            flash('You do not have access to the admin panel.', 'error')
            return redirect(url_for('dashboard'))
        return view(*args, **kwargs)
    return wrapped


# ---------- HELPERS ----------

def _active_docs(collection):
    """All documents in a collection that are not soft-deleted, as (id, dict) pairs."""
    docs = []
    for doc in db.collection(collection).stream():
        data = doc.to_dict() or {}
        if not data.get('deleted_at'):
            docs.append((doc.id, data))
    return docs


def _fmt_date(value):
    return value.strftime('%b %d, %Y') if hasattr(value, 'strftime') else '—'


def _sort_key(value):
    return value.timestamp() if hasattr(value, 'timestamp') else 0


def _initials(name):
    parts = (name or '').split()
    return ''.join(p[0] for p in parts[:2]).upper() or '?'


def _short_id(prefix, doc_id):
    return f'{prefix}-{doc_id[:6].upper()}'


def _fmt_when(value):
    """'Just now' / '20 minutes ago' / 'Yesterday' / 'May 22, 2026'."""
    moment = value if hasattr(value, 'strftime') else None
    if not moment:
        return 'Recently'
    now = datetime.now(moment.tzinfo) if moment.tzinfo else datetime.now()
    gap = now - moment
    minutes = gap.total_seconds() / 60
    if minutes < 1:
        return 'Just now'
    if minutes < 60:
        return f'{int(minutes)} minute{"s" if int(minutes) != 1 else ""} ago'
    if gap.days < 1:
        hours = int(minutes / 60)
        return f'{hours} hour{"s" if hours != 1 else ""} ago'
    if gap.days == 1:
        return 'Yesterday'
    if gap.days < 7:
        return f'{gap.days} days ago'
    return _fmt_date(moment)


def _pending_bhws():
    """BHW registrations waiting for the admin to approve or reject, newest first."""
    profiles = {}
    for doc in db.collection('bhw_profile').stream():
        profile = doc.to_dict() or {}
        profiles[profile.get('user_id')] = profile

    pending = []
    for user_id, user in _active_docs('user_account'):
        if user.get('role') != 'bhw' or user.get('approval_status', 'pending') != 'pending':
            continue
        profile = profiles.get(user_id, {})
        name = user.get('full_name') or 'Unnamed'
        pending.append({
            'id': user_id,
            'name': name,
            'initials': _initials(name),
            'barangay': profile.get('barangay_assigned') or '',
            'health_center': profile.get('health_center') or '',
            'when': _fmt_when(user.get('created_at')),
            'sort': _sort_key(user.get('created_at')),
        })

    pending.sort(key=lambda p: -p['sort'])
    return pending


@admin_bp.context_processor
def _admin_layout_context():
    """Values every admin page needs for the sidebar and top bar."""
    if session.get('role') != 'admin':
        return {}
    pending = _pending_bhws()
    return {
        'admin_name': session.get('full_name', 'Administrator'),
        'admin_initial': _initials(session.get('full_name', 'Administrator'))[:1],
        'pending_count': len(pending),
        # The bell shows the five newest; the rest are one click away.
        'pending_bhws': pending[:5],
    }


# ---------- DASHBOARD ----------


# ---------- READING THE WHOLE SYSTEM (all barangays) ----------

# Alert types the device can send, grouped by how urgent they are. A document may
# set its own "severity" field, and that always wins over this table.
SEVERITY_BY_TYPE = {
    'emergency': 'high',
    'fall': 'high',
    'help': 'medium',
    'health_check': 'low',
    'medication': 'low',
    'false_alarm': 'low',
}
SEVERITY_ORDER = ('high', 'medium', 'low')


def _barangay_of(address):
    """Best guess at the barangay from an address of either shape."""
    if isinstance(address, dict):
        return (address.get('barangay') or '').strip()
    parts = _address_parts(address)
    # "house, street, barangay, city, province, zip"
    return parts[2] if len(parts) >= 4 else ''


def _all_elders():
    return {
        doc.id: (doc.to_dict() or {})
        for doc in db.collection('elder_profile').stream()
        if not (doc.to_dict() or {}).get('deleted_at')
    }


def _devices_by_elder():
    by_elder = {}
    for doc in db.collection('device').stream():
        device = doc.to_dict() or {}
        if device.get('elder_id'):
            by_elder.setdefault(device['elder_id'], []).append((doc.id, device))
    return by_elder


def _age(date_of_birth):
    try:
        born = datetime.strptime(date_of_birth or '', '%Y-%m-%d').date()
    except ValueError:
        return None
    today = datetime.now().date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _alert_code(doc_id, created_at):
    """A short human-readable reference, e.g. AL-2026-3F9C."""
    moment = ac.local(created_at)
    year = moment.year if moment else datetime.now().year
    return f'AL-{year}-{doc_id[-4:].upper()}'


def _all_alert_rows(since=None):
    """Every alert in the system, newest first, with the resident joined in."""
    elders = _all_elders()
    devices_by_elder = _devices_by_elder()
    rows = []

    for doc in db.collection(ac.ALERT_COLLECTION).stream():
        alert = doc.to_dict() or {}
        created = ac.local(alert.get('created_at'))
        if since and created and created < since:
            continue

        elder = elders.get(alert.get('elder_id'), {})
        name = elder.get('full_name') or 'Unknown resident'
        type_key = alert.get('type') or 'emergency'
        status = alert.get('status') or 'active'
        severity = alert.get('severity') or SEVERITY_BY_TYPE.get(type_key, 'medium')
        serial = alert.get('device_serial') or ''
        if not serial:
            owned = devices_by_elder.get(alert.get('elder_id'), [])
            serial = owned[0][0] if owned else ''

        rows.append({
            'id': doc.id,
            'code': _alert_code(doc.id, alert.get('created_at')),
            'elder_id': alert.get('elder_id'),
            'name': name,
            'initials': _initials(name),
            'age': _age(elder.get('date_of_birth')),
            'device': serial or '—',
            'barangay': _barangay_of(elder.get('address')),
            'location': _short_address(elder.get('address')),
            'type': type_key,
            'type_label': ac.type_label(type_key),
            'type_icon': ac.type_icon(type_key),
            'type_colour': ac.type_colour(type_key),
            'status': status,
            'status_label': ac.status_label(status),
            'status_class': ac.status_class(status),
            'severity': severity,
            'created_at': alert.get('created_at'),
            'time': ac.fmt_time(alert.get('created_at')),
            'full_time': ac.local(alert.get('created_at')).strftime('%b %d, %Y %I:%M %p')
                         if ac.local(alert.get('created_at')) else '—',
            'notes': alert.get('notes') or '',
            'trigger_phrase': alert.get('trigger_phrase') or '',
            'response_minutes': ac.response_minutes(alert),
            'is_open': status in ac.OPEN_STATUSES,
            'latitude': alert.get('latitude'),
            'longitude': alert.get('longitude'),
        })

    oldest = datetime.min.replace(tzinfo=ac.PH_TIME)
    rows.sort(key=lambda r: ac.local(r['created_at']) or oldest, reverse=True)
    return rows


def _resident_rows():
    """Every elder with their device status and whether they have an open alert."""
    elders = _all_elders()
    devices_by_elder = _devices_by_elder()
    open_by_elder = {r['elder_id']: r for r in reversed(_all_alert_rows()) if r['is_open']}
    order = ('online', 'weak', 'offline')

    rows = []
    for elder_id, elder in elders.items():
        devices = devices_by_elder.get(elder_id, [])
        if devices:
            (state, last_seen), device = min(
                ((_device_state(d), d) for _, d in devices),
                key=lambda pair: order.index(pair[0][0]),
            )
        else:
            state, last_seen, device = 'none', None, {}

        open_alert = open_by_elder.get(elder_id)
        name = elder.get('full_name') or 'Unnamed'

        rows.append({
            'id': elder_id,
            'name': name,
            'initials': _initials(name),
            'age': _age(elder.get('date_of_birth')),
            'barangay': _barangay_of(elder.get('address')),
            'address': _address_text(elder.get('address')),
            'short_address': _short_address(elder.get('address')),
            'device_status': state,
            'device_ids': [d_id for d_id, _ in devices],
            'last_seen': ac.fmt_time(last_seen),
            'latitude': device.get('latitude'),
            'longitude': device.get('longitude'),
            'open_alert': open_alert,
            'map_status': 'alert' if open_alert else state,
        })

    rows.sort(key=lambda r: (not r['open_alert'], r['name'].casefold()))
    return rows


def _avg_response(rows):
    answered = [r['response_minutes'] for r in rows if r['response_minutes'] is not None]
    return round(sum(answered) / len(answered)) if answered else None


def _fmt_minutes(minutes):
    if minutes is None:
        return '—'
    if minutes < 60:
        return f'{int(minutes)}m'
    return f'{int(minutes // 60)}h {int(minutes % 60)}m'


@admin_bp.route('/')
@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    users = _active_docs('user_account')
    elders = _active_docs('elder_profile')
    devices = list(db.collection('device').stream())
    alerts = _all_alert_rows()
    residents = _resident_rows()

    stats = {
        'active_alerts': sum(1 for a in alerts if a['is_open']),
        'total_elderly': len(elders),
        'total_bhws': sum(1 for _, u in users
                          if u.get('role') == 'bhw' and u.get('approval_status') == 'approved'),
        'total_devices': len(devices),
    }

    month_start = datetime.now(ac.PH_TIME).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this_month = [a for a in alerts if (ac.local(a['created_at']) or month_start) >= month_start]
    summary = {
        'active': sum(1 for a in this_month if a['is_open']),
        'acknowledged': sum(1 for a in this_month if a['status'] == 'acknowledged'),
        'resolved': sum(1 for a in this_month if a['status'] in ac.RESOLVED_STATUSES),
    }
    summary['total'] = sum(summary.values())
    total = summary['total'] or 1

    return render_template(
        'admin/dashboard.html', active_page='dashboard',
        stats=stats, recent_alerts=alerts[:3], alert_summary=summary,
        donut={
            'active_pct': summary['active'] / total * 100,
            'ack_pct': (summary['active'] + summary['acknowledged']) / total * 100,
            'empty': summary['total'] == 0,
        },
        map_points=[r for r in residents if r['latitude'] and r['longitude']],
        system={
            'devices_online': sum(1 for r in residents if r['device_status'] in ('online', 'weak')),
            'barangays': len({r['barangay'] for r in residents if r['barangay']}),
        },
    )


# ---------- USERS ----------

@admin_bp.route('/users')
@admin_required
def users():
    accounts = _active_docs('user_account')
    elders = _active_docs('elder_profile')

    devices_per_elder = {}
    for doc in db.collection('device').stream():
        elder_id = (doc.to_dict() or {}).get('elder_id')
        if elder_id:
            devices_per_elder[elder_id] = devices_per_elder.get(elder_id, 0) + 1

    elders_per_family = {}
    for doc in db.collection('family_elder_link').stream():
        link = doc.to_dict() or {}
        elders_per_family.setdefault(link.get('family_user_id'), []).append(link.get('elder_id'))

    bhw_profiles = {}
    for doc in db.collection('bhw_profile').stream():
        profile = doc.to_dict() or {}
        bhw_profiles[profile.get('user_id')] = profile

    rows = []

    for elder_id, elder in elders:
        rows.append({
            'id': elder_id, 'display_id': _short_id('ELD', elder_id), 'type': 'elderly',
            'name': elder.get('full_name') or 'Unnamed', 'initials': _initials(elder.get('full_name')),
            'phone': None, 'email': None,
            'devices': devices_per_elder.get(elder_id, 0),
            'status': 'archived' if elder.get('is_archived') else 'active',
            'joined': _fmt_date(elder.get('created_at')), 'sort': _sort_key(elder.get('created_at')),
            # A list (not a dict) so the details keep this order in the modal.
            'details': [
                ['Full Name', elder.get('full_name') or '—'],
                ['Date of Birth', elder.get('date_of_birth') or '—'],
                ['Address', elder.get('address') or '—'],
            ],
        })

    for user_id, user in accounts:
        role = user.get('role')
        if role not in ('family', 'bhw'):
            continue  # admins are not listed

        row = {
            'id': user_id, 'type': role,
            'name': user.get('full_name') or 'Unnamed', 'initials': _initials(user.get('full_name')),
            'phone': user.get('contact_number') or None, 'email': user.get('email') or None,
            'joined': _fmt_date(user.get('created_at')), 'sort': _sort_key(user.get('created_at')),
            'details': [
                ['Full Name', user.get('full_name') or '—'],
                ['Email Address', user.get('email') or '—'],
                ['Phone Number', user.get('contact_number') or '—'],
            ],
        }

        if role == 'family':
            row['display_id'] = _short_id('FAM', user_id)
            row['devices'] = sum(devices_per_elder.get(e, 0) for e in elders_per_family.get(user_id, []))
            row['status'] = 'archived' if user.get('is_archived') else 'active'
            row['details'].append(['Linked Loved Ones', len(elders_per_family.get(user_id, []))])
        else:
            approval = user.get('approval_status', 'pending')
            profile = bhw_profiles.get(user_id, {})
            row['display_id'] = _short_id('BHW', user_id)
            row['devices'] = 0
            row['status'] = {'approved': 'active', 'rejected': 'rejected'}.get(approval, 'pending')
            row['details'] += [
                ['Date of Birth', profile.get('date_of_birth') or '—'],
                ['Barangay', profile.get('barangay_assigned') or '—'],
                ['Health Center', profile.get('health_center') or '—'],
                ['Years of Service', profile.get('years_of_service') or '—'],
                ['BHW ID Number', profile.get('bhw_id_number') or '—'],
            ]
            if profile.get('valid_id_stored_as'):
                row['valid_id_url'] = url_for('admin.bhw_valid_id', user_id=user_id)
                row['valid_id_name'] = profile.get('valid_id_filename') or 'View file'

        rows.append(row)

    # Pending BHWs first so the admin sees them right away, then newest first.
    rows.sort(key=lambda r: (r['status'] != 'pending', -r['sort']))

    stats = {
        'total_users': len(rows),
        'elderly': sum(1 for r in rows if r['type'] == 'elderly'),
        'family': sum(1 for r in rows if r['type'] == 'family'),
        'bhw': sum(1 for r in rows if r['type'] == 'bhw'),
        'pending_bhw': sum(1 for r in rows if r['status'] == 'pending'),
        'total_devices': sum(devices_per_elder.values()),
    }

    return render_template('admin/users.html', active_page='users', rows=rows, stats=stats)


@admin_bp.route('/users/<user_id>/approval', methods=['POST'])
@admin_required
def update_bhw_approval(user_id):
    action = request.form.get('action')
    if action not in ('approve', 'reject'):
        flash('Invalid action.', 'error')
        return redirect(url_for('admin.users'))

    ref = db.collection('user_account').document(user_id)
    doc = ref.get()
    user = doc.to_dict() if doc.exists else None
    if not user or user.get('role') != 'bhw':
        flash('That BHW account could not be found.', 'error')
        return redirect(url_for('admin.users'))

    ref.update({
        'approval_status': 'approved' if action == 'approve' else 'rejected',
        'approval_reviewed_at': firestore.SERVER_TIMESTAMP,
        'approval_reviewed_by': session['user_id'],
    })

    name = user.get('full_name', 'The BHW')
    if action == 'approve':
        flash(f'{name} is approved and can now sign in.', 'success')
    else:
        flash(f"{name}'s registration was rejected.", 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<user_id>/valid-id')
@admin_required
def bhw_valid_id(user_id):
    """Serve a BHW's uploaded ID to admins only (the file is not in /static)."""
    matches = list(db.collection('bhw_profile').where('user_id', '==', user_id).limit(1).stream())
    stored = (matches[0].to_dict() or {}).get('valid_id_stored_as') if matches else None
    if not stored:
        abort(404)
    folder = os.path.join(current_app.root_path, 'uploads', 'bhw_ids')
    return send_from_directory(folder, stored)


@admin_bp.route('/users/new')
@admin_required
def add_user():
    flash('Add User is not implemented yet.', 'info')
    return redirect(url_for('admin.users'))


# ---------- DEVICES ----------

# Serial numbers are pre-registered here by the admin. A family can only sign up
# with a serial number that already exists in this collection.
SERIAL_PATTERN = re.compile(r'^[A-Z0-9][A-Z0-9\-]{3,31}$')
MAX_SERIALS_PER_SUBMIT = 200


def _clean_serial(raw):
    """Normalize one typed serial number, or return None if it is unusable."""
    serial = re.sub(r'\s+', '', (raw or '')).upper()
    return serial if serial and SERIAL_PATTERN.match(serial) else None


@admin_bp.route('/devices')
@admin_required
def devices():
    elders = {doc.id: (doc.to_dict() or {}).get('full_name') or 'Unnamed'
              for doc in db.collection('elder_profile').stream()}

    rows = []
    for doc in db.collection('device').stream():
        device = doc.to_dict() or {}
        elder_id = device.get('elder_id')
        registered = bool(device.get('is_registered'))
        rows.append({
            'serial': doc.id,
            'assigned_to': elders.get(elder_id) if registered else None,
            'elder_id': elder_id if registered else None,
            'status': 'registered' if registered else 'available',
            'added': _fmt_date(device.get('created_at')),
            'registered_at': _fmt_date(device.get('registered_at')),
            'batch': device.get('batch') or '',
            'sort': _sort_key(device.get('created_at')),
        })

    rows.sort(key=lambda r: (r['status'] != 'available', -r['sort'], r['serial']))

    stats = {
        'total': len(rows),
        'registered': sum(1 for r in rows if r['status'] == 'registered'),
        'available': sum(1 for r in rows if r['status'] == 'available'),
    }

    return render_template('admin/devices.html', active_page='devices', rows=rows, stats=stats)


@admin_bp.route('/devices/add', methods=['POST'])
@admin_required
def add_devices():
    """Pre-register one or many serial numbers. One serial per line."""
    raw_lines = (request.form.get('serial_numbers') or '').replace(',', '\n').splitlines()
    batch = (request.form.get('batch') or '').strip()

    typed = [line for line in (l.strip() for l in raw_lines) if line]
    if not typed:
        flash('Please enter at least one serial number.', 'error')
        return redirect(url_for('admin.devices'))
    if len(typed) > MAX_SERIALS_PER_SUBMIT:
        flash(f'Please add at most {MAX_SERIALS_PER_SUBMIT} serial numbers at a time.', 'error')
        return redirect(url_for('admin.devices'))

    invalid, seen, to_add = [], set(), []
    for line in typed:
        serial = _clean_serial(line)
        if not serial:
            invalid.append(line)
        elif serial not in seen:
            seen.add(serial)
            to_add.append(serial)

    added, existing = [], []
    for serial in to_add:
        ref = db.collection('device').document(serial)
        if ref.get().exists:
            existing.append(serial)
            continue
        ref.set({
            'serial_number': serial,
            'is_registered': False,
            'elder_id': None,
            'batch': batch,
            'created_at': firestore.SERVER_TIMESTAMP,
            'added_by': session['user_id'],
        })
        added.append(serial)

    if added:
        flash(f"Pre-registered {len(added)} serial number{'s' if len(added) != 1 else ''}.", 'success')
    if existing:
        flash(f"Already in the system, skipped: {_join_sample(existing)}", 'info')
    if invalid:
        flash(f"Not a valid serial number, skipped: {_join_sample(invalid)}", 'error')
    return redirect(url_for('admin.devices'))


@admin_bp.route('/devices/<serial>/unregister', methods=['POST'])
@admin_required
def unregister_device(serial):
    """Unlink a device from its elder so the serial can be handed to someone else.

    The elder profile and the family accounts linked to it are NOT deleted — they
    simply stop having a device until a new one is registered to them.
    """
    ref = db.collection('device').document(serial)
    doc = ref.get()
    device = doc.to_dict() if doc.exists else None

    if not device:
        flash('That serial number is not in the system.', 'error')
        return redirect(url_for('admin.devices'))
    if not device.get('is_registered'):
        flash('That device is not registered to anyone.', 'error')
        return redirect(url_for('admin.devices'))

    elder_id = device.get('elder_id')
    elder_doc = db.collection('elder_profile').document(elder_id).get() if elder_id else None
    elder_name = (elder_doc.to_dict() or {}).get('full_name') if elder_doc and elder_doc.exists else None

    ref.update({
        'is_registered': False,
        'elder_id': None,
        'registered_at': None,
        # Kept for history so you can see who the device used to belong to.
        'previous_elder_id': elder_id,
        'unregistered_at': firestore.SERVER_TIMESTAMP,
        'unregistered_by': session['user_id'],
    })

    flash(
        f"{serial} is unregistered and available again."
        + (f" {elder_name} no longer has a device linked." if elder_name else ''),
        'success',
    )
    return redirect(url_for('admin.devices'))


@admin_bp.route('/devices/<serial>/delete', methods=['POST'])
@admin_required
def delete_device(serial):
    """Remove a serial number that was never claimed (typo, wrong batch)."""
    ref = db.collection('device').document(serial)
    doc = ref.get()
    if not doc.exists:
        flash('That serial number is not in the system.', 'error')
    elif (doc.to_dict() or {}).get('is_registered'):
        flash('That device is already linked to an elder, so it cannot be removed.', 'error')
    else:
        ref.delete()
        flash(f'Removed serial number {serial}.', 'success')
    return redirect(url_for('admin.devices'))


def _join_sample(items, limit=5):
    head = ', '.join(items[:limit])
    return head if len(items) <= limit else f'{head} and {len(items) - limit} more'


# ---------- MAP VIEW ----------

@admin_bp.route('/map')
@admin_required
def map_view():
    residents = _resident_rows()
    alerts = _all_alert_rows()
    barangays = sorted({r['barangay'] for r in residents if r['barangay']})
    located = [r for r in residents if r['latitude'] and r['longitude']]

    stats = {
        'active_alerts': sum(1 for a in alerts if a['is_open']),
        'online': sum(1 for r in residents if r['device_status'] in ('online', 'weak')),
        'offline': sum(1 for r in residents if r['device_status'] == 'offline'),
        'areas': len(barangays),
    }

    points = [{
        'id': r['id'], 'name': r['name'], 'initials': r['initials'], 'age': r['age'],
        'address': r['short_address'], 'barangay': r['barangay'],
        'latitude': r['latitude'], 'longitude': r['longitude'],
        'status': r['map_status'], 'time': r['last_seen'],
        'alert_label': r['open_alert']['type_label'] if r['open_alert'] else None,
    } for r in located]

    return render_template(
        'admin/map_view.html', active_page='map_view',
        residents=residents, points=points, barangays=barangays, stats=stats,
        recent=alerts[:4], missing_location=len(residents) - len(located),
    )


# ---------- EMERGENCY LOGS ----------

LOG_RANGES = {
    'this_month': 'This Month',
    'last_7': 'Last 7 Days',
    'last_30': 'Last 30 Days',
    'all': 'All Time',
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


@admin_bp.route('/emergency-logs')
@admin_required
def emergency_logs():
    range_key = request.args.get('range', 'all')
    if range_key not in LOG_RANGES:
        range_key = 'all'

    rows = _all_alert_rows(since=_range_start(range_key))
    everything = _all_alert_rows()
    month_start = datetime.now(ac.PH_TIME).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    stats = {
        'total': len(everything),
        'active': sum(1 for a in everything if a['is_open']),
        'resolved': sum(1 for a in everything if a['status'] in ac.RESOLVED_STATUSES),
        'avg_response': _fmt_minutes(_avg_response(everything)),
        'this_month': sum(1 for a in everything
                          if (ac.local(a['created_at']) or month_start) >= month_start),
        'month_label': month_start.strftime('%B %Y'),
    }

    return render_template(
        'admin/emergency_logs.html', active_page='emergency_logs',
        rows=rows, stats=stats, ranges=LOG_RANGES, range_key=range_key,
        barangays=sorted({r['barangay'] for r in rows if r['barangay']}),
    )


@admin_bp.route('/emergency-logs.csv')
@admin_required
def download_logs():
    rows = _all_alert_rows(since=_range_start(request.args.get('range', 'all')))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Alert ID', 'Device', 'Resident', 'Age', 'Location', 'Barangay',
                     'Type', 'Severity', 'Status', 'Date & Time', 'Response (minutes)', 'Notes'])
    for r in rows:
        writer.writerow([r['code'], r['device'], r['name'], r['age'] or '', r['location'],
                         r['barangay'], r['type_label'], r['severity'].title(), r['status_label'],
                         r['full_time'],
                         '' if r['response_minutes'] is None else round(r['response_minutes']),
                         r['notes']])
    return _csv_response(buffer, 'emergency-logs')


def _csv_response(buffer, name):
    filename = f"alisto-{name}-{datetime.now(ac.PH_TIME).strftime('%Y-%m-%d')}.csv"
    return Response(
        '\ufeff' + buffer.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


# ---------- REPORTS ----------

@admin_bp.route('/reports')
@admin_required
def reports():
    range_key = request.args.get('range', 'last_30')
    if range_key not in LOG_RANGES:
        range_key = 'last_30'

    start = _range_start(range_key)
    rows = _all_alert_rows(since=start)
    residents = _resident_rows()
    devices = list(db.collection('device').stream())
    users = _active_docs('user_account')

    stats = {
        'total_devices': len(devices),
        'online_devices': sum(1 for r in residents if r['device_status'] in ('online', 'weak')),
        'total_alerts': len(rows),
        'resolved': sum(1 for r in rows if r['status'] in ac.RESOLVED_STATUSES),
        'avg_response': _fmt_minutes(_avg_response(rows)),
        'active_users': len(users),
    }
    stats['online_pct'] = round(stats['online_devices'] / (stats['total_devices'] or 1) * 100, 1)
    stats['resolved_pct'] = round(stats['resolved'] / (len(rows) or 1) * 100, 1)

    # Alerts per day across the range, for the line chart.
    now = datetime.now(ac.PH_TIME)
    window_start = start or (now - timedelta(days=30))
    days = max((now - window_start).days, 1)
    step = max(days // 12, 1)
    buckets, labels = [], []
    for i in range(0, days + 1, step):
        day = (window_start + timedelta(days=i)).date()
        buckets.append(day)
        labels.append(day.strftime('%b %d'))

    per_bucket = [0] * len(buckets)
    resolved_bucket = [0] * len(buckets)
    for row in rows:
        moment = ac.local(row['created_at'])
        if not moment:
            continue
        index = min(max((moment.date() - window_start.date()).days // step, 0), len(buckets) - 1)
        per_bucket[index] += 1
        if row['status'] in ac.RESOLVED_STATUSES:
            resolved_bucket[index] += 1

    by_type = Counter(r['type'] for r in rows)
    type_breakdown = [{
        'label': ac.type_label(key),
        'count': count,
        'pct': round(count / (len(rows) or 1) * 100, 1),
        'colour': ac.type_colour(key),
    } for key, count in by_type.most_common()]

    by_place = Counter(r['barangay'] or 'Unknown' for r in rows)
    top_places = [{
        'name': place,
        'count': count,
        'pct': round(count / (len(rows) or 1) * 100, 1),
    } for place, count in by_place.most_common(5)]

    # One summary row per barangay.
    summary = defaultdict(lambda: {'devices': 0, 'online': 0, 'alerts': 0,
                                   'resolved': 0, 'residents': 0, 'minutes': []})
    for r in residents:
        place = summary[r['barangay'] or 'Unknown']
        place['residents'] += 1
        place['devices'] += len(r['device_ids'])
        if r['device_status'] in ('online', 'weak'):
            place['online'] += len(r['device_ids'])
    for r in rows:
        place = summary[r['barangay'] or 'Unknown']
        place['alerts'] += 1
        if r['status'] in ac.RESOLVED_STATUSES:
            place['resolved'] += 1
        if r['response_minutes'] is not None:
            place['minutes'].append(r['response_minutes'])

    barangay_rows = []
    for name, data in sorted(summary.items(), key=lambda kv: -kv[1]['alerts']):
        minutes = data['minutes']
        barangay_rows.append({
            'name': name,
            'devices': data['devices'],
            'online': data['online'],
            'online_pct': round(data['online'] / (data['devices'] or 1) * 100, 1),
            'alerts': data['alerts'],
            'resolved': data['resolved'],
            'resolved_pct': round(data['resolved'] / (data['alerts'] or 1) * 100, 1),
            'residents': data['residents'],
            'avg_response': _fmt_minutes(round(sum(minutes) / len(minutes)) if minutes else None),
        })

    peak = max(per_bucket + [1])
    return render_template(
        'admin/reports.html', active_page='reports',
        stats=stats, ranges=LOG_RANGES, range_key=range_key,
        chart={
            'labels': labels,
            'alerts': per_bucket,
            'resolved': resolved_bucket,
            'peak': peak,
            'points': _spark_points(per_bucket, peak),
            'resolved_points': _spark_points(resolved_bucket, peak),
        },
        type_breakdown=type_breakdown, top_places=top_places, barangay_rows=barangay_rows,
    )


def _spark_points(values, peak):
    """Turn counts into 'x,y x,y ...' for an SVG polyline in a 100x100 box."""
    if len(values) < 2:
        values = values * 2 or [0, 0]
    step = 100 / (len(values) - 1)
    return ' '.join(f'{i * step:.2f},{100 - (v / peak * 100):.2f}' for i, v in enumerate(values))


@admin_bp.route('/reports.csv')
@admin_required
def download_report():
    rows = _all_alert_rows(since=_range_start(request.args.get('range', 'last_30')))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Alert ID', 'Resident', 'Barangay', 'Type', 'Severity', 'Status',
                     'Date & Time', 'Response (minutes)'])
    for r in rows:
        writer.writerow([r['code'], r['name'], r['barangay'], r['type_label'],
                         r['severity'].title(), r['status_label'], r['full_time'],
                         '' if r['response_minutes'] is None else round(r['response_minutes'])])
    return _csv_response(buffer, 'report')


# ---------- SUBSCRIPTIONS ----------

# Plans are defined here rather than in the database so the prices stay in one
# place. A subscription document keeps the plan key, and the price is read back
# from this table.
PLANS = {
    'basic': {'name': 'Basic Plan', 'price': 299, 'devices': 1, 'blurb': '1 Device'},
    'premium': {'name': 'Premium Plan', 'price': 599, 'devices': 5, 'blurb': 'Up to 5 Devices'},
}
EXPIRING_SOON_DAYS = 30


def _plan(key):
    return PLANS.get(key or 'basic', PLANS['basic'])


def _subscription_rows():
    """Subscriptions joined to whoever they belong to.

    A document in the `subscription` collection looks like:
        elder_id | user_id   who it is for (one of the two)
        plan                 'basic' or 'premium'
        start_date           timestamp
        expiry_date          timestamp
        auto_renew           boolean
        device_serials       list of strings, optional
    """
    elders = _all_elders()
    accounts = dict(_active_docs('user_account'))
    devices_by_elder = _devices_by_elder()
    today = datetime.now(ac.PH_TIME).date()

    rows = []
    for doc in db.collection('subscription').stream():
        sub = doc.to_dict() or {}
        elder_id, user_id = sub.get('elder_id'), sub.get('user_id')

        if elder_id and elder_id in elders:
            owner, kind, prefix = elders[elder_id], 'elderly', 'ELD'
            owner_id = elder_id
            serials = sub.get('device_serials') or [d for d, _ in devices_by_elder.get(elder_id, [])]
        elif user_id and user_id in accounts:
            owner = accounts[user_id]
            kind = owner.get('role') or 'family'
            prefix = 'BHW' if kind == 'bhw' else 'FAM'
            owner_id = user_id
            serials = sub.get('device_serials') or []
        else:
            continue  # points at someone who no longer exists

        expiry = ac.local(sub.get('expiry_date'))
        days_left = (expiry.date() - today).days if expiry else None

        if days_left is None:
            status = 'active'
        elif days_left < 0:
            status = 'expired'
        elif days_left <= EXPIRING_SOON_DAYS:
            status = 'expiring'
        else:
            status = 'active'

        plan = _plan(sub.get('plan'))
        name = owner.get('full_name') or 'Unnamed'
        rows.append({
            'id': doc.id,
            'name': name,
            'initials': _initials(name),
            'display_id': _short_id(prefix, owner_id),
            'type': kind,
            'type_label': 'BHW' if kind == 'bhw' else kind.title(),
            'plan_key': sub.get('plan') or 'basic',
            'plan': plan['name'],
            'plan_blurb': plan['blurb'],
            'price': plan['price'],
            'devices': serials,
            'start': _fmt_date(sub.get('start_date')),
            'expiry': _fmt_date(sub.get('expiry_date')),
            'days_left': days_left,
            'status': status,
            'auto_renew': bool(sub.get('auto_renew')),
            'sort': _sort_key(sub.get('expiry_date')),
        })

    rows.sort(key=lambda r: (('expired', 'expiring', 'active').index(r['status']), r['name'].casefold()))
    return rows


@admin_bp.route('/subscriptions')
@admin_required
def subscriptions():
    rows = _subscription_rows()
    active = [r for r in rows if r['status'] in ('active', 'expiring')]

    stats = {
        'total': len(rows),
        'active': len(active),
        'active_pct': round(len(active) / (len(rows) or 1) * 100, 1),
        'expiring': sum(1 for r in rows if r['status'] == 'expiring'),
        'expired': sum(1 for r in rows if r['status'] == 'expired'),
        'revenue': sum(r['price'] for r in active),
    }

    return render_template(
        'admin/subscriptions.html', active_page='subscriptions',
        rows=rows, stats=stats, plans=PLANS, expiring_days=EXPIRING_SOON_DAYS,
    )


@admin_bp.route('/subscriptions.csv')
@admin_required
def download_subscriptions():
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Subscriber', 'ID', 'User Type', 'Plan', 'Price (PHP/month)',
                     'Devices', 'Status', 'Start Date', 'Expiry Date', 'Auto Renew'])
    for r in _subscription_rows():
        writer.writerow([r['name'], r['display_id'], r['type_label'], r['plan'], r['price'],
                         ', '.join(r['devices']) or 'None', r['status'].title(),
                         r['start'], r['expiry'], 'Yes' if r['auto_renew'] else 'No'])
    return _csv_response(buffer, 'subscriptions')


# ---------- SETTINGS (account settings) ----------

NOTIFICATION_KEYS = ('emergency_alerts', 'registration_requests',
                     'device_offline_alerts', 'subscription_expiry_alerts')
TIMEZONES = ['Asia/Manila (GMT+08:00)', 'UTC (GMT+00:00)']
DATE_FORMATS = ['MMM DD, YYYY', 'DD/MM/YYYY', 'YYYY-MM-DD']
SYSTEM_DOC = 'system_settings'


def _system_settings():
    doc = db.collection('app_settings').document(SYSTEM_DOC).get()
    saved = (doc.to_dict() or {}) if doc.exists else {}
    return {
        'timezone': saved.get('timezone') or TIMEZONES[0],
        'date_format': saved.get('date_format') or DATE_FORMATS[0],
        'auto_backup': bool(saved.get('auto_backup', True)),
        'last_backup': _fmt_date(saved.get('last_backup_at')),
    }


@admin_bp.route('/settings')
@admin_required
def settings():
    doc = db.collection('user_account').document(session['user_id']).get()
    user = doc.to_dict() if doc.exists else {}
    saved = user.get('notification_settings') or {}

    return render_template(
        'admin/settings.html', active_page='settings',
        notifications={key: bool(saved.get(key, True)) for key in NOTIFICATION_KEYS},
        email_notifications=bool(user.get('email_notifications', True)),
        system=_system_settings(), timezones=TIMEZONES, date_formats=DATE_FORMATS,
    )


@admin_bp.route('/settings', methods=['POST'])
@admin_required
def save_settings():
    timezone_choice = request.form.get('timezone')
    date_choice = request.form.get('date_format')

    db.collection('user_account').document(session['user_id']).update({
        'notification_settings': {key: request.form.get(key) == 'on' for key in NOTIFICATION_KEYS},
        'email_notifications': request.form.get('email_notifications') == 'on',
        'settings_updated_at': firestore.SERVER_TIMESTAMP,
    })
    db.collection('app_settings').document(SYSTEM_DOC).set({
        'timezone': timezone_choice if timezone_choice in TIMEZONES else TIMEZONES[0],
        'date_format': date_choice if date_choice in DATE_FORMATS else DATE_FORMATS[0],
        'auto_backup': request.form.get('auto_backup') == 'on',
        'updated_at': firestore.SERVER_TIMESTAMP,
        'updated_by': session['user_id'],
    })

    flash('Your settings have been saved.', 'success')
    return redirect(url_for('admin.settings'))


# ---------- MY PROFILE ----------

@admin_bp.route('/profile')
@admin_required
def profile():
    doc = db.collection('user_account').document(session['user_id']).get()
    user = doc.to_dict() if doc.exists else {}
    admins = sum(1 for _, u in _active_docs('user_account') if u.get('role') == 'admin')

    return render_template(
        'admin/profile.html', active_page='profile', user=user,
        member_since=_fmt_date(user.get('created_at')),
        last_login=session.get('previous_login') or 'First sign-in',
        password_changed=_fmt_date(user.get('password_changed_at')),
        admin_count=admins,
    )


@admin_bp.route('/profile/password', methods=['POST'])
@admin_required
def change_password():
    doc = db.collection('user_account').document(session['user_id']).get()
    user = doc.to_dict() if doc.exists else {}

    current = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')

    if not check_password_hash(user.get('password_hash', ''), current):
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
    return redirect(url_for('admin.profile'))


@admin_bp.route('/profile/edit', methods=['POST'])
@admin_required
def edit_profile():
    full_name = (request.form.get('full_name') or '').strip()
    contact = (request.form.get('contact_number') or '').strip()

    if not full_name:
        flash('Please enter your full name.', 'error')
        return redirect(url_for('admin.profile'))

    db.collection('user_account').document(session['user_id']).update({
        'full_name': full_name,
        'contact_number': contact,
    })
    session['full_name'] = full_name
    flash('Your profile has been updated.', 'success')
    return redirect(url_for('admin.profile'))