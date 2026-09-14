from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from database import db
from admin_routes import admin_bp
from bhw_routes import bhw_bp
from firebase_admin import firestore, auth as firebase_auth
from datetime import datetime, timedelta, timezone
import requests
import os
import uuid
import re

app = Flask(__name__)
app.secret_key = 'alisto-secret-key-change-this-later'
app.register_blueprint(admin_bp)  # admin panel at /admin
app.register_blueprint(bhw_bp)    # BHW portal at /bhw

db = firestore.client()

MAX_FAMILY_PER_DEVICE = 5
FIREBASE_WEB_API_KEY = "AIzaSyBFkRaWyU_j6qcspXuOsJUXteRDIw8thqE"

# ---------- BHW REGISTRATION SETTINGS ----------
# Edit these lists to change the dropdown options on the register page.
HEALTH_CENTERS = ['Sudlon II Health Center']
YEARS_OF_SERVICE_OPTIONS = [
    'Less than 1 year', '1-3 years', '4-6 years', '7-10 years', 'More than 10 years'
]
VALID_ID_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf'}
VALID_ID_MAX_BYTES = 2 * 1024 * 1024  # 2MB
# Saved OUTSIDE /static so uploaded IDs are not publicly viewable.
VALID_ID_UPLOAD_FOLDER = os.path.join(app.root_path, 'uploads', 'bhw_ids')


def _verify_firebase_password(email, password):
    """Returns the Firebase user record if email/password match, else None."""
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


def _get_care_plan():
    return {'status': 'Active', 'days_left': 12,
            'renew_date': 'July 20, 2026', 'price': 55}


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

# ---------- FAMILY PASSWORD RULES ----------
# Mirrors PASSWORD_RULES in static/js/auth.js — keep the two in sync.
FAMILY_PASSWORD_RULES = [
    (lambda p: len(p) >= 6,                   'be at least 6 characters'),
    (lambda p: re.search(r'[A-Z]', p),        'include an uppercase letter'),
    (lambda p: re.search(r'[a-z]', p),        'include a lowercase letter'),
    (lambda p: re.search(r'\d', p),           'include a number'),
    (lambda p: re.search(r'[^A-Za-z0-9]', p), 'include a special character'),
]


