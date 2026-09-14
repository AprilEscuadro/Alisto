# ============================================================
# WRITE HOOKS
# Small edits to routes you already have, so history fills up
# from today instead of waiting on the Raspberry Pi.
# ============================================================

# ---- in add_medication_reminder(), just before the return ----

    _log_activity(
        data['elder_id'], 'med_reminder',
        title=f"Reminder added: {data['medicine_name']}",
        detail=f"Set for {data['reminder_time']}",
        badge='Added', badge_class='success',
        actor_user_id=session['user_id'],
    )
    return jsonify(success=True, message='Reminder saved.')


# ---- in update_medication_reminder(), just before the return ----

    _log_activity(
        data['elder_id'], 'med_reminder',
        title=f"Reminder updated: {data['medicine_name']}",
        detail=f"Now set for {data['reminder_time']}",
        badge='Updated', badge_class='warning',
        actor_user_id=session['user_id'],
    )
    return jsonify(success=True, message='Changes saved.')


# ---- in delete_medication_reminder() ----
# Capture the reminder BEFORE deleting it, or there is nothing left to log.

    existing = doc.to_dict() or {}
    try:
        ref.delete()
    except Exception as e:
        return jsonify(success=False, message=f'Error: {str(e)}'), 500

    _log_activity(
        existing.get('elder_id'), 'med_reminder',
        title=f"Reminder removed: {existing.get('medicine_name') or 'Unnamed'}",
        badge='Deleted', badge_class='danger',
        actor_user_id=session['user_id'],
    )
    return jsonify(success=True, message='Reminder deleted.')


# ---- in add_loved_one(), just before the success return ----

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


# ---- in loved_one_update(), just before the success return ----

    _log_activity(
        elder_id, 'device',
        title=f'{full_name} profile updated',
        badge='Updated', badge_class='warning',
        location_address=address,
        actor_user_id=session['user_id'],
    )


# ============================================================
# FOR THE RASPBERRY PI / ALERT PIPELINE
# When an emergency fires, write the alert AND the history row.
# One extra call, and the event shows up for family, BHW and admin
# at the same time.
# ============================================================

    _log_activity(
        elder_id, 'alert',
        title='Emergency alert triggered',
        detail='Detected phrase: Tabang',
        badge='Pending', badge_class='danger',
        location_address=address,
        device_id=serial_number,
    )