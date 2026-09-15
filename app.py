from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from database import db
from admin_routes import admin_bp
from bhw_routes import bhw_bp
from firebase_admin import firestore, auth as firebase_auth
from datetime import datetime, timedelta, timezone
import care_plan_service as cps
import requests
import os
import uuid
import re
import base64
import csv
import io

app = Flask(__name__)
app.secret_key = 'alisto-secret-key-change-this-later'
app.register_blueprint(admin_bp)  # admin panel at /admin
app.register_blueprint(bhw_bp)    # BHW portal at /bhw

db = firestore.client()

MAX_FAMILY_PER_DEVICE = 5
FIREBASE_WEB_API_KEY = "AIzaSyBFkRaWyU_j6qcspXuOsJUXteRDIw8thqE"

# ---------- BHW REGISTRATION SETTINGS ----------
HEALTH_CENTERS = ['Sudlon II Health Center']
YEARS_OF_SERVICE_OPTIONS = [
    'Less than 1 year', '1-3 years', '4-6 years', '7-10 years', 'More than 10 years'
]
VALID_ID_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf'}
VALID_ID_MAX_BYTES = 2 * 1024 * 1024
VALID_ID_UPLOAD_FOLDER = os.path.join(app.root_path, 'uploads', 'bhw_ids')


def _verify_firebase_password(email, password):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
    resp = requests.post(url, json={
        "email": email, "password": password, "returnSecureToken": True
    })
    return resp.json() if resp.ok else None


TEMP_LOCATION = {
    'latitude': 10.322429,
    'longitude': 123.902542,
    'address': 'Lahug, Cebu City',
    'last_updated': 'Sample data (waiting for device)',
}


@app.context_processor
def inject_account():
    if 'user_id' not in session:
        return {}

    doc = db.collection('user_account').document(session['user_id']).get()
    user = doc.to_dict() if doc.exists else {}

    name = user.get('full_name') or session.get('full_name') or ''
    return {
        'account': {
            'full_name': name,
            'initials': _initials(name) if name else '?',
            'role': user.get('role') or session.get('role') or '',
            'photo_base64': user.get('photoBase64') or None,
        }
    }


# AFTER (tinuod na, Firestore + auto-picks the family's elder):
def _get_care_plan(elder_id=None):
    if elder_id is None and 'user_id' in session:
        elders = _linked_elders_for_dashboard()
        elder_id = next(iter(elders), None)
    plan = cps.get_plan()
    sub = {}
    if elder_id:
        doc = db.collection('subscription').document(elder_id).get()
        sub = doc.to_dict() or {} if doc.exists else {}
    state = cps.subscription_state(sub)
    return {
        'status': state['label'], 'days_left': state['days_left'],
        'renew_date': state['paid_until'], 'price': plan['price'],
        'currency': plan.get('currency', 'PHP'),
    }


@app.context_processor
def inject_care_plan_extras():
    if 'user_id' not in session or session.get('role') != 'family':
        return {}
    return {
        'care_plan_elders': _linked_elders_for_dashboard(),
        'care_plan_gcash': cps.get_payment_methods(),
        'care_plan_month_options': cps.PLAN_MONTH_OPTIONS,
        'care_plan_qr_codes': cps.get_plan_qr_codes(),   # NEW
    }


def _first_name(full_name: str) -> str:
    full_name = (full_name or '').strip()
    return full_name.split(' ')[0] if full_name else 'this elder'


def _family_link_count(elder_id: str) -> int:
    links = db.collection('family_elder_link').where(
        'elder_id', '==', elder_id).stream()
    count = 0
    for link in links:
        user_doc = db.collection('user_account').document(
            link.to_dict()['family_user_id']).get()
        if user_doc.exists:
            u = user_doc.to_dict()
            if u.get('role') == 'family' and not u.get('deleted_at'):
                count += 1
    return count


FAMILY_PASSWORD_RULES = [
    (lambda p: len(p) >= 6,                   'be at least 6 characters'),
    (lambda p: re.search(r'[A-Z]', p),        'include an uppercase letter'),
    (lambda p: re.search(r'[a-z]', p),        'include a lowercase letter'),
    (lambda p: re.search(r'\d', p),           'include a number'),
    (lambda p: re.search(r'[^A-Za-z0-9]', p), 'include a special character'),
]


def _check_family_password(password):
    missing = [label for rule,
               label in FAMILY_PASSWORD_RULES if not rule(password)]
    return 'Password must ' + ', '.join(missing) + '.' if missing else None


@app.route('/check_device', methods=['POST'])
def check_device():
    data = request.get_json()
    serial_number = (data.get('device_id') or '').strip()

    if not serial_number:
        return jsonify(valid=False, message='Please enter a Device ID.'), 400

    device_doc = db.collection('device').document(serial_number).get()
    if not device_doc.exists:
        return jsonify(valid=False, message='No matching Device ID found in the system. Please check the QR code or serial number.')

    device = device_doc.to_dict()

    if device.get('is_registered'):
        elder_id = device.get('elder_id')
        elder_doc = db.collection('elder_profile').document(
            elder_id).get() if elder_id else None
        elder = elder_doc.to_dict() if elder_doc and elder_doc.exists and not elder_doc.to_dict(
        ).get('deleted_at') else None

        if not elder:
            return jsonify(valid=False, message='This Device ID cannot be registered right now. Please contact an administrator.')

        requesting_role = (data.get('role') or 'family').strip()
        family_count = _family_link_count(elder_id)

        if requesting_role == 'family' and family_count >= MAX_FAMILY_PER_DEVICE:
            return jsonify(
                valid=True, already_registered=True, can_join=False,
                elder_id=elder_id,
                elder_display_name=_first_name(elder.get('full_name')),
                family_count=family_count, family_limit=MAX_FAMILY_PER_DEVICE,
                message=f"This device already has the maximum of {MAX_FAMILY_PER_DEVICE} linked family members."
            )

        return jsonify(
            valid=True, already_registered=True, can_join=True,
            elder_id=elder_id,
            elder_display_name=_first_name(elder.get('full_name')),
            elder_full_name=elder.get('full_name'),
            elder_dob=elder.get('date_of_birth'),
            family_count=family_count, family_limit=MAX_FAMILY_PER_DEVICE,
            message='This device is already linked to an ALISTO user.'
        )

    return jsonify(valid=True, already_registered=False, message='Device ID verified!')


# ---------- LANDING ----------

@app.route('/')
def landing():
    return render_template('landing.html')


# ---------- REGISTER ----------

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        role = request.form.get('role', '')
        contact_number = request.form.get('contact_number', '').strip()
        barangay_assigned = request.form.get(
            'barangay_assigned', 'Sudlon II').strip()

        serial_number = request.form.get('device_id', '').strip()
        join_mode = request.form.get('join_mode', 'claim').strip()
        existing_elder_id = request.form.get('existing_elder_id', '').strip()

        elder_full_name = request.form.get('senior_full_name', '').strip()
        dob = request.form.get('dob', '').strip()
        relationship = request.form.get('relationship', '').strip()

        house_no = request.form.get('house_no', '').strip()
        street = request.form.get('street', '').strip()
        elder_barangay = request.form.get('barangay', '').strip()
        city = request.form.get('city', '').strip()
        province = request.form.get('province', '').strip()
        zip_code = request.form.get('zip_code', '').strip()

        if not full_name or not email or not password or not role:
            return jsonify(success=False, message='Please fill up all required account fields.'), 400
        if password != confirm_password:
            return jsonify(success=False, message='Password and confirm password do not match.'), 400
        if role not in ('family', 'bhw'):
            return jsonify(success=False, message='Invalid role selected.'), 400

        if role == 'bhw':
            return _register_bhw(full_name, email, password, contact_number, barangay_assigned)

        password_error = _check_family_password(password)
        if password_error:
            return jsonify(success=False, message=password_error), 400

        if not serial_number:
            return jsonify(success=False, message='Please enter a Device ID.'), 400
        if join_mode not in ('claim', 'join'):
            return jsonify(success=False, message='Invalid registration mode.'), 400

        if join_mode == 'claim':
            if not elder_full_name or not dob:
                return jsonify(success=False, message='Please fill up the elderly personal information.'), 400
        else:
            if not existing_elder_id:
                return jsonify(success=False, message='Missing elder reference for joining.'), 400
            if not relationship:
                return jsonify(success=False, message='Please specify your relationship to the elder.'), 400

        existing_user = list(
            db.collection('user_account').where(
                'email', '==', email).limit(1).stream()
        )
        if existing_user:
            return jsonify(success=False, message='An account with this email already exists.'), 400

        try:
            device_doc = db.collection('device').document(serial_number).get()
            if not device_doc.exists:
                return jsonify(success=False, message='No matching Device ID found in the system.'), 400
            device = device_doc.to_dict()

            if join_mode == 'claim':
                if device.get('is_registered'):
                    elder_id = device.get('elder_id')
                    elder_doc = db.collection(
                        'elder_profile').document(elder_id).get()
                    if not elder_doc.exists or elder_doc.to_dict().get('deleted_at'):
                        return jsonify(success=False, message='That elder profile no longer exists.'), 400

                    if role == 'family' and _family_link_count(elder_id) >= MAX_FAMILY_PER_DEVICE:
                        return jsonify(success=False, message=f'This device already has the maximum of {MAX_FAMILY_PER_DEVICE} linked family members.'), 409

                    elder_id = _create_account_and_join(
                        full_name, email, password, role, contact_number,
                        barangay_assigned, elder_id, relationship
                    )
                else:
                    elder_id = _create_account_and_elder(
                        full_name, email, password, role, contact_number,
                        barangay_assigned, elder_full_name, dob,
                        ', '.join(
                            filter(None, [house_no, street, elder_barangay, city, province, zip_code])),
                        relationship, serial_number
                    )
            else:
                if not device.get('is_registered') or device.get('elder_id') != existing_elder_id:
                    return jsonify(success=False, message='This device is no longer linked to that elder. Please re-check the Device ID.'), 409

                elder_doc = db.collection('elder_profile').document(
                    existing_elder_id).get()
                if not elder_doc.exists or elder_doc.to_dict().get('deleted_at'):
                    return jsonify(success=False, message='That elder profile no longer exists.'), 400

                if role == 'family' and _family_link_count(existing_elder_id) >= MAX_FAMILY_PER_DEVICE:
                    return jsonify(success=False, message=f'This device already has the maximum of {MAX_FAMILY_PER_DEVICE} linked family members.'), 409

                elder_id = _create_account_and_join(
                    full_name, email, password, role, contact_number,
                    barangay_assigned, existing_elder_id, relationship
                )

            return jsonify(success=True, message='Registration successful!')

        except Exception as e:
            return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return render_template(
        'register.html',
        health_centers=HEALTH_CENTERS,
        years_of_service_options=YEARS_OF_SERVICE_OPTIONS,
    )


