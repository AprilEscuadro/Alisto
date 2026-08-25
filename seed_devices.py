import sqlite3

DB_NAME = 'alisto.db'

# I-list diri ang mga device serial numbers/IDs nga na-manufacture/naka-QR na
DEVICE_IDS = [
    '2026-0718ALISTO9X3',
    # dugangi diri ang uban pang device IDs
]


def seed_devices():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    for serial in DEVICE_IDS:
        try:
            cursor.execute(
                'INSERT INTO device (serial_number, is_registered) VALUES (?, 0)',
                (serial,)
            )
            print(f"✅ Added device: {serial}")
        except sqlite3.IntegrityError:
            print(f"⚠️  Skipped (already exists): {serial}")

    conn.commit()
    conn.close()
    print("Done seeding devices.")


if __name__ == '__main__':
    seed_devices()
