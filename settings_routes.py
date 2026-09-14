# ============================================================
# SETTINGS
# Paste into app.py, replacing the existing settings() route.
# Keep it above `if __name__ == '__main__':`.
# ============================================================

# Stored as a map on user_account/{id}, so each family member has their
# own preferences even when several are linked to the same elder.
NOTIFICATION_KEYS = ('emergency_alerts', 'medicine_reminders', 'device_alerts')

# What a brand-new account gets. Everything on: a family member who has
# not touched this page still wants to hear about an emergency.
DEFAULT_NOTIFICATION_SETTINGS = {key: True for key in NOTIFICATION_KEYS}


def _notification_settings(user_id):
    """This user's notification preferences, with defaults filled in.

    THIS IS THE HOOK. Whatever ends up sending alerts — the Flask side,
    the Raspberry Pi, or a push service — should call this and skip the
    send when the matching key is False. Until something calls it, the
    toggles are stored but nothing acts on them.

        settings = _notification_settings(family_user_id)
        if not settings['emergency_alerts']:
            continue  # this family member opted out
    """
    doc = db.collection('user_account').document(user_id).get()
    saved = (doc.to_dict() or {}).get('notification_settings') or {}

    return {
        key: bool(saved.get(key, DEFAULT_NOTIFICATION_SETTINGS[key]))
        for key in NOTIFICATION_KEYS
    }


@app.route('/settings')
def settings():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    return render_template(
        'family/settings.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(),
        settings=_notification_settings(session['user_id']),
        notification_count=0, current_year=datetime.now().year,
    )


@app.route('/settings/save', methods=['POST'])
def save_settings():
    if 'user_id' not in session:
        return jsonify(success=False, message='Please log in first.'), 401

    # The page posts every toggle as 'on' or 'off', so an unticked box is
    # an explicit False rather than a missing key.
    updated = {
        key: request.form.get(key) == 'on' for key in NOTIFICATION_KEYS
    }

    try:
        db.collection('user_account').document(session['user_id']).set({
            'notification_settings': updated,
            'settings_updated_at': firestore.SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        return jsonify(success=False, message=f'Could not save: {str(e)}'), 500

    return jsonify(success=True, message='Settings saved.', settings=updated)