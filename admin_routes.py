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
import re
from datetime import timedelta, timezone
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
        'open_ticket_count': _open_ticket_count(),
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
            devices_per_elder[elder_id] = devices_per_elder.get(
                elder_id, 0) + 1

    elders_per_family = {}
    for doc in db.collection('family_elder_link').stream():
        link = doc.to_dict() or {}
        elders_per_family.setdefault(
            link.get('family_user_id'), []).append(link.get('elder_id'))

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
            row['devices'] = sum(devices_per_elder.get(e, 0)
                                 for e in elders_per_family.get(user_id, []))
            row['status'] = 'archived' if user.get('is_archived') else 'active'
            row['details'].append(
                ['Linked Loved Ones', len(elders_per_family.get(user_id, []))])
        else:
            approval = user.get('approval_status', 'pending')
            profile = bhw_profiles.get(user_id, {})
            row['display_id'] = _short_id('BHW', user_id)
            row['devices'] = 0
            row['status'] = {'approved': 'active',
                             'rejected': 'rejected'}.get(approval, 'pending')
            row['details'] += [
                ['Date of Birth', profile.get('date_of_birth') or '—'],
                ['Barangay', profile.get('barangay_assigned') or '—'],
                ['Health Center', profile.get('health_center') or '—'],
                ['Years of Service', profile.get('years_of_service') or '—'],
                ['BHW ID Number', profile.get('bhw_id_number') or '—'],
            ]
            if profile.get('valid_id_stored_as'):
                row['valid_id_url'] = url_for(
                    'admin.bhw_valid_id', user_id=user_id)
                row['valid_id_name'] = profile.get(
                    'valid_id_filename') or 'View file'

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
    matches = list(db.collection('bhw_profile').where(
        'user_id', '==', user_id).limit(1).stream())
    stored = (matches[0].to_dict() or {}).get(
        'valid_id_stored_as') if matches else None
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

    rows.sort(key=lambda r: (r['status'] !=
              'available', -r['sort'], r['serial']))

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
    raw_lines = (request.form.get('serial_numbers')
                 or '').replace(',', '\n').splitlines()
    batch = (request.form.get('batch') or '').strip()

    typed = [line for line in (l.strip() for l in raw_lines) if line]
    if not typed:
        flash('Please enter at least one serial number.', 'error')
        return redirect(url_for('admin.devices'))
    if len(typed) > MAX_SERIALS_PER_SUBMIT:
        flash(
            f'Please add at most {MAX_SERIALS_PER_SUBMIT} serial numbers at a time.', 'error')
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
        flash(
            f"Pre-registered {len(added)} serial number{'s' if len(added) != 1 else ''}.", 'success')
    if existing:
        flash(
            f"Already in the system, skipped: {_join_sample(existing)}", 'info')
    if invalid:
        flash(
            f"Not a valid serial number, skipped: {_join_sample(invalid)}", 'error')
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
    elder_doc = db.collection('elder_profile').document(
        elder_id).get() if elder_id else None
    elder_name = (elder_doc.to_dict() or {}).get(
        'full_name') if elder_doc and elder_doc.exists else None

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
        flash(
            'That device is already linked to an elder, so it cannot be removed.', 'error')
    else:
        ref.delete()
        flash(f'Removed serial number {serial}.', 'success')
    return redirect(url_for('admin.devices'))


def _join_sample(items, limit=5):
    head = ', '.join(items[:limit])
    return head if len(items) <= limit else f'{head} and {len(items) - limit} more'

# ---------- SUPPORT TICKETS (Report a Problem + Contact Support) ----------


TICKET_STATUSES = {
    'open': 'Open',
    'in_progress': 'In Progress',
    'resolved': 'Resolved',
    'closed': 'Closed',
}
TICKET_BADGES = {
    'open': 'badge-rejected',
    'in_progress': 'badge-pending',
    'resolved': 'badge-active',
    'closed': 'badge-archived',
}
PH_TIME = timezone(timedelta(hours=8))


def _ph_datetime(value):
    if not hasattr(value, 'strftime'):
        return '—'
    if getattr(value, 'tzinfo', None):
        value = value.astimezone(PH_TIME)
    return value.strftime('%b %d, %Y %I:%M %p')


def _open_ticket_count():
    try:
        return sum(1 for d in db.collection('support_ticket').stream()
                   if (d.to_dict() or {}).get('status', 'open') in ('open', 'in_progress'))
    except Exception:
        return 0


