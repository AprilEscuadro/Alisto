from flask import Flask, render_template, request, redirect, url_for, session, flash
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
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        role = request.form.get('role', '')
        contact_number = request.form.get('contact_number', '').strip()
        barangay_assigned = request.form.get(
            'barangay_assigned', 'Sudlon II').strip()

        # basic validation
        if not full_name or not email or not password or not role:
            flash('Palihug i-fill up ang tanan required fields.', 'error')
            return redirect(url_for('register'))

        if password != confirm_password:
            flash('Dili magtugma ang password ug confirm password.', 'error')
            return redirect(url_for('register'))

        if role not in ('family', 'bhw', 'admin'):
            flash('Invalid role selected.', 'error')
            return redirect(url_for('register'))

        conn = get_db_connection()
        try:
            password_hash = generate_password_hash(password)
            cursor = conn.execute(
                'INSERT INTO user_account (full_name, email, password_hash, role, contact_number) VALUES (?, ?, ?, ?, ?)',
                (full_name, email, password_hash, role, contact_number)
            )
            user_id = cursor.lastrowid

            # if BHW, also insert into bhw_profile
            if role == 'bhw':
                conn.execute(
                    'INSERT INTO bhw_profile (user_id, barangay_assigned) VALUES (?, ?)',
                    (user_id, barangay_assigned)
                )

            conn.commit()
            flash('Success! Pwede na ka mag-login.', 'success')
            return redirect(url_for('login'))

        except sqlite3.IntegrityError:
            flash('Naa nay account naka-register niining email.', 'error')
            return redirect(url_for('register'))
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
