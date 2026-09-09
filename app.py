from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from database import db
from firebase_admin import firestore
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'alisto-secret-key-change-this-later'

MAX_FAMILY_PER_DEVICE = 3


def _get_care_plan():
    return {'status': 'Active', 'days_left': 12,
            'renew_date': 'July 20, 2026', 'price': 55}


def _first_name(full_name: str) -> str:
    full_name = (full_name or '').strip()
    return full_name.split(' ')[0] if full_name else 'this elder'


def _family_link_count(elder_id: str) -> int:
    links = db.collection('family_elder_link').where('elder_id', '==', elder_id).stream()
    count = 0
    for link in links:
        user_doc = db.collection('user_account').document(link.to_dict()['family_user_id']).get()
        if user_doc.exists:
            u = user_doc.to_dict()
            if u.get('role') == 'family' and not u.get('deleted_at'):
                count += 1
    return count


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
        elder_doc = db.collection('elder_profile').document(elder_id).get() if elder_id else None
        elder = elder_doc.to_dict() if elder_doc and elder_doc.exists and not elder_doc.to_dict().get('deleted_at') else None

        if not elder:
            return jsonify(valid=False, message='This Device ID cannot be registered right now. Please contact an administrator.')

        requesting_role = (data.get('role') or 'family').strip()
        family_count = _family_link_count(elder_id)

        if requesting_role == 'family' and family_count >= MAX_FAMILY_PER_DEVICE:
            return jsonify(
                valid=True, already_registered=True, can_join=False,
                elder_id=elder_id, elder_display_name=_first_name(elder.get('full_name')),
                family_count=family_count, family_limit=MAX_FAMILY_PER_DEVICE,
                message=f"This device already has the maximum of {MAX_FAMILY_PER_DEVICE} linked family members."
            )

        return jsonify(
            valid=True, already_registered=True, can_join=True,
            elder_id=elder_id, elder_display_name=_first_name(elder.get('full_name')),
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
        barangay_assigned = request.form.get('barangay_assigned', 'Sudlon II').strip()

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
        if role not in ('family', 'bhw', 'admin'):
            return jsonify(success=False, message='Invalid role selected.'), 400
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
            db.collection('user_account').where('email', '==', email).limit(1).stream()
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
                    return jsonify(success=False, message='This Device ID was just registered by someone else. Please refresh and try again.'), 409

                elder_id = _create_account_and_elder(
                    full_name, email, password, role, contact_number,
                    barangay_assigned, elder_full_name, dob,
                    ', '.join(filter(None, [house_no, street, elder_barangay, city, province, zip_code])),
                    relationship, serial_number
                )
            else:
                if not device.get('is_registered') or device.get('elder_id') != existing_elder_id:
                    return jsonify(success=False, message='This device is no longer linked to that elder. Please re-check the Device ID.'), 409

                elder_doc = db.collection('elder_profile').document(existing_elder_id).get()
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

    return render_template('register.html')


def _create_account_and_elder(full_name, email, password, role, contact_number,
                              barangay_assigned, elder_full_name, dob, address,
                              relationship, serial_number):
    password_hash = generate_password_hash(password)
    user_ref = db.collection('user_account').document()
    user_ref.set({
        'full_name': full_name, 'email': email, 'password_hash': password_hash,
        'role': role, 'contact_number': contact_number,
        'created_at': firestore.SERVER_TIMESTAMP, 'deleted_at': None, 'is_archived': 0
    })
    user_id = user_ref.id

    if role == 'bhw':
        db.collection('bhw_profile').add({'user_id': user_id, 'barangay_assigned': barangay_assigned})

    elder_ref = db.collection('elder_profile').document()
    elder_ref.set({
        'full_name': elder_full_name, 'date_of_birth': dob, 'address': address,
        'created_at': firestore.SERVER_TIMESTAMP, 'deleted_at': None, 'is_archived': 0
    })
    elder_id = elder_ref.id

    db.collection('family_elder_link').add({
        'family_user_id': user_id, 'elder_id': elder_id, 'relationship': relationship
    })

    db.collection('device').document(serial_number).update({
        'elder_id': elder_id, 'is_registered': True, 'registered_at': firestore.SERVER_TIMESTAMP
    })
    return elder_id


def _create_account_and_join(full_name, email, password, role, contact_number,
                             barangay_assigned, existing_elder_id, relationship):
    password_hash = generate_password_hash(password)
    user_ref = db.collection('user_account').document()
    user_ref.set({
        'full_name': full_name, 'email': email, 'password_hash': password_hash,
        'role': role, 'contact_number': contact_number,
        'created_at': firestore.SERVER_TIMESTAMP, 'deleted_at': None, 'is_archived': 0
    })
    user_id = user_ref.id

    if role == 'bhw':
        db.collection('bhw_profile').add({'user_id': user_id, 'barangay_assigned': barangay_assigned})

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
            db.collection('user_account').where('email', '==', email).limit(1).stream()
        )
        user_doc = matches[0] if matches else None
        user = user_doc.to_dict() if user_doc else None

        if user and not user.get('deleted_at') and check_password_hash(user['password_hash'], password):
            session['user_id'] = user_doc.id
            session['full_name'] = user['full_name']
            session['role'] = user['role']
            flash(f"Welcome back, {user['full_name']}!", 'success')
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

    return render_template(
        'family/my_loved_ones.html',
        full_name=session['full_name'], role=session['role'],
        loved_ones=[], online_count=0, offline_count=0,
        attention_count=0, notification_count=0,
        current_year=datetime.now().year,
    )


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

    pagination = {'start': 0, 'end': 0, 'total': 0, 'current_page': 1, 'total_pages': 1, 'pages': [1]}

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

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    links = db.collection('family_elder_link').where('family_user_id', '==', session['user_id']).stream()
    elder_ids = [l.to_dict()['elder_id'] for l in links]

    emergency_contact_count = 0
    if elder_ids:
        contacts = db.collection('emergency_contact').where('elder_id', 'in', elder_ids[:10]).stream()
        emergency_contact_count = sum(1 for _ in contacts)

    device = {'status': 'Online', 'location_status': 'Available'}
    care_plan = _get_care_plan()
    last_trigger = {'headline': 'No emergency triggered', 'subtext': 'You are safe. Keep it up!'}
    next_reminder = {'time': '2:00 PM Today', 'medicine_name': 'Vitamin B-Complex'}
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

    return render_template(
        'family/dashboard.html',
        full_name=session['full_name'], role=session['role'],
        device=device, care_plan=care_plan,
        emergency_contact_count=emergency_contact_count,
        last_trigger=last_trigger, next_reminder=next_reminder,
        recent_activity=recent_activity, notification_count=2,
        current_year=datetime.now().year,
    )


if __name__ == '__main__':
    app.run(debug=True)