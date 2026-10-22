# 🩺 ALISTO

**Offline Emergency Response System for the Elderly**

---

## 📖 About

ALISTO is an emergency response system designed to help elderly individuals during emergencies. A Raspberry Pi device listens for emergency voice commands in Cebuano (Bisaya) **without needing an internet connection**, then alerts registered family members, Barangay Health Workers (BHWs), and responders through SMS, push notifications, and the web portal.

The system has three parts:

- **ALISTO device** (Raspberry Pi + ESP32): offline voice detection, GPS location, SMS alerts
- **Web portal** (Flask): separate dashboards for Family, BHW, and Admin users
- **Mobile app** (Flutter): kept in a separate repository

All parts share one **Firebase Firestore** database.

---

## ✨ Features

- 🎤 Offline Cebuano voice detection on the device
- ❌ 5-second false alarm cancellation
- 🚨 Emergency alerts by SMS and push notification
- 📍 GPS location tracking with map view
- 👨‍👩‍👧 Family portal: loved ones, alerts, alert history
- 🏥 BHW portal: elderly users in their barangay, alerts, reports
- 🛡️ Admin panel: users, devices, BHW approval, emergency logs, reports, subscriptions
- 💬 Inbox: support tickets, announcements, and chat between family, BHW, and admin
- ⏫ Automatic alert escalation and scheduled database backups

---

## 🚨 Emergency Detection Flow

1. The ALISTO device continuously listens for emergency voice commands.
2. Speech is analyzed locally on the device, with no internet needed.
3. If an emergency phrase is recognized, a 5-second cancellation timer starts.
4. If not canceled, the device sends an emergency alert.
5. Registered contacts receive SMS and push notifications.
6. The alert appears in the web portal and is saved in the alert history.

---

## 🛠️ Technologies Used

| Part        | Technology                                   |
| ----------- | -------------------------------------------- |
| Web portal  | Python, Flask, HTML, CSS, JavaScript         |
| Database    | Firebase Firestore, Firebase Authentication  |
| Device      | Raspberry Pi (Python), ESP32 (Arduino)       |
| Mobile app  | Flutter                                      |

---

## 📁 Project Structure

```
Alisto/
├── app.py                 # Main Flask app (family pages, login, register, device API)
├── admin_routes.py        # Admin panel  (/admin)
├── bhw_routes.py          # BHW portal   (/bhw)
├── inbox_routes.py        # Inbox API    (/inbox/api)
├── database.py            # Firestore connection
├── create_admin.py        # Creates an admin account
├── seed_*.py              # Optional sample data
├── templates/             # HTML pages (family, bhw, admin, inbox, account)
├── static/                # CSS, JavaScript, images
└── alisto-iot/
    ├── pi/                # Raspberry Pi code (see pi/DEPLOY.md)
    └── esp32/             # ESP32 firmware
```

---

## 🚀 How to Run the Web Portal (Windows)

### 1. Requirements

- Python 3
- Access to the team's Firebase project

### 2. Clone and install

Open PowerShell:

```powershell
git clone https://github.com/AprilEscuadro/Alisto.git
cd Alisto
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Add the Firebase service account key

1. Open the Firebase Console and go to **Project settings → Service accounts**.
2. Click **Generate new private key**.
3. Rename the downloaded file to `serviceAccountKey.json`.
4. Put it in the project folder, beside `app.py`.

### 4. Create the `.env` file

Create a file named `.env` in the project folder:

```
SECRET_KEY=paste-a-long-random-string-here
FIREBASE_WEB_API_KEY=your-firebase-web-api-key
ALISTO_DEVICE_KEY=paste-another-long-random-string-here
FLASK_DEBUG=1
```

| Variable               | Required? | Where to get it                                                                 |
| ---------------------- | --------- | ------------------------------------------------------------------------------- |
| `SECRET_KEY`           | Yes       | Generate one with the command below                                             |
| `FIREBASE_WEB_API_KEY` | Yes       | Firebase Console → **Project settings → General → Web API Key**                 |
| `ALISTO_DEVICE_KEY`    | For device | Any long random string. The Raspberry Pi must send the same key.              |
| `FLASK_DEBUG`          | No        | `1` while developing. Remove it when deploying.                                 |

To generate a random key:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

> ⚠️ Never commit `serviceAccountKey.json` or `.env`. Both are already in `.gitignore`.

### 5. Create an admin account (first time only)

```powershell
python create_admin.py
```

### 6. (Optional) Add sample data

```powershell
python seed_help_content.py
python seed_devices.py
python seed_alerts.py
python seed_subscriptions.py
```

### 7. Run

```powershell
python app.py
```

Open **http://127.0.0.1:5000** in your browser.

---

## 📟 Device Setup

See [`alisto-iot/pi/DEPLOY.md`](alisto-iot/pi/DEPLOY.md) for setting up the Raspberry Pi, and [`alisto-iot/pi/voice_detector/README.md`](alisto-iot/pi/voice_detector/README.md) for voice enrollment and calibration.

---

## 👨‍💻 Developers

**Bachelor of Science in Information Technology**  
University of Cebu – Main Campus

- April Dianne D. Escuadro
- Engel B. Bellita
- Josh Neo Ylaya
- Lemuel Alonso

---

## 📜 License

This project is developed for academic purposes as part of the BSIT Capstone Project.