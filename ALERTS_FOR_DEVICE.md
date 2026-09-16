# What the ALISTO device must write for an alert

The website reads one Firestore collection: **`emergency_alert`**. One document per
alert. The Raspberry Pi (or whatever sends the alert) only needs to write these
fields; the website works out the resident's name, age, barangay, and the response
time by itself.

| Field | Type | Required | Notes |
|---|---|---|---|
| `elder_id` | string | yes | The `elder_profile` document ID of the resident. |
| `device_serial` | string | yes | The device document ID, which is its serial number. |
| `type` | string | yes | `emergency`, `fall`, `help`, `medication`, `health_check`, or `false_alarm`. |
| `status` | string | yes | New alerts are `active`. Medication reminders are `delivered`. |
| `trigger` | string | no | `voice`, `button`, `sensor`, or `schedule`. |
| `trigger_phrase` | string | no | What the elder said, e.g. `Tabang! Nalipong ko.` |
| `latitude` | number | no | Where the device was at the time. |
| `longitude` | number | no | |
| `gps_verified` | boolean | no | `true` if the coordinates came from a real GPS fix. |
| `notes` | string | no | Free text shown in the detail panel. |
| `sms_sent_count` | number | no | How many emergency contacts were texted. |
| `created_at` | timestamp | yes | Use the server timestamp. |

The BHW pages fill in the rest when someone responds:
`acknowledged_at`, `acknowledged_by`, `resolved_at`, `resolved_by`, `resolution_note`.

## Status flow

    active  --Acknowledge-->  acknowledged  --Resolve-->  resolved
      |
      +--Mark as false alarm-->  false_alarm

`closed` also counts as handled. `delivered` is for medication reminders, which
nobody needs to respond to.

## Device document fields the map uses

On each document in the `device` collection:

| Field | Type | Notes |
|---|---|---|
| `latitude`, `longitude` | number | Latest known position. No location means no pin on the map. |
| `last_seen` | timestamp | Updated on every check-in. Older than 10 minutes counts as offline. |
| `signal_strength` | number | 0–100. Below 40 shows as "Weak Signal". Optional. |

Instead of `last_seen`, the device may set `status` to `online`/`offline`, or
`is_online` to true/false. Any of the three works.

## Changing a field name

If the hardware team already uses different names, they don't all have to change.
Edit `alerts_common.py` (alert fields) or `_device_state()` in `bhw_routes.py`
(device fields) and the whole site follows.