"""
One-time move of BHW valid IDs from the local uploads/bhw_ids/ folder into
Firestore (bhw_valid_id/<user id>), so they open on every computer.

    python migrate_bhw_ids.py

Run it on EACH computer that ran app.py when BHWs registered, since each
one only has the files of the sign-ups it handled. Safe to run more than
once: IDs already in Firestore are skipped. The local files are not deleted.
"""
import os

import valid_id_store
from database import db

FOLDER = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), 'uploads', 'bhw_ids')


def main():
    moved, already, missing, too_big = 0, 0, [], []
    for doc in db.collection('bhw_profile').stream():
        p = doc.to_dict() or {}
        stored, user_id = p.get('valid_id_stored_as'), p.get('user_id')
        if not stored or not user_id:
            continue
        if valid_id_store.is_firestore_path(stored):
            already += 1
            continue

        path = os.path.join(FOLDER, os.path.basename(stored))
        if not os.path.exists(path):
            missing.append(p.get('valid_id_filename') or stored)
            continue
        with open(path, 'rb') as fh:
            raw = fh.read()
        if len(raw) > valid_id_store.MAX_BYTES:
            too_big.append(
                f"{p.get('valid_id_filename') or stored} ({len(raw) // 1024} KB)")
            continue

        ext = stored.rsplit('.', 1)[-1].lower() if '.' in stored else ''
        new_path = valid_id_store.save(
            user_id, raw, p.get('valid_id_filename') or stored, ext)
        doc.reference.update({'valid_id_stored_as': new_path})
        moved += 1

    print(f'Moved {moved} ID(s) to Firestore. {already} were already there.')
    if missing:
        print(f'{len(missing)} file(s) are not on this computer (run this on the computer '
              f'that has them): ' + ', '.join(missing))
    if too_big:
        print(f'{len(too_big)} file(s) are over 700 KB and were skipped. Ask the BHW for a '
              f'smaller copy, or shrink them and run this again: ' + ', '.join(too_big))


if __name__ == '__main__':
    main()