def _register_bhw(full_name, email, password, contact_number, barangay_assigned):
    dob = request.form.get('bhw_dob', '').strip()
    health_center = request.form.get('health_center', '').strip()
    years_of_service = request.form.get('years_of_service', '').strip()
    bhw_id_number = request.form.get('bhw_id_number', '').strip()
    valid_id = request.files.get('valid_id')

    if not barangay_assigned:
        return jsonify(success=False, message='Please enter your assigned barangay.'), 400
    if not dob or not health_center or not years_of_service or not bhw_id_number:
        return jsonify(success=False, message='Please fill up all professional information fields.'), 400
    if health_center not in HEALTH_CENTERS:
        return jsonify(success=False, message='Please select a valid health center.'), 400
    if years_of_service not in YEARS_OF_SERVICE_OPTIONS:
        return jsonify(success=False, message='Please select a valid years of service option.'), 400

    try:
        if datetime.strptime(dob, '%Y-%m-%d').date() > datetime.now().date():
            return jsonify(success=False, message='Date of birth cannot be in the future.'), 400
    except ValueError:
        return jsonify(success=False, message='Please enter a valid date of birth.'), 400

    if list(db.collection('user_account').where('email', '==', email).limit(1).stream()):
        return jsonify(success=False, message='An account with this email already exists.'), 400
    if list(db.collection('bhw_profile').where('bhw_id_number', '==', bhw_id_number).limit(1).stream()):
        return jsonify(success=False, message='This BHW ID number is already registered.'), 400

    valid_id_filename = None
    stored_path = None
    if valid_id and valid_id.filename:
        ext = valid_id.filename.rsplit(
            '.', 1)[-1].lower() if '.' in valid_id.filename else ''
        if ext not in VALID_ID_EXTENSIONS:
            return jsonify(success=False, message='Valid ID must be a JPG, PNG, or PDF file.'), 400

        valid_id.stream.seek(0, os.SEEK_END)
        size = valid_id.stream.tell()
        valid_id.stream.seek(0)
        if size > VALID_ID_MAX_BYTES:
            return jsonify(success=False, message='Valid ID must be 2MB or smaller.'), 400

        valid_id_filename = secure_filename(
            valid_id.filename) or f'valid_id.{ext}'
        os.makedirs(VALID_ID_UPLOAD_FOLDER, exist_ok=True)
        stored_name = f'{uuid.uuid4().hex}.{ext}'
        stored_path = os.path.join(VALID_ID_UPLOAD_FOLDER, stored_name)

    try:
        if stored_path:
            valid_id.save(stored_path)

        user_ref = db.collection('user_account').document()
        user_ref.set({
            'full_name': full_name, 'email': email,
            'password_hash': generate_password_hash(password),
            'role': 'bhw', 'contact_number': contact_number,
            'approval_status': 'pending',
            'created_at': firestore.SERVER_TIMESTAMP, 'deleted_at': None, 'is_archived': 0
        })

        db.collection('bhw_profile').add({
            'user_id': user_ref.id,
            'barangay_assigned': barangay_assigned,
            'date_of_birth': dob,
            'health_center': health_center,
            'years_of_service': years_of_service,
            'bhw_id_number': bhw_id_number,
            'valid_id_filename': valid_id_filename,
            'valid_id_stored_as': os.path.basename(stored_path) if stored_path else None,
            'created_at': firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        if stored_path and os.path.exists(stored_path):
            os.remove(stored_path)
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message='Registration submitted. Your account is pending approval.')


def _create_account_and_elder(full_name, email, password, role, contact_number,
                              barangay_assigned, elder_full_name, dob, address,
                              relationship, serial_number):
    firebase_user = firebase_auth.create_user(
        email=email, password=password, display_name=full_name)
    user_ref = db.collection('user_account').document(firebase_user.uid)
    user_ref.set({
        'full_name': full_name, 'email': email,
        'role': role, 'contact_number': contact_number,
        'created_at': firestore.SERVER_TIMESTAMP, 'deleted_at': None, 'is_archived': 0
    })
    user_id = user_ref.id

    if role == 'bhw':
        db.collection('bhw_profile').add(
            {'user_id': user_id, 'barangay_assigned': barangay_assigned})

    elder_ref = db.collection('elder_profile').document()
    elder_ref.set({
        'full_name': elder_full_name, 'date_of_birth': dob, 'address': address,
        'created_at': firestore.SERVER_TIMESTAMP, 'deleted_at': None, 'is_archived': 0
    })
    elder_id = elder_ref.id

    db.collection('family_elder_link').add({
        'family_user_id': user_id, 'elder_id': elder_id, 'relationship': relationship
    })

    device_ref = db.collection('device').document(serial_number)
    device_doc = device_ref.get()
    device_data = device_doc.to_dict() if device_doc.exists else {}

    device_ref.set({
        'elder_id': elder_id,
        'is_registered': True,
        'registered_at': firestore.SERVER_TIMESTAMP,
        'serial_number': serial_number,
        'gps': device_data.get('gps', 'Inactive'),
        'sim_number': device_data.get('sim_number', None),
        'status': device_data.get('status', 'Offline'),
    }, merge=True)

    return elder_id


def _create_account_and_join(full_name, email, password, role, contact_number,
                             barangay_assigned, existing_elder_id, relationship):
    firebase_user = firebase_auth.create_user(
        email=email, password=password, display_name=full_name)
    user_ref = db.collection('user_account').document(firebase_user.uid)
    user_ref.set({
        'full_name': full_name, 'email': email,
        'role': role, 'contact_number': contact_number,
        'created_at': firestore.SERVER_TIMESTAMP, 'deleted_at': None, 'is_archived': 0
    })
    user_id = user_ref.id

    if role == 'bhw':
        db.collection('bhw_profile').add(
            {'user_id': user_id, 'barangay_assigned': barangay_assigned})

    existing_link = list(
        db.collection('family_elder_link')
        .where('family_user_id', '==', user_id)
        .where('elder_id', '==', existing_elder_id)
        .stream()
    )
    if not existing_link:
        db.collection('family_elder_link').add({
            'family_user_id': user_id, 'elder_id': existing_elder_id, 'relationship': relationship
        })

    return existing_elder_id


# ---------- LOGIN ----------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        matches = list(
            db.collection('user_account').where(
                'email', '==', email).limit(1).stream()
        )
        user_doc = matches[0] if matches else None
        user = user_doc.to_dict() if user_doc else None

        password_ok = False
        if user and not user.get('deleted_at'):
            if 'password_hash' in user:
                password_ok = check_password_hash(
                    user['password_hash'], password)
            else:
                password_ok = _verify_firebase_password(
                    email, password) is not None

        if user and not user.get('deleted_at') and password_ok:
            if user.get('role') == 'bhw' and user.get('approval_status') != 'approved':
                if user.get('approval_status') == 'rejected':
                    flash(
                        'Your BHW account was not approved. Please contact the system administrator.', 'error')
                else:
                    flash(
                        'Your BHW account is still pending approval by the system administrator.', 'error')
                return redirect(url_for('login'))

            session['user_id'] = user_doc.id
            session['full_name'] = user['full_name']
            session['role'] = user['role']

            previous = user.get('last_login_at')
            if hasattr(previous, 'astimezone'):
                previous = previous.astimezone(
                    timezone(timedelta(hours=8)))
            session['previous_login'] = previous.strftime(
                '%b %d, %Y %I:%M %p') if hasattr(previous, 'strftime') else None
            updates = {'last_login_at': firestore.SERVER_TIMESTAMP}

            if user['role'] == 'bhw':
                if not user.get('approved_notice_seen'):
                    updates['approved_notice_seen'] = True
                    user_doc.reference.update(updates)
                    return redirect(url_for('bhw.account_approved'))
                user_doc.reference.update(updates)
                flash(f"Welcome back, {user['full_name']}!", 'success')
                return redirect(url_for('bhw.dashboard'))

            user_doc.reference.update(updates)
            flash(f"Welcome back, {user['full_name']}!", 'success')
            if user['role'] == 'admin':
                return redirect(url_for('admin.dashboard'))
            return redirect(url_for('dashboard'))
        else:
            flash('Incorrect email or password.', 'error')
            return redirect(url_for('login'))

    return render_template('login.html')


# ---------- LOGOUT ----------

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))


# ---------- MY LOVED ONES ----------
#
# Card status (data-status on each card, used by the filter tabs):
#   online    -> device checked in within DEVICE_ONLINE_WINDOW
#   offline   -> device registered but quiet
#   attention -> open emergency, no device, or device not registered
# TEMP_LOCATION is only a placeholder until the device sends a location.

LOVED_ONE_PHOTO_MAX_BYTES = 700 * 1024       # base64 text stored in Firestore
LOVED_ONE_PHOTO_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MISSED_MED_STATUSES = {'missed', 'overdue'}  # written by the device


def _clean_device_id(raw):
    """Same normalisation the admin panel uses when it pre-registers serials."""
    return re.sub(r'\s+', '', raw or '').upper()


def _valid_dob(dob):
    """None if OK (or blank), otherwise an error message."""
    if not dob:
        return None
    try:
        born = datetime.strptime(dob, '%Y-%m-%d').date()
    except ValueError:
        return 'Please enter a valid date of birth.'
    if born > datetime.now(PH_TZ).date():
        return 'Date of birth cannot be in the future.'
    if born.year < 1900:
        return 'Please enter a valid date of birth.'
    return None


def _has_missed_medicine(elder_id):
    try:
        docs = (db.collection('medication_reminder')
                .where('elder_id', '==', elder_id).stream())
        for doc in docs:
            med = doc.to_dict() or {}
            if med.get('deleted_at') or med.get('is_active') is False:
                continue
            if str(med.get('status') or '').lower() in MISSED_MED_STATUSES:
                return True
    except Exception as e:
        print(f'[loved-ones] could not read reminders for {elder_id}: {e}')
    return False


def _loved_one_snapshot(elder_id, elder_data, link_data, has_open_alert):
    """Everything a loved-one card or the details modal shows, from Firestore."""
    name = elder_data.get('full_name') or 'Unnamed'
    serial, device = _device_for_elder(elder_id)
    loc = _latest_device_location(serial)
    summary = _device_summary(serial, device, loc)
    last_seen = _device_last_seen(device, loc)
    reminder = _next_medicine(elder_id)

    if has_open_alert:
        status_class, badge_class = 'attention', 'emergency'
        status_label, status_icon = 'Emergency', 'siren'
    elif summary['status_class'] == 'online':
        status_class = badge_class = 'online'
        status_label, status_icon = 'Online', 'check-circle-2'
    elif summary['status_class'] == 'offline':
        status_class = badge_class = 'offline'
        status_label, status_icon = 'Offline', 'circle'
    else:  # no device / not registered
        status_class = badge_class = 'attention'
        status_label, status_icon = summary['status'], 'triangle-alert'

    # Location: real report first, TEMP_LOCATION until the device sends one.
    location_text = TEMP_LOCATION['address']
    if loc and (loc.get('location_address') or
                isinstance(loc.get('gps_lat'), (int, float))):
        location_text = loc.get('location_address') or (
            f"{loc['gps_lat']:.5f}, {loc['gps_long']:.5f}"
            if isinstance(loc.get('gps_long'), (int, float))
            else 'Address not reported')

    if reminder['date'] == '—':
        next_medicine_text = 'No reminders set'
    else:
        next_medicine_text = f"{reminder['medicine_name']} — {reminder['time']}"

    return {
        'id': elder_id,
        'name': name,
        'initials': _initials(name),
        'photo_url': elder_data.get('photo_url') or None,
        'relationship': link_data.get('relationship') or 'Family',
        'date_of_birth': elder_data.get('date_of_birth'),
        'address': elder_data.get('address'),

        'status_class': status_class,
        'badge_class': badge_class,
        'status_label': status_label,
        'status_icon': status_icon,

        'device_id': serial,
        'device_status': summary['status'],
        'device_note': summary['note'],

        'last_checkin': (_friendly_time(last_seen) if last_seen
                         else TEMP_LOCATION['last_updated']),
        'checkin_overdue': bool(serial) and summary['status_class'] != 'online',
        'next_medicine': next_medicine_text,
        'medicine_overdue': _has_missed_medicine(elder_id),
        'location': location_text,
    }


@app.route('/my-loved-ones')
def my_loved_ones():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    links = (db.collection('family_elder_link')
             .where('family_user_id', '==', session['user_id']).stream())

    elders = {}
    for link in links:
        link_data = link.to_dict() or {}
        elder_id = link_data.get('elder_id')
        if not elder_id or elder_id in elders:
            continue
        elder_doc = db.collection('elder_profile').document(elder_id).get()
        if not elder_doc.exists:
            continue
        elder_data = elder_doc.to_dict() or {}
        if elder_data.get('deleted_at'):
            continue
        elders[elder_id] = (elder_data, link_data)

    names = {i: {'name': d.get('full_name') or 'Unnamed'}
             for i, (d, _) in elders.items()}
    open_alerts = [a for a in _alerts_for_elders(names)
                   if _is_open_alert(a['data'])]
    alert_elders = {a['elder_id'] for a in open_alerts}

    loved_ones = [
        _loved_one_snapshot(i, d, l, i in alert_elders)
        for i, (d, l) in elders.items()
    ]
    # Emergencies first, then everyone else by name.
    loved_ones.sort(key=lambda e: (e['badge_class'] != 'emergency',
                                   e['name'].casefold()))

    return render_template(
        'family/my_loved_ones.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(),
        loved_ones=loved_ones,
        online_count=sum(e['status_class'] == 'online' for e in loved_ones),
        offline_count=sum(e['status_class'] == 'offline' for e in loved_ones),
        attention_count=sum(e['status_class'] ==
                            'attention' for e in loved_ones),
        notification_count=len(open_alerts),
        current_year=datetime.now().year,
    )


# ---------- ADD LOVED ONE ----------

@app.route('/loved-ones/add', methods=['POST'])
def add_loved_one():
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    full_name = request.form.get('full_name', '').strip()
    relationship = request.form.get('relationship', '').strip()
    relationship_other = request.form.get('relationship_other', '').strip()
    dob = request.form.get('date_of_birth', '').strip()
    house_no = request.form.get('house_no', '').strip()
    street = request.form.get('street', '').strip()
    barangay = request.form.get('barangay', '').strip()
    city = request.form.get('city', '').strip()
    province = request.form.get('province', '').strip()
    zip_code = request.form.get('zip_code', '').strip()
    device_id = _clean_device_id(request.form.get('device_id'))

    if not full_name:
        return jsonify(success=False, field='full_name',
                       message='Enter the full name of your loved one.'), 400
    if not relationship:
        return jsonify(success=False, field='relationship',
                       message='Choose how you are related to them.'), 400
    if relationship == 'Other':
        if not relationship_other:
            return jsonify(success=False, field='relationship_other',
                           message='Please specify your relationship.'), 400
        relationship = relationship_other
    dob_error = _valid_dob(dob)
    if dob_error:
        return jsonify(success=False, field='dob', message=dob_error), 400

    address = ', '.join(filter(
        None, [house_no, street, barangay, city, province, zip_code]))

    try:
        device_data = None
        if device_id:
            device_doc = db.collection('device').document(device_id).get()
            if not device_doc.exists:
                return jsonify(success=False, field='device_id',
                               message='No matching Device ID found in the system.'), 400
            device_data = device_doc.to_dict() or {}
            if device_data.get('is_registered'):
                return jsonify(
                    success=False, field='device_id',
                    message='This Device ID is already linked to another elder. '
                            'Leave it blank if you just want to register the person for now.'
                ), 409

        elder_ref = db.collection('elder_profile').document()
        elder_ref.set({
            'full_name': full_name,
            'date_of_birth': dob or None,
            'address': address,
            'photo_url': None,
            'created_at': firestore.SERVER_TIMESTAMP,
            'created_by': session['user_id'],
            'deleted_at': None,
            'is_archived': 0,
        })
        elder_id = elder_ref.id

        db.collection('family_elder_link').add({
            'family_user_id': session['user_id'],
            'elder_id': elder_id,
            'relationship': relationship,
            'created_at': firestore.SERVER_TIMESTAMP,
        })

        if device_id:
            _attach_device(device_id, device_data, elder_id)

        _log_activity(
            elder_id, 'device',
            title=f'{full_name} was added to the account',
            detail=f'Device {device_id} linked' if device_id else 'No device linked yet',
            badge='Linked' if device_id else 'Pending',
            badge_class='success' if device_id else 'warning',
            location_address=address,
            device_id=device_id or None,
            actor_user_id=session['user_id'],
        )

        return jsonify(success=True, message=f'{full_name} has been added to your loved ones.')

    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500


def _attach_device(serial, device_data, elder_id):
    """Mark a device as registered to this elder (keeps what the device wrote)."""
    device_data = device_data or {}
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


# ---------- LOVED ONE DETAILS (view modal) ----------

def _get_elder_if_linked(elder_id):
    links = list(
        db.collection('family_elder_link')
        .where('family_user_id', '==', session['user_id'])
        .where('elder_id', '==', elder_id)
        .limit(1)
        .stream()
    )
    if not links:
        return None, None

    elder_doc = db.collection('elder_profile').document(elder_id).get()
    if not elder_doc.exists or (elder_doc.to_dict() or {}).get('deleted_at'):
        return None, None

    return elder_doc, links[0].to_dict() or {}


