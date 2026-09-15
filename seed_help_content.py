"""
Put the Help & Support content in Firestore (run once from the project folder):

    python seed_help_content.py

- faq/{id}                     -> the FAQ list on the Help & Support page
- app_settings/support_contact -> phone, email, address, response time

Existing FAQs are left alone (nothing is duplicated). Edit the values below
before running, or edit them later in the Firebase console.
"""
from firebase_admin import firestore

from database import db

SUPPORT_CONTACT = {
    'phone': '+63 9XX XXX XXXX',          # <- put the real support number
    'phone_hours': 'Mon–Fri, 8:00 AM – 5:00 PM',
    'email': 'support@alisto.ph',          # <- put the real support email
    'address_line1': 'Barangay Sudlon II Hall',
    'address_line2': 'Cebu City, Cebu, Philippines',
    'response_time': '24 hours',
}

FAQS = [
    ('How do I set up the ALISTO device for my loved one?',
     'Charge the device, then enter its Device ID (printed on the device or its QR code) when you '
     'register or in My Loved Ones > View Details > Link device. Once it is switched on and connected, '
     'its status shows as Online.'),
    ('What happens when an emergency alert is triggered?',
     'The device waits 5 seconds so a false alarm can be cancelled. If it is not cancelled, it sends an '
     'SMS to the linked family members and emergency contacts, and the alert appears on your Dashboard '
     'and Alerts page with the last known location.'),
    ('Can more than one family member link to the same elder?',
     'Yes. Up to {max_family} family accounts can link to the same device. Each person registers with '
     'the same Device ID and chooses to join the existing elder.'),
    ('Does ALISTO work without internet?',
     'Yes. Voice detection runs on the device itself, and alerts are sent by SMS. The website and app '
     'update once the device is back online.'),
    ('What if the alarm is triggered by accident?',
     'Cancel it on the device within 5 seconds. It is saved as a false alarm and no SMS is sent.'),
    ('Where can I see past alerts and reminders?',
     'Open History. You can filter by type and date range, and click any row for details.'),
    ('How do I choose which notifications I receive?',
     'Open Settings and switch Emergency Alerts, Medicine Reminders, or Device Alerts on or off. '
     'Make sure your contact number is set in My Profile so SMS can reach you.'),
    ("Is my loved one's location private?",
     'Location is only shown to family members linked to that elder and to the Barangay Health Worker '
     'assigned to their barangay.'),
]


def main():
    db.collection('app_settings').document('support_contact').set(
        {**SUPPORT_CONTACT, 'updated_at': firestore.SERVER_TIMESTAMP}, merge=True)
    print('Saved app_settings/support_contact')

    existing = {(d.to_dict() or {}).get('question')
                for d in db.collection('faq').stream()}
    added = 0
    for order, (question, answer) in enumerate(FAQS):
        if question in existing:
            continue
        db.collection('faq').add({
            'question': question, 'answer': answer, 'order': order,
            'is_active': True, 'created_at': firestore.SERVER_TIMESTAMP,
        })
        added += 1
    print(f'Added {added} FAQ(s); {len(FAQS) - added} already existed.')


if __name__ == '__main__':
    main()
