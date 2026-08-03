-- USER ACCOUNTS (Family, BHW, Admin only — elderly walay web login)
CREATE TABLE IF NOT EXISTS user_account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN ('family', 'bhw', 'admin')
    ),
    contact_number TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP,
    is_archived INTEGER DEFAULT 0
);

-- BHW PROFILE (extra info for BHW role)
CREATE TABLE IF NOT EXISTS bhw_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    barangay_assigned TEXT NOT NULL DEFAULT 'Sudlon II',
    FOREIGN KEY (user_id) REFERENCES user_account (id)
);

-- ELDER PROFILE (managed by family/BHW, not a login account)
CREATE TABLE IF NOT EXISTS elder_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    date_of_birth DATE,
    address TEXT,
    medical_info TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP,
    is_archived INTEGER DEFAULT 0
);

-- FAMILY <-> ELDER LINK (many-to-many)
CREATE TABLE IF NOT EXISTS family_elder_link (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_user_id INTEGER NOT NULL,
    elder_id INTEGER NOT NULL,
    relationship TEXT,
    FOREIGN KEY (family_user_id) REFERENCES user_account (id),
    FOREIGN KEY (elder_id) REFERENCES elder_profile (id)
);

-- DEVICE
CREATE TABLE IF NOT EXISTS device (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    elder_id INTEGER NOT NULL,
    serial_number TEXT UNIQUE NOT NULL,
    sim_number TEXT,
    battery_level INTEGER DEFAULT 100,
    led_status TEXT DEFAULT 'green',
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (elder_id) REFERENCES elder_profile (id)
);

-- DEVICE LOCATION HISTORY
CREATE TABLE IF NOT EXISTS device_location (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    latitude REAL,
    longitude REAL,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES device (id)
);

-- DEVICE HEALTH LOG
CREATE TABLE IF NOT EXISTS device_health_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    battery_level INTEGER,
    signal_strength TEXT,
    uptime_hours INTEGER,
    firmware_version TEXT,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES device (id)
);

-- EMERGENCY CONTACTS
CREATE TABLE IF NOT EXISTS emergency_contact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    elder_id INTEGER NOT NULL,
    contact_name TEXT NOT NULL,
    contact_number TEXT NOT NULL,
    relationship TEXT,
    FOREIGN KEY (elder_id) REFERENCES elder_profile (id)
);

-- MEDICATION REMINDERS
CREATE TABLE IF NOT EXISTS medication_reminder (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    elder_id INTEGER NOT NULL,
    medicine_name TEXT NOT NULL,
    dosage TEXT,
    reminder_time TEXT,
    frequency TEXT CHECK (
        frequency IN ('DAILY', 'WEEKLY')
    ),
    FOREIGN KEY (elder_id) REFERENCES elder_profile (id)
);

-- DISTRESS PHRASES (predefined Cebuano phrases)
CREATE TABLE IF NOT EXISTS distress_phrase (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase_text TEXT NOT NULL,
    meaning TEXT
);

-- VOICE DETECTION LOG
CREATE TABLE IF NOT EXISTS voice_detection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    phrase_detected TEXT,
    confidence_score REAL,
    noise_level TEXT,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES device (id)
);

-- EMERGENCY ALERT (core table)
CREATE TABLE IF NOT EXISTS emergency_alert (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    elder_id INTEGER NOT NULL,
    trigger_type TEXT CHECK (
        trigger_type IN ('VOICE', 'BUTTON')
    ),
    phrase_used TEXT,
    latitude REAL,
    longitude REAL,
    status TEXT DEFAULT 'PENDING' CHECK (
        status IN (
            'PENDING',
            'CANCELLED',
            'SENT',
            'ACKNOWLEDGED',
            'RESOLVED'
        )
    ),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES device (id),
    FOREIGN KEY (elder_id) REFERENCES elder_profile (id)
);

-- EMERGENCY STATUS HISTORY
CREATE TABLE IF NOT EXISTS emergency_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    changed_by_user_id INTEGER,
    notes TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (alert_id) REFERENCES emergency_alert (id),
    FOREIGN KEY (changed_by_user_id) REFERENCES user_account (id)
);

-- ALERT NOTIFICATIONS (who got notified)
CREATE TABLE IF NOT EXISTS alert_notification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    is_read INTEGER DEFAULT 0,
    notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (alert_id) REFERENCES emergency_alert (id),
    FOREIGN KEY (user_id) REFERENCES user_account (id)
);

-- SMS DELIVERY LOG
CREATE TABLE IF NOT EXISTS sms_delivery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    recipient_number TEXT NOT NULL,
    delivery_status TEXT DEFAULT 'PENDING' CHECK (
        delivery_status IN ('PENDING', 'SENT', 'FAILED')
    ),
    sent_at TIMESTAMP,
    FOREIGN KEY (alert_id) REFERENCES emergency_alert (id)
);