from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_db_connection
import sqlite3

app = Flask(__name__)
# used only for session, change this in real deployment
app.secret_key = 'alisto-secret-key-change-this-later'

# Max number of 'family' role accounts allowed to link to the same elder
# (i.e. same device). BHW joins don't count against this.
MAX_FAMILY_PER_DEVICE = 3

# Shared care-plan placeholder used across every family-side page's sidebar
# card. TODO: swap for a real billing/care_plan query once that table exists.


def _get_care_plan():
    return {'status': 'Active', 'days_left': 12,
            'renew_date': 'July 20, 2026', 'price': 55}


@app.route('/check_device', methods=['POST'])
def check_device():
    data = request.get_json()
    serial_number = (data.get('device_id') or '').strip()

    if not serial_number:
        return jsonify(valid=False, message='Please enter a Device ID.'), 400

    conn = get_db_connection()
    device = conn.execute(
        'SELECT * FROM device WHERE serial_number = ?', (serial_number,)
    ).fetchone()

    if not device:
        conn.close()
        return jsonify(valid=False, message='No matching Device ID found in the system. Please check the QR code or serial number.')

    # Device exists but is already claimed by an elder -> offer "join" instead
    # of a hard block, per Option B (multiple family members per device).
    if device['is_registered']:
        elder = conn.execute(
            'SELECT id, full_name FROM elder_profile WHERE id = ? AND deleted_at IS NULL',
            (device['elder_id'],)
        ).fetchone()

        if not elder:
            # Device flagged registered but elder record missing/deleted —
            # treat as unclaimable rather than let anyone silently join a ghost profile.
            conn.close()
            return jsonify(valid=False, message='This Device ID cannot be registered right now. Please contact an administrator.')

        # requesting_role lets the frontend ask on behalf of either a family
        # signup or a BHW signup — BHW never counts against/against-checks the cap.
        requesting_role = (data.get('role') or 'family').strip()

        family_count = _family_link_count(conn, elder['id'])
        conn.close()

        if requesting_role == 'family' and family_count >= MAX_FAMILY_PER_DEVICE:
            return jsonify(
                valid=True,
                already_registered=True,
                can_join=False,
                elder_id=elder['id'],
                elder_display_name=_first_name(elder['full_name']),
                family_count=family_count,
                family_limit=MAX_FAMILY_PER_DEVICE,
                message=f"This device already has the maximum of {MAX_FAMILY_PER_DEVICE} linked family members."
            )

        return jsonify(
            valid=True,
            already_registered=True,
            can_join=True,
            elder_id=elder['id'],
            # Only expose the first name for confirmation purposes — avoid
            # leaking the full identity of someone else's family member.
            elder_display_name=_first_name(elder['full_name']),
            family_count=family_count,
            family_limit=MAX_FAMILY_PER_DEVICE,
            message='This device is already linked to an ALISTO user.'
        )

    conn.close()
    return jsonify(valid=True, already_registered=False, message='Device ID verified!')


def _first_name(full_name: str) -> str:
    full_name = (full_name or '').strip()
    return full_name.split(' ')[0] if full_name else 'this elder'


def _family_link_count(conn, elder_id: int) -> int:
    """How many 'family' role users are already linked to this elder.
    BHW links are intentionally excluded from this count."""
    row = conn.execute(
        '''SELECT COUNT(*) AS cnt
           FROM family_elder_link fel
           JOIN user_account ua ON ua.id = fel.family_user_id
           WHERE fel.elder_id = ? AND ua.role = 'family' AND ua.deleted_at IS NULL''',
        (elder_id,)
    ).fetchone()
    return row['cnt']


# ---------- LANDING ----------


@app.route('/')
def landing():
    return render_template('landing.html')

