from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_db_connection
import sqlite3

app = Flask(__name__)
# gamit lang ni for session, ilisi sa real deployment
app.secret_key = 'alisto-secret-key-change-this-later'

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

        # STEP 3: Elder Personal Info
        elder_full_name = request.form.get('senior_full_name', '').strip()
        dob = request.form.get('dob', '').strip()
        relationship = request.form.get('relationship', '').strip()

        # STEP 4: Elder Address
        house_no = request.form.get('house_no', '').strip()
        street = request.form.get('street', '').strip()
        elder_barangay = request.form.get('barangay', '').strip()
        city = request.form.get('city', '').strip()
        province = request.form.get('province', '').strip()
        zip_code = request.form.get('zip_code', '').strip()

        # ---------- VALIDATION ----------
        if not full_name or not email or not password or not role:
            return jsonify(success=False, message='Palihug i-fill up ang tanan required fields sa account.'), 400

        if password != confirm_password:
            return jsonify(success=False, message='Dili magtugma ang password ug confirm password.'), 400

        if role not in ('family', 'bhw', 'admin'):
            return jsonify(success=False, message='Invalid role selected.'), 400

        if not serial_number:
            return jsonify(success=False, message='Palihug i-enter ang Device ID.'), 400

        if not elder_full_name or not dob:
            return jsonify(success=False, message='Palihug i-fill up ang personal information sa elderly.'), 400

        address = ', '.join(
            filter(None, [house_no, street, elder_barangay, city, province, zip_code]))

        conn = get_db_connection()
        try:
            # 1. Create user account
            password_hash = generate_password_hash(password)
            cursor = conn.execute(
                'INSERT INTO user_account (full_name, email, password_hash, role, contact_number) VALUES (?, ?, ?, ?, ?)',
                (full_name, email, password_hash, role, contact_number)
            )
            user_id = cursor.lastrowid

            # 2. If BHW, insert into bhw_profile
            if role == 'bhw':
                conn.execute(
                    'INSERT INTO bhw_profile (user_id, barangay_assigned) VALUES (?, ?)',
                    (user_id, barangay_assigned)
                )

            # 3. Create elder profile
            elder_cursor = conn.execute(
                'INSERT INTO elder_profile (full_name, date_of_birth, address) VALUES (?, ?, ?)',
                (elder_full_name, dob, address)
            )
            elder_id = elder_cursor.lastrowid

            # 4. Link family user to elder
            conn.execute(
                'INSERT INTO family_elder_link (family_user_id, elder_id, relationship) VALUES (?, ?, ?)',
                (user_id, elder_id, relationship)
            )

            # 5. Register device, linked to elder
            conn.execute(
                'INSERT INTO device (elder_id, serial_number) VALUES (?, ?)',
                (elder_id, serial_number)
            )

            conn.commit()
            return jsonify(success=True, message='Registration successful!')

        except sqlite3.IntegrityError as e:
            conn.rollback()
            error_msg = str(e)
            if 'email' in error_msg:
                return jsonify(success=False, message='Naa nay account naka-register niining email.'), 400
            elif 'serial_number' in error_msg:
                return jsonify(success=False, message='Naa nay naka-register niining Device ID.'), 400
            else:
                return jsonify(success=False, message='Duplicate entry error.'), 400
        except Exception as e:
            conn.rollback()
            return jsonify(success=False, message=f'Error: {str(e)}'), 500
        finally:
            conn.close()

    return render_template('register.html')


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
            flash('Sayop ang email or password.', 'error')
            return redirect(url_for('login'))

    return render_template('login.html')

# ---------- LOGOUT ----------


@app.route('/logout')
def logout():
    session.clear()
    flash('Na-logout ka na.', 'success')
    return redirect(url_for('login'))

# ---------- DASHBOARD (placeholder, magbuhat pa ta separate per role) ----------


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        flash('Palihug login una.', 'error')
        return redirect(url_for('login'))
    return render_template('dashboard.html', full_name=session['full_name'], role=session['role'])


if __name__ == '__main__':
    app.run(debug=True)
