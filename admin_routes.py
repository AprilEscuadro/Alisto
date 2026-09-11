"""
ALISTO admin panel.

Registered in app.py with:
    from admin_routes import admin_bp
    app.register_blueprint(admin_bp)

Every route lives under /admin and only works for accounts with role == 'admin'.
Templates are in templates/admin/, styles in static/css/admin_css/admin.css,
and the page behavior (menus, filters, pagination) in static/js/admin.js.
"""
import os
from functools import wraps

from flask import (Blueprint, render_template, session, redirect, url_for,
                   flash, request, send_from_directory, current_app, abort)
from firebase_admin import firestore

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


def _pending_bhw_count():
    return sum(
        1 for _, u in _active_docs('user_account')
        if u.get('role') == 'bhw' and u.get('approval_status', 'pending') == 'pending'
    )


@admin_bp.context_processor
def _admin_layout_context():
    """Values every admin page needs for the sidebar and top bar."""
    if session.get('role') != 'admin':
        return {}
    return {
        'admin_name': session.get('full_name', 'Administrator'),
        'admin_initial': _initials(session.get('full_name', 'Administrator'))[:1],
        'pending_count': _pending_bhw_count(),
    }


# ---------- DASHBOARD ----------

@admin_bp.route('/')
@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    users = _active_docs('user_account')
    elders = _active_docs('elder_profile')
    devices = list(db.collection('device').stream())

    stats = {
        # TODO: count from the emergency alert collection once its name/fields are settled.
        'active_alerts': 0,
        'total_elderly': len(elders),
        'total_bhws': sum(1 for _, u in users
                          if u.get('role') == 'bhw' and u.get('approval_status') == 'approved'),
        'total_devices': len(devices),
    }

    # TODO: fill from the emergency alert collection.
    recent_alerts = []
    alert_summary = {'active': 0, 'acknowledged': 0, 'resolved': 0}
    alert_summary['total'] = sum(alert_summary.values())

    return render_template(
        'admin/dashboard.html', active_page='dashboard',
        stats=stats, recent_alerts=recent_alerts, alert_summary=alert_summary,
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


# ---------- PAGES NOT BUILT YET ----------

_COMING_SOON = {
    'devices': ('Devices', 'Manage and monitor all Alisto devices.'),
    'map_view': ('Map View', 'Monitor device and emergency status across the community.'),
    'emergency_logs': ('Emergency Logs', 'View and manage all emergency alerts and incidents.'),
    'reports': ('Reports', 'Analyze devices, alerts, and community activity.'),
    'subscriptions': ('Subscriptions', 'Manage all subscription plans and user subscriptions.'),
    'settings': ('Settings', 'Manage admin account and basic system preferences.'),
}


def _coming_soon(page):
    title, subtitle = _COMING_SOON[page]
    return render_template('admin/coming_soon.html', active_page=page, title=title, subtitle=subtitle)


@admin_bp.route('/devices')
@admin_required
def devices():
    return _coming_soon('devices')


@admin_bp.route('/map')
@admin_required
def map_view():
    return _coming_soon('map_view')


@admin_bp.route('/emergency-logs')
@admin_required
def emergency_logs():
    return _coming_soon('emergency_logs')


@admin_bp.route('/reports')
@admin_required
def reports():
    return _coming_soon('reports')


@admin_bp.route('/subscriptions')
@admin_required
def subscriptions():
    return _coming_soon('subscriptions')


@admin_bp.route('/settings')
@admin_required
def settings():
    return _coming_soon('settings')