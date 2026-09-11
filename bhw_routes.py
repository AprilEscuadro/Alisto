"""
ALISTO BHW portal.

Registered in app.py with:
    from bhw_routes import bhw_bp
    app.register_blueprint(bhw_bp)

Every route lives under /bhw and only works for approved BHW accounts.
The portal reuses the admin layout styles (admin.css) with a BHW theme on top
(bhw_css/bhw.css), and the same admin.js for menus, tables, and the details window.
"""
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, render_template, session, redirect, url_for, flash, request, g
from firebase_admin import firestore
from werkzeug.security import check_password_hash, generate_password_hash

from database import db

bhw_bp = Blueprint('bhw', __name__, url_prefix='/bhw')

# A device counts as online if it checked in within this window.
ONLINE_WINDOW = timedelta(minutes=10)


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
            flash('Your BHW account is not active. Please contact the system administrator.', 'error')
            return redirect(url_for('login'))

        g.bhw_user = user
        g.bhw_profile = _bhw_profile(session['user_id'])
        return view(*args, **kwargs)
    return wrapped


# ---------- HELPERS ----------

def _bhw_profile(user_id):
    matches = list(db.collection('bhw_profile').where('user_id', '==', user_id).limit(1).stream())
    return (matches[0].to_dict() or {}) if matches else {}


def _initials(name):
    parts = (name or '').split()
    return ''.join(p[0] for p in parts[:2]).upper() or '?'


# Firestore stores times in UTC; show them in Philippine time.
PH_TIME = timezone(timedelta(hours=8))


def _local(value):
    return value.astimezone(PH_TIME) if getattr(value, 'tzinfo', None) else value


def _fmt_date(value):
    return _local(value).strftime('%b %d, %Y') if hasattr(value, 'strftime') else '—'


def _fmt_datetime(value):
    return _local(value).strftime('%b %d, %Y %I:%M %p') if hasattr(value, 'strftime') else '—'


def _age(date_of_birth):
    try:
        born = datetime.strptime(date_of_birth or '', '%Y-%m-%d').date()
    except ValueError:
        return None
    today = datetime.now().date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _in_barangay(address, barangay):
    """Addresses are saved as 'house, street, barangay, city, province, zip'.
    Compare whole parts so 'Sudlon I' doesn't match 'Sudlon II'."""
    if not address or not barangay:
        return False
    parts = [p.strip().casefold() for p in address.split(',')]
    return barangay.strip().casefold() in parts


def _device_state(device):
    """Return ('online' or 'offline', last_seen) for a device document."""
    last_seen = device.get('last_seen')
    if device.get('status') in ('online', 'offline'):
        return device['status'], last_seen
    if 'is_online' in device:
        return ('online' if device['is_online'] else 'offline'), last_seen
    if hasattr(last_seen, 'timestamp'):
        seen = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - seen < ONLINE_WINDOW:
            return 'online', last_seen
    # Never checked in, or not recently: treat as offline.
    return 'offline', last_seen


def _elderly_rows(barangay):
    """Elders whose address is in the BHW's barangay, with their device status."""
    devices_by_elder = {}
    for doc in db.collection('device').stream():
        device = doc.to_dict() or {}
        if device.get('elder_id'):
            devices_by_elder.setdefault(device['elder_id'], []).append((doc.id, device))

    rows = []
    for doc in db.collection('elder_profile').stream():
        elder = doc.to_dict() or {}
        if elder.get('deleted_at') or not _in_barangay(elder.get('address'), barangay):
            continue

        devices = devices_by_elder.get(doc.id, [])
        states = [_device_state(d) for _, d in devices]
        if not devices:
            device_status, last_seen = 'none', None
        elif any(state == 'online' for state, _ in states):
            device_status = 'online'
            last_seen = next(seen for state, seen in states if state == 'online')
        else:
            device_status, last_seen = 'offline', states[0][1]

        name = elder.get('full_name') or 'Unnamed'
        age = _age(elder.get('date_of_birth'))
        rows.append({
            'id': doc.id,
            'name': name,
            'initials': _initials(name),
            'age': age,
            'address': elder.get('address') or '—',
            'device_status': device_status,
            # TODO: show "Emergency" once alerts are connected.
            'current_status': {'online': 'safe', 'offline': 'offline'}.get(device_status, 'none'),
            'last_update': _fmt_datetime(last_seen),
            'device_ids': [d_id for d_id, _ in devices],
            'details': [
                ['Full Name', name],
                ['Age', age if age is not None else '—'],
                ['Date of Birth', elder.get('date_of_birth') or '—'],
                ['Address', elder.get('address') or '—'],
                ['Device ID', ', '.join(d_id for d_id, _ in devices) or 'No device'],
                ['Last Update', _fmt_datetime(last_seen)],
            ],
        })

    rows.sort(key=lambda r: r['name'].casefold())
    return rows


