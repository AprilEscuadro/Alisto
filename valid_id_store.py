"""
BHW valid IDs, stored in Firestore so every computer (and Vercel) can open them.

bhw_valid_id/{user_id}
    data          the file itself, base64 text
    content_type  'image/jpeg' | 'image/png' | 'application/pdf'
    filename      the name the BHW uploaded
    size          bytes, before base64
    uploaded_at

bhw_profile.valid_id_stored_as is set to 'bhw_valid_id/<user_id>', the path
of that document. Older profiles may still hold a file name from the
uploads/bhw_ids/ folder; admin_routes.bhw_valid_id() still opens those.

A Firestore document can be at most 1 MB and base64 makes a file about a
third bigger, so files are limited to 700 KB.
"""
import base64

from firebase_admin import firestore

from database import db

COLLECTION = 'bhw_valid_id'
MAX_BYTES = 700 * 1024
CONTENT_TYPES = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                 'png': 'image/png', 'pdf': 'application/pdf'}


def path_for(user_id):
    return f'{COLLECTION}/{user_id}'


def is_firestore_path(stored_as):
    return bool(stored_as) and stored_as.startswith(COLLECTION + '/')


def save(user_id, raw, filename, ext):
    """Saves the file. Returns the path to keep in bhw_profile.valid_id_stored_as."""
    db.collection(COLLECTION).document(user_id).set({
        'data': base64.b64encode(raw).decode('ascii'),
        'content_type': CONTENT_TYPES.get(ext, 'application/octet-stream'),
        'filename': filename,
        'size': len(raw),
        'uploaded_at': firestore.SERVER_TIMESTAMP,
    })
    return path_for(user_id)


def load(user_id):
    """(bytes, content_type, filename) or None."""
    doc = db.collection(COLLECTION).document(user_id).get()
    data = (doc.to_dict() or {}) if doc.exists else {}
    if not data.get('data'):
        return None
    return (base64.b64decode(data['data']),
            data.get('content_type') or 'application/octet-stream',
            data.get('filename') or 'valid_id')


def delete(user_id):
    db.collection(COLLECTION).document(user_id).delete()