@app.route('/loved-ones/<elder_id>/details')
def loved_one_details_json(elder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_doc, link_data = _get_elder_if_linked(elder_id)
    if not elder_doc:
        return jsonify(success=False, message='Loved one not found.'), 404

    elder_data = elder_doc.to_dict() or {}
    alerts = _alerts_for_elders(
        {elder_id: {'name': elder_data.get('full_name')}})
    has_open_alert = any(_is_open_alert(a['data']) for a in alerts)

    elder = _loved_one_snapshot(
        elder_id, elder_data, link_data, has_open_alert)
    return jsonify(success=True, elder=elder)


@app.route('/loved-ones/<elder_id>/link-device', methods=['POST'])
def loved_one_link_device(elder_id):
    """Link a device to a loved one that was added without one."""
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_doc, _ = _get_elder_if_linked(elder_id)
    if not elder_doc:
        return jsonify(success=False, message='Loved one not found.'), 404

    data = request.get_json(silent=True) or {}
    serial = _clean_device_id(data.get('device_id'))
    if not serial:
        return jsonify(success=False, message='Enter the Device ID.'), 400

    existing_serial, _ = _device_for_elder(elder_id)
    if existing_serial:
        return jsonify(success=False,
                       message=f'This loved one already has device {existing_serial}.'), 409

    try:
        device_doc = db.collection('device').document(serial).get()
        if not device_doc.exists:
            return jsonify(success=False,
                           message='No matching Device ID found in the system.'), 400
        device_data = device_doc.to_dict() or {}
        if device_data.get('is_registered'):
            return jsonify(success=False,
                           message='This Device ID is already linked to another elder.'), 409

        _attach_device(serial, device_data, elder_id)
        name = (elder_doc.to_dict() or {}).get('full_name') or 'Loved one'
        _log_activity(
            elder_id, 'device',
            title=f'Device linked to {name}',
            detail=f'Device {serial} linked',
            badge='Linked', badge_class='success',
            device_id=serial, actor_user_id=session['user_id'],
        )
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, device_id=serial,
                   message=f'Device {serial} is now linked.')


@app.route('/loved-ones/<elder_id>/photo', methods=['POST'])
def loved_one_upload_photo(elder_id):
    """Saves the photo in Firestore (as a data URL in photo_url), not on disk,
    so it shows on every computer and in the mobile app.
    The page shrinks the image before sending it."""
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_doc, _ = _get_elder_if_linked(elder_id)
    if not elder_doc:
        return jsonify(success=False, message='Loved one not found.'), 404

    data_url = (request.form.get('photo_data_url') or '').strip()
    match = re.match(r'^data:(image/[a-z]+);base64,(.+)$', data_url, re.S)
    if not match:
        return jsonify(success=False, message='No photo was uploaded.'), 400

    mime, encoded = match.group(1), match.group(2)
    if mime not in LOVED_ONE_PHOTO_TYPES:
        return jsonify(success=False, message='Photo must be a JPG, PNG, or WEBP image.'), 400
    if len(encoded) > LOVED_ONE_PHOTO_MAX_BYTES:
        return jsonify(success=False,
                       message='That photo is too large. Please choose a smaller one.'), 400
    try:
        base64.b64decode(encoded, validate=True)
    except Exception:
        return jsonify(success=False, message='That photo could not be read.'), 400

    photo_url = f'data:{mime};base64,{encoded}'
    db.collection('elder_profile').document(elder_id).update({
        'photo_url': photo_url,
        'photo_updated_at': firestore.SERVER_TIMESTAMP,
    })
    return jsonify(success=True, photo_url=photo_url, message='Photo updated.')


@app.route('/loved-ones/<elder_id>/delete', methods=['POST'])
def loved_one_delete(elder_id):
    """Removes this loved one from YOUR account only.

    Other family members linked to the same elder keep access. The elder
    profile is soft-deleted and the device freed only when nobody is left.
    """
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_doc, _ = _get_elder_if_linked(elder_id)
    if not elder_doc:
        return jsonify(success=False, message='Loved one not found.'), 404

    name = (elder_doc.to_dict() or {}).get('full_name') or 'Loved one'

    try:
        all_links = list(db.collection('family_elder_link')
                         .where('elder_id', '==', elder_id).stream())
        mine = [l for l in all_links
                if (l.to_dict() or {}).get('family_user_id') == session['user_id']]
        mine_ids = {l.id for l in mine}
        others = [l for l in all_links if l.id not in mine_ids]

        for link in mine:
            link.reference.delete()

        if others:
            _log_activity(
                elder_id, 'device',
                title=f'A family member was removed from {name}',
                badge='Removed', badge_class='warning',
                actor_user_id=session['user_id'],
            )
            return jsonify(success=True,
                           message=f'{name} was removed from your account. '
                           'Other linked family members still have access.')

        # Nobody else is linked: retire the profile and free the device.
        db.collection('elder_profile').document(elder_id).update({
            'deleted_at': firestore.SERVER_TIMESTAMP,
            'deleted_by': session['user_id'],
        })
        for d in db.collection('device').where('elder_id', '==', elder_id).stream():
            d.reference.update({
                'elder_id': None,
                'is_registered': False,
                'registered_at': None,
                'previous_elder_id': elder_id,
                'unregistered_at': firestore.SERVER_TIMESTAMP,
                'unregistered_by': session['user_id'],
            })
        _log_activity(
            elder_id, 'device',
            title=f'{name} was deleted',
            detail='Device unlinked',
            badge='Deleted', badge_class='danger',
            actor_user_id=session['user_id'],
        )
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message=f'{name} was deleted.')


# ---------- loved one update ----------
@app.route('/loved-ones/<elder_id>/update', methods=['POST'])
def loved_one_update(elder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_doc, _ = _get_elder_if_linked(elder_id)
    if not elder_doc:
        return jsonify(success=False, message='Loved one not found.'), 404

    data = request.get_json(silent=True) or {}
    full_name = (data.get('full_name') or '').strip()
    relationship = (data.get('relationship') or '').strip()
    dob = (data.get('date_of_birth') or '').strip()
    address = (data.get('address') or '').strip()

    if not full_name:
        return jsonify(success=False, message='Full name cannot be empty.'), 400
    if not relationship:
        return jsonify(success=False, message='Please choose a relationship.'), 400
    dob_error = _valid_dob(dob)
    if dob_error:
        return jsonify(success=False, message=dob_error), 400

    try:
        db.collection('elder_profile').document(elder_id).update({
            'full_name': full_name,
            'date_of_birth': dob or None,
            'address': address,
            'updated_at': firestore.SERVER_TIMESTAMP,
        })

        links = list(
            db.collection('family_elder_link')
            .where('family_user_id', '==', session['user_id'])
            .where('elder_id', '==', elder_id)
            .limit(1)
            .stream()
        )
        if links:
            links[0].reference.update({'relationship': relationship})

        _log_activity(
            elder_id, 'device',
            title=f'{full_name} profile updated',
            badge='Updated', badge_class='warning',
            location_address=address,
            actor_user_id=session['user_id'],
        )

        return jsonify(success=True, message='Changes saved.')
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500


# ---------- ALERTS ----------
#
# alert/{id}  (written by the device through /api/device/alert)
#   elder_id, device_id, trigger_type ('VOICE'|'BUTTON'), phrase_used,
#   confidence_score, latitude, longitude, location_address,
#   status ('PENDING'|'SENT'|'CANCELLED'|'ACKNOWLEDGED'|'RESOLVED'),
#   created_at, cancel_reason, acknowledged_at/by, resolved_at/by
# emergency_status_history/{id}: alert_id, status, changed_by_user_id, notes, changed_at

ALERT_STATUS_LABELS = {
    'PENDING': ('Pending', 'status-pending'),
    'SENT': ('Alert Sent', 'status-alert-sent'),
    'ACKNOWLEDGED': ('Responded', 'status-responded'),
    'RESPONDED': ('Responded', 'status-responded'),
    'RESOLVED': ('Resolved', 'status-resolved'),
    'CANCELLED': ('Cancelled', 'status-cancelled'),
}
ALERT_ACTIONS = {'acknowledge': 'ACKNOWLEDGED', 'resolve': 'RESOLVED'}


def _alert_coords(alert):
    lat = alert.get('latitude', alert.get('gps_lat'))
    lng = alert.get('longitude', alert.get('gps_long'))
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return lat, lng
    return None, None


def _sms_sent_count(alert_id, alert):
    if isinstance(alert.get('sms_sent_count'), int):
        return alert['sms_sent_count']
    try:
        return sum(1 for d in db.collection('sms_delivery_log')
                   .where('alert_id', '==', alert_id).stream()
                   if (d.to_dict() or {}).get('delivery_status') == 'SENT')
    except Exception:
        return 0


def _alert_card(row, elder):
    """One alert shaped for templates/family/alerts.html."""
    alert = row['data']
    status = str(alert.get('status') or 'PENDING').upper()
    label, status_class = ALERT_STATUS_LABELS.get(
        status, (status.title(), 'status-pending'))
    lat, lng = _alert_coords(alert)
    created = _to_ph(alert.get('created_at'))
    trigger = str(alert.get('trigger_type') or 'VOICE').upper()

    return {
        'id': row['id'],
        'short_id': row['id'][:8].upper(),
        'type': 'false-alarm' if status == 'CANCELLED' else 'emergency',
        'is_open': _is_open_alert(alert),
        'elder_name': elder['name'],
        'elder_photo_url': elder.get('photo_url'),
        'relationship': elder.get('relationship') or 'Family',
        'trigger_label': 'BUTTON PRESS' if trigger == 'BUTTON' else 'VOICE TRIGGER',
        'trigger_icon': 'circle-dot' if trigger == 'BUTTON' else 'mic',
        'trigger_phrase': alert.get('phrase_used') or alert.get('phrase'),
        'timestamp': _friendly_time(alert.get('created_at')),
        'created_iso': created.isoformat() if created else '',
        'location': (alert.get('location_address')
                     or (f'{lat:.5f}, {lng:.5f}' if lat is not None else 'Location not recorded')),
        'gps_verified': lat is not None,
        'latitude': lat,
        'longitude': lng,
        'sms_sent_count': _sms_sent_count(row['id'], alert),
        'status': label,
        'status_class': status_class,
        'reason': alert.get('cancel_reason') or 'Cancelled within 5 seconds',
    }


def _family_alert_cards():
    elders = _linked_elders_for_dashboard()
    rows = _alerts_for_elders(elders)
    return [_alert_card(r, elders[r['elder_id']]) for r in rows]


def _get_linked_alert(alert_id):
    """(doc_ref, alert dict) if the alert belongs to one of this user's elders."""
    ref = db.collection(ALERT_COLLECTION).document(alert_id)
    doc = ref.get()
    if not doc.exists:
        return None, None
    alert = doc.to_dict() or {}
    elder_doc, _ = _get_elder_if_linked(alert.get('elder_id'))
    if not elder_doc:
        return None, None
    return ref, alert


@app.route('/alerts')
def alerts():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    cards = _family_alert_cards()
    return render_template(
        'family/alerts.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(), alerts=cards,
        emergency_count=sum(c['type'] == 'emergency' for c in cards),
        false_alarm_count=sum(c['type'] == 'false-alarm' for c in cards),
        notification_count=sum(c['is_open'] for c in cards),
        current_year=datetime.now().year,
    )


@app.route('/alerts/<alert_id>')
def alert_details(alert_id):
    """Old link target — the page now opens details in a modal."""
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    return redirect(url_for('alerts', open=alert_id))


@app.route('/alerts/<alert_id>/details')
def alert_details_json(alert_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    ref, alert = _get_linked_alert(alert_id)
    if not ref:
        return jsonify(success=False, message='Alert not found.'), 404

    elders = _linked_elders_for_dashboard()
    elder = elders.get(alert.get('elder_id'), {'name': 'Unknown'})
    card = _alert_card({'id': alert_id, 'data': alert}, elder)
    card['device_id'] = alert.get('device_id') or '—'
    card['confidence'] = alert.get('confidence_score')

    history = []
    try:
        for d in (db.collection('emergency_status_history')
                  .where('alert_id', '==', alert_id).stream()):
            h = d.to_dict() or {}
            who = 'Device'
            if h.get('changed_by_user_id'):
                u = db.collection('user_account').document(
                    h['changed_by_user_id']).get()
                who = (u.to_dict() or {}).get('full_name',
                                              'Unknown') if u.exists else 'Unknown'
            changed = _to_ph(h.get('changed_at'))
            history.append({
                'status': ALERT_STATUS_LABELS.get(str(h.get('status')).upper(), (h.get('status'),))[0],
                'by': who,
                'notes': h.get('notes') or '',
                'when': _friendly_time(h.get('changed_at')),
                'sort_key': changed.timestamp() if changed else 0,
            })
    except Exception as e:
        print(f'[alerts] could not read history for {alert_id}: {e}')
    history.sort(key=lambda h: h['sort_key'])
    card['history'] = history

    return jsonify(success=True, alert=card)


@app.route('/alerts/<alert_id>/respond', methods=['POST'])
def alert_respond(alert_id):
    """Family member marks an alert as responded or resolved."""
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    data = request.get_json(silent=True) or {}
    new_status = ALERT_ACTIONS.get(data.get('action'))
    notes = (data.get('notes') or '').strip()[:500]
    if not new_status:
        return jsonify(success=False, message='Invalid action.'), 400

    ref, alert = _get_linked_alert(alert_id)
    if not ref:
        return jsonify(success=False, message='Alert not found.'), 404

    current = str(alert.get('status') or '').upper()
    if current in {'RESOLVED', 'CANCELLED'}:
        return jsonify(success=False, message='This alert is already closed.'), 409
    if current in {'ACKNOWLEDGED', 'RESPONDED'} and new_status == 'ACKNOWLEDGED':
        return jsonify(success=False, message='Someone already responded to this alert.'), 409

    stamp = 'acknowledged' if new_status == 'ACKNOWLEDGED' else 'resolved'
    try:
        ref.update({
            'status': new_status,
            f'{stamp}_at': firestore.SERVER_TIMESTAMP,
            f'{stamp}_by': session['user_id'],
        })
        db.collection('emergency_status_history').add({
            'alert_id': alert_id,
            'status': new_status,
            'changed_by_user_id': session['user_id'],
            'notes': notes,
            'changed_at': firestore.SERVER_TIMESTAMP,
        })
        _log_activity(
            alert.get('elder_id'), 'alert',
            title='Emergency alert ' +
            ('responded to' if stamp == 'acknowledged' else 'resolved'),
            detail=notes,
            badge='Responded' if stamp == 'acknowledged' else 'Resolved',
            badge_class='warning' if stamp == 'acknowledged' else 'success',
            location_address=alert.get('location_address') or '',
            device_id=alert.get('device_id'),
            actor_user_id=session['user_id'],
            ref_id=alert_id,
        )
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message='Alert updated.')


@app.route('/alerts/export')
def export_alerts():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Alert ID', 'Date/Time', 'Type', 'Loved One', 'Relationship',
                     'Trigger', 'Phrase', 'Status', 'Location', 'Latitude',
                     'Longitude', 'SMS Sent'])
    for c in _family_alert_cards():
        writer.writerow([
            c['short_id'], c['timestamp'],
            'Emergency' if c['type'] == 'emergency' else 'False Alarm',
            c['elder_name'], c['relationship'], c['trigger_label'],
            c['trigger_phrase'] or '', c['status'], c['location'],
            c['latitude'] or '', c['longitude'] or '', c['sms_sent_count'],
        ])

    filename = f"alisto-alerts-{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(buffer.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


