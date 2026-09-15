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
from datetime import timedelta, timezone, datetime
from functools import wraps

from flask import (Blueprint, render_template, session, redirect, url_for,
                   flash, request, send_from_directory, current_app, abort,
                   jsonify)
from firebase_admin import firestore

from database import db
import care_plan_service as cps

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


def _admin_required():
    """Guard for JSON endpoints, which return a status code instead of redirecting."""
    return session.get('role') == 'admin'


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


def _pending_payment_count():
    try:
        return sum(1 for d in db.collection('payment').stream()
                   if (d.to_dict() or {}).get('status') == 'pending')
    except Exception:
        return 0


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
        'pending_payment_count': _pending_payment_count(),
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


# ---------- SUBSCRIPTIONS / CARE PLAN PAYMENTS ----------
#
# payment/{id}       -- one per GCash submission from a family (see care_plan_service.py)
# subscription/{elder_id} -- current plan state per elder, extended on approval
# app_settings/plan_qr_codes -- per-duration GCash name/number/QR the admin manages here


PAYMENT_BADGES = {
    'pending': 'badge-pending',
    'approved': 'badge-active',
    'rejected': 'badge-rejected',
}


@admin_bp.route('/subscriptions')
@admin_required
def subscriptions():
    status_filter = request.args.get('status', 'pending')

    all_rows = []
    for doc in db.collection('payment').stream():
        p = doc.to_dict() or {}
        status = p.get('status') or 'pending'
        all_rows.append({
            'id': doc.id,
            'payment_no': p.get('payment_no') or _short_id('PAY', doc.id),
            'elder_name': p.get('elder_name') or 'Unknown',
            'device_id': p.get('device_id') or '—',
            'months': p.get('months') or 1,
            'amount': cps.format_money(p.get('amount')),
            'method': cps.PAYMENT_METHODS.get(p.get('method'), p.get('method') or '—'),
            'reference_no': p.get('reference_no') or '—',
            'payer_name': p.get('payer_name') or '—',
            'status': status,
            'status_label': status.capitalize(),
            'badge': PAYMENT_BADGES.get(status, 'badge-pending'),
            'admin_note': p.get('admin_note') or '',
            'reviewed_by_name': p.get('reviewed_by_name') or '',
            'created': cps.fmt_datetime(p.get('created_at')),
            'sort': _sort_key(p.get('created_at')),
        })

    stats = {
        'pending': sum(r['status'] == 'pending' for r in all_rows),
        'approved': sum(r['status'] == 'approved' for r in all_rows),
        'rejected': sum(r['status'] == 'rejected' for r in all_rows),
        'total': len(all_rows),
    }

    rows = all_rows
    if status_filter in ('pending', 'approved', 'rejected'):
        rows = [r for r in rows if r['status'] == status_filter]
    else:
        status_filter = 'all'
    rows.sort(key=lambda r: -r['sort'])

    plan = cps.get_plan()
    payment_methods = cps.get_payment_methods()
    plan_qr_codes = cps.get_plan_qr_codes()

    return render_template(
        'admin/subscriptions.html', active_page='subscriptions',
        rows=rows, stats=stats, status_filter=status_filter,
        plan=plan, payment_methods=payment_methods,
        plan_qr_codes=plan_qr_codes, month_options=cps.PLAN_MONTH_OPTIONS,
    )


@admin_bp.route('/subscriptions/<payment_id>/approve', methods=['POST'])
@admin_required
def approve_payment(payment_id):
    if not _admin_required():
        return jsonify(success=False, message='Please log in as an administrator.'), 401

    ref = db.collection('payment').document(payment_id)
    doc = ref.get()
    if not doc.exists:
        return jsonify(success=False, message='Payment not found.'), 404
    payment = doc.to_dict() or {}
    if payment.get('status') != 'pending':
        return jsonify(success=False, message='This payment was already reviewed.'), 409

    note = (request.form.get('admin_note') or '').strip()
    try:
        start, end = cps.apply_approved_payment(
            ref, payment,
            reviewer_id=session['user_id'],
            reviewer_name=session.get('full_name'),
            admin_note=note,
        )
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True,
                   message=f'Payment approved. Plan now runs until {cps.fmt_date(end)}.')


