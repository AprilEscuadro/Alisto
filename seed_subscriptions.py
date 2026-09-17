"""
Add sample subscriptions so the admin Subscriptions page has something to show.

Run from the project folder:
    python seed_subscriptions.py

It gives every registered elder a plan, with a mix of active, expiring-soon, and
expired dates. Run it again to clear the samples and re-add them.

Delete this file before the final defense if you don't want sample data around.
"""
import random
from datetime import datetime, timedelta, timezone

from database import db

# Must match PLANS in admin_routes.py.
PLAN_KEYS = ['basic', 'premium']
# days until expiry -> roughly a third active, a third expiring soon, a third expired
EXPIRY_PATTERN = [250, 300, 20, 8, -25, -3, 180, 12]


def main():
    elders = [
        (doc.id, doc.to_dict() or {})
        for doc in db.collection('elder_profile').stream()
        if not (doc.to_dict() or {}).get('deleted_at')
    ]

    if not elders:
        print('No elders found. Register a family account for an elder first.')
        return

    devices_by_elder = {}
    for doc in db.collection('device').stream():
        device = doc.to_dict() or {}
        if device.get('elder_id'):
            devices_by_elder.setdefault(device['elder_id'], []).append(doc.id)

    removed = 0
    for doc in db.collection('subscription').where('is_sample', '==', True).stream():
        doc.reference.delete()
        removed += 1
    if removed:
        print(f'Removed {removed} sample subscription(s) from a previous run.')

    random.seed(11)
    now = datetime.now(timezone.utc)
    created = 0

    for index, (elder_id, elder) in enumerate(elders):
        serials = devices_by_elder.get(elder_id, [])
        plan = 'premium' if len(serials) > 1 else PLAN_KEYS[index % 2]
        days_left = EXPIRY_PATTERN[index % len(EXPIRY_PATTERN)]
        expiry = now + timedelta(days=days_left)

        db.collection('subscription').add({
            'elder_id': elder_id,
            'plan': plan,
            'start_date': expiry - timedelta(days=365),
            'expiry_date': expiry,
            'auto_renew': days_left >= 0,
            'device_serials': serials,
            'created_at': now,
            'is_sample': True,  # so this script can clean up after itself
        })
        created += 1

        state = 'expired' if days_left < 0 else ('expiring soon' if days_left <= 30 else 'active')
        print(f"  {elder.get('full_name', 'Unnamed'):<24} {plan:<8} {state}")

    print(f'\nCreated {created} sample subscription(s).')
    print('Open the admin panel and go to Subscriptions to see them.')


if __name__ == '__main__':
    main()