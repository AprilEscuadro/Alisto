from database import db

BARANGAYS = [
    {'id': 'sudlon-ii', 'name': 'Sudlon II', 'city': 'Cebu City',
     'health_centers': ['Sudlon II Health Center']},
    {'id': 'lahug', 'name': 'Lahug', 'city': 'Cebu City',
     'health_centers': ['Lahug Health Center']},
]


def seed_barangays():
    for b in BARANGAYS:
        ref = db.collection('barangay').document(b['id'])
        existed = ref.get().exists
        # merge=True so re-running also adds health_centers to a barangay
        # that was seeded earlier without them.
        ref.set({'name': b['name'], 'city': b['city'],
                 'health_centers': b['health_centers'], 'is_active': True},
                merge=True)
        print(f"{'Updated' if existed else 'Added'} barangay: {b['name']}")

    print("Done seeding barangays.")


if __name__ == '__main__':
    seed_barangays()