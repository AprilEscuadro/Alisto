import firebase_admin
from firebase_admin import credentials, firestore, auth

firebase_admin.initialize_app(
    credentials.Certificate('serviceAccountKey.json'))
db = firestore.client()
