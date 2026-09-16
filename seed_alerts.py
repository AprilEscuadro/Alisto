"""
Add sample emergency alerts so the BHW pages have something to show.

Run from the project folder:
    python seed_alerts.py

It only touches elders whose address is in the barangay you pick, creates a few
alerts spread over the past few weeks, and gives their devices a location around
Sudlon II so the map has pins. Run it again to clear the samples and re-add them.

Delete this file before the final defense if you don't want sample data around.
"""
import random
from datetime import datetime, timedelta, timezone

from bhw_routes import _in_barangay
from database import db

BARANGAY = 'Sudlon II'
# Roughly Sudlon II, Cebu City. Pins scatter a little around this point.
CENTRE = (10.3980, 123.8600)
SPREAD = 0.012
PH_TIME = timezone(timedelta(hours=8))

SAMPLES = [
    # (type, status, hours ago, trigger, phrase, minutes to respond, notes)
    ('emergency', 'active', 2, 'voice', 'Tabang! Nalipong ko.', None, 'Emergency alert received.'),
    ('fall', 'active', 5, 'sensor', '', None, 'Possible fall detected by the device.'),
    ('help', 'acknowledged', 26, 'button', '', 7, 'Resident pressed the help button.'),
    ('emergency', 'resolved', 50, 'voice', 'Sakit akong dughan.', 5, 'BHW visited. Resident is stable.'),
    ('medication', 'delivered', 30, 'schedule', '', None, 'Morning medicine reminder played.'),
    ('medication', 'delivered', 54, 'schedule', '', None, 'Evening medicine reminder played.'),
    ('emergency', 'false_alarm', 78, 'voice', '', 3, 'Cancelled within a few seconds. Accidental trigger.'),
    ('health_check', 'closed', 120, 'schedule', '', 9, 'Routine check-in.'),
    ('emergency', 'resolved', 200, 'voice', 'Tabang!', 11, 'Family arrived first. No hospital needed.'),
    ('fall', 'resolved', 320, 'sensor', '', 14, 'Minor slip, no injury.'),
]


def in_barangay(address):
    # Uses the same address handling as the site, so a map-shaped address works too.
    return _in_barangay(address, BARANGAY)


def main():
    elders = [
        (doc.id, doc.to_dict() or {})
        for doc in db.collection('elder_profile').stream()
        if not (doc.to_dict() or {}).get('deleted_at')
        and in_barangay((doc.to_dict() or {}).get('address'))
    ]

    if not elders:
        print(f'No elders found with an address in {BARANGAY}.')
        print('Register a family account for an elder in that barangay first, then run this again.')
        return

    devices_by_elder = {}
    for doc in db.collection('device').stream():
        device = doc.to_dict() or {}
        if device.get('elder_id'):
            devices_by_elder.setdefault(device['elder_id'], []).append(doc.id)

    # Clear any samples from an earlier run.
    removed = 0
    for doc in db.collection('emergency_alert').where('is_sample', '==', True).stream():
        doc.reference.delete()
        removed += 1
    if removed:
        print(f'Removed {removed} sample alert(s) from a previous run.')

    # Give every device a location so the map has pins.
    random.seed(7)
    located = 0
    for elder_id, _ in elders:
        for serial in devices_by_elder.get(elder_id, []):
            db.collection('device').document(serial).update({
                'latitude': CENTRE[0] + random.uniform(-SPREAD, SPREAD),
                'longitude': CENTRE[1] + random.uniform(-SPREAD, SPREAD),
                'last_seen': datetime.now(timezone.utc) - timedelta(minutes=random.choice([1, 3, 5, 240])),
                'signal_strength': random.choice([95, 80, 62, 35]),
            })
            located += 1
    print(f'Set a location on {located} device(s).')

    now = datetime.now(timezone.utc)
    created = 0
    for index, (type_key, status, hours, trigger, phrase, response, notes) in enumerate(SAMPLES):
        elder_id, elder = elders[index % len(elders)]
        serials = devices_by_elder.get(elder_id, [])
        created_at = now - timedelta(hours=hours)

        alert = {
            'elder_id': elder_id,
            'device_serial': serials[0] if serials else '',
            'type': type_key,
            'status': status,
            'trigger': trigger,
            'trigger_phrase': phrase,
            'latitude': CENTRE[0] + random.uniform(-SPREAD, SPREAD),
            'longitude': CENTRE[1] + random.uniform(-SPREAD, SPREAD),
            'gps_verified': trigger in ('voice', 'button'),
            'notes': notes,
            'sms_sent_count': 2 if type_key in ('emergency', 'fall') else 0,
            'created_at': created_at,
            'is_sample': True,  # so this script can clean up after itself
        }
        if response is not None:
            alert['acknowledged_at'] = created_at + timedelta(minutes=response)
        if status in ('resolved', 'closed', 'false_alarm'):
            alert['resolved_at'] = created_at + timedelta(minutes=(response or 5) + 20)

        db.collection('emergency_alert').add(alert)
        created += 1
        print(f"  {elder.get('full_name', 'Unnamed'):<24} {type_key:<13} {status}")

    print(f'\nCreated {created} sample alert(s) for {len(elders)} resident(s) in {BARANGAY}.')
    print('Sign in as a BHW for that barangay to see them.')


if __name__ == '__main__':
    main()