"""
One-time cleanup: removes the old SAMPLE subscriptions (Basic / Premium plans)
that seed_subscriptions.py created. Real care plans are never touched.

A real care plan lives at subscription/{elder_id} and has current_period_end
(written when an admin approves a payment). The old samples have
expiry_date / plan / is_sample instead.

Run from the project folder:
    py cleanup_sample_subscriptions.py            (shows what would be removed)
    py cleanup_sample_subscriptions.py --delete   (actually removes them)
"""
import sys

from database import db


def main():
    delete = '--delete' in sys.argv
    old = []
    for doc in db.collection('subscription').stream():
        sub = doc.to_dict() or {}
        if sub.get('current_period_end'):
            continue  # a real care plan: keep
        old.append((doc, sub))

    if not old:
        print('Nothing to clean up. Only real care plans are left.')
        return

    for doc, sub in old:
        print(
            f"  {doc.id:<24} plan={sub.get('plan', '?'):<8} sample={bool(sub.get('is_sample'))}")
        if delete:
            doc.reference.delete()

    if delete:
        print(f'Removed {len(old)} old sample subscription(s).')
    else:
        print(f'{len(old)} old sample subscription(s) found. '
              'Run again with --delete to remove them.')


if __name__ == '__main__':
    main()
