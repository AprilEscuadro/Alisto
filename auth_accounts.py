"""Sign-in passwords live in Firebase Authentication for every role.

Family accounts have always been Firebase Auth users. BHW, responder and
admin accounts used to keep a `password_hash` on their user_account doc
instead, which meant Firebase couldn't send them a reset email. New
sign-ups now go straight to Firebase Auth, and older accounts are moved
over the first time they sign in (or ask for a password reset), with the
Firebase uid = their existing user_account doc id, so nothing else in the
database has to change.

While an account still has a `password_hash` it is a "legacy" account:
  * login checks the hash; on success the same password is saved in
    Firebase Auth and the hash is removed (fully moved over).
  * forgot password creates its Firebase Auth user with a random password
    but KEEPS the hash, so a stranger requesting a reset can't lock the
    real owner out. If the owner then resets through Firebase, login sees
    the hash no longer matches, tries Firebase, and finishes the move.
"""
import os
import secrets

import requests
from firebase_admin import auth as firebase_auth
from firebase_admin import firestore

from database import db

_IDENTITY = 'https://identitytoolkit.googleapis.com/v1/accounts:'


def _api_key():
    return os.environ.get('FIREBASE_WEB_API_KEY', '')


def verify_password(email, password):
    """True if Firebase Auth accepts this email + password."""
    if not email or not password:
        return False
    try:
        resp = requests.post(f'{_IDENTITY}signInWithPassword?key={_api_key()}', json={
            'email': email, 'password': password, 'returnSecureToken': True,
        }, timeout=15)
        return resp.ok
    except requests.RequestException:
        return False


def send_reset_email(email):
    """Firebase emails its own password reset link (no mail setup needed)."""
    try:
        resp = requests.post(f'{_IDENTITY}sendOobCode?key={_api_key()}', json={
            'requestType': 'PASSWORD_RESET', 'email': email,
        }, timeout=15)
        if not resp.ok:
            print(f'[auth] Firebase reset email failed for {email}: {resp.text}')
        return resp.ok
    except requests.RequestException as e:
        print(f'[auth] Firebase reset email error for {email}: {e}')
        return False


def create_user(uid, email, password, display_name):
    """New sign-up: a Firebase Auth user whose uid is the user_account doc id."""
    firebase_auth.create_user(uid=uid, email=email, password=password,
                              display_name=display_name or None)


def ensure_firebase_user(user_id, user, password=None):
    """Makes sure a legacy account has a Firebase Auth user (uid = doc id).

    With `password`: sets it as the Firebase password. Without: a random one
    (forgot-password case). Returns True if the Firebase user is ready."""
    email = (user.get('email') or '').strip().lower()
    if not email:
        return False
    pw = password or secrets.token_urlsafe(24)
    try:
        firebase_auth.create_user(uid=user_id, email=email, password=pw,
                                  display_name=user.get('full_name') or None)
        return True
    except firebase_auth.UidAlreadyExistsError:
        if password:
            firebase_auth.update_user(user_id, email=email, password=password)
        return True
    except firebase_auth.EmailAlreadyExistsError:
        # Some other Firebase Auth user already has this email (for example a
        # family account made in the mobile app). Leave this one as it is.
        print(f'[auth] {email} already belongs to another Firebase Auth user; '
              f'account {user_id} keeps its old password login.')
        return False
    except Exception as e:
        print(f'[auth] Could not create Firebase Auth user for {email}: {e}')
        return False


def legacy_login_ok(user_id, user, password):
    """Login check for an account that still has a password_hash.

    Accepts the old password, or a new one set through a Firebase reset
    email -- but only from the Firebase user that belongs to THIS account."""
    from werkzeug.security import check_password_hash
    if check_password_hash(user.get('password_hash') or '', password):
        finish_migration(user_id, password)
        return True
    email = user.get('email') or ''
    try:
        if firebase_auth.get_user_by_email(email).uid != user_id:
            return False
    except Exception:
        return False
    if verify_password(email, password):
        db.collection('user_account').document(user_id).update(
            {'password_hash': firestore.DELETE_FIELD})
        return True
    return False


def finish_migration(user_id, password):
    """Moves a legacy account fully onto Firebase Auth after a good login."""
    doc = db.collection('user_account').document(user_id).get()
    user = doc.to_dict() if doc.exists else None
    if not user or 'password_hash' not in user:
        return
    if ensure_firebase_user(user_id, user, password=password):
        doc.reference.update({'password_hash': firestore.DELETE_FIELD})