# ---------- REGISTER ----------


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        # STEP 1: Account Info
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        role = request.form.get('role', '')
        contact_number = request.form.get('contact_number', '').strip()
        barangay_assigned = request.form.get(
            'barangay_assigned', 'Sudlon II').strip()

        # STEP 2: Device
        serial_number = request.form.get('device_id', '').strip()

        # join_mode: 'claim' (default, brand-new elder + device) or
        # 'join' (link to an elder that already owns this device).
        join_mode = request.form.get('join_mode', 'claim').strip()
        existing_elder_id_raw = request.form.get(
            'existing_elder_id', '').strip()

        # STEP 3: Elder Personal Info (only required in 'claim' mode)
        elder_full_name = request.form.get('senior_full_name', '').strip()
        dob = request.form.get('dob', '').strip()
        relationship = request.form.get('relationship', '').strip()

        # STEP 4: Elder Address (only required in 'claim' mode)
        house_no = request.form.get('house_no', '').strip()
        street = request.form.get('street', '').strip()
        elder_barangay = request.form.get('barangay', '').strip()
        city = request.form.get('city', '').strip()
        province = request.form.get('province', '').strip()
        zip_code = request.form.get('zip_code', '').strip()

        # ---------- VALIDATION (shared) ----------
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
        else:  # join
            if not existing_elder_id_raw.isdigit():
                return jsonify(success=False, message='Missing elder reference for joining.'), 400
            if not relationship:
                return jsonify(success=False, message='Please specify your relationship to the elder.'), 400

        conn = get_db_connection()
        try:
            device = conn.execute(
                'SELECT * FROM device WHERE serial_number = ?', (serial_number,)
            ).fetchone()

            if not device:
                conn.close()
                return jsonify(success=False, message='No matching Device ID found in the system.'), 400

            if join_mode == 'claim':
                # 0. Re-validate device is still unclaimed right before writing
                # (handles the race where someone else claimed it in between).
                if device['is_registered']:
                    conn.close()
                    return jsonify(success=False, message='This Device ID was just registered by someone else. Please refresh and try again.'), 409

                elder_id = _create_account_and_elder(
                    conn, full_name, email, password, role, contact_number,
                    barangay_assigned, elder_full_name, dob,
                    ', '.join(
                        filter(None, [house_no, street, elder_barangay, city, province, zip_code])),
                    relationship, serial_number
                )
            else:
                # join mode: device must still be registered, and must still
                # point at the elder the client says it does.
                existing_elder_id = int(existing_elder_id_raw)
                if not device['is_registered'] or device['elder_id'] != existing_elder_id:
                    conn.close()
                    return jsonify(success=False, message='This device is no longer linked to that elder. Please re-check the Device ID.'), 409

                elder = conn.execute(
                    'SELECT id FROM elder_profile WHERE id = ? AND deleted_at IS NULL',
                    (existing_elder_id,)
                ).fetchone()
                if not elder:
                    conn.close()
                    return jsonify(success=False, message='That elder profile no longer exists.'), 400

                # Re-check the cap right before writing — someone else may have
                # taken the last slot between check_device and this submit.
                if role == 'family' and _family_link_count(conn, existing_elder_id) >= MAX_FAMILY_PER_DEVICE:
                    conn.close()
                    return jsonify(success=False, message=f'This device already has the maximum of {MAX_FAMILY_PER_DEVICE} linked family members.'), 409

                elder_id = _create_account_and_join(
                    conn, full_name, email, password, role, contact_number,
                    barangay_assigned, existing_elder_id, relationship
                )

            conn.commit()
            return jsonify(success=True, message='Registration successful!')

        except sqlite3.IntegrityError as e:
            conn.rollback()
            error_msg = str(e)
            if 'email' in error_msg:
                return jsonify(success=False, message='An account with this email already exists.'), 400
            else:
                return jsonify(success=False, message='Duplicate entry error.'), 400
        except Exception as e:
            conn.rollback()
            return jsonify(success=False, message=f'Error: {str(e)}'), 500
        finally:
            conn.close()

    return render_template('register.html')


def _create_account_and_elder(conn, full_name, email, password, role, contact_number,
                              barangay_assigned, elder_full_name, dob, address,
                              relationship, serial_number):
    """Original flow: new user + new elder + claim device."""
    password_hash = generate_password_hash(password)
    cursor = conn.execute(
        'INSERT INTO user_account (full_name, email, password_hash, role, contact_number) VALUES (?, ?, ?, ?, ?)',
        (full_name, email, password_hash, role, contact_number)
    )
    user_id = cursor.lastrowid

    if role == 'bhw':
        conn.execute(
            'INSERT INTO bhw_profile (user_id, barangay_assigned) VALUES (?, ?)',
            (user_id, barangay_assigned)
        )

    elder_cursor = conn.execute(
        'INSERT INTO elder_profile (full_name, date_of_birth, address) VALUES (?, ?, ?)',
        (elder_full_name, dob, address)
    )
    elder_id = elder_cursor.lastrowid

    conn.execute(
        'INSERT INTO family_elder_link (family_user_id, elder_id, relationship) VALUES (?, ?, ?)',
        (user_id, elder_id, relationship)
    )

    conn.execute(
        '''UPDATE device
           SET elder_id = ?, is_registered = 1, registered_at = CURRENT_TIMESTAMP
           WHERE serial_number = ?''',
        (elder_id, serial_number)
    )
    return elder_id