@admin_bp.route('/subscriptions/<payment_id>/reject', methods=['POST'])
@admin_required
def reject_payment(payment_id):
    if not _admin_required():
        return jsonify(success=False, message='Please log in as an administrator.'), 401

    ref = db.collection('payment').document(payment_id)
    doc = ref.get()
    if not doc.exists:
        return jsonify(success=False, message='Payment not found.'), 404
    payment = doc.to_dict() or {}
    if payment.get('status') != 'pending':
        return jsonify(success=False, message='This payment was already reviewed.'), 409

    reason = (request.form.get('admin_note') or '').strip()
    if not reason:
        return jsonify(success=False, message='Please give a reason for rejecting.'), 400

    try:
        ref.update({
            'status': 'rejected',
            'admin_note': reason,
            'reviewed_by': session['user_id'],
            'reviewed_by_name': session.get('full_name'),
            'reviewed_at': firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message='Payment rejected.')


@admin_bp.route('/subscriptions/qr-codes/save', methods=['POST'])
@admin_required
def save_plan_qr_codes():
    """Admin sets the GCash name/number/QR image URL for one plan length at a time."""
    try:
        months = int(request.form.get('months', 0))
    except ValueError:
        months = 0
    if months not in cps.PLAN_MONTH_OPTIONS:
        flash('Please choose a valid plan length.', 'error')
        return redirect(url_for('admin.subscriptions'))

    gcash_name = (request.form.get('gcash_name') or '').strip()
    gcash_number = (request.form.get('gcash_number') or '').strip()
    qr_image_url = (request.form.get('qr_image_url') or '').strip()

    if not gcash_number:
        flash('Please enter the GCash number for this plan.', 'error')
        return redirect(url_for('admin.subscriptions'))

    try:
        cps.save_plan_qr_code(months, gcash_name, gcash_number, qr_image_url)
    except Exception as e:
        flash(f'Could not save: {e}', 'error')
        return redirect(url_for('admin.subscriptions'))

    flash(f'GCash details for the {months}-month plan were saved.', 'success')
    return redirect(url_for('admin.subscriptions'))

# ---------- EMERGENCY LOGS ----------
#
# Reads and manages the same Firestore data the family and BHW sides use:
#   alert/{id}                    written by the device API, the family Alerts page,
#                                 and "Log Incident" here
#   emergency_status_history/{id} one row per status change (device, family, BHW, admin)
#   sms_delivery_log/{id}         texts the device sent for an alert
#   activity_log/{id}             shows up in the family History page
# Extra fields this page adds to an alert:
#   alert_no ('AL-2026-0001'), severity ('high'|'medium'|'low'),
#   alert_type, description, admin_notes, source ('device'|'admin'),
#   deleted_at / deleted_by (soft delete)


ALERT_STATUS_GROUPS = {
    'PENDING': 'active', 'SENT': 'active',
    'ACKNOWLEDGED': 'acknowledged', 'RESPONDED': 'acknowledged',
    'RESOLVED': 'resolved',
    'CANCELLED': 'cancelled',
}
ALERT_STATUS_LABELS = {
    'active': 'Active', 'acknowledged': 'Acknowledged',
    'resolved': 'Resolved', 'cancelled': 'Cancelled',
}
ALERT_STATUS_BADGES = {
    'active': 'badge-rejected', 'acknowledged': 'badge-acknowledged',
    'resolved': 'badge-resolved', 'cancelled': 'badge-archived',
}
# What the admin can set -> what gets stored.
ALERT_STATUS_VALUES = {
    'active': 'SENT', 'acknowledged': 'ACKNOWLEDGED',
    'resolved': 'RESOLVED', 'cancelled': 'CANCELLED',
}
SEVERITY_LABELS = {'high': 'High', 'medium': 'Medium', 'low': 'Low'}
SEVERITY_BADGES = {'high': 'badge-rejected',
                   'medium': 'badge-pending', 'low': 'badge-low'}
ALERT_TYPES = {
    'voice': 'Voice Emergency',
    'help_button': 'Help Button Pressed',
    'medical': 'Medical Emergency',
    'fall': 'Fall Detected',
    'no_movement': 'No Movement',
    'other': 'Other Emergency',
}
ROLE_LABELS = {'family': 'Family', 'bhw': 'BHW', 'admin': 'Admin'}


def _alert_type_key(alert):
    if alert.get('alert_type') in ALERT_TYPES:
        return alert['alert_type']
    return 'help_button' if str(alert.get('trigger_type')).upper() == 'BUTTON' else 'voice'


def _alert_type_sub(alert, type_key):
    if alert.get('description'):
        return alert['description']
    if type_key == 'voice' and alert.get('phrase_used'):
        return f'Said "{alert["phrase_used"]}"'
    if type_key == 'help_button':
        return 'Manual alert'
    return 'Device triggered' if alert.get('device_id') else '—'


def _age(dob):
    try:
        born = datetime.strptime(dob, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None
    today = cps.now_ph().date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _split_location(text):
    """'Purok 1, Sitio Apas, Lahug, Cebu City' -> ('Lahug, Cebu City', 'Purok 1, Sitio Apas')"""
    parts = [p.strip() for p in (text or '').split(',') if p.strip()]
    if not parts:
        return 'Location not recorded', ''
    if len(parts) <= 2:
        return ', '.join(parts), ''
    return ', '.join(parts[-2:]), ', '.join(parts[:-2])


def _duration(seconds):
    if seconds is None:
        return '—'
    seconds = int(round(seconds))
    if seconds < 60:
        return f'{seconds}s'
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}m {secs:02d}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes:02d}m'


