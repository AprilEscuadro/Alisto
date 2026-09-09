from database import db

DEVICE_IDS = [
    '2026-0718ALISTO9X3',
]


def seed_devices():
    for serial in DEVICE_IDS:
        ref = db.collection('device').document(serial)
        if ref.get().exists:
            print(f"Skipped (already exists): {serial}")
            continue
        ref.set({'serial_number': serial, 'is_registered': False})
        print(f"Added device: {serial}")

    print("Done seeding devices.")


if __name__ == '__main__':
    seed_devices()