def _create_account_and_join(conn, full_name, email, password, role, contact_number,
                             barangay_assigned, existing_elder_id, relationship):
    """New flow: new user, existing elder — just link them. Device stays as-is."""
    password_hash = generate_password_hash(password)
    cursor = conn.execute(
        'INSERT INTO user_account (full_name, email, password_hash, role, contact_number) VALUES (?, ?, ?, ?, ?)',
        (full_name, email, password_hash, role, contact_number)
    )
    user_id = cursor.lastrowid

    if role == 'bhw':
        conn.execute(
            'INSERT INTO bhw_profile (user_id, barangay_assigned) VALUES (?, ?)',
            (user_id, barangay_assigned)
        )

    # Avoid duplicate links if this user already somehow linked to this elder
    existing_link = conn.execute(
        'SELECT id FROM family_elder_link WHERE family_user_id = ? AND elder_id = ?',
        (user_id, existing_elder_id)
    ).fetchone()
    if not existing_link:
        conn.execute(
            'INSERT INTO family_elder_link (family_user_id, elder_id, relationship) VALUES (?, ?, ?)',
            (user_id, existing_elder_id, relationship)
        )

    return existing_elder_id


# ---------- LOGIN ----------


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        conn = get_db_connection()
        user = conn.execute(
            'SELECT * FROM user_account WHERE email = ? AND deleted_at IS NULL',
            (email,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
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

    from datetime import datetime

    conn = get_db_connection()

    # TODO: Replace with real queries for loved ones data
    loved_ones = []
    online_count = 0
    offline_count = 0
    attention_count = 0
    notification_count = 0

    conn.close()

    return render_template(
        'family/my_loved_ones.html',
        full_name=session['full_name'],
        role=session['role'],
        loved_ones=loved_ones,
        online_count=online_count,
        offline_count=offline_count,
        attention_count=attention_count,
        notification_count=notification_count,
        current_year=datetime.now().year,
    )


# ---------- ALERTS ----------


@app.route('/alerts')
def alerts():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    from datetime import datetime

    conn = get_db_connection()

    # TODO: Replace with a real query against the emergency_alert table,
    # scoped to elders linked to this family user via family_elder_link.
    alerts_list = []
    emergency_count = 0
    false_alarm_count = 0
    notification_count = 0

    conn.close()

    return render_template(
        'family/alerts.html',
        full_name=session['full_name'],
        role=session['role'],
        care_plan=_get_care_plan(),
        alerts=alerts_list,
        emergency_count=emergency_count,
        false_alarm_count=false_alarm_count,
        notification_count=notification_count,
        current_year=datetime.now().year,
    )


@app.route('/alerts/<int:alert_id>')
def alert_details(alert_id):
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    # TODO: Build a dedicated alert-details template and real query.
    flash('Alert details page not implemented yet.', 'info')
    return redirect(url_for('alerts'))


# ---------- MEDICATION REMINDERS ----------


@app.route('/medication-reminders')
def medication_reminders():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    from datetime import datetime

    conn = get_db_connection()

    # TODO: Replace with a real query against the medicine_reminder table,
    # scoped to elders linked to this family user via family_elder_link.
    schedules = []
    today_count = 0
    tomorrow_count = 0
    total_schedule_count = 0
    notification_count = 0

    conn.close()

    return render_template(
        'family/medication_reminders.html',
        full_name=session['full_name'],
        role=session['role'],
        care_plan=_get_care_plan(),
        schedules=schedules,
        today_count=today_count,
        tomorrow_count=tomorrow_count,
        total_schedule_count=total_schedule_count,
        notification_count=notification_count,
        current_year=datetime.now().year,
    )


# ---------- HISTORY ----------


@app.route('/history')
def history():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    from datetime import datetime

    conn = get_db_connection()

    # TODO: Replace with a real query unioning alerts, medicine reminders,
    # and device activity logs, scoped to this family user's linked elders.
    activities = []
    alert_count = 0
    med_reminder_count = 0
    device_activity_count = 0
    notification_count = 0
    date_range_label = 'This Month'
    pagination = {
        'start': 0,
        'end': 0,
        'total': 0,
        'current_page': 1,
        'total_pages': 1,
        'pages': [1],
    }

    conn.close()

    return render_template(
        'family/history.html',
        full_name=session['full_name'],
        role=session['role'],
        care_plan=_get_care_plan(),
        activities=activities,
        alert_count=alert_count,
        med_reminder_count=med_reminder_count,
        device_activity_count=device_activity_count,
        notification_count=notification_count,
        date_range_label=date_range_label,
        pagination=pagination,
        current_year=datetime.now().year,
    )


@app.route('/history/<int:activity_id>')
def activity_details(activity_id):
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    # TODO: Build a dedicated activity-details template and real query.
    flash('Activity details page not implemented yet.', 'info')
    return redirect(url_for('history'))


# ---------- SETTINGS ----------


@app.route('/settings')
def settings():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    from datetime import datetime

    conn = get_db_connection()

    # TODO: Replace with a real query against a user_settings/preferences
    # table once one exists; for now default everything to enabled.
    user_settings = {
        'emergency_alerts': True,
        'medicine_reminders': True,
        'device_alerts': True,
        'location_updates': True,
    }
    notification_count = 0

    conn.close()

    return render_template(
        'family/settings.html',
        full_name=session['full_name'],
        role=session['role'],
        care_plan=_get_care_plan(),
        settings=user_settings,
        notification_count=notification_count,
        current_year=datetime.now().year,
    )


@app.route('/settings/change-password')
def change_password():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    # TODO: Build a dedicated change-password template and form handling.
    flash('Change Password page not implemented yet.', 'info')
    return redirect(url_for('settings'))


# ---------- HELP & SUPPORT ----------


@app.route('/help-support')
def help_support():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    from datetime import datetime

    # TODO: Move FAQs and contact info into the database once there's an
    # admin-editable content table; hardcoded for now so the page renders.
    faqs = [
        {
            'question': 'How do I set up the ALISTO device for my loved one?',
            'answer': 'Follow the step-by-step pairing guide in the User Guide, '
            'then confirm the Device ID matches the one on your registration.'
        },
        {
            'question': 'What happens when an emergency alert is triggered?',
            'answer': 'ALISTO sends an SMS to your registered emergency contacts '
            'and marks the alert on your dashboard with the elder\'s last known location.'
        },
        {
            'question': 'Can more than one family member link to the same elder?',
            'answer': f'Yes, up to {MAX_FAMILY_PER_DEVICE} family accounts can be linked to the same device/elder.'
        },
    ]

    contact_info = {
        'phone': '+63 917 000 0000',
        'phone_hours': 'Mon–Fri, 8:00 AM – 5:00 PM',
        'email': 'support@alisto.ph',
        'address_line1': 'Barangay Sudlon II Hall',
        'address_line2': 'Cebu City, Cebu, Philippines',
    }

    notification_count = 0

    return render_template(
        'family/help_support.html',
        full_name=session['full_name'],
        role=session['role'],
        care_plan=_get_care_plan(),
        faqs=faqs,
        contact_info=contact_info,
        notification_count=notification_count,
        current_year=datetime.now().year,
    )


@app.route('/help-support/faqs')
def faqs():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    # TODO: Build a dedicated full-FAQs template.
    flash('Full FAQs page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/help-support/contact')
def contact_support():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    # TODO: Build a dedicated contact-support form template.
    flash('Contact Support page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/help-support/user-guide')
def user_guide():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    # TODO: Build a dedicated user-guide template.
    flash('User Guide page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/help-support/report-issue')
def report_issue():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    # TODO: Build a dedicated report-issue form template.
    flash('Report Issue page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/help-support/about')
def about():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    # TODO: Build a dedicated about template.
    flash('About page not implemented yet.', 'info')
    return redirect(url_for('help_support'))


@app.route('/loved-ones/<int:elder_id>')
def loved_one_details(elder_id):
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    # TODO: Build a dedicated loved-one-details template and real query.
    flash('Loved one details page not implemented yet.', 'info')
    return redirect(url_for('my_loved_ones'))


# ---------- DASHBOARD (placeholder, will build separate per role) ----------


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    from datetime import datetime

    conn = get_db_connection()

    # Real: how many emergency contacts exist for the elder(s) linked to this user
    row = conn.execute(
        '''SELECT COUNT(*) AS cnt
           FROM emergency_contact ec
           JOIN family_elder_link fel ON fel.elder_id = ec.elder_id
           WHERE fel.family_user_id = ?''',
        (session['user_id'],)
    ).fetchone()
    emergency_contact_count = row['cnt'] if row else 0
    conn.close()

    # TODO: the blocks below are placeholder/mock data — swap in real queries
    # once device telemetry (device_health_log/device_location), care-plan
    # billing, and medicine_reminder scheduling are hooked up.
    device = {'status': 'Online', 'location_status': 'Available'}
    care_plan = _get_care_plan()
    last_trigger = {'headline': 'No emergency triggered',
                    'subtext': 'You are safe. Keep it up!'}
    next_reminder = {'time': '2:00 PM Today',
                     'medicine_name': 'Vitamin B-Complex'}
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
    notification_count = 2

    return render_template(
        'family/dashboard.html',
        full_name=session['full_name'],
        role=session['role'],
        device=device,
        care_plan=care_plan,
        emergency_contact_count=emergency_contact_count,
        last_trigger=last_trigger,
        next_reminder=next_reminder,
        recent_activity=recent_activity,
        notification_count=notification_count,
        current_year=datetime.now().year,
    )


if __name__ == '__main__':
    app.run(debug=True)
