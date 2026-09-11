"""
Create (or promote) an ALISTO admin account.

Run once from the project folder:
    python create_admin.py

The password is typed in the terminal (nothing shows while typing) and only its
hash is saved, so no password is ever stored in the code or pushed to GitHub.
"""
import getpass

from firebase_admin import firestore
from werkzeug.security import generate_password_hash

from database import db

DEFAULT_EMAIL = 'joshneoylaya@gmail.com'


def main():
    email = (input(f'Admin email [{DEFAULT_EMAIL}]: ').strip() or DEFAULT_EMAIL).lower()
    full_name = input('Full name [Administrator]: ').strip()

    password = getpass.getpass('Password (at least 6 characters, hidden while typing): ')
    if len(password) < 6:
        print('Password must be at least 6 characters. Nothing was saved.')
        return
    if password != getpass.getpass('Confirm password: '):
        print('Passwords do not match. Nothing was saved.')
        return

    data = {
        'email': email,
        'password_hash': generate_password_hash(password),
        'role': 'admin',
        'approval_status': 'approved',
        'deleted_at': None,
        'is_archived': 0,
    }

    matches = list(db.collection('user_account').where('email', '==', email).limit(1).stream())
    if matches:
        if full_name:
            data['full_name'] = full_name
        matches[0].reference.update(data)
        print(f'Updated the existing account {email}. It is now an admin.')
    else:
        data.update({
            'full_name': full_name or 'Administrator',
            'contact_number': '',
            'created_at': firestore.SERVER_TIMESTAMP,
        })
        db.collection('user_account').document().set(data)
        print(f'Created admin account {email}.')

    print('Sign in at http://127.0.0.1:5000/login to open the admin panel.')


if __name__ == '__main__':
    main()