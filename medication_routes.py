# ============================================================
# MEDICATION REMINDERS
# Paste this into app.py, replacing the existing
# medication_reminders() route. Keep it above
# `if __name__ == '__main__':`.
# ============================================================

# Field-for-field with the mobile app's FirestoreService.addMedicine():
#   elder_id, medicine_name, reminder_time, status, is_active,
#   created_at, updated_at
# The web adds dosage / purpose / frequency / schedule_days /
# voice_reminder_enabled on top. Firestore is schemaless, so the app
# simply ignores them — the same trick the app already uses with
# elder_profile.sex, which Flask ignores.
WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _parse_hour(reminder_time):
    """Leading hour (0-23) from '08:00', '8:00 PM' or '09:00 - 10:00 AM'.

    The app stores reminder_time as free text, so this has to cope with
    whatever it wrote. Returns None when nothing usable is there.
    """
    text = (reminder_time or '').strip()
    match = re.match(r'\s*(\d{1,2})\s*:?\s*(\d{2})?', text)
    if not match:
        return None

    hour = int(match.group(1))
    upper = text.upper()
    if 'PM' in upper and hour < 12:
        hour += 12
    if 'AM' in upper and hour == 12:
        hour = 0
    return hour if 0 <= hour <= 23 else None


def _time_of_day(reminder_time):
    """morning / afternoon / evening, or '' when the time is unreadable."""
    hour = _parse_hour(reminder_time)
    if hour is None:
        return ''
    if hour < 12:
        return 'morning'
    if hour < 18:
        return 'afternoon'
    return 'evening'


def _day_filter(days):
    """Space-separated 'today'/'tomorrow' tags the filter tabs match on.

    A reminder with no days set repeats daily, which is how every
    reminder created in the mobile app behaves — it has no day concept.
    """
    ph_now = datetime.now(timezone(timedelta(hours=8)))
    today = WEEKDAYS[ph_now.weekday()]
    tomorrow = WEEKDAYS[(ph_now.weekday() + 1) % 7]

    if not days:
        return 'today tomorrow'

    tags = []
    if today in days:
        tags.append('today')
    if tomorrow in days:
        tags.append('tomorrow')
    return ' '.join(tags)


def _linked_elders():
    """(elders, elder_lookup) for the signed-in family account."""
    links = list(
        db.collection('family_elder_link')
        .where('family_user_id', '==', session['user_id'])
        .stream()
    )

    elders = []
    lookup = {}
    for link in links:
        link_data = link.to_dict() or {}
        elder_id = link_data.get('elder_id')
        if not elder_id:
            continue

        doc = db.collection('elder_profile').document(elder_id).get()
        if not doc.exists or doc.to_dict().get('deleted_at'):
            continue

        elder = doc.to_dict()
        entry = {
            'id': elder_id,
            'name': elder.get('full_name') or 'Unnamed',
            'photo_url': elder.get('photo_url') or None,
            'relationship': link_data.get('relationship') or 'Family',
        }
        elders.append(entry)
        lookup[elder_id] = entry

    elders.sort(key=lambda e: e['name'].casefold())
    return elders, lookup


@app.route('/medication-reminders')
def medication_reminders():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    elders, lookup = _linked_elders()

    schedules = []
    for elder_id in lookup:
        try:
            docs = list(
                db.collection('medication_reminder')
                .where('elder_id', '==', elder_id)
                .stream()
            )
        except Exception:
            docs = []

        for doc in docs:
            med = doc.to_dict() or {}
            if med.get('deleted_at'):
                continue

            elder = lookup[elder_id]
            reminder_time = med.get('reminder_time') or ''
            days = med.get('schedule_days') or []
            status = med.get('status') or 'Upcoming'

            schedules.append({
                'id': doc.id,
                'elder_id': elder_id,
                'elder_name': elder['name'],
                'elder_photo_url': elder['photo_url'],
                'relationship': elder['relationship'],

                'medicine_name': med.get('medicine_name') or 'Unnamed medicine',
                # Blank for reminders the app created — it has no such field.
                'dosage': med.get('dosage') or '',
                'purpose': med.get('purpose') or 'general use',
                'frequency': med.get('frequency') or 'Daily',
                'voice_reminder_enabled': med.get('voice_reminder_enabled', True),

                'schedule_time': reminder_time or 'No time set',
                'schedule_day': ', '.join(days) if days else 'Every day',
                'time_value': reminder_time if _parse_hour(reminder_time) is not None else '',
                'days_csv': ','.join(days),

                'time_of_day': _time_of_day(reminder_time),
                'day_filter': _day_filter(days),

                'status': status,
                'status_class': 'notified' if status == 'Notified' else 'upcoming',
                'sort_hour': _parse_hour(reminder_time),
            })

    # Unreadable times sink to the bottom rather than crashing the sort.
    schedules.sort(key=lambda s: (s['sort_hour'] is None, s['sort_hour'] or 0))

    return render_template(
        'family/medication_reminders.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(),
        schedules=schedules, elders=elders,
        today_count=sum(1 for s in schedules if 'today' in s['day_filter']),
        tomorrow_count=sum(1 for s in schedules if 'tomorrow' in s['day_filter']),
        total_schedule_count=len(schedules),
        notification_count=0, current_year=datetime.now().year,
    )