@bhw_bp.context_processor
def _bhw_layout_context():
    """Values every BHW page needs for the sidebar and top bar."""
    user = getattr(g, 'bhw_user', None)
    if not user:
        return {}
    profile = getattr(g, 'bhw_profile', {}) or {}
    return {
        'bhw_name': user.get('full_name') or 'BHW',
        'bhw_initials': _initials(user.get('full_name')),
        'bhw_barangay': profile.get('barangay_assigned') or '',
        # TODO: count active alerts in the barangay once alerts are connected.
        'active_alert_count': 0,
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
    rows = _elderly_rows(g.bhw_profile.get('barangay_assigned'))
    with_device = [r for r in rows if r['device_status'] != 'none']

    stats = {
        'active_alerts': 0,  # TODO: from alerts
        'elderly_monitored': len(rows),
        'active_devices': sum(1 for r in with_device if r['device_status'] == 'online'),
        'offline_devices': sum(1 for r in with_device if r['device_status'] == 'offline'),
    }

    return render_template(
        'bhw/dashboard.html', active_page='dashboard',
        stats=stats, recent_alerts=[],  # TODO: from alerts
    )


# ---------- ELDERLY USERS ----------

@bhw_bp.route('/elderly')
@bhw_required
def elderly_users():
    return render_template(
        'bhw/elderly_users.html', active_page='elderly_users',
        rows=_elderly_rows(g.bhw_profile.get('barangay_assigned')),
    )


@bhw_bp.route('/elderly/new')
@bhw_required
def add_elderly():
    flash('Add Elderly in Community is not implemented yet.', 'info')
    return redirect(url_for('bhw.elderly_users'))


# ---------- MY PROFILE ----------

PASSWORD_RULES = [
    (lambda p: len(p) >= 8, 'at least 8 characters'),
    (lambda p: re.search(r'[a-z]', p) and re.search(r'[A-Z]', p), 'uppercase and lowercase letters'),
    (lambda p: re.search(r'\d', p), 'at least one number'),
    (lambda p: re.search(r'[^A-Za-z0-9]', p), 'at least one special character'),
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


# ---------- PAGES NOT BUILT YET ----------

_COMING_SOON = {
    'alerts': ('Alerts', 'Monitor and respond to alerts from elderly users in your barangay.'),
    'map_view': ('Map View', 'Live location of elderly devices in your community.'),
    'reports': ('Reports', 'View and generate monitoring and emergency response reports.'),
    'settings': ('Settings', 'Customize Alisto based on your role.'),
}


def _coming_soon(page):
    title, subtitle = _COMING_SOON[page]
    return render_template('bhw/coming_soon.html', active_page=page, title=title, subtitle=subtitle)


@bhw_bp.route('/alerts')
@bhw_required
def alerts():
    return _coming_soon('alerts')


@bhw_bp.route('/map')
@bhw_required
def map_view():
    return _coming_soon('map_view')


@bhw_bp.route('/reports')
@bhw_required
def reports():
    return _coming_soon('reports')


@bhw_bp.route('/settings')
@bhw_required
def settings():
    return _coming_soon('settings')