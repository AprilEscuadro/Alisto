"""
My Profile for admins and BHWs: the same behaviour as the family page
(app.py my_profile): edit name / email / phone, change password with the
live checklist, and a profile photo saved as photoBase64 on user_account
(the same field the family page and the mobile app use).

Used by admin_routes.py and bhw_routes.py. Every function returns
(ok, message) so the route can flash it and redirect back.
"""
import base64
import re

from firebase_admin import auth as firebase_auth
from firebase_admin import firestore
from werkzeug.security import check_password_hash, generate_password_hash

from database import db

MAX_PHOTO_BASE64_BYTES = 700 * 1024   # same cap as the family page and the app

# Same rules the admin/BHW checklist shows (8+ characters).
PASSWORD_RULES = [
    (lambda p: len(p) >= 8, 'at least 8 characters'),
    (lambda p: re.search(r'[A-Z]', p), 'an uppercase letter'),
    (lambda p: re.search(r'[a-z]', p), 'a lowercase letter'),
    (lambda p: re.search(r'\d', p), 'a number'),
    (lambda p: re.search(r'[^A-Za-z0-9]', p), 'a special character'),
]


def _initials(name):
    parts = (name or '').split()
    return ''.join(p[0] for p in parts[:2]).upper() or '?'


def _user(user_id):
    ref = db.collection('user_account').document(user_id)
    doc = ref.get()
    return ref, ((doc.to_dict() or {}) if doc.exists else None)


def view(user):
    """What the profile page needs about the signed-in account."""
    name = user.get('full_name') or ''
    return {
        'full_name': name,
        'initials': _initials(name),
        'email': user.get('email') or '',
        'contact_number': user.get('contact_number') or '',
        'photo_base64': user.get('photoBase64') or None,
    }


def update_details(user_id, form):
    """Saves full name, email, and phone. Returns (ok, message, new name)."""
    ref, user = _user(user_id)
    if not user:
        return False, 'Your account could not be found.', None

    full_name = (form.get('full_name') or '').strip()[:100]
    email = (form.get('email') or '').strip().lower()[:120]
    contact_number = (form.get('contact_number') or '').strip()[:20]

    if not full_name:
        return False, 'Full name cannot be empty.', None
    if not email or '@' not in email:
        return False, 'Enter a valid email address.', None
    if contact_number and not re.fullmatch(r'[0-9+\s-]{7,20}', contact_number):
        return False, 'Enter a valid phone number, like 09171234567.', None

    if email != (user.get('email') or '').lower():
        clash = list(db.collection('user_account').where(
            'email', '==', email).limit(1).stream())
        if clash and clash[0].id != user_id:
            return False, 'Another account already uses that email address.', None

    try:
        if 'password_hash' not in user:     # Firebase Auth account: keep its login in sync
            firebase_auth.update_user(
                user_id, email=email, display_name=full_name)
        ref.update({
            'full_name': full_name,
            'email': email,
            'contact_number': contact_number,
            'profile_updated_at': firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        return False, f'Could not save your changes: {e}', None
    return True, 'Your profile has been updated.', full_name


def change_password(user_id, form):
    ref, user = _user(user_id)
    if not user:
        return False, 'Your account could not be found.'

    current = form.get('current_password') or ''
    new = form.get('new_password') or ''
    confirm = form.get('confirm_password') or ''

    if not check_password_hash(user.get('password_hash') or '', current):
        return False, 'Your current password is incorrect.'
    if new != confirm:
        return False, 'New password and confirm password do not match.'
    if new == current:
        return False, 'Your new password must be different from your current one.'
    missing = [label for rule, label in PASSWORD_RULES if not rule(new)]
    if missing:
        return False, 'New password needs ' + ', '.join(missing) + '.'

    ref.update({
        'password_hash': generate_password_hash(new),
        'password_changed_at': firestore.SERVER_TIMESTAMP,
    })
    return True, 'Your password has been updated.'


def save_photo(user_id, encoded):
    encoded = (encoded or '').strip()
    if not encoded:
        return False, 'No photo was selected.'
    if len(encoded) > MAX_PHOTO_BASE64_BYTES:
        return False, 'That photo is too large. Please choose a different one.'
    try:
        base64.b64decode(encoded, validate=True)
    except Exception:
        return False, 'That photo could not be read.'
    db.collection('user_account').document(user_id).set({
        'photoBase64': encoded,
        'photoUpdatedAt': firestore.SERVER_TIMESTAMP,
    }, merge=True)
    return True, 'Your profile photo has been updated.'


def remove_photo(user_id):
    db.collection('user_account').document(user_id).update({
        'photoBase64': firestore.DELETE_FIELD,
        'photoUpdatedAt': firestore.DELETE_FIELD,
    })
    return True, 'Your profile photo has been removed.'