def _assign_alert_numbers(alerts):
    """Gives every alert a permanent 'AL-YYYY-NNNN' number, in the order they happened."""
    highest = {}
    for _, a in alerts:
        m = re.match(r'^AL-(\d{4})-(\d+)$', a.get('alert_no') or '')
        if m:
            highest[m.group(1)] = max(
                highest.get(m.group(1), 0), int(m.group(2)))

    missing = sorted((pair for pair in alerts if not pair[1].get('alert_no')),
                     key=lambda pair: _sort_key(pair[1].get('created_at')))
    for doc_id, a in missing:
        created = cps.to_ph(a.get('created_at')) or cps.now_ph()
        year = str(created.year)
        highest[year] = highest.get(year, 0) + 1
        a['alert_no'] = f'AL-{year}-{highest[year]:04d}'
        try:
            db.collection('alert').document(
                doc_id).update({'alert_no': a['alert_no']})
        except Exception as e:
            print(f'[emergency logs] could not save alert number: {e}')


def _log_admin_activity(elder_id, title, detail='', badge='', badge_class='',
                        location='', device_id=None, ref_id=None):
    """Same shape as app.py's _log_activity, so it shows in the family History page."""
    if not elder_id:
        return
    try:
        db.collection('activity_log').add({
            'elder_id': elder_id, 'type': 'alert',
            'title': title, 'detail': detail or '',
            'badge': badge, 'badge_class': badge_class,
            'location_address': location or '', 'device_id': device_id,
            'actor_user_id': session.get('user_id'), 'ref_id': ref_id,
            'created_at': firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f'[emergency logs] could not write history: {e}')


def _add_status_history(alert_id, status, notes=''):
    db.collection('emergency_status_history').add({
        'alert_id': alert_id,
        'status': status,
        'changed_by_user_id': session.get('user_id'),
        'notes': notes,
        'changed_at': firestore.SERVER_TIMESTAMP,
    })


@admin_bp.route('/emergency-logs')
@admin_required
def emergency_logs():
    now = cps.now_ph()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # --- load everything once ---
    all_alerts = [(d.id, d.to_dict() or {})
                  for d in db.collection('alert').stream()]
    _assign_alert_numbers(all_alerts)
    alerts = [(i, a) for i, a in all_alerts if not a.get('deleted_at')]

    elders = {i: e for i, e in ((d.id, d.to_dict() or {})
                                for d in db.collection('elder_profile').stream())}
    devices = {d.id: d.to_dict() or {}
               for d in db.collection('device').stream()}
    users = {d.id: d.to_dict() or {}
             for d in db.collection('user_account').stream()}

    family_by_elder = {}
    for d in db.collection('family_elder_link').stream():
        link = d.to_dict() or {}
        user = users.get(link.get('family_user_id'))
        if user and not user.get('deleted_at'):
            family_by_elder.setdefault(link.get('elder_id'), []).append(
                f"{user.get('full_name') or 'Unnamed'} ({link.get('relationship') or 'Family'})")

    bhws = []
    for d in db.collection('bhw_profile').stream():
        p = d.to_dict() or {}
        user = users.get(p.get('user_id'))
        if user and user.get('approval_status') == 'approved' and p.get('barangay_assigned'):
            bhws.append((p['barangay_assigned'].strip().casefold(),
                        user.get('full_name') or 'Unnamed'))

    history_by_alert = {}
    for d in db.collection('emergency_status_history').stream():
        h = d.to_dict() or {}
        history_by_alert.setdefault(h.get('alert_id'), []).append(h)

    sms_by_alert = {}
    for d in db.collection('sms_delivery_log').stream():
        s = d.to_dict() or {}
        sms_by_alert.setdefault(s.get('alert_id'), []).append(s)

    def person(user_id):
        if not user_id:
            return 'ALISTO device'
        u = users.get(user_id) or {}
        role = ROLE_LABELS.get(u.get('role'), 'User')
        return f"{u.get('full_name') or 'Unknown'} ({role})"

    rows = []
    response_times = []
    for alert_id, a in alerts:
        elder = elders.get(a.get('elder_id')) or {}
        device = devices.get(a.get('device_id')) or {}
        group = ALERT_STATUS_GROUPS.get(
            str(a.get('status') or 'PENDING').upper(), 'active')
        severity = a.get('severity') if a.get(
            'severity') in SEVERITY_LABELS else 'high'
        type_key = _alert_type_key(a)
        created = cps.to_ph(a.get('created_at'))
        location_text = a.get('location_address') or elder.get('address') or ''
        loc_main, loc_sub = _split_location(location_text)
        age = _age(elder.get('date_of_birth'))

        # Response time = first response (acknowledged, else resolved) minus when it started.
        responded = cps.to_ph(a.get('acknowledged_at')
                              ) or cps.to_ph(a.get('resolved_at'))
        response_seconds = None
        if created and responded and responded >= created:
            response_seconds = (responded - created).total_seconds()
            if created >= month_start:
                response_times.append(response_seconds)

        barangay_parts = [p.strip().casefold()
                          for p in (elder.get('address') or '').split(',')]
        assigned_bhws = [name for brgy, name in bhws if brgy in barangay_parts]

        timeline = []
        for h in sorted(history_by_alert.get(alert_id, []), key=lambda h: _sort_key(h.get('changed_at'))):
            g = ALERT_STATUS_GROUPS.get(
                str(h.get('status') or '').upper(), 'active')
            timeline.append({
                'when': cps.fmt_datetime(h.get('changed_at')),
                'status': ALERT_STATUS_LABELS[g],
                'by': person(h.get('changed_by_user_id')),
                'notes': h.get('notes') or '',
            })

        sms = sms_by_alert.get(alert_id, [])
        lat = a.get('latitude')
        lng = a.get('longitude')

        rows.append({
            'id': alert_id,
            'alert_no': a.get('alert_no'),
            'status': group,
            'status_label': ALERT_STATUS_LABELS[group],
            'status_badge': ALERT_STATUS_BADGES[group],
            'severity': severity,
            'severity_label': SEVERITY_LABELS[severity],
            'severity_badge': SEVERITY_BADGES[severity],
            'device_id': a.get('device_id') or '—',
            'device_place': device.get('placement') or device.get('batch') or '',
            'elder_id': a.get('elder_id') or '',
            'elder_name': elder.get('full_name') or 'Unknown elder',
            'age': age,
            'loc_main': loc_main,
            'loc_sub': loc_sub,
            'type_key': type_key,
            'type_label': ALERT_TYPES[type_key],
            'type_sub': _alert_type_sub(a, type_key),
            'date': created.strftime('%Y-%m-%d') if created else '',
            'date_label': created.strftime('%b %d, %Y') if created else '—',
            'time_label': created.strftime('%I:%M %p').lstrip('0') if created else '',
            'sort': created.timestamp() if created else 0,
            'details': {
                'id': alert_id,
                'alert_no': a.get('alert_no'),
                'status': group,
                'severity': severity,
                'admin_notes': a.get('admin_notes') or '',
                'rows': [
                    ['Elder', f"{elder.get('full_name') or 'Unknown'}" +
                     (f', {age} yrs old' if age is not None else '')],
                    ['Address', elder.get('address') or '—'],
                    ['Device', a.get('device_id') or '—'],
                    ['Type', ALERT_TYPES[type_key]],
                    ['Details', _alert_type_sub(a, type_key)],
                    ['Severity', SEVERITY_LABELS[severity]],
                    ['Status', ALERT_STATUS_LABELS[group]],
                    ['Location', location_text or 'Not recorded'],
                    ['Started', cps.fmt_datetime(a.get('created_at'))],
                    ['Response time', _duration(response_seconds)],
                    ['Responded by', person(a.get('acknowledged_by')) if a.get(
                        'acknowledged_by') else '—'],
                    ['Resolved by', person(a.get('resolved_by')) if a.get(
                        'resolved_by') else '—'],
                    ['Family', ', '.join(family_by_elder.get(
                        a.get('elder_id'), [])) or 'None linked'],
                    ['Barangay BHW', ', '.join(
                        assigned_bhws) or 'None assigned'],
                    ['SMS sent',
                        f"{sum(1 for s in sms if s.get('delivery_status') == 'SENT')} of {len(sms)}" if sms else 'None reported'],
                    ['Source', 'Logged by admin' if a.get(
                        'source') == 'admin' else 'ALISTO device'],
                ],
                'map_url': (f'https://www.google.com/maps?q={lat},{lng}'
                            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) else ''),
                'timeline': timeline,
            },
        })

    rows.sort(key=lambda r: -r['sort'])

    stats = {
        'total': len(rows),
        'active': sum(r['status'] == 'active' for r in rows),
        'resolved': sum(r['status'] == 'resolved' for r in rows),
        'avg_response': _duration(sum(response_times) / len(response_times)) if response_times else '—',
        'this_month': sum(r['sort'] >= month_start.timestamp() for r in rows),
        'month_label': now.strftime('%B %Y'),
    }

    elder_options = sorted(
        ({'id': i, 'name': e.get('full_name') or 'Unnamed', 'address': e.get('address') or ''}
         for i, e in elders.items() if not e.get('deleted_at')),
        key=lambda e: e['name'].casefold())

    return render_template(
        'admin/emergency_logs.html', active_page='emergency_logs',
        rows=rows, stats=stats, elder_options=elder_options,
        statuses=ALERT_STATUS_LABELS, severities=SEVERITY_LABELS, alert_types=ALERT_TYPES,
    )


@admin_bp.route('/emergency-logs/new', methods=['POST'])
@admin_required
def create_emergency_log():
    """An incident reported another way (phone call, walk-in) that the device did not send."""
    elder_id = request.form.get('elder_id', '')
    alert_type = request.form.get('alert_type', '')
    severity = request.form.get('severity', '')
    status = request.form.get('status', 'active')
    description = (request.form.get('description') or '').strip()[:300]
    location = (request.form.get('location_address') or '').strip()[:200]

    elder_doc = db.collection('elder_profile').document(
        elder_id).get() if elder_id else None
    elder = (elder_doc.to_dict() or {}
             ) if elder_doc and elder_doc.exists else None
    if not elder or elder.get('deleted_at'):
        flash('Please choose the elder this incident is for.', 'error')
        return redirect(url_for('admin.emergency_logs'))
    if alert_type not in ALERT_TYPES or severity not in SEVERITY_LABELS or status not in ALERT_STATUS_VALUES:
        flash('Please fill in the type, severity, and status.', 'error')
        return redirect(url_for('admin.emergency_logs'))

    stored_status = ALERT_STATUS_VALUES[status]
    device_id = cps.device_for_elder(elder_id)
    location = location or elder.get('address') or ''

    data = {
        'elder_id': elder_id,
        'device_id': device_id,
        'trigger_type': 'MANUAL',
        'alert_type': alert_type,
        'description': description,
        'severity': severity,
        'phrase_used': None,
        'latitude': None,
        'longitude': None,
        'location_address': location,
        'status': stored_status,
        'title': ALERT_TYPES[alert_type],
        'source': 'admin',
        'created_by': session['user_id'],
        'created_at': firestore.SERVER_TIMESTAMP,
        'deleted_at': None,
    }
    if status in ('acknowledged', 'resolved'):
        data.update(acknowledged_at=firestore.SERVER_TIMESTAMP,
                    acknowledged_by=session['user_id'])
    if status == 'resolved':
        data.update(resolved_at=firestore.SERVER_TIMESTAMP,
                    resolved_by=session['user_id'])

    ref = db.collection('alert').document()
    ref.set(data)
    _add_status_history(ref.id, stored_status, 'Logged by admin')
    _log_admin_activity(elder_id, f'{ALERT_TYPES[alert_type]} logged by admin',
                        detail=description, badge=ALERT_STATUS_LABELS[status],
                        badge_class='danger' if status == 'active' else 'success',
                        location=location, device_id=device_id, ref_id=ref.id)

    flash(
        f"Incident logged for {elder.get('full_name') or 'the elder'}.", 'success')
    return redirect(url_for('admin.emergency_logs'))