def _reminder_form():
    """Reads and validates the add/edit form. Returns (data, error)."""
    elder_id = request.form.get('elder_id', '').strip()
    medicine_name = request.form.get('medicine_name', '').strip()
    reminder_time = request.form.get('reminder_time', '').strip()

    if not elder_id:
        return None, 'Choose which loved one this reminder is for.'
    if not medicine_name:
        return None, 'Enter the medicine name.'
    if not reminder_time:
        return None, 'Set the time this reminder should go off.'

    # Only elders this account is actually linked to.
    _, lookup = _linked_elders()
    if elder_id not in lookup:
        return None, 'That loved one is not linked to your account.'

    days = [d for d in request.form.getlist('schedule_days') if d in WEEKDAYS]

    return {
        'elder_id': elder_id,
        'medicine_name': medicine_name,
        'reminder_time': reminder_time,
        'dosage': request.form.get('dosage', '').strip(),
        'purpose': request.form.get('purpose', '').strip(),
        'frequency': request.form.get('frequency', 'Daily').strip() or 'Daily',
        'schedule_days': days,
        'voice_reminder_enabled': request.form.get('voice_reminder_enabled') == 'on',
    }, None


@app.route('/medication-reminders/add', methods=['POST'])
def add_medication_reminder():
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    data, error = _reminder_form()
    if error:
        return jsonify(success=False, message=error), 400

    try:
        db.collection('medication_reminder').add({
            **data,
            # The app reads these two. Keep writing them or reminders made
            # on the web show up blank in the app.
            'status': 'Upcoming',
            'is_active': True,
            'created_at': firestore.SERVER_TIMESTAMP,
            'updated_at': firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message='Reminder saved.')


@app.route('/medication-reminders/<reminder_id>/update', methods=['POST'])
def update_medication_reminder(reminder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    ref = db.collection('medication_reminder').document(reminder_id)
    doc = ref.get()
    if not doc.exists:
        return jsonify(success=False, message='That reminder no longer exists.'), 404

    # The reminder must belong to an elder this account is linked to.
    _, lookup = _linked_elders()
    if (doc.to_dict() or {}).get('elder_id') not in lookup:
        return jsonify(success=False, message='You cannot edit that reminder.'), 403

    data, error = _reminder_form()
    if error:
        return jsonify(success=False, message=error), 400

    try:
        # status and is_active are left alone — the device owns them.
        ref.update({**data, 'updated_at': firestore.SERVER_TIMESTAMP})
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message='Changes saved.')


@app.route('/medication-reminders/<reminder_id>/delete', methods=['POST'])
def delete_medication_reminder(reminder_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    ref = db.collection('medication_reminder').document(reminder_id)
    doc = ref.get()
    if not doc.exists:
        return jsonify(success=False, message='That reminder no longer exists.'), 404

    _, lookup = _linked_elders()
    if (doc.to_dict() or {}).get('elder_id') not in lookup:
        return jsonify(success=False, message='You cannot delete that reminder.'), 403

    try:
        # A hard delete, matching FirestoreService.deleteMedicine() in the
        # app, so a reminder removed on one side disappears on the other.
        ref.delete()
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    return jsonify(success=True, message='Reminder deleted.')