@admin_bp.route('/support')
@admin_required
def support_tickets():
    status_filter = request.args.get('status', 'active')
    type_filter = request.args.get('type', '')

    all_rows = []
    for doc in db.collection('support_ticket').stream():
        t = doc.to_dict() or {}
        status = t.get('status') or 'open'
        all_rows.append({
            'id': doc.id,
            'ticket_no': t.get('ticket_no') or _short_id('TCK', doc.id),
            'type': t.get('type') or '',
            'type_label': 'Problem Report' if t.get('type') == 'problem_report' else 'Contact Support',
            'category': t.get('category_label') or '',
            'subcategory': t.get('subcategory_label') or '',
            'subject': t.get('subject') or '—',
            'message': t.get('message') or '',
            'user_name': t.get('contact_name') or t.get('user_name') or 'Unknown',
            'user_role': (t.get('user_role') or '').upper() if t.get('user_role') == 'bhw'
            else (t.get('user_role') or '').capitalize(),
            'user_email': t.get('user_email') or '',
            'reply_email': t.get('reply_email') or '',
            'phone': t.get('user_contact_number') or '',
            'status': status,
            'status_label': TICKET_STATUSES.get(status, status.title()),
            'badge': TICKET_BADGES.get(status, 'badge-pending'),
            'admin_reply': t.get('admin_reply') or '',
            'admin_notes': t.get('admin_notes') or '',
            'handled_by': t.get('handled_by_name') or '',
            'created': _ph_datetime(t.get('created_at')),
            'updated': _ph_datetime(t.get('updated_at')),
            'sort': _sort_key(t.get('created_at')),
        })

    stats = {
        'open': sum(r['status'] == 'open' for r in all_rows),
        'in_progress': sum(r['status'] == 'in_progress' for r in all_rows),
        'resolved': sum(r['status'] in ('resolved', 'closed') for r in all_rows),
        'total': len(all_rows),
    }

    rows = all_rows
    if status_filter == 'active':
        rows = [r for r in rows if r['status'] in ('open', 'in_progress')]
    elif status_filter in TICKET_STATUSES:
        rows = [r for r in rows if r['status'] == status_filter]
    else:
        status_filter = 'all'
    if type_filter in ('problem_report', 'contact_support'):
        rows = [r for r in rows if r['type'] == type_filter]
    else:
        type_filter = ''
    # Open first, then newest.
    order = {'open': 0, 'in_progress': 1, 'resolved': 2, 'closed': 3}
    rows.sort(key=lambda r: (order.get(r['status'], 9), -r['sort']))

    return render_template(
        'admin/support.html', active_page='support',
        rows=rows, stats=stats, statuses=TICKET_STATUSES,
        status_filter=status_filter, type_filter=type_filter,
    )


@admin_bp.route('/support/<ticket_id>/update', methods=['POST'])
@admin_required
def update_support_ticket(ticket_id):
    ref = db.collection('support_ticket').document(ticket_id)
    doc = ref.get()
    if not doc.exists:
        flash('That ticket no longer exists.', 'error')
        return redirect(url_for('admin.support_tickets'))

    status = request.form.get('status', '')
    if status not in TICKET_STATUSES:
        flash('Please choose a valid status.', 'error')
        return redirect(url_for('admin.support_tickets'))

    ticket = doc.to_dict() or {}
    updates = {
        'status': status,
        'admin_reply': (request.form.get('admin_reply') or '').strip()[:2000],
        'admin_notes': (request.form.get('admin_notes') or '').strip()[:2000],
        'handled_by': session['user_id'],
        'handled_by_name': session.get('full_name', 'Administrator'),
        'updated_at': firestore.SERVER_TIMESTAMP,
    }
    if status in ('resolved', 'closed') and ticket.get('status') not in ('resolved', 'closed'):
        updates['resolved_at'] = firestore.SERVER_TIMESTAMP
    elif status in ('open', 'in_progress'):
        updates['resolved_at'] = None
    ref.update(updates)

    flash(
        f"{ticket.get('ticket_no') or 'Ticket'} updated to {TICKET_STATUSES[status]}.", 'success')
    return redirect(url_for('admin.support_tickets',
                            status=request.form.get('return_status', 'active')))

# ---------- PAGES NOT BUILT YET ----------


_COMING_SOON = {
    'map_view': ('Map View', 'Monitor device and emergency status across the community.'),
    'emergency_logs': ('Emergency Logs', 'View and manage all emergency alerts and incidents.'),
    'reports': ('Reports', 'Analyze devices, alerts, and community activity.'),
    'subscriptions': ('Subscriptions', 'Manage all subscription plans and user subscriptions.'),
    'settings': ('Settings', 'Manage admin account and basic system preferences.'),
}


def _coming_soon(page):
    title, subtitle = _COMING_SOON[page]
    return render_template('admin/coming_soon.html', active_page=page, title=title, subtitle=subtitle)


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