def _check_family_password(password):
    """Returns an error message, or None if the password is fine."""
    missing = [label for rule, label in FAMILY_PASSWORD_RULES if not rule(password)]
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

        # No free family slots left on this device.
        if requesting_role == 'family' and family_count >= MAX_FAMILY_PER_DEVICE:
            return jsonify(
                valid=True, already_registered=True, can_join=False,
                elder_id=elder_id,
                elder_display_name=_first_name(elder.get('full_name')),
                family_count=family_count, family_limit=MAX_FAMILY_PER_DEVICE,
                message=f"This device already has the maximum of {MAX_FAMILY_PER_DEVICE} linked family members."
            )

        # There is room — this family member joins the existing elder.
        # elder_full_name and elder_dob pre-fill step 3 on the register page.
        return jsonify(
            valid=True, already_registered=True, can_join=True,
            elder_id=elder_id,
            elder_display_name=_first_name(elder.get('full_name')),
            elder_full_name=elder.get('full_name'),
            elder_dob=elder.get('date_of_birth'),
            family_count=family_count, family_limit=MAX_FAMILY_PER_DEVICE,
            message='This device is already linked to an ALISTO user.'
        )

    # Device exists but has never been claimed — this is the first family member.
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
        # Admins are created with create_admin.py, never through the public form.
        if role not in ('family', 'bhw'):
            return jsonify(success=False, message='Invalid role selected.'), 400

        # NEW: BHWs don't register a device or an elder. They go through their own flow
        # and wait for admin approval.
                # NEW: BHWs don't register a device or an elder. They go through their own flow
        # and wait for admin approval.
        if role == 'bhw':
            return _register_bhw(full_name, email, password, contact_number, barangay_assigned)

        # NEW: family password strength. Mirrors PASSWORD_RULES in static/js/auth.js.
        # BHW passwords are not checked here — that flow returns above.
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
            
        # Email uniqueness (Firestore has no UNIQUE constraint — check manually)
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
                    # Device already registered — auto-join if there's room and user is family
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
    """Create a BHW account with approval_status 'pending' plus its bhw_profile."""
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

    # Uniqueness checks (Firestore has no UNIQUE constraint)
    if list(db.collection('user_account').where('email', '==', email).limit(1).stream()):
        return jsonify(success=False, message='An account with this email already exists.'), 400
    if list(db.collection('bhw_profile').where('bhw_id_number', '==', bhw_id_number).limit(1).stream()):
        return jsonify(success=False, message='This BHW ID number is already registered.'), 400

    # Optional valid ID upload
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
            'approval_status': 'pending',  # admin changes this to 'approved' or 'rejected'
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

    # ---- Device: fill in the fields the IoT device is expected to report.
    # These start as placeholders; once the physical device comes online it
    # should PATCH/update this same document with its real gps/status/sim_number.
    device_ref = db.collection('device').document(serial_number)
    device_doc = device_ref.get()
    device_data = device_doc.to_dict() if device_doc.exists else {}

    device_ref.set({
        'elder_id': elder_id,
        'is_registered': True,
        'registered_at': firestore.SERVER_TIMESTAMP,
        'serial_number': serial_number,
        # Only backfill these if they're not already on the document, so we
        # never overwrite real data that the IoT device may have already sent.
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
                # Old system — bhw/admin accounts, or family accounts not yet migrated
                password_ok = check_password_hash(
                    user['password_hash'], password)
            else:
                # New system — family accounts created via Firebase Auth
                password_ok = _verify_firebase_password(
                    email, password) is not None

        if user and not user.get('deleted_at') and password_ok:
            # NEW: BHWs can't sign in until an admin approves them.
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

            # Remember the previous sign-in for "Last Login" on the profile page.
            previous = user.get('last_login_at')
            if hasattr(previous, 'astimezone'):
                previous = previous.astimezone(
                    timezone(timedelta(hours=8)))  # Philippine time
            session['previous_login'] = previous.strftime(
                '%b %d, %Y %I:%M %p') if hasattr(previous, 'strftime') else None
            updates = {'last_login_at': firestore.SERVER_TIMESTAMP}

            if user['role'] == 'bhw':
                # First sign-in after approval: show the "Account Approved!" screen once.
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

@app.route('/my-loved-ones')
def my_loved_ones():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    links = list(
        db.collection('family_elder_link')
        .where('family_user_id', '==', session['user_id'])
        .stream()
    )

    loved_ones = []
    online_count = 0
    offline_count = 0
    attention_count = 0
    ph_tz = timezone(timedelta(hours=8))

    for link in links:
        link_data = link.to_dict()
        elder_id = link_data.get('elder_id')
        if not elder_id:
            continue

        elder_doc = db.collection('elder_profile').document(elder_id).get()
        if not elder_doc.exists or elder_doc.to_dict().get('deleted_at'):
            continue

        elder_data = elder_doc.to_dict()
        elder_name = elder_data.get('full_name', 'Unnamed')

        # ---- Device: 'device' docs are keyed by serial number and store elder_id ----
        device_docs = list(
            db.collection('device')
            .where('elder_id', '==', elder_id)
            .limit(1)
            .stream()
        )
        device_doc = device_docs[0] if device_docs else None
        device_serial = device_doc.id if device_doc else None
        device_registered = bool(
            device_doc and device_doc.to_dict().get('is_registered'))

        # ---- Last known location + last check-in (from DEVICE_LOCATION) ----
        # Falls back to TEMP_LOCATION (sample/dummy coords) until the real
        # IoT device has sent at least one location update.
        location_text = TEMP_LOCATION['address']
        last_checkin_text = TEMP_LOCATION['last_updated']
        checkin_overdue = True
        status_class = 'offline'

        if device_serial:
            try:
                loc_query = list(
                    db.collection('DEVICE_LOCATION')
                    .where('device_id', '==', device_serial)
                    .order_by('recorded_at', direction=firestore.Query.DESCENDING)
                    .limit(1)
                    .stream()
                )
            except Exception:
                loc_query = []

            if loc_query:
                loc_data = loc_query[0].to_dict()
                location_text = loc_data.get('location_address', location_text)
                recorded_at = loc_data.get('recorded_at')

                if hasattr(recorded_at, 'astimezone'):
                    recorded_local = recorded_at.astimezone(ph_tz)
                    last_checkin_text = recorded_local.strftime(
                        '%b %d, %Y %I:%M %p')
                    checkin_overdue = (datetime.now(
                        ph_tz) - recorded_local) > timedelta(hours=6)
                    status_class = 'offline' if checkin_overdue else 'online'

        if not device_registered:
            status_class = 'attention'

        # ---- Next medicine reminder ----
        next_medicine_text = 'No reminders set'
        medicine_overdue = False
        try:
            reminder_query = list(
                db.collection('medication_reminder')
                .where('elder_id', '==', elder_id)
                .order_by('reminder_time')
                .limit(1)
                .stream()
            )
        except Exception:
            reminder_query = []

        if reminder_query:
            reminder_data = reminder_query[0].to_dict()
            next_medicine_text = reminder_data.get(
                'medicine_name', next_medicine_text)

        if status_class == 'attention':
            attention_count += 1
        elif status_class == 'online':
            online_count += 1
        else:
            offline_count += 1

        loved_ones.append({
            'id': elder_id,
            'name': elder_name,
            'photo_url': elder_data.get('photo_url') or None,
            'initials': _initials(elder_name),
            'relationship': link_data.get('relationship', 'Family'),
            'status_class': status_class,
            'last_checkin': last_checkin_text,
            'checkin_overdue': checkin_overdue,
            'next_medicine': next_medicine_text,
            'medicine_overdue': medicine_overdue,
            'location': location_text,
        })

    return render_template(
        'family/my_loved_ones.html',
        full_name=session['full_name'], role=session['role'],
        loved_ones=loved_ones,
        online_count=online_count, offline_count=offline_count,
        attention_count=attention_count, notification_count=0,
        current_year=datetime.now().year,
    )


# ---------- ADD LOVED ONE ----------

@app.route('/loved-ones/add', methods=['POST'])
def add_loved_one():
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    full_name = request.form.get('full_name', '').strip()
    relationship = request.form.get('relationship', '').strip()
    dob = request.form.get('date_of_birth', '').strip()
    house_no = request.form.get('house_no', '').strip()
    street = request.form.get('street', '').strip()
    barangay = request.form.get('barangay', '').strip()
    city = request.form.get('city', '').strip()
    province = request.form.get('province', '').strip()
    zip_code = request.form.get('zip_code', '').strip()
    device_id = request.form.get('device_id', '').strip()

    if not full_name:
        return jsonify(success=False, field='full_name',
                       message='Enter the full name of your loved one.'), 400
    if not relationship:
        return jsonify(success=False, field='relationship',
                       message='Choose how you are related to them.'), 400

    address = ', '.join(filter(
        None, [house_no, street, barangay, city, province, zip_code]))

    try:
        # ---- If a Device ID was given, check it BEFORE creating anything ----
        device_data = None
        if device_id:
            device_doc = db.collection('device').document(device_id).get()
            if not device_doc.exists:
                return jsonify(success=False, field='device_id',
                               message='No matching Device ID found in the system.'), 400

            device_data = device_doc.to_dict()
            if device_data.get('is_registered'):
                linked_elder_id = device_data.get('elder_id')
                return jsonify(
                    success=False, field='device_id',
                    message='This Device ID is already linked to another elder. '
                            'Leave it blank if you just want to register the person for now.'
                ), 409

        # ---- Create the elder profile ----
        elder_ref = db.collection('elder_profile').document()
        elder_ref.set({
            'full_name': full_name,
            'date_of_birth': dob or None,
            'address': address,
            'created_at': firestore.SERVER_TIMESTAMP,
            'deleted_at': None,
            'is_archived': 0,
        })
        elder_id = elder_ref.id

        # ---- Link the elder to the logged-in family account ----
        db.collection('family_elder_link').add({
            'family_user_id': session['user_id'],
            'elder_id': elder_id,
            'relationship': relationship,
        })

        # ---- Link the device, if one was provided ----
        if device_id:
            db.collection('device').document(device_id).set({
                'elder_id': elder_id,
                'is_registered': True,
                'registered_at': firestore.SERVER_TIMESTAMP,
                'serial_number': device_id,
                'gps': (device_data or {}).get('gps', 'Inactive'),
                'sim_number': (device_data or {}).get('sim_number', None),
                'status': (device_data or {}).get('status', 'Offline'),
            }, merge=True)

        return jsonify(success=True, message=f'{full_name} has been added to your loved ones.')

    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500


# ---------- LOVED ONE DETAILS (view modal) ----------

def _get_elder_if_linked(elder_id):
    """Return (elder_doc, link_data) if the logged-in family user is linked
    to this elder, else (None, None)."""
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
    if not elder_doc.exists or elder_doc.to_dict().get('deleted_at'):
        return None, None

    return elder_doc, links[0].to_dict()


@app.route('/loved-ones/<elder_id>/details')
def loved_one_details_json(elder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_doc, link_data = _get_elder_if_linked(elder_id)
    if not elder_doc:
        return jsonify(success=False, message='Loved one not found.'), 404

    elder_data = elder_doc.to_dict()
    elder_name = elder_data.get('full_name', 'Unnamed')
    ph_tz = timezone(timedelta(hours=8))

    device_docs = list(
        db.collection('device')
        .where('elder_id', '==', elder_id)
        .limit(1)
        .stream()
    )
    device_doc = device_docs[0] if device_docs else None
    device_data = device_doc.to_dict() if device_doc else {}
    device_serial = device_doc.id if device_doc else None
    device_registered = bool(device_data.get('is_registered'))

    location_text = TEMP_LOCATION['address']
    last_checkin_text = TEMP_LOCATION['last_updated']
    checkin_overdue = True
    status_class = 'offline'

    if device_serial:
        try:
            loc_query = list(
                db.collection('DEVICE_LOCATION')
                .where('device_id', '==', device_serial)
                .order_by('recorded_at', direction=firestore.Query.DESCENDING)
                .limit(1)
                .stream()
            )
        except Exception:
            loc_query = []

        if loc_query:
            loc_data = loc_query[0].to_dict()
            location_text = loc_data.get('location_address', location_text)
            recorded_at = loc_data.get('recorded_at')
            if hasattr(recorded_at, 'astimezone'):
                recorded_local = recorded_at.astimezone(ph_tz)
                last_checkin_text = recorded_local.strftime(
                    '%b %d, %Y %I:%M %p')
                checkin_overdue = (datetime.now(ph_tz) -
                                   recorded_local) > timedelta(hours=6)
                status_class = 'offline' if checkin_overdue else 'online'

    if not device_registered:
        status_class = 'attention'

    next_medicine_text = 'No reminders set'
    try:
        reminder_query = list(
            db.collection('medication_reminder')
            .where('elder_id', '==', elder_id)
            .order_by('reminder_time')
            .limit(1)
            .stream()
        )
    except Exception:
        reminder_query = []
    if reminder_query:
        next_medicine_text = reminder_query[0].to_dict().get(
            'medicine_name', next_medicine_text)

    elder = {
        'name': elder_name,
        'initials': _initials(elder_name),
        'relationship': link_data.get('relationship', 'Family'),
        'status_class': status_class,
        'photo_url': elder_data.get('photo_url') or None,
        'date_of_birth': elder_data.get('date_of_birth'),
        'address': elder_data.get('address'),
        'device_id': device_serial,
        'device_status': device_data.get('status', 'Offline'),
        'last_checkin': last_checkin_text,
        'next_medicine': next_medicine_text,
        'location': location_text,
    }

    return jsonify(success=True, elder=elder)


@app.route('/loved-ones/<elder_id>/photo', methods=['POST'])
def loved_one_upload_photo(elder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_doc, _ = _get_elder_if_linked(elder_id)
    if not elder_doc:
        return jsonify(success=False, message='Loved one not found.'), 404

    photo = request.files.get('photo')
    if not photo or not photo.filename:
        return jsonify(success=False, message='No photo was uploaded.'), 400

    ext = photo.filename.rsplit(
        '.', 1)[-1].lower() if '.' in photo.filename else ''
    if ext not in {'jpg', 'jpeg', 'png', 'gif', 'webp'}:
        return jsonify(success=False, message='Photo must be a JPG, PNG, GIF, or WEBP file.'), 400

    upload_dir = os.path.join(app.root_path, 'static',
                              'uploads', 'elder_photos')
    os.makedirs(upload_dir, exist_ok=True)
    stored_name = f'{uuid.uuid4().hex}.{ext}'
    photo.save(os.path.join(upload_dir, stored_name))

    photo_url = url_for(
        'static', filename=f'uploads/elder_photos/{stored_name}')
    db.collection('elder_profile').document(
        elder_id).update({'photo_url': photo_url})

    return jsonify(success=True, photo_url=photo_url, message='Photo updated.')


@app.route('/loved-ones/<elder_id>/delete', methods=['POST'])
def loved_one_delete(elder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_doc, _ = _get_elder_if_linked(elder_id)
    if not elder_doc:
        return jsonify(success=False, message='Loved one not found.'), 404

    # Soft-delete the elder profile
    db.collection('elder_profile').document(elder_id).update({
        'deleted_at': firestore.SERVER_TIMESTAMP
    })

    # Remove the link between this family user and the elder
    links = list(
        db.collection('family_elder_link')
        .where('family_user_id', '==', session['user_id'])
        .where('elder_id', '==', elder_id)
        .stream()
    )
    for link in links:
        link.reference.delete()

    # Unlink any device pointed at this elder
    device_docs = list(
        db.collection('device').where('elder_id', '==', elder_id).stream()
    )
    for d in device_docs:
        d.reference.update({'elder_id': None, 'is_registered': False})

    return jsonify(success=True, message='Loved one removed.')


# ---------- loved one update ----------
@app.route('/loved-ones/<elder_id>/update', methods=['POST'])
def loved_one_update(elder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    elder_doc, link_data = _get_elder_if_linked(elder_id)
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

    try:
        db.collection('elder_profile').document(elder_id).update({
            'full_name': full_name,
            'date_of_birth': dob or None,
            'address': address,
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

        return jsonify(success=True, message='Changes saved.')
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500


# ---------- ALERTS ----------


@app.route('/alerts')
def alerts():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    return render_template(
        'family/alerts.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(), alerts=[],
        emergency_count=0, false_alarm_count=0, notification_count=0,
        current_year=datetime.now().year,
    )


@app.route('/alerts/<alert_id>')
def alert_details(alert_id):
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('Alert details page not implemented yet.', 'info')
    return redirect(url_for('alerts'))


# ---------- MEDICATION REMINDERS ----------

@app.route('/medication-reminders')
def medication_reminders():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    return render_template(
        'family/medication_reminders.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(), schedules=[],
        today_count=0, tomorrow_count=0, total_schedule_count=0,
        notification_count=0, current_year=datetime.now().year,
    )


# ---------- HISTORY ----------

@app.route('/history')
def history():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    pagination = {'start': 0, 'end': 0, 'total': 0,
                  'current_page': 1, 'total_pages': 1, 'pages': [1]}

    return render_template(
        'family/history.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(), activities=[],
        alert_count=0, med_reminder_count=0, device_activity_count=0,
        notification_count=0, date_range_label='This Month',
        pagination=pagination, current_year=datetime.now().year,
    )


@app.route('/history/<activity_id>')
def activity_details(activity_id):
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('Activity details page not implemented yet.', 'info')
    return redirect(url_for('history'))


# ---------- SETTINGS ----------

@app.route('/settings')
def settings():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    user_settings = {
        'emergency_alerts': True, 'medicine_reminders': True,
        'device_alerts': True, 'location_updates': True,
    }

    return render_template(
        'family/settings.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(), settings=user_settings,
        notification_count=0, current_year=datetime.now().year,
    )


@app.route('/settings/change-password')
def change_password():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('Change Password page not implemented yet.', 'info')
    return redirect(url_for('settings'))


# ---------- HELP & SUPPORT ----------

@app.route('/help-support')
def help_support():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    faqs = [
        {'question': 'How do I set up the ALISTO device for my loved one?',
         'answer': 'Follow the step-by-step pairing guide in the User Guide, then confirm the Device ID matches the one on your registration.'},
        {'question': 'What happens when an emergency alert is triggered?',
         'answer': "ALISTO sends an SMS to your registered emergency contacts and marks the alert on your dashboard with the elder's last known location."},
        {'question': 'Can more than one family member link to the same elder?',
         'answer': f'Yes, up to {MAX_FAMILY_PER_DEVICE} family accounts can be linked to the same device/elder.'},
    ]

    contact_info = {
        'phone': '+63 917 000 0000', 'phone_hours': 'Mon–Fri, 8:00 AM – 5:00 PM',
        'email': 'support@alisto.ph', 'address_line1': 'Barangay Sudlon II Hall',
        'address_line2': 'Cebu City, Cebu, Philippines',
    }

    return render_template(
        'family/help_support.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(), faqs=faqs, contact_info=contact_info,
        notification_count=0, current_year=datetime.now().year,
    )


@app.route('/help-support/faqs')
def faqs():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('Full FAQs page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/help-support/contact')
def contact_support():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('Contact Support page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/help-support/user-guide')
def user_guide():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('User Guide page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/help-support/report-issue')
def report_issue():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('Report Issue page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/help-support/about')
def about():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('About page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/loved-ones/<elder_id>')
def loved_one_details(elder_id):
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('Loved one details page not implemented yet.', 'info')
    return redirect(url_for('my_loved_ones'))


# ---------- DASHBOARD ----------

def _initials(name: str) -> str:
    parts = name.strip().split()
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


@app.route('/map-modal/<elderly_id>')
def map_modal(elderly_id):
    """Fetch location data and return modal HTML"""
    try:
        # Get elderly profile
        elderly = db.collection('elder_profile').document(elderly_id).get()
        if not elderly.exists:
            return jsonify({"error": "Elderly not found"}), 404

        elderly_data = elderly.to_dict()

        # Get device linked to this elderly
        device_id = elderly_data.get('device_id')
        if not device_id:
            # No device linked yet — show fallback location
            return render_template('map_modal.html',
                                   elderly_name=elderly_data.get('full_name'),
                                   lat=10.3157, lng=123.8854,
                                   address='No device linked yet')

        # Get latest location from DEVICE_LOCATION collection
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
            # Device exists but no location logged yet
            lat, lng, address = 10.3157, 123.8854, 'Waiting for first location update...'

        return render_template('map_modal.html',
                               elderly_name=elderly_data.get('full_name'),
                               lat=lat, lng=lng, address=address)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    if session.get('role') == 'admin':
        return redirect(url_for('admin.dashboard'))
    if session.get('role') == 'bhw':
        return redirect(url_for('bhw.dashboard'))

    links = db.collection('family_elder_link').where(
        'family_user_id', '==', session['user_id']).stream()
    elder_ids = [l.to_dict()['elder_id'] for l in links]

    emergency_contact_count = 0
    if elder_ids:
        contacts = db.collection('emergency_contact').where(
            'elder_id', 'in', elder_ids[:10]).stream()
        emergency_contact_count = sum(1 for _ in contacts)

    # ---- My Loved One (first linked elder) ----
    loved_one = {'name': 'No elder linked yet',
                 'photo_url': None, 'initials': '?', 'elder_id': None}
    elderly_id = None  # ← STORE ELDERLY ID FOR MAP MODAL

    if elder_ids:
        elder_doc = db.collection('elder_profile').document(elder_ids[0]).get()
        if elder_doc.exists:
            elder_data = elder_doc.to_dict()
            elder_name = elder_data.get('full_name', 'Unnamed')
            elderly_id = elder_ids[0]  # ← CAPTURE IT HERE
            loved_one = {
                'name': elder_name,
                'photo_url': elder_data.get('photo_url') or None,
                'initials': _initials(elder_name),
                'elder_id': elderly_id,  # ← PASS IT TO DICT
            }

    # ---- Last Known Location (DEVICE_LOCATION collection) ----
    # start with fallback; overwritten below if real data exists
    location = dict(TEMP_LOCATION)
    location['map_link'] = '#'

    if elder_ids:
        elder_doc = db.collection('elder_profile').document(elder_ids[0]).get()
        device_id = elder_doc.to_dict().get('device_id') if elder_doc.exists else None

        if device_id:
            loc_query = (
                db.collection('DEVICE_LOCATION')
                .where('device_id', '==', device_id)
                .order_by('recorded_at', direction=firestore.Query.DESCENDING)
                .limit(1)
                .stream()
            )
            loc_docs = list(loc_query)
            if loc_docs:
                loc_data = loc_docs[0].to_dict()
                location = {
                    'latitude': loc_data.get('gps_lat'),
                    'longitude': loc_data.get('gps_long'),
                    'address': loc_data.get('location_address', location['address']),
                    'last_updated': loc_data.get('recorded_at', location['last_updated']),
                    'map_link': '#',
                }

    device = {'status': 'Online', 'location_status': 'Available'}
    care_plan = _get_care_plan()
    last_trigger = {'headline': 'No emergency triggered',
                    'subtext': 'You are safe. Keep it up!'}
    next_reminder = {'time': '2:00 PM Today',
                     'medicine_name': 'Vitamin B-Complex',
                     'date': datetime.now().strftime('%B %d, %Y')}

    recent_activity = [
        {'title': 'Device check-in', 'timestamp': 'Today, 9:30 AM', 'status': 'Online',
         'status_class': 'online', 'icon': 'radio', 'icon_class': 'ok'},
        {'title': 'Medicine reminder', 'timestamp': 'Today, 8:00 AM', 'status': 'Sent',
         'status_class': 'sent', 'icon': 'message-circle', 'icon_class': 'info'},
        {'title': 'Contact updated', 'timestamp': 'Yesterday, 6:15 PM', 'status': 'Updated',
         'status_class': 'updated', 'icon': 'users-round', 'icon_class': 'neutral'},
        {'title': 'System update', 'timestamp': 'May 20, 2025 · 10:45 AM', 'status': 'Completed',
         'status_class': 'completed', 'icon': 'bell', 'icon_class': 'warn'},
    ]

    # ---- Recent Alerts (feeds the redesigned dashboard panel) ----
    recent_alerts = [
        {'icon': 'check-circle-2', 'icon_class': 'ok', 'title': 'All is well',
         'subtext': f"{loved_one['name']} is doing well.", 'timestamp': 'Today, 10:29 AM'},
        {'icon': 'bell', 'icon_class': 'warn', 'title': 'Medication Reminder',
         'subtext': 'Reminder sent: Take your medicine.', 'timestamp': 'Today, 8:00 AM'},
        {'icon': 'siren', 'icon_class': 'sos', 'title': 'Emergency',
         'subtext': 'Emergency alert triggered by the elderly user.', 'timestamp': 'Yesterday, 4:15 PM'},
    ]

    return render_template(
        'family/dashboard.html',
        full_name=session['full_name'], role=session['role'],
        device=device, care_plan=care_plan,
        emergency_contact_count=emergency_contact_count,
        last_trigger=last_trigger, next_reminder=next_reminder,
        recent_activity=recent_activity, notification_count=2,
        loved_one=loved_one, location=location, recent_alerts=recent_alerts,
        elderly_id=elderly_id,  # ← PASS THIS TO TEMPLATE FOR MAP MODAL BUTTON
        current_year=datetime.now().year,
    )


if __name__ == '__main__':
    app.run(debug=True)