@admin_bp.route('/emergency-logs/<alert_id>/update', methods=['POST'])
@admin_required
def update_emergency_log(alert_id):
    ref = db.collection('alert').document(alert_id)
    doc = ref.get()
    alert = (doc.to_dict() or {}) if doc.exists else None
    if not alert or alert.get('deleted_at'):
        flash('That alert no longer exists.', 'error')
        return redirect(url_for('admin.emergency_logs'))

    status = request.form.get('status', '')
    severity = request.form.get('severity') or alert.get('severity') or 'high'
    notes = (request.form.get('admin_notes') or '').strip()[:1000]
    if status not in ALERT_STATUS_VALUES or severity not in SEVERITY_LABELS:
        flash('Please choose a valid status and severity.', 'error')
        return redirect(url_for('admin.emergency_logs'))

    old_group = ALERT_STATUS_GROUPS.get(
        str(alert.get('status') or 'PENDING').upper(), 'active')
    updates = {
        'severity': severity,
        'admin_notes': notes,
        'updated_at': firestore.SERVER_TIMESTAMP,
        'updated_by': session['user_id'],
    }

    if status != old_group:
        updates['status'] = ALERT_STATUS_VALUES[status]
        if status in ('acknowledged', 'resolved') and not alert.get('acknowledged_at'):
            updates.update(acknowledged_at=firestore.SERVER_TIMESTAMP,
                           acknowledged_by=session['user_id'])
        if status == 'resolved':
            updates.update(resolved_at=firestore.SERVER_TIMESTAMP,
                           resolved_by=session['user_id'])
        if status == 'cancelled':
            updates.update(cancelled_at=firestore.SERVER_TIMESTAMP,
                           cancel_reason=notes or 'Marked as false alarm by admin')
        if status == 'active':  # reopened
            updates.update(resolved_at=None, resolved_by=None)

    ref.update(updates)

    if status != old_group:
        _add_status_history(
            alert_id, ALERT_STATUS_VALUES[status], notes or 'Updated by admin')
        _log_admin_activity(
            alert.get(
                'elder_id'), f'Emergency alert marked {ALERT_STATUS_LABELS[status].lower()} by admin',
            detail=notes, badge=ALERT_STATUS_LABELS[status],
            badge_class={'active': 'danger', 'acknowledged': 'warning',
                         'resolved': 'success', 'cancelled': 'warning'}[status],
            location=alert.get('location_address') or '',
            device_id=alert.get('device_id'), ref_id=alert_id)
        flash(
            f"{alert.get('alert_no') or 'Alert'} is now {ALERT_STATUS_LABELS[status]}.", 'success')
    else:
        flash(f"{alert.get('alert_no') or 'Alert'} was updated.", 'success')
    return redirect(url_for('admin.emergency_logs'))


