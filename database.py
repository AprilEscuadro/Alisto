import json
import os
import firebase_admin
from firebase_admin import credentials, firestore

# Vercel can't see serviceAccountKey.json (it's gitignored), so it reads the
# same JSON out of an environment variable instead. Local keeps using the file.
_raw = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
cred = (credentials.Certificate(json.loads(_raw)) if _raw
        else credentials.Certificate('serviceAccountKey.json'))

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()