# ---------- DEVICE API (Raspberry Pi -> Flask) ----------
#
# Set the key before running:   $env:ALISTO_DEVICE_KEY = "some-long-secret"
# The device sends it in the X-Device-Key header.

def _device_key_ok():
    expected = os.environ.get('ALISTO_DEVICE_KEY')
    return bool(expected) and request.headers.get('X-Device-Key') == expected


@app.route('/api/device/alert', methods=['POST'])
def device_create_alert():
    if not _device_key_ok():
        return jsonify(success=False, message='Unauthorized device.'), 401

    data = request.get_json(silent=True) or {}
    serial = _clean_device_id(data.get('serial_number'))
    status = str(data.get('status') or 'SENT').upper()
    trigger = str(data.get('trigger_type') or 'VOICE').upper()
    if status not in {'PENDING', 'SENT', 'CANCELLED'}:
        return jsonify(success=False, message='Invalid status.'), 400
    if trigger not in {'VOICE', 'BUTTON'}:
        return jsonify(success=False, message='Invalid trigger_type.'), 400

    device_doc = db.collection('device').document(
        serial).get() if serial else None
    device = (device_doc.to_dict() or {}
              ) if device_doc and device_doc.exists else None
    if not device or not device.get('is_registered') or not device.get('elder_id'):
        return jsonify(success=False, message='Device is not registered to an elder.'), 404

    elder_id = device['elder_id']
    lat, lng = data.get('latitude'), data.get('longitude')
    has_coords = isinstance(
        lat, (int, float)) and isinstance(lng, (int, float))
    address = (data.get('location_address') or '').strip()
    phrase = (data.get('phrase_used') or '').strip()

    alert_ref = db.collection(ALERT_COLLECTION).document()
    alert_ref.set({
        'elder_id': elder_id,
        'device_id': serial,
        'trigger_type': trigger,
        'phrase_used': phrase or None,
        'confidence_score': data.get('confidence_score'),
        'latitude': lat if has_coords else None,
        'longitude': lng if has_coords else None,
        'location_address': address or None,
        'status': status,
        'title': 'Emergency Alert',
        'cancel_reason': data.get('cancel_reason') if status == 'CANCELLED' else None,
        'created_at': firestore.SERVER_TIMESTAMP,
    })
    db.collection('emergency_status_history').add({
        'alert_id': alert_ref.id, 'status': status,
        'changed_by_user_id': None, 'notes': 'Reported by device',
        'changed_at': firestore.SERVER_TIMESTAMP,
    })

    # The alert also counts as a check-in (and a location report if it has GPS).
    db.collection('device').document(serial).update(
        {'last_seen': firestore.SERVER_TIMESTAMP})
    if has_coords:
        db.collection('DEVICE_LOCATION').add({
            'device_id': serial, 'gps_lat': lat, 'gps_long': lng,
            'location_address': address or None,
            'recorded_at': firestore.SERVER_TIMESTAMP,
        })

    _log_activity(
        elder_id, 'alert',
        title='False alarm cancelled' if status == 'CANCELLED' else 'Emergency alert triggered',
        detail=f'Detected phrase: {phrase}' if phrase else f'{trigger.title()} trigger',
        badge='Cancelled' if status == 'CANCELLED' else 'Pending',
        badge_class='warning' if status == 'CANCELLED' else 'danger',
        location_address=address, device_id=serial,
        ref_id=alert_ref.id,
    )
    return jsonify(
        success=True, alert_id=alert_ref.id,
        notify=[] if status == 'CANCELLED' else _notify_list(
            elder_id, 'emergency_alerts'),
    ), 201


@app.route('/api/device/alert/<alert_id>/cancel', methods=['POST'])
def device_cancel_alert(alert_id):
    """The elder cancelled within the 5-second window."""
    if not _device_key_ok():
        return jsonify(success=False, message='Unauthorized device.'), 401

    data = request.get_json(silent=True) or {}
    ref = db.collection(ALERT_COLLECTION).document(alert_id)
    doc = ref.get()
    if not doc.exists:
        return jsonify(success=False, message='Alert not found.'), 404
    alert = doc.to_dict() or {}
    if _clean_device_id(data.get('serial_number')) != alert.get('device_id'):
        return jsonify(success=False, message='Alert belongs to another device.'), 403
    if str(alert.get('status')).upper() not in {'PENDING', 'SENT'}:
        return jsonify(success=False, message='Alert can no longer be cancelled.'), 409

    ref.update({'status': 'CANCELLED',
                'cancel_reason': data.get('cancel_reason') or 'Cancelled within 5 seconds',
                'cancelled_at': firestore.SERVER_TIMESTAMP})
    db.collection('emergency_status_history').add({
        'alert_id': alert_id, 'status': 'CANCELLED',
        'changed_by_user_id': None, 'notes': 'Cancelled on device',
        'changed_at': firestore.SERVER_TIMESTAMP,
    })
    _log_activity(alert.get('elder_id'), 'alert', title='False alarm cancelled',
                  badge='Cancelled', badge_class='warning',
                  device_id=alert.get('device_id'), ref_id=alert_id)
    return jsonify(success=True)


@app.route('/api/device/alert/<alert_id>/sms', methods=['POST'])
def device_report_sms(alert_id):
    """Device reports one SMS it sent (or failed to send) for an alert."""
    if not _device_key_ok():
        return jsonify(success=False, message='Unauthorized device.'), 401

    data = request.get_json(silent=True) or {}
    number = (data.get('recipient_number') or '').strip()
    delivery = str(data.get('delivery_status') or '').upper()
    if not number or delivery not in {'SENT', 'FAILED'}:
        return jsonify(success=False, message='Need recipient_number and delivery_status SENT/FAILED.'), 400

    doc = db.collection(ALERT_COLLECTION).document(alert_id).get()
    alert = (doc.to_dict() or {}) if doc.exists else None
    if not alert:
        return jsonify(success=False, message='Alert not found.'), 404
    if _clean_device_id(data.get('serial_number')) != alert.get('device_id'):
        return jsonify(success=False, message='Alert belongs to another device.'), 403

    db.collection('sms_delivery_log').add({
        'alert_id': alert_id,
        'recipient_number': number,
        'delivery_status': delivery,
        'sent_at': firestore.SERVER_TIMESTAMP,
    })
    return jsonify(success=True)

# ---------- DEVICE API: check-in + medication (Raspberry Pi -> Flask) ----------


LOW_BATTERY_PERCENT = 20
DEVICE_MED_STATUSES = {
    'NOTIFIED': ('Notified', 'Reminder played: {name}', 'Sent', 'info'),
    'TAKEN': ('Taken', 'Medicine taken: {name}', 'Taken', 'success'),
    'MISSED': ('Missed', 'Missed medicine: {name}', 'Missed', 'danger'),
}


def _registered_device(serial):
    """(device dict) if the serial is registered to an elder, else None."""
    if not serial:
        return None
    doc = db.collection('device').document(serial).get()
    device = (doc.to_dict() or {}) if doc.exists else None
    if not device or not device.get('is_registered') or not device.get('elder_id'):
        return None
    return device


@app.route('/api/device/checkin', methods=['POST'])
def device_checkin():
    """Heartbeat: location, battery, signal. Call every few minutes."""
    if not _device_key_ok():
        return jsonify(success=False, message='Unauthorized device.'), 401

    data = request.get_json(silent=True) or {}
    serial = _clean_device_id(data.get('serial_number'))
    device = _registered_device(serial)
    if not device:
        return jsonify(success=False, message='Device is not registered to an elder.'), 404

    elder_id = device['elder_id']
    lat, lng = data.get('latitude'), data.get('longitude')
    has_coords = isinstance(
        lat, (int, float)) and isinstance(lng, (int, float))
    address = (data.get('location_address') or '').strip()
    battery = data.get('battery_level')
    battery = battery if isinstance(
        battery, (int, float)) and 0 <= battery <= 100 else None

    # Was it offline before this check-in?
    last_seen = _to_ph(device.get('last_seen'))
    was_offline = not last_seen or datetime.now(
        PH_TZ) - last_seen >= DEVICE_ONLINE_WINDOW
    previous_battery = device.get('battery_level')

    updates = {'last_seen': firestore.SERVER_TIMESTAMP}
    if battery is not None:
        updates['battery_level'] = battery
    for key in ('signal_strength', 'firmware_version'):
        if data.get(key) is not None:
            updates[key] = data[key]
    if has_coords:
        updates['gps'] = 'Active'
    db.collection('device').document(serial).update(updates)

    if has_coords:
        db.collection('DEVICE_LOCATION').add({
            'device_id': serial, 'gps_lat': lat, 'gps_long': lng,
            'location_address': address or None,
            'recorded_at': firestore.SERVER_TIMESTAMP,
        })
    if battery is not None or data.get('signal_strength') is not None:
        db.collection('device_health_log').add({
            'device_id': serial,
            'battery_level': battery,
            'signal_strength': data.get('signal_strength'),
            'firmware_version': data.get('firmware_version'),
            'logged_at': firestore.SERVER_TIMESTAMP,
        })

    # History only for changes worth seeing, not every heartbeat.
    device_event = False
    if was_offline:
        device_event = True
        _log_activity(elder_id, 'device', title='Device connected',
                      detail=f'Device {serial} is online',
                      badge='Online', badge_class='success',
                      location_address=address, device_id=serial)
    if (battery is not None and battery <= LOW_BATTERY_PERCENT
            and not (isinstance(previous_battery, (int, float))
                     and previous_battery <= LOW_BATTERY_PERCENT)):
        device_event = True
        _log_activity(elder_id, 'device', title=f'Low battery ({int(battery)}%)',
                      detail='Please charge the ALISTO device',
                      badge='Low Battery', badge_class='warning',
                      location_address=address, device_id=serial)

    return jsonify(success=True,
                   notify=_notify_list(elder_id, 'device_alerts') if device_event else [])


@app.route('/api/device/medications', methods=['GET'])
def device_medications():
    """The reminder schedule for the elder this device belongs to."""
    if not _device_key_ok():
        return jsonify(success=False, message='Unauthorized device.'), 401

    serial = _clean_device_id(request.args.get('serial_number'))
    device = _registered_device(serial)
    if not device:
        return jsonify(success=False, message='Device is not registered to an elder.'), 404

    reminders = []
    for doc in (db.collection('medication_reminder')
                .where('elder_id', '==', device['elder_id']).stream()):
        med = doc.to_dict() or {}
        if med.get('deleted_at') or med.get('is_active') is False:
            continue
        clock = _reminder_clock(_med_time(med))
        reminders.append({
            'id': doc.id,
            'medicine_name': _med_name(med),
            'dosage': med.get('dosage') or '',
            'reminder_time': _med_time(med),
            'time_24h': f'{clock[0]:02d}:{clock[1]:02d}' if clock else None,
            # empty = every day
            'schedule_days': med.get('schedule_days') or [],
            'voice_reminder_enabled': med.get('voice_reminder_enabled', True),
            'status': med.get('status') or 'Upcoming',
        })
    return jsonify(success=True, reminders=reminders)


@app.route('/api/device/medication/<reminder_id>/status', methods=['POST'])
def device_medication_status(reminder_id):
    """Device reports a reminder as Notified, Taken, or Missed."""
    if not _device_key_ok():
        return jsonify(success=False, message='Unauthorized device.'), 401

    data = request.get_json(silent=True) or {}
    serial = _clean_device_id(data.get('serial_number'))
    status_key = str(data.get('status') or '').upper()
    if status_key not in DEVICE_MED_STATUSES:
        return jsonify(success=False, message='Status must be NOTIFIED, TAKEN, or MISSED.'), 400

    device = _registered_device(serial)
    if not device:
        return jsonify(success=False, message='Device is not registered to an elder.'), 404

    ref = db.collection('medication_reminder').document(reminder_id)
    doc = ref.get()
    med = (doc.to_dict() or {}) if doc.exists else None
    if not med or med.get('elder_id') != device['elder_id']:
        return jsonify(success=False, message='Reminder not found for this device.'), 404

    status, title, badge, badge_class = DEVICE_MED_STATUSES[status_key]
    ref.update({
        'status': status,
        'last_status_at': firestore.SERVER_TIMESTAMP,
        'updated_at': firestore.SERVER_TIMESTAMP,
        'updatedAt': firestore.SERVER_TIMESTAMP,
    })
    db.collection('device').document(serial).update(
        {'last_seen': firestore.SERVER_TIMESTAMP})
    _log_activity(device['elder_id'], 'med_reminder',
                  title=title.format(name=_med_name(med)),
                  detail=f'Scheduled for {_med_time(med)}',
                  badge=badge, badge_class=badge_class,
                  device_id=serial, ref_id=reminder_id)
    return jsonify(success=True,
                   notify=_notify_list(
                       device['elder_id'], 'medicine_reminders')
                   if status_key == 'MISSED' else [])

# ---------- MEDICATION REMINDERS ----------
#
# Shared with the Flutter app (medicine_screens.dart), same collection:
#   medication_reminder/{id}
# The app and the web have used two sets of field names, so the web
# READS both and WRITES both — whichever the app reads stays in sync:
#   medicine_name + name          (medicine)
#   reminder_time + time          (free text, e.g. "8:00 AM", "09:00 - 10:00 AM")
#   created_at + createdAt, updated_at + updatedAt
#   elder_id, status ('Upcoming' / 'Notified' ...), is_active
# Web-only extras the app simply ignores:
#   dosage, purpose, frequency, schedule_days, voice_reminder_enabled
# Delete is a hard delete, same as FirestoreService.deleteMedicine().


MED_STATUS_CLASSES = {
    'notified': 'success', 'taken': 'success',
    'missed': 'danger', 'overdue': 'danger',
}
MED_FREQUENCIES = {'Daily', 'Weekly', 'As needed'}


def _med_name(med):
    return (med.get('medicine_name') or med.get('name') or '').strip() or 'Unnamed medicine'


def _med_time(med):
    return (med.get('reminder_time') or med.get('time') or '').strip()


def _display_time(hhmm):
    """'14:30' (from <input type=time>) -> '2:30 PM', the format the app shows."""
    try:
        return datetime.strptime(hhmm, '%H:%M').strftime('%I:%M %p').lstrip('0')
    except ValueError:
        return hhmm


def _time_of_day(clock):
    if not clock:
        return ''
    if clock[0] < 12:
        return 'morning'
    if clock[0] < 18:
        return 'afternoon'
    return 'evening'


def _day_filter(days):
    """'today tomorrow' tags for the filter tabs. No days = every day
    (every reminder made in the app, which has no day picker)."""
    now = datetime.now(PH_TZ)
    today = DASHBOARD_WEEKDAYS[now.weekday()]
    tomorrow = DASHBOARD_WEEKDAYS[(now.weekday() + 1) % 7]
    if not days:
        return 'today tomorrow'
    return ' '.join(tag for tag, day in (('today', today), ('tomorrow', tomorrow))
                    if day in days)


def _linked_elder_list():
    """(sorted list, {id: elder}) of this account's loved ones."""
    lookup = {i: {**e, 'id': i}
              for i, e in _linked_elders_for_dashboard().items()}
    elders = sorted(lookup.values(), key=lambda e: e['name'].casefold())
    return elders, lookup


@app.route('/medication-reminders')
def medication_reminders():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    elders, lookup = _linked_elder_list()

    schedules = []
    for elder_id, elder in lookup.items():
        try:
            docs = list(db.collection('medication_reminder')
                        .where('elder_id', '==', elder_id).stream())
        except Exception as e:
            print(f'[meds] could not read reminders for {elder_id}: {e}')
            docs = []

        for doc in docs:
            med = doc.to_dict() or {}
            if med.get('deleted_at') or med.get('is_active') is False:
                continue

            reminder_time = _med_time(med)
            clock = _reminder_clock(reminder_time)
            days = [d for d in (med.get('schedule_days') or [])
                    if d in DASHBOARD_WEEKDAYS]
            status = med.get('status') or 'Upcoming'

            schedules.append({
                'id': doc.id,
                'elder_id': elder_id,
                'elder_name': elder['name'],
                'elder_photo_url': elder['photo_url'],
                'relationship': elder['relationship'],

                'medicine_name': _med_name(med),
                # blank for app-made reminders
                'dosage': med.get('dosage') or '',
                'purpose': med.get('purpose') or 'general use',
                'frequency': med.get('frequency') or 'Daily',
                'voice_reminder_enabled': med.get('voice_reminder_enabled', True),

                'schedule_time': reminder_time or 'No time set',
                'schedule_day': ', '.join(days) if days else 'Every day',
                # <input type="time"> only accepts 24-hour HH:MM
                'time_value': f'{clock[0]:02d}:{clock[1]:02d}' if clock else '',
                'days_csv': ','.join(days),

                'time_of_day': _time_of_day(clock),
                'day_filter': _day_filter(days),

                'status': status,
                'status_class': MED_STATUS_CLASSES.get(status.lower(), ''),
                'sort_key': (clock is None, clock or (0, 0), _med_name(med).casefold()),
            })

    schedules.sort(key=lambda s: s['sort_key'])
    open_alerts = [a for a in _alerts_for_elders(
        lookup) if _is_open_alert(a['data'])]

    return render_template(
        'family/medication_reminders.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(),
        schedules=schedules, elders=elders,
        today_count=sum('today' in s['day_filter'] for s in schedules),
        tomorrow_count=sum('tomorrow' in s['day_filter'] for s in schedules),
        total_schedule_count=len(schedules),
        notification_count=len(open_alerts),
        current_year=datetime.now().year,
    )


def _reminder_form(lookup):
    """Reads and validates the add/edit form. Returns (fields, error)."""
    elder_id = request.form.get('elder_id', '').strip()
    medicine_name = request.form.get('medicine_name', '').strip()
    time_input = request.form.get('reminder_time', '').strip()

    if not elder_id:
        return None, 'Choose which loved one this reminder is for.'
    if elder_id not in lookup:
        return None, 'That loved one is not linked to your account.'
    if not medicine_name:
        return None, 'Enter the medicine name.'
    if len(medicine_name) > 100:
        return None, 'Medicine name is too long.'
    if not time_input or not _reminder_clock(time_input):
        return None, 'Set the time this reminder should go off.'

    frequency = request.form.get('frequency', 'Daily').strip() or 'Daily'
    if frequency not in MED_FREQUENCIES:
        frequency = 'Daily'
    days = [d for d in request.form.getlist(
        'schedule_days') if d in DASHBOARD_WEEKDAYS]
    if frequency == 'Weekly' and not days:
        return None, 'Pick at least one day for a weekly reminder.'

    reminder_time = _display_time(time_input)
    return {
        'elder_id': elder_id,
        'medicine_name': medicine_name,
        'name': medicine_name,            # app field name
        'reminder_time': reminder_time,
        'time': reminder_time,            # app field name
        'dosage': request.form.get('dosage', '').strip()[:50],
        'purpose': request.form.get('purpose', '').strip()[:100],
        'frequency': frequency,
        'schedule_days': days,
        'voice_reminder_enabled': request.form.get('voice_reminder_enabled') == 'on',
    }, None


def _get_linked_reminder(reminder_id, lookup):
    """(ref, data) if the reminder exists and belongs to a linked elder."""
    ref = db.collection('medication_reminder').document(reminder_id)
    doc = ref.get()
    if not doc.exists:
        return None, None, 'That reminder no longer exists.', 404
    data = doc.to_dict() or {}
    if data.get('elder_id') not in lookup:
        return None, None, 'You cannot change that reminder.', 403
    return ref, data, None, None


@app.route('/medication-reminders/add', methods=['POST'])
def add_medication_reminder():
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    _, lookup = _linked_elder_list()
    fields, error = _reminder_form(lookup)
    if error:
        return jsonify(success=False, message=error), 400

    try:
        db.collection('medication_reminder').add({
            **fields,
            'status': 'Upcoming',
            'is_active': True,
            'created_by': session['user_id'],
            'created_at': firestore.SERVER_TIMESTAMP,
            'createdAt': firestore.SERVER_TIMESTAMP,   # the app may sort by this
            'updated_at': firestore.SERVER_TIMESTAMP,
            'updatedAt': firestore.SERVER_TIMESTAMP,
        })
        _log_activity(
            fields['elder_id'], 'med_reminder',
            title=f"Reminder added: {fields['medicine_name']}",
            detail=f"Set for {fields['reminder_time']}",
            badge='Added', badge_class='success',
            actor_user_id=session['user_id'],
        )
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message='Reminder saved.')


@app.route('/medication-reminders/<reminder_id>/update', methods=['POST'])
def update_medication_reminder(reminder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    _, lookup = _linked_elder_list()
    ref, existing, error, code = _get_linked_reminder(reminder_id, lookup)
    if error:
        return jsonify(success=False, message=error), code

    fields, error = _reminder_form(lookup)
    if error:
        return jsonify(success=False, message=error), 400

    updates = {**fields,
               'updated_at': firestore.SERVER_TIMESTAMP,
               'updatedAt': firestore.SERVER_TIMESTAMP}
    # A new time means the device should remind again.
    if fields['reminder_time'] != _med_time(existing):
        updates['status'] = 'Upcoming'
    # Old reminders made before both names were written: give the app its sort field.
    if 'createdAt' not in existing:
        updates['createdAt'] = existing.get(
            'created_at') or firestore.SERVER_TIMESTAMP

    try:
        ref.update(updates)
        _log_activity(
            fields['elder_id'], 'med_reminder',
            title=f"Reminder updated: {fields['medicine_name']}",
            detail=f"Now set for {fields['reminder_time']}",
            badge='Updated', badge_class='warning',
            actor_user_id=session['user_id'],
        )
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message='Changes saved.')


@app.route('/medication-reminders/<reminder_id>/delete', methods=['POST'])
def delete_medication_reminder(reminder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    _, lookup = _linked_elder_list()
    ref, existing, error, code = _get_linked_reminder(reminder_id, lookup)
    if error:
        return jsonify(success=False, message=error), code

    try:
        ref.delete()   # hard delete, same as the app
        _log_activity(
            existing.get('elder_id'), 'med_reminder',
            title=f'Reminder removed: {_med_name(existing)}',
            badge='Deleted', badge_class='danger',
            actor_user_id=session['user_id'],
        )
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message='Reminder deleted.')

# ---------- HISTORY ----------
#
# activity_log/{id}
#   elder_id      -- the anchor every role scopes on
#   type          -- 'alert' | 'med_reminder' | 'device'
#   title, detail -- what to show in the row
#   badge, badge_class
#   location_address, device_id
#   actor_user_id -- who did it (None = the device)
#   ref_id        -- alert id / reminder id the row is about (optional)
#   created_at    -- REQUIRED, this is the sort key
#
# Family sees elders they are linked to, a BHW sees elders in their
# barangay, an admin sees everything — all through _activity_feed().


ACTIVITY_PAGE_SIZE = 20

ACTIVITY_TYPES = {
    'alert': {
        'label': 'Emergency Alert',
        'icon': 'triangle-alert',
        'class': 'alert',
        'person_icon': 'user-round',
    },
    'med_reminder': {
        'label': 'Med Reminder',
        'icon': 'pill',
        'class': 'med',
        'person_icon': 'user-round',
    },
    'device': {
        'label': 'Device Activity',
        'icon': 'smartphone',
        'class': 'device',
        'person_icon': 'smartphone',
    },
}

HISTORY_RANGES = {
    'all': ('All Time', None),
    'today': ('Today', 0),
    'week': ('Last 7 Days', 6),
    'month': ('Last 30 Days', 29),
}


def _log_activity(elder_id, activity_type, title, detail='',
                  badge='', badge_class='', location_address='',
                  device_id=None, actor_user_id=None, ref_id=None):
    """Append one row to the shared history.

    Deliberately swallows its own errors: a history write failing should
    never break the action the user actually asked for.
    """
    if not elder_id or activity_type not in ACTIVITY_TYPES:
        return

    try:
        db.collection('activity_log').add({
            'elder_id': elder_id,
            'type': activity_type,
            'title': title,
            'detail': detail or '',
            'badge': badge,
            'badge_class': badge_class,
            'location_address': location_address or '',
            'device_id': device_id,
            'actor_user_id': actor_user_id,
            'ref_id': ref_id,
            'created_at': firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f'[history] could not log activity: {e}')


def _elders_for_role():
    """{elder_id: display info} the signed-in account may see.

    family -> elders linked to this account
    bhw    -> elders whose address is in this BHW's barangay
    admin  -> every elder
    """
    role = session.get('role')

    if role == 'family':
        # Only read the linked profiles, not the whole collection.
        return {
            elder_id: {'name': e['name'], 'photo_url': e['photo_url'],
                       'relationship': e['relationship']}
            for elder_id, e in _linked_elders_for_dashboard().items()
        }

    barangay = None
    if role == 'bhw':
        matches = list(
            db.collection('bhw_profile')
            .where('user_id', '==', session['user_id']).limit(1).stream()
        )
        barangay = (matches[0].to_dict() or {}).get(
            'barangay_assigned') if matches else None
    elif role != 'admin':
        return {}

    elders = {}
    for doc in db.collection('elder_profile').stream():
        elder = doc.to_dict() or {}
        if elder.get('deleted_at'):
            continue
        if role == 'bhw' and not _in_elder_barangay(elder.get('address'), barangay):
            continue
        elders[doc.id] = {
            'name': elder.get('full_name') or 'Unnamed',
            'photo_url': elder.get('photo_url') or None,
            'relationship': 'Elderly',
        }
    return elders


def _in_elder_barangay(address, barangay):
    """Whole-part match so 'Sudlon I' never matches 'Sudlon II'."""
    if not address or not barangay:
        return False
    parts = [p.strip().casefold() for p in address.split(',')]
    return barangay.strip().casefold() in parts


def _activity_row(doc_id, item, elder_id, elder):
    meta = ACTIVITY_TYPES.get(item.get('type')) or ACTIVITY_TYPES['device']
    local = _to_ph(item.get('created_at'))
    return {
        'id': doc_id,
        'elder_id': elder_id,
        'date': local.strftime('%b %d, %Y') if local else '—',
        'time': local.strftime('%I:%M %p').lstrip('0') if local else '',
        'sort_key': local.timestamp() if local else 0,
        'local_time': local,

        'type_filter': item.get('type') if item.get('type') in ACTIVITY_TYPES else 'device',
        'type_label': meta['label'],
        'type_icon': meta['icon'],
        'type_class': meta['class'],

        'person_name': elder['name'],
        'person_photo_url': elder['photo_url'],
        'person_relationship': elder['relationship'],
        'person_icon': meta['person_icon'],

        'detail_main': item.get('title') or '—',
        'detail_sub': item.get('detail') or '',
        'detail_badge': item.get('badge') or '',
        'detail_badge_class': item.get('badge_class') or '',
        'location': item.get('location_address') or '',
    }


def _activity_feed(elders):
    """Every activity row for the given elders, newest first."""
    rows = []
    for elder_id, elder in elders.items():
        try:
            docs = (db.collection('activity_log')
                    .where('elder_id', '==', elder_id).stream())
            for doc in docs:
                rows.append(_activity_row(
                    doc.id, doc.to_dict() or {}, elder_id, elder))
        except Exception as e:
            print(f'[history] could not read activity for {elder_id}: {e}')

    rows.sort(key=lambda r: r['sort_key'], reverse=True)
    return rows


def _rows_in_range(rows, range_key):
    days_back = HISTORY_RANGES[range_key][1]
    if days_back is None:
        return rows
    now = datetime.now(PH_TZ)
    start = now.replace(hour=0, minute=0, second=0,
                        microsecond=0) - timedelta(days=days_back)
    return [r for r in rows if r['local_time'] and r['local_time'] >= start]


def _paginate(rows, page, per_page=ACTIVITY_PAGE_SIZE):
    """Slices rows and builds the page-number list the template renders."""
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    window = rows[start:start + per_page]

    if total_pages <= 7:
        pages = list(range(1, total_pages + 1))
    else:
        pages = [1]
        if page > 3:
            pages.append('...')
        for p in range(max(2, page - 1), min(total_pages, page + 1) + 1):
            pages.append(p)
        if page < total_pages - 2:
            pages.append('...')
        if total_pages not in pages:
            pages.append(total_pages)

    return window, {
        'start': start + 1 if window else 0,
        'end': start + len(window),
        'total': total,
        'current_page': page,
        'total_pages': total_pages,
        'pages': pages,
    }


@app.route('/history')
def history():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    active_type = request.args.get('type', 'all')
    if active_type != 'all' and active_type not in ACTIVITY_TYPES:
        active_type = 'all'
    active_range = request.args.get('range', 'all')
    if active_range not in HISTORY_RANGES:
        active_range = 'all'

    elders = _elders_for_role()
    in_range = _rows_in_range(_activity_feed(elders), active_range)
    shown = in_range if active_type == 'all' else [
        r for r in in_range if r['type_filter'] == active_type]
    activities, pagination = _paginate(shown, page)

    open_alerts = [a for a in _alerts_for_elders(
        elders) if _is_open_alert(a['data'])]

    return render_template(
        'family/history.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(),
        activities=activities,
        alert_count=sum(r['type_filter'] == 'alert' for r in in_range),
        med_reminder_count=sum(
            r['type_filter'] == 'med_reminder' for r in in_range),
        device_activity_count=sum(
            r['type_filter'] == 'device' for r in in_range),
        total_activity_count=len(in_range),
        active_type=active_type, active_range=active_range,
        history_ranges={k: v[0] for k, v in HISTORY_RANGES.items()},
        date_range_label=HISTORY_RANGES[active_range][0],
        notification_count=len(open_alerts),
        pagination=pagination, current_year=datetime.now().year,
    )


@app.route('/history/<activity_id>')
def activity_details(activity_id):
    """Details for one history row (JSON, shown in a modal)."""
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    doc = db.collection('activity_log').document(activity_id).get()
    item = (doc.to_dict() or {}) if doc.exists else None
    elders = _elders_for_role()
    if not item or item.get('elder_id') not in elders:
        return jsonify(success=False, message='Activity not found.'), 404

    row = _activity_row(
        doc.id, item, item['elder_id'], elders[item['elder_id']])
    row.pop('local_time', None)

    actor = 'ALISTO device' if item.get(
        'type') != 'device' or item.get('device_id') else 'System'
    if item.get('actor_user_id'):
        user_doc = db.collection('user_account').document(
            item['actor_user_id']).get()
        actor = ((user_doc.to_dict() or {}).get('full_name')
                 if user_doc.exists else None) or 'Unknown user'
        if item['actor_user_id'] == session['user_id']:
            actor += ' (you)'
    row['actor'] = actor
    row['device_id'] = item.get('device_id') or ''

    # Where the "Open" button in the modal should go.
    ref_id = item.get('ref_id')
    if item.get('type') == 'alert' and ref_id:
        row['open_url'] = url_for('alerts', open=ref_id)
        row['open_label'] = 'Open alert'
    elif item.get('type') == 'med_reminder':
        row['open_url'] = url_for('medication_reminders')
        row['open_label'] = 'Open medication reminders'
    else:
        row['open_url'] = url_for('my_loved_ones')
        row['open_label'] = 'Open my loved ones'

    return jsonify(success=True, activity=row)

# ---------- SETTINGS ----------
#
# user_account/{id}.notification_settings = {
#   emergency_alerts: bool, medicine_reminders: bool, device_alerts: bool }
# Each family member has their own, even when several share one elder.
# The device API uses them (through _notify_list) to decide who gets an SMS.


NOTIFICATION_KEYS = ('emergency_alerts', 'medicine_reminders', 'device_alerts')

# A brand-new account has everything on: someone who never opened this
# page still needs to hear about an emergency.
DEFAULT_NOTIFICATION_SETTINGS = {key: True for key in NOTIFICATION_KEYS}


def _notification_settings(user_id, user=None):
    """This user's notification preferences, with defaults filled in."""
    if user is None:
        doc = db.collection('user_account').document(user_id).get()
        user = (doc.to_dict() or {}) if doc.exists else {}
    saved = user.get('notification_settings') or {}
    return {key: bool(saved.get(key, DEFAULT_NOTIFICATION_SETTINGS[key]))
            for key in NOTIFICATION_KEYS}


def _notify_list(elder_id, key):
    """Who the device should text for this kind of event.

    Family members linked to the elder who have `key` switched on, plus —
    for emergencies only — the elder's emergency contacts (they have no
    account, so no settings to respect).
    """
    people, seen = [], set()
    try:
        for link in (db.collection('family_elder_link')
                     .where('elder_id', '==', elder_id).stream()):
            user_id = (link.to_dict() or {}).get('family_user_id')
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            doc = db.collection('user_account').document(user_id).get()
            user = (doc.to_dict() or {}) if doc.exists else None
            if not user or user.get('deleted_at') or not user.get('contact_number'):
                continue
            if not _notification_settings(user_id, user)[key]:
                continue
            people.append({'name': user.get('full_name') or '',
                           'contact_number': user['contact_number'],
                           'type': 'family'})

        if key == 'emergency_alerts':
            numbers = {p['contact_number'] for p in people}
            for doc in (db.collection('emergency_contact')
                        .where('elder_id', '==', elder_id).stream()):
                c = doc.to_dict() or {}
                if c.get('contact_number') and c['contact_number'] not in numbers:
                    numbers.add(c['contact_number'])
                    people.append({'name': c.get('contact_name') or '',
                                   'contact_number': c['contact_number'],
                                   'type': 'emergency_contact'})
    except Exception as e:
        print(f'[settings] could not build notify list for {elder_id}: {e}')
    return people


@app.route('/settings')
def settings():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    elders = _linked_elders_for_dashboard()
    open_alerts = [a for a in _alerts_for_elders(
        elders) if _is_open_alert(a['data'])]

    return render_template(
        'family/settings.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(),
        settings=_notification_settings(session['user_id']),
        notification_count=len(open_alerts),
        current_year=datetime.now().year,
    )


@app.route('/settings/save', methods=['POST'])
def save_settings():
    """Saves the toggles that were sent ('on' / 'off'). Keys not sent are left alone."""
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    sent = {key: request.form[key]
            for key in NOTIFICATION_KEYS if key in request.form}
    if not sent or any(v not in ('on', 'off') for v in sent.values()):
        return jsonify(success=False, message='Nothing to save.'), 400

    ref = db.collection('user_account').document(session['user_id'])
    doc = ref.get()
    if not doc.exists:
        return jsonify(success=False, message='Your account could not be found.'), 404

    current = _notification_settings(session['user_id'], doc.to_dict() or {})
    updated = {**current, **{k: v == 'on' for k, v in sent.items()}}

    try:
        ref.update({
            'notification_settings': updated,
            'settings_updated_at': firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        return jsonify(success=False, message=f'Could not save: {str(e)}'), 500

    if len(sent) == 1:
        key, value = next(iter(sent.items()))
        label = key.replace('_', ' ').capitalize()
        message = f"{label} turned {'on' if value == 'on' else 'off'}."
    else:
        message = 'Settings saved.'
    return jsonify(success=True, message=message, settings=updated)


@app.route('/care-plan/pay', methods=['POST'])
def pay_care_plan():
    """Family submits a GCash reference number; goes into 'payment' as
    pending until an admin approves it (see care_plan_service.apply_approved_payment)."""
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_id = (request.form.get('elder_id') or '').strip()
    reference_no = (request.form.get('reference_no') or '').strip()
    payer_name = (request.form.get('payer_name') or '').strip()
    try:
        months = int(request.form.get('months', 1))
    except ValueError:
        months = 0

    if not elder_id or not reference_no or months not in cps.PLAN_MONTH_OPTIONS:
        return jsonify(success=False, message='Please fill in all fields correctly.'), 400

    elder_doc = db.collection('elder_profile').document(elder_id).get()
    if not elder_doc.exists:
        return jsonify(success=False, message='Loved one not found.'), 404

    plan = cps.get_plan()
    unit_price = plan['price']

    payment_ref = db.collection('payment').document()
    payment_ref.set({
        'payment_no': cps.new_payment_no(payment_ref.id),
        'elder_id': elder_id,
        'elder_name': (elder_doc.to_dict() or {}).get('full_name', ''),
        'device_id': cps.device_for_elder(elder_id),
        'months': months,
        'unit_price': unit_price,
        'amount': unit_price * months,
        'currency': plan.get('currency', 'PHP'),
        'method': 'gcash',
        'reference_no': reference_no,
        'payer_name': payer_name or session.get('full_name'),
        'note': '',
        'paid_by_user_id': session['user_id'],
        'paid_by_name': session.get('full_name'),
        'status': 'pending',
        'created_at': firestore.SERVER_TIMESTAMP,
    })

    return jsonify(success=True,
                   message=f'Payment submitted ({payment_ref.id[:6].upper()}). '
                   'Waiting for admin approval.')


# ---------- MY PROFILE ----------

MAX_PHOTO_BASE64_BYTES = 700 * 1024


@app.route('/my-profile')
def my_profile():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    if session.get('role') == 'admin':
        return redirect(url_for('admin.dashboard'))
    if session.get('role') == 'bhw':
        return redirect(url_for('bhw.profile'))

    user_doc = db.collection('user_account').document(session['user_id']).get()
    user = user_doc.to_dict() if user_doc.exists else {}

    created_at = user.get('created_at')
    member_since = (
        created_at.astimezone(timezone(timedelta(hours=8))).strftime('%B %Y')
        if hasattr(created_at, 'astimezone') else '—'
    )

    full_name = user.get('full_name') or session['full_name']
    profile = {
        'full_name': full_name,
        'initials': _initials(full_name),
        'email': user.get('email') or '',
        'contact_number': user.get('contact_number') or '',
        'member_since': member_since,
        'photo_base64': user.get('photoBase64') or None,
    }

    return render_template(
        'family/profile.html',
        full_name=session['full_name'], role=session['role'],
        profile=profile, care_plan=_get_care_plan(),
        notification_count=0, current_year=datetime.now().year,
    )


@app.route('/my-profile/update', methods=['POST'])
def update_my_profile():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    user_ref = db.collection('user_account').document(session['user_id'])
    doc = user_ref.get()
    user = doc.to_dict() if doc.exists else None
    if not user:
        flash('Your account could not be found.', 'profile_error')
        return redirect(url_for('my_profile'))

    full_name = request.form.get('full_name', '').strip()
    email = request.form.get('email', '').strip().lower()
    contact_number = request.form.get('contact_number', '').strip()

    if not full_name:
        flash('Full name cannot be empty.', 'profile_error')
        return redirect(url_for('my_profile'))
    if not email:
        flash('Email address cannot be empty.', 'profile_error')
        return redirect(url_for('my_profile'))

    if email != user.get('email'):
        clash = list(db.collection('user_account')
                     .where('email', '==', email).limit(1).stream())
        if clash and clash[0].id != session['user_id']:
            flash('Another account already uses that email address.', 'profile_error')
            return redirect(url_for('my_profile'))

    try:
        if 'password_hash' not in user:
            firebase_auth.update_user(session['user_id'],
                                      email=email, display_name=full_name)

        user_ref.update({
            'full_name': full_name,
            'email': email,
            'contact_number': contact_number,
            'profile_updated_at': firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        flash(f'Could not save your changes: {e}', 'profile_error')
        return redirect(url_for('my_profile'))

    session['full_name'] = full_name
    flash('Your profile has been updated.', 'profile_success')
    return redirect(url_for('my_profile'))


@app.route('/my-profile/password', methods=['POST'])
def change_my_password():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    user_ref = db.collection('user_account').document(session['user_id'])
    doc = user_ref.get()
    user = doc.to_dict() if doc.exists else None
    if not user:
        flash('Your account could not be found.', 'profile_error')
        return redirect(url_for('my_profile'))

    current = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')

    if 'password_hash' in user:
        current_ok = check_password_hash(user['password_hash'], current)
    else:
        current_ok = _verify_firebase_password(
            user.get('email'), current) is not None

    if not current_ok:
        flash('Your current password is incorrect.', 'profile_error')
        return redirect(url_for('my_profile'))
    if new != confirm:
        flash('New password and confirm password do not match.', 'profile_error')
        return redirect(url_for('my_profile'))
    if new == current:
        flash('Your new password must be different from your current one.',
              'profile_error')
        return redirect(url_for('my_profile'))

    error = _check_family_password(new)
    if error:
        flash(error, 'profile_error')
        return redirect(url_for('my_profile'))

    try:
        if 'password_hash' in user:
            user_ref.update({
                'password_hash': generate_password_hash(new),
                'password_changed_at': firestore.SERVER_TIMESTAMP,
            })
        else:
            firebase_auth.update_user(session['user_id'], password=new)
            user_ref.update(
                {'password_changed_at': firestore.SERVER_TIMESTAMP})
    except Exception as e:
        flash(f'Could not update your password: {e}', 'profile_error')
        return redirect(url_for('my_profile'))

    flash('Your password has been updated.', 'profile_success')
    return redirect(url_for('my_profile'))


@app.route('/my-profile/photo', methods=['POST'])
def upload_my_photo():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    encoded = (request.form.get('photo_base64') or '').strip()
    if not encoded:
        flash('No photo was selected.', 'profile_error')
        return redirect(url_for('my_profile'))
    if len(encoded) > MAX_PHOTO_BASE64_BYTES:
        flash('That photo is too large. Please choose a different one.',
              'profile_error')
        return redirect(url_for('my_profile'))

    try:
        base64.b64decode(encoded, validate=True)
    except Exception:
        flash('That photo could not be read.', 'profile_error')
        return redirect(url_for('my_profile'))

    db.collection('user_account').document(session['user_id']).set({
        'photoBase64': encoded,
        'photoUpdatedAt': firestore.SERVER_TIMESTAMP,
    }, merge=True)

    flash('Your profile photo has been updated.', 'profile_success')
    return redirect(url_for('my_profile'))


@app.route('/my-profile/photo/remove', methods=['POST'])
def remove_my_photo():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    db.collection('user_account').document(session['user_id']).update({
        'photoBase64': firestore.DELETE_FIELD,
        'photoUpdatedAt': firestore.DELETE_FIELD,
    })

    flash('Your profile photo has been removed.', 'profile_success')
    return redirect(url_for('my_profile'))


# ---------- HELP & SUPPORT ----------
#
# Firestore:
#   faq/{id}                      question, answer, order, is_active
#   app_settings/support_contact  phone, phone_hours, email,
#                                 address_line1, address_line2, response_time
#   support_ticket/{id}           one per "Report a Problem" or "Contact Support"
#       ticket_no, type ('problem_report'|'contact_support'),
#       status ('open'|'in_progress'|'resolved'|'closed'),
#       category, category_label, subcategory, subcategory_label,
#       subject, message, reply_email,
#       user_id, user_name, user_email, user_role, user_contact_number,
#       admin_reply, admin_notes, handled_by, handled_by_name,
#       created_at, updated_at, resolved_at, source
# The admin panel (/admin/support) reads and updates support_ticket.
# Run `python seed_help_content.py` once to put the FAQs and contact info in Firestore.

# Used only when Firestore has nothing yet.
DEFAULT_FAQS = [
    {'question': 'How do I set up the ALISTO device for my loved one?',
     'answer': 'Charge the device, then enter its Device ID (printed on the device or its QR code) '
               'when you register or in My Loved Ones > View Details > Link device. '
               'Once it is switched on and connected, its status shows as Online.'},
    {'question': 'What happens when an emergency alert is triggered?',
     'answer': 'The device waits 5 seconds so a false alarm can be cancelled. If it is not cancelled, '
               'it sends an SMS to the linked family members and emergency contacts, and the alert '
               'appears on your Dashboard and Alerts page with the last known location.'},
    {'question': 'Can more than one family member link to the same elder?',
     'answer': 'Yes. Up to {max_family} family accounts can link to the same device. Each person '
               'registers with the same Device ID and chooses to join the existing elder.'},
    {'question': 'Does ALISTO work without internet?',
     'answer': 'Yes. Voice detection runs on the device itself, and alerts are sent by SMS. '
               'The website and app update once the device is back online.'},
    {'question': 'What if the alarm is triggered by accident?',
     'answer': 'Cancel it on the device within 5 seconds. It is saved as a false alarm and no SMS is sent.'},
    {'question': 'Where can I see past alerts and reminders?',
     'answer': 'Open History. You can filter by type and date range, and click any row for details.'},
    {'question': 'How do I choose which notifications I receive?',
     'answer': 'Open Settings and switch Emergency Alerts, Medicine Reminders, or Device Alerts on or off. '
               'Make sure your contact number is set in My Profile so SMS can reach you.'},
    {'question': 'Is my loved one\'s location private?',
     'answer': 'Location is only shown to family members linked to that elder and to the Barangay Health '
               'Worker assigned to their barangay.'},
]

DEFAULT_SUPPORT_CONTACT = {
    'phone': 'Not set yet',
    'phone_hours': 'Mon–Fri, 8:00 AM – 5:00 PM',
    'email': 'Not set yet',
    'address_line1': 'Barangay Sudlon II Hall',
    'address_line2': 'Cebu City, Cebu, Philippines',
    'response_time': '24 hours',
}

TICKET_STATUSES = {
    'open': 'Open',
    'in_progress': 'In Progress',
    'resolved': 'Resolved',
    'closed': 'Closed',
}

REPORT_CATEGORIES = {
    'app': ('App Issue', {
        'crash': 'App crashes or freezes',
        'buttons': 'Buttons or links not working',
        'slow': 'App is slow',
        'other_app': 'Other app issue',
    }),
    'device': ('Device Issue', {
        'connect': "Device won't connect",
        'battery': 'Battery drains too fast',
        'voice': "Device isn't detecting voice",
        'other_device': 'Other device issue',
    }),
    'alert': ('Alert Issue', {
        'not_received': "Alert wasn't received",
        'false_alarm': 'False alarm triggered',
        'wrong_location': 'Location was incorrect',
        'other_alert': 'Other alert issue',
    }),
    'account': ('Account Issue', {
        'login': "Can't log in",
        'reset_password': "Can't reset password",
        'profile_info': 'Profile info is wrong',
        'other_account': 'Other account issue',
    }),
    'other': ('Other', {}),
}

TICKET_MAX_PER_10_MIN = 5
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _help_faqs():
    faqs = []
    try:
        for doc in db.collection('faq').stream():
            f = doc.to_dict() or {}
            if f.get('is_active') is False or not f.get('question') or not f.get('answer'):
                continue
            faqs.append({'question': f['question'], 'answer': f['answer'],
                         'order': f.get('order', 999)})
    except Exception as e:
        print(f'[help] could not read FAQs: {e}')
    if not faqs:
        faqs = [dict(f, order=i) for i, f in enumerate(DEFAULT_FAQS)]
    faqs.sort(key=lambda f: (f['order'], f['question']))
    for f in faqs:
        f['answer'] = f['answer'].replace(
            '{max_family}', str(MAX_FAMILY_PER_DEVICE))
    return faqs


def _support_contact():
    info = dict(DEFAULT_SUPPORT_CONTACT)
    try:
        doc = db.collection('app_settings').document('support_contact').get()
        if doc.exists:
            info.update({k: v for k, v in (doc.to_dict() or {}).items() if v})
    except Exception as e:
        print(f'[help] could not read support contact: {e}')
    return info


def _ticket_row(doc_id, t):
    status = t.get('status') or 'open'
    return {
        'id': doc_id,
        'ticket_no': t.get('ticket_no') or doc_id[:8].upper(),
        'type': t.get('type'),
        'type_label': 'Problem Report' if t.get('type') == 'problem_report' else 'Support Message',
        'subject': t.get('subject') or '—',
        'message': t.get('message') or '',
        'status': status,
        'status_label': TICKET_STATUSES.get(status, status.title()),
        'admin_reply': t.get('admin_reply') or '',
        'created': _friendly_time(t.get('created_at')),
        'updated': _friendly_time(t.get('updated_at')),
        'sort_key': (_to_ph(t.get('created_at')).timestamp()
                     if _to_ph(t.get('created_at')) else 0),
    }


def _my_tickets(limit=10):
    rows = []
    try:
        for doc in (db.collection('support_ticket')
                    .where('user_id', '==', session['user_id']).stream()):
            rows.append(_ticket_row(doc.id, doc.to_dict() or {}))
    except Exception as e:
        print(f'[help] could not read tickets: {e}')
    rows.sort(key=lambda r: r['sort_key'], reverse=True)
    return rows[:limit], rows


def _create_ticket(fields):
    """Saves one ticket. Returns (ticket_no, error)."""
    user_doc = db.collection('user_account').document(session['user_id']).get()
    user = (user_doc.to_dict() or {}) if user_doc.exists else {}

    # Simple spam guard.
    _, mine = _my_tickets()
    recent_cutoff = datetime.now(PH_TZ).timestamp() - 600
    if sum(1 for r in mine if r['sort_key'] >= recent_cutoff) >= TICKET_MAX_PER_10_MIN:
        return None, 'You sent several requests just now. Please wait a few minutes.'

    ref = db.collection('support_ticket').document()
    ticket_no = f'TCK-{ref.id[:6].upper()}'
    ref.set({
        **fields,
        'ticket_no': ticket_no,
        'status': 'open',
        'user_id': session['user_id'],
        'user_name': user.get('full_name') or session.get('full_name') or '',
        'user_email': user.get('email') or '',
        'user_role': user.get('role') or session.get('role') or '',
        'user_contact_number': user.get('contact_number') or '',
        'admin_reply': '',
        'admin_notes': '',
        'handled_by': None,
        'handled_by_name': '',
        'source': 'web',
        'created_at': firestore.SERVER_TIMESTAMP,
        'updated_at': firestore.SERVER_TIMESTAMP,
        'resolved_at': None,
    })
    return ticket_no, None


@app.route('/help-support')
def help_support():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    user_doc = db.collection('user_account').document(session['user_id']).get()
    user = (user_doc.to_dict() or {}) if user_doc.exists else {}
    faqs = _help_faqs()
    my_tickets, _ = _my_tickets(limit=5)

    elders = _linked_elders_for_dashboard()
    open_alerts = [a for a in _alerts_for_elders(
        elders) if _is_open_alert(a['data'])]

    return render_template(
        'family/help_support.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(),
        faqs=faqs, top_faqs=faqs[:4],
        contact_info=_support_contact(),
        my_tickets=my_tickets,
        user_email=user.get('email') or '',
        user_full_name=user.get('full_name') or session['full_name'],
        notification_count=len(open_alerts),
        current_year=datetime.now().year,
    )


@app.route('/help-support/report', methods=['POST'])
def submit_problem_report():
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    data = request.get_json(silent=True) or {}
    category = (data.get('category') or '').strip()
    subcategory = (data.get('subcategory') or '').strip()
    description = (data.get('description') or '').strip()
    reply_email = (data.get('email') or '').strip().lower()

    if category not in REPORT_CATEGORIES:
        return jsonify(success=False, message='Please choose a category.'), 400
    category_label, subcategories = REPORT_CATEGORIES[category]
    if subcategories and subcategory not in subcategories:
        return jsonify(success=False, message='Please choose what best describes the problem.'), 400
    subcategory_label = subcategories.get(
        subcategory, '') if subcategories else ''
    if len(description) < 5:
        return jsonify(success=False, message='Please describe the problem.'), 400
    if len(description) > 2000:
        return jsonify(success=False, message='Please keep the description under 2000 characters.'), 400
    if reply_email and not EMAIL_RE.match(reply_email):
        return jsonify(success=False, message='Please enter a valid email or leave it blank.'), 400

    try:
        ticket_no, error = _create_ticket({
            'type': 'problem_report',
            'category': category,
            'category_label': category_label,
            'subcategory': subcategory if subcategories else None,
            'subcategory_label': subcategory_label,
            'subject': f'{category_label}: {subcategory_label}' if subcategory_label else category_label,
            'message': description,
            'reply_email': reply_email,
        })
    except Exception as e:
        return jsonify(success=False, message=f'Could not send your report: {e}'), 500
    if error:
        return jsonify(success=False, message=error), 429

    return jsonify(success=True, ticket_no=ticket_no,
                   message=f'Problem reported. Your ticket number is {ticket_no}.')


@app.route('/help-support/contact', methods=['POST'])
def submit_contact_support():
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    reply_email = (data.get('email') or '').strip().lower()
    subject = (data.get('subject') or '').strip()
    message = (data.get('message') or '').strip()

    if not name or not reply_email or not subject or not message:
        return jsonify(success=False, message='Please fill in all fields.'), 400
    if not EMAIL_RE.match(reply_email):
        return jsonify(success=False, message='Please enter a valid email address.'), 400
    if len(subject) > 150 or len(message) > 2000:
        return jsonify(success=False, message='Your subject or message is too long.'), 400

    try:
        ticket_no, error = _create_ticket({
            'type': 'contact_support',
            'category': None, 'category_label': '',
            'subcategory': None, 'subcategory_label': '',
            'subject': subject,
            'message': message,
            'reply_email': reply_email,
            'contact_name': name[:100],
        })
    except Exception as e:
        return jsonify(success=False, message=f'Could not send your message: {e}'), 500
    if error:
        return jsonify(success=False, message=error), 429

    info = _support_contact()
    return jsonify(success=True, ticket_no=ticket_no,
                   message=f"Message sent ({ticket_no}). We usually respond within {info['response_time']}.")


@app.route('/loved-ones/<elder_id>')
def loved_one_details(elder_id):
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('Loved one details page not implemented yet.', 'info')
    return redirect(url_for('my_loved_ones'))


# ---------- DASHBOARD ----------
#
# Everything on this page comes from Firestore. The only fallback is
# TEMP_LOCATION, used until the IoT device sends its first location.

def _initials(name: str) -> str:
    parts = name.strip().split()
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


PH_TZ = timezone(timedelta(hours=8))
DASHBOARD_WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
DASHBOARD_ALERT_LIMIT = 3


def _to_ph(value):
    """Firestore timestamp -> Philippine time, or None if it isn't a time."""
    if not hasattr(value, 'astimezone'):
        return None
    if getattr(value, 'tzinfo', None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PH_TZ)


def _friendly_time(value):
    """'Today, 10:29 AM' / 'Yesterday, 4:15 PM' / 'May 20, 2026 · 10:45 AM'."""
    local = _to_ph(value)
    if not local:
        return '—'
    today = datetime.now(PH_TZ).date()
    clock = local.strftime('%I:%M %p').lstrip('0')
    if local.date() == today:
        return f'Today, {clock}'
    if local.date() == today - timedelta(days=1):
        return f'Yesterday, {clock}'
    return f"{local.strftime('%b %d, %Y')} · {clock}"


def _reminder_clock(reminder_time):
    """(hour, minute) of the START time in text like '14:30', '8:00 PM',
    '9 am', or an app range '09:00 - 10:00 AM'. None if unreadable."""
    text = (reminder_time or '').strip().upper()
    tokens = re.findall(
        r'(\d{1,2})(?:\s*:\s*(\d{2}))?\s*(AM|PM|A\.M\.|P\.M\.)?', text)
    if not tokens:
        return None
    hour, minute = int(tokens[0][0]), int(tokens[0][1] or 0)
    meridiem = tokens[0][2][:1]
    if not meridiem:
        # '09:00 - 10:00 AM': the AM/PM written at the end applies to the start too,
        # except a range that crosses noon, like '11:00 - 1:00 PM'.
        later = [t for t in tokens[1:] if t[2]]
        if later:
            meridiem = later[0][2][:1]
            end_hour = int(later[0][0])
            if meridiem == 'P' and hour != 12 and end_hour != 12 and hour > end_hour:
                meridiem = 'A'
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == 'P' and hour < 12:
            hour += 12
        if meridiem == 'A' and hour == 12:
            hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _linked_elders_for_dashboard():
    """{elder_id: {...}} for every non-deleted elder linked to this account."""
    links = (
        db.collection('family_elder_link')
        .where('family_user_id', '==', session['user_id'])
        .stream()
    )
    elders = {}
    for link in links:
        link_data = link.to_dict() or {}
        elder_id = link_data.get('elder_id')
        if not elder_id or elder_id in elders:
            continue
        doc = db.collection('elder_profile').document(elder_id).get()
        if not doc.exists:
            continue
        data = doc.to_dict() or {}
        if data.get('deleted_at'):
            continue
        name = data.get('full_name') or 'Unnamed'
        elders[elder_id] = {
            'name': name,
            'photo_url': data.get('photo_url') or None,
            'initials': _initials(name),
            'relationship': link_data.get('relationship') or 'Family',
        }
    return elders


def _alerts_for_elders(elders):
    """Every alert for these elders, newest first.

    No order_by in the query, so no composite index is needed;
    sorting happens here instead.
    """
    rows = []
    for elder_id, elder in elders.items():
        try:
            docs = (
                db.collection(ALERT_COLLECTION)
                .where('elder_id', '==', elder_id)
                .stream()
            )
            for doc in docs:
                alert = doc.to_dict() or {}
                created = _to_ph(alert.get('created_at'))
                rows.append({
                    'id': doc.id,
                    'elder_id': elder_id,
                    'elder_name': elder['name'],
                    'data': alert,
                    'sort_key': created.timestamp() if created else 0,
                })
        except Exception as e:
            print(f'[dashboard] could not read alerts for {elder_id}: {e}')
    rows.sort(key=lambda r: r['sort_key'], reverse=True)
    return rows


def _is_open_alert(alert):
    return str(alert.get('status') or '') not in RESOLVED_STATUSES


def _alert_row(row):
    """One alert shaped for the Recent Alerts list in the template."""
    alert = row['data']
    status = str(alert.get('status') or 'PENDING').upper()
    phrase = alert.get('phrase_used') or alert.get('phrase')

    if status == 'RESOLVED':
        icon, icon_class, label = 'check-circle-2', 'ok', 'Resolved'
    elif status == 'CANCELLED':
        icon, icon_class, label = 'circle-x', 'warn', 'Cancelled (false alarm)'
    elif status in {s.upper() for s in RESPONDED_STATUSES}:
        icon, icon_class, label = 'bell', 'warn', 'Responded'
    else:
        icon, icon_class, label = 'siren', 'sos', 'Needs attention'

    subtext = f"{row['elder_name']} · {label}"
    if phrase:
        subtext += f' · "{phrase}"'

    return {
        'icon': icon,
        'icon_class': icon_class,
        'title': alert.get('title') or 'Emergency Alert',
        'subtext': subtext,
        'timestamp': _friendly_time(alert.get('created_at')),
    }


def _device_for_elder(elder_id):
    """(serial, device dict) for the elder's device, or (None, {})."""
    docs = list(
        db.collection('device')
        .where('elder_id', '==', elder_id)
        .limit(1)
        .stream()
    )
    if not docs:
        return None, {}
    return docs[0].id, (docs[0].to_dict() or {})


def _device_last_seen(device, loc):
    """Newest check-in we know of: a location report or the device's last_seen."""
    seen_times = [t for t in (
        _to_ph((loc or {}).get('recorded_at')),
        _to_ph((device or {}).get('last_seen')),
    ) if t]
    return max(seen_times) if seen_times else None


def _device_summary(serial, device, loc):
    """Status label, CSS class, and note for the Device Status card."""
    if not serial:
        return {'status': 'No Device', 'status_class': 'none',
                'note': 'No ALISTO device is linked to this loved one yet.'}
    if not device.get('is_registered'):
        return {'status': 'Not Registered', 'status_class': 'attention',
                'note': f'Device {serial} is not registered yet.'}

    last_seen = _device_last_seen(device, loc)

    if last_seen:
        online = datetime.now(PH_TZ) - last_seen < DEVICE_ONLINE_WINDOW
    else:
        # No timestamps at all: trust the status field the device writes.
        online = str(device.get('status') or '').lower() == 'online'

    battery = device.get('battery_level')
    battery_text = f' Battery {battery}%.' if isinstance(
        battery, (int, float)) else ''

    if online:
        return {'status': 'Online', 'status_class': 'online',
                'note': 'Device is connected and working properly.' + battery_text}

    note = (f'Last check-in: {_friendly_time(last_seen)}.' if last_seen
            else 'No check-in received from the device yet.')
    return {'status': 'Offline', 'status_class': 'offline',
            'note': note + battery_text}


def _next_medicine(elder_id):
    """Soonest upcoming active reminder for this elder (next 7 days)."""
    empty = {'time': 'No upcoming reminder',
             'medicine_name': 'Add one in Medication Reminders',
             'date': '—'}
    try:
        docs = list(
            db.collection('medication_reminder')
            .where('elder_id', '==', elder_id)
            .stream()
        )
    except Exception as e:
        print(f'[dashboard] could not read reminders for {elder_id}: {e}')
        return empty

    now = datetime.now(PH_TZ)
    best = None
    for doc in docs:
        med = doc.to_dict() or {}
        if med.get('deleted_at') or med.get('is_active') is False:
            continue
        clock = _reminder_clock(_med_time(med))
        if not clock:
            continue
        # No schedule_days means it repeats every day (how the mobile app saves it).
        days = [d for d in (med.get('schedule_days') or [])
                if d in DASHBOARD_WEEKDAYS]

        for offset in range(8):
            day = now + timedelta(days=offset)
            if days and DASHBOARD_WEEKDAYS[day.weekday()] not in days:
                continue
            due = day.replace(hour=clock[0], minute=clock[1],
                              second=0, microsecond=0)
            if due <= now:
                continue
            if best is None or due < best[0]:
                best = (due, med)
            break

    if not best:
        return empty

    due, med = best
    if due.date() == now.date():
        day_label = 'Today'
    elif due.date() == (now + timedelta(days=1)).date():
        day_label = 'Tomorrow'
    else:
        day_label = due.strftime('%A')

    name = _med_name(med)
    if med.get('dosage'):
        name += f" · {med['dosage']}"

    return {
        'time': f"{due.strftime('%I:%M %p').lstrip('0')} {day_label}",
        'medicine_name': name,
        'date': due.strftime('%B %d, %Y'),
    }


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    if session.get('role') == 'admin':
        return redirect(url_for('admin.dashboard'))
    if session.get('role') == 'bhw':
        return redirect(url_for('bhw.dashboard'))

    elders = _linked_elders_for_dashboard()
    all_alerts = _alerts_for_elders(elders)
    open_alerts = [a for a in all_alerts if _is_open_alert(a['data'])]

    # Which loved one to feature: whoever has an open emergency first,
    # otherwise the first one alphabetically.
    elderly_id = None
    if open_alerts:
        elderly_id = open_alerts[0]['elder_id']
    elif elders:
        elderly_id = min(elders, key=lambda i: elders[i]['name'].casefold())

    loved_one = {'name': 'No loved one linked yet', 'photo_url': None,
                 'initials': '?', 'elder_id': None}
    device = _device_summary(None, {}, None)
    next_reminder = {'time': '—', 'medicine_name': 'No loved one linked yet',
                     'date': '—'}
    location = dict(TEMP_LOCATION)

    if elderly_id:
        loved_one = {**elders[elderly_id], 'elder_id': elderly_id}

        serial, device_data = _device_for_elder(elderly_id)
        loc = _latest_device_location(serial)
        device = _device_summary(serial, device_data, loc)
        next_reminder = _next_medicine(elderly_id)

        # Real location only once the device has sent coordinates;
        # until then TEMP_LOCATION stays on screen.
        if loc and isinstance(loc.get('gps_lat'), (int, float)) \
                and isinstance(loc.get('gps_long'), (int, float)):
            location = {
                'latitude': loc['gps_lat'],
                'longitude': loc['gps_long'],
                'address': loc.get('location_address') or 'Address not reported',
                'last_updated': _friendly_time(loc.get('recorded_at')),
            }

        # An open emergency outranks the device status on the loved-one pill.
        if any(a['elder_id'] == elderly_id for a in open_alerts):
            loved_one['pill_label'] = 'Emergency'
            loved_one['pill_class'] = 'emergency'
        else:
            loved_one['pill_label'] = device['status']
            loved_one['pill_class'] = device['status_class']

    recent_alerts = [_alert_row(a) for a in all_alerts[:DASHBOARD_ALERT_LIMIT]]

    return render_template(
        'family/dashboard.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(),
        loved_one=loved_one, elderly_id=elderly_id,
        device=device, next_reminder=next_reminder,
        location=location, recent_alerts=recent_alerts,
        notification_count=len(open_alerts),
        current_year=datetime.now().year,
    )


# ---------- MAP MODAL (not used by any template yet) ----------

@app.route('/map-modal/<elderly_id>')
def map_modal(elderly_id):
    try:
        elderly = db.collection('elder_profile').document(elderly_id).get()
        if not elderly.exists:
            return jsonify({"error": "Elderly not found"}), 404

        elderly_data = elderly.to_dict()

        device_id = elderly_data.get('device_id')
        if not device_id:
            return render_template('map_modal.html',
                                   elderly_name=elderly_data.get('full_name'),
                                   lat=10.3157, lng=123.8854,
                                   address='No device linked yet')

        locations = list(
            db.collection('DEVICE_LOCATION')
            .where('device_id', '==', device_id)
            .order_by('recorded_at', direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )

        if locations:
            loc = locations[0].to_dict()
            lat = loc.get('gps_lat', 10.3157)
            lng = loc.get('gps_long', 123.8854)
            address = loc.get('location_address', 'Location pending...')
        else:
            lat, lng, address = 10.3157, 123.8854, 'Waiting for first location update...'

        return render_template('map_modal.html',
                               elderly_name=elderly_data.get('full_name'),
                               lat=lat, lng=lng, address=address)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------- LIVE MAP ----------

DEVICE_ONLINE_WINDOW = timedelta(minutes=10)
ALERT_COLLECTION = 'alert'

RESPONDED_STATUSES = {'ACKNOWLEDGED', 'RESPONDED', 'acknowledged', 'responded'}
RESOLVED_STATUSES = {'RESOLVED', 'CANCELLED', 'resolved', 'cancelled'}


def _latest_device_location(device_serial):
    if not device_serial:
        return None
    try:
        docs = list(
            db.collection('DEVICE_LOCATION')
            .where('device_id', '==', device_serial)
            .order_by('recorded_at', direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
        return docs[0].to_dict() if docs else None
    except Exception as e:
        # Usually a missing composite index (device_id + recorded_at desc).
        # The error message has a link to create it in the Firebase console.
        print(
            f'[location] ordered query failed, sorting in Python instead: {e}')

    # Fallback that needs no index: read this device's rows and pick the newest.
    try:
        rows = [d.to_dict() or {} for d in
                db.collection('DEVICE_LOCATION')
                .where('device_id', '==', device_serial)
                .stream()]
    except Exception as e:
        print(f'[location] could not read DEVICE_LOCATION: {e}')
        return None
    rows = [r for r in rows if hasattr(r.get('recorded_at'), 'timestamp')]
    return max(rows, key=lambda r: r['recorded_at'].timestamp()) if rows else None


def _open_alert_for_elder(elder_id):
    try:
        docs = list(
            db.collection(ALERT_COLLECTION)
            .where('elder_id', '==', elder_id)
            .order_by('created_at', direction=firestore.Query.DESCENDING)
            .limit(5)
            .stream()
        )
    except Exception:
        return None

    for doc in docs:
        alert = doc.to_dict() or {}
        if str(alert.get('status') or '') in RESOLVED_STATUSES:
            continue
        return alert
    return None


def _elder_map_status(alert, recorded_at):
    if alert:
        status = str(alert.get('status') or '')
        return 'responded' if status in RESPONDED_STATUSES else 'emergency'

    if hasattr(recorded_at, 'timestamp'):
        seen = recorded_at if recorded_at.tzinfo else recorded_at.replace(
            tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - seen < DEVICE_ONLINE_WINDOW:
            return 'online'

    return 'offline'


@app.route('/api/loved-ones/map')
def loved_ones_map_data():
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    ph_tz = timezone(timedelta(hours=8))
    links = list(
        db.collection('family_elder_link')
        .where('family_user_id', '==', session['user_id'])
        .stream()
    )

    elders = []
    for link in links:
        link_data = link.to_dict() or {}
        elder_id = link_data.get('elder_id')
        if not elder_id:
            continue

        elder_doc = db.collection('elder_profile').document(elder_id).get()
        if not elder_doc.exists or elder_doc.to_dict().get('deleted_at'):
            continue
        elder = elder_doc.to_dict()

        device_docs = list(
            db.collection('device')
            .where('elder_id', '==', elder_id)
            .limit(1)
            .stream()
        )
        if not device_docs:
            continue
        device_serial = device_docs[0].id
        device = device_docs[0].to_dict() or {}

        loc = _latest_device_location(device_serial)
        recorded_at = loc.get('recorded_at') if loc else None
        alert = _open_alert_for_elder(elder_id)

        lat = loc.get('gps_lat') if loc else None
        lng = loc.get('gps_long') if loc else None
        has_location = isinstance(
            lat, (int, float)) and isinstance(lng, (int, float))

        name = elder.get('full_name') or 'Unnamed'
        elders.append({
            'id': elder_id,
            'name': name,
            'initials': _initials(name),
            'photo_url': elder.get('photo_url') or None,
            'relationship': link_data.get('relationship') or 'Family',
            'device_id': device_serial,
            'device_registered': bool(device.get('is_registered')),
            'status': _elder_map_status(alert, recorded_at),
            'has_location': has_location,
            'latitude': lat if has_location else None,
            'longitude': lng if has_location else None,
            'address': (loc or {}).get('location_address') or 'Address not reported',
            'last_seen': (
                recorded_at.astimezone(ph_tz).strftime('%b %d, %Y %I:%M %p')
                if hasattr(recorded_at, 'astimezone') else 'No report yet'
            ),
            'alert_title': (alert or {}).get('title') or None,
        })

    plotted = [e for e in elders if e['has_location']]
    return jsonify(
        success=True, elders=elders, total=len(elders),
        plotted=len(plotted), waiting=len(elders) - len(plotted),
    )


if __name__ == '__main__':
    app.run(debug=True)