@admin_bp.route('/emergency-logs/<alert_id>/delete', methods=['POST'])
@admin_required
def delete_emergency_log(alert_id):
    """Soft delete (for tests and duplicates). The record stays in Firestore."""
    ref = db.collection('alert').document(alert_id)
    doc = ref.get()
    alert = (doc.to_dict() or {}) if doc.exists else None
    if not alert or alert.get('deleted_at'):
        flash('That alert no longer exists.', 'error')
        return redirect(url_for('admin.emergency_logs'))

    ref.update({'deleted_at': firestore.SERVER_TIMESTAMP,
               'deleted_by': session['user_id']})
    db.collection('emergency_status_history').add({
        'alert_id': alert_id, 'status': 'DELETED',
        'changed_by_user_id': session['user_id'],
        'notes': 'Removed from the logs by admin',
        'changed_at': firestore.SERVER_TIMESTAMP,
    })
    flash(f"{alert.get('alert_no') or 'Alert'} was removed from the logs.", 'success')
    return redirect(url_for('admin.emergency_logs'))


# ---------- PAGES NOT BUILT YET ----------
_COMING_SOON = {
    'map_view': ('Map View', 'Monitor device and emergency status across the community.'),
    'reports': ('Reports', 'Analyze devices, alerts, and community activity.'),
    'settings': ('Settings', 'Manage admin account and basic system preferences.'),
}


def _coming_soon(page):
    title, subtitle = _COMING_SOON[page]
    return render_template('admin/coming_soon.html', active_page=page, title=title, subtitle=subtitle)


@admin_bp.route('/map')
@admin_required
def map_view():
    return _coming_soon('map_view')


@admin_bp.route('/reports')
@admin_required
def reports():
    return _coming_soon('reports')


@admin_bp.route('/settings')
@admin_required
def settings():
    return _coming_soon('settings')
