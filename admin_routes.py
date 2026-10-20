"""
ALISTO admin panel.

Registered in app.py with:
    from admin_routes import admin_bp
    app.register_blueprint(admin_bp)

Every route lives under /admin and only works for accounts with role == 'admin'.
Templates are in templates/admin/, styles in static/css/admin_css/admin.css,
and the page behavior (menus, filters, pagination) in static/js/admin.js.
"""
import csv
import io
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Blueprint, render_template, session, redirect, url_for,
                   flash, request, send_from_directory, current_app, abort, Response,
                   jsonify, g)
from firebase_admin import auth as fb_auth
from firebase_admin import firestore, messaging
from werkzeug.security import check_password_hash, generate_password_hash

import account_profile
import alerts_common as ac
import care_plan_service as cps
import valid_id_store
# One source of truth for reading addresses and working out whether a device is
# online. Both portals show the same thing, so they share the same helpers.
from bhw_routes import (PASSWORD_RULES, _address_parts, _address_text,
                        _device_state, _short_address)
from database import db

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


# ---------- ACCESS GUARD ----------

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'error')
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            flash('You do not have access to the admin panel.', 'error')
            return redirect(url_for('dashboard'))
        return view(*args, **kwargs)
    return wrapped


# ---------- HELPERS ----------

def _active_docs(collection):
    """All documents in a collection that are not soft-deleted, as (id, dict) pairs."""
    docs = []
    for doc in db.collection(collection).stream():
        data = doc.to_dict() or {}
        if not data.get('deleted_at'):
            docs.append((doc.id, data))
    return docs


DATE_FORMAT_STRFTIME = {
    'MMM DD, YYYY': '%b %d, %Y',
    'DD/MM/YYYY': '%d/%m/%Y',
    'YYYY-MM-DD': '%Y-%m-%d',
}


def _display_settings():
    """The saved Timezone/Date Format from Settings > System Preferences,
    cached on Flask's request-scoped `g` -- _fmt_date() below is called
    once per row on pages with dozens of rows, and re-reading
    app_settings/system_settings from Firestore that many times per page
    load would be wasteful for a value that can't change mid-request."""
    if not hasattr(g, '_alisto_display_settings'):
        g._alisto_display_settings = _system_settings()
    return g._alisto_display_settings


def _fmt_date(value):
    """Renders a Firestore timestamp using whatever Timezone/Date Format
    the admin picked in Settings -- previously always '%b %d, %Y' in
    Asia/Manila regardless of what those two dropdowns said, making them
    pure decoration. Alert timestamps (ac.fmt_time()'s "Today"/"Yesterday"
    relative wording) are intentionally NOT touched here -- a raw
    strftime format doesn't apply cleanly to relative phrasing, so that
    one stays fixed to Philippine time everywhere in the app."""
    if not hasattr(value, 'strftime'):
        return '—'
    settings = _display_settings()
    tz = timezone.utc if settings['timezone'].startswith('UTC') else ac.PH_TIME
    moment = value.astimezone(tz) if value.tzinfo else value
    return moment.strftime(DATE_FORMAT_STRFTIME.get(settings['date_format'], '%b %d, %Y'))


def _sort_key(value):
    return value.timestamp() if hasattr(value, 'timestamp') else 0


def _initials(name):
    parts = (name or '').split()
    return ''.join(p[0] for p in parts[:2]).upper() or '?'


def _short_id(prefix, doc_id):
    return f'{prefix}-{doc_id[:6].upper()}'


def _fmt_when(value):
    """'Just now' / '20 minutes ago' / 'Yesterday' / 'May 22, 2026'."""
    moment = value if hasattr(value, 'strftime') else None
    if not moment:
        return 'Recently'
    now = datetime.now(moment.tzinfo) if moment.tzinfo else datetime.now()
    gap = now - moment
    minutes = gap.total_seconds() / 60
    if minutes < 1:
        return 'Just now'
    if minutes < 60:
        return f'{int(minutes)} minute{"s" if int(minutes) != 1 else ""} ago'
    if gap.days < 1:
        hours = int(minutes / 60)
        return f'{hours} hour{"s" if hours != 1 else ""} ago'
    if gap.days == 1:
        return 'Yesterday'
    if gap.days < 7:
        return f'{gap.days} days ago'
    return _fmt_date(moment)


def _pending_bhws():
    """BHW registrations waiting for the admin to approve or reject, newest first."""
    profiles = {}
    for doc in db.collection('bhw_profile').stream():
        profile = doc.to_dict() or {}
        profiles[profile.get('user_id')] = profile

    pending = []
    for user_id, user in _active_docs('user_account'):
        if user.get('role') != 'bhw' or user.get('approval_status', 'pending') != 'pending':
            continue
        profile = profiles.get(user_id, {})
        name = user.get('full_name') or 'Unnamed'
        pending.append({
            'id': user_id,
            'name': name,
            'initials': _initials(name),
            'barangay': profile.get('barangay_assigned') or '',
            'health_center': profile.get('health_center') or '',
            'when': _fmt_when(user.get('created_at')),
            'sort': _sort_key(user.get('created_at')),
        })

    pending.sort(key=lambda p: -p['sort'])
    return pending


# kind -> (lucide icon, .tint-* class) for the bell's icon bubble.
NOTIF_STYLE = {
    'bhw': ('user-plus', 'tint-purple'),
    'emergency': ('siren', 'tint-red'),
    'device': ('wifi-off', 'tint-orange'),
    'subscription': ('credit-card', 'tint-blue'),
}
# Emergencies come first regardless of how recent everything else is --
# an active emergency always outranks a pending registration or an
# expiring subscription.
NOTIF_PRIORITY = {'emergency': 0, 'bhw': 1, 'device': 2, 'subscription': 3}

# The bell's context_processor runs on EVERY admin page load, and each of
# its 4 categories scans a whole collection -- real testing found that
# just clicking through Users -> Devices -> Settings re-ran all 4 scans
# each time, and burned through the project's Firestore free-tier quota
# in one session. A short cache means normal navigation only actually
# hits Firestore once per _NOTIF_CACHE_TTL_SECONDS, no matter how many
# admin pages get loaded (by one admin or several) in that window. Pages
# that actually WORK with this data (Users, Emergency Logs, Subscriptions,
# Devices) call the same underlying functions directly, uncached, so
# nothing there is ever stale -- only the bell's opportunistic preview is.
_notif_cache = {}
_NOTIF_CACHE_TTL_SECONDS = 300


def _cached(key, builder):
    now = time.time()
    hit = _notif_cache.get(key)
    if hit and now - hit[0] < _NOTIF_CACHE_TTL_SECONDS:
        return hit[1]
    value = builder()
    _notif_cache[key] = (now, value)
    return value


def _offline_devices_raw():
    elders = dict(_active_docs('elder_profile'))
    out = []
    for doc in db.collection('device').stream():
        device = doc.to_dict() or {}
        if not device.get('is_registered') or not device.get('elder_id'):
            continue
        state, last_seen = _device_state(device)
        if state != 'offline':
            continue
        name = (elders.get(device['elder_id']) or {}).get(
            'full_name') or 'Unnamed resident'
        out.append({'doc_id': doc.id, 'name': name, 'last_seen': last_seen})
    return out


def _open_alert_items():
    """Active emergencies for the admin bell, built from memory (0 reads)."""
    elders = _cached('poll_elders', _all_elders)
    out = []
    for alert_id, a in ac.open_alerts(db).items():
        if ac.row_status(a) not in ac.OPEN_STATUSES:
            continue
        elder = elders.get(a.get('elder_id'), {})
        out.append({
            'name': elder.get('full_name') or 'Unknown resident',
            'type_label': ac.type_label('emergency'),
            'time': ac.fmt_time(a.get('date_triggered')),
            'created_at': a.get('date_triggered'),
        })
    return out


def _notif_items():
    """Builds the admin bell's combined feed. Every kind (emergencies, BHW
    registrations, offline devices, expiring subscriptions) is always on --
    Settings no longer has toggles for them, because an emergency system
    shouldn't let the admin switch alerts off. What an admin CAN tune is how
    soon a device counts as offline (Settings > Device monitoring)."""
    items = []

    # always on
    for p in _cached('pending_bhws', _pending_bhws):
        items.append({
            'kind': 'bhw', 'title': p['name'],
            'subtitle': 'BHW registration waiting for approval' + (f" · {p['barangay']}" if p['barangay'] else ''),
            'when': p['when'], 'sort': p['sort'],
            'href': url_for('admin.users', status='pending'),
        })

    # emergency alerts are always on
    open_alerts = _open_alert_items()
    for a in open_alerts:
        items.append({
            'kind': 'emergency', 'title': a['name'],
            'subtitle': f"{a['type_label']} -- still active",
            'when': a['time'], 'sort': _sort_key(a['created_at']),
            'href': url_for('admin.emergency_logs'),
        })

    # always on
    for d in _cached('offline_devices', _offline_devices_raw):
        items.append({
            'kind': 'device', 'title': d['name'],
            'subtitle': f"Device {d['doc_id']} is offline",
            'when': _fmt_when(d['last_seen']), 'sort': _sort_key(d['last_seen']),
            'href': url_for('admin.devices'),
        })

    # always on
    expiring = _cached('expiring_subs', lambda: [
                       s for s in _subscription_rows() if s['status'] == 'expiring'])
    for s in expiring:
        days = s['days_left']
        items.append({
            'kind': 'subscription', 'title': s['name'],
            'subtitle': f"{s['plan']} plan expires in {days} day{'' if days == 1 else 's'}",
            # Soonest-expiring first within this kind -- see the
            # final sort below for why this can use a different
            # scale than the timestamp-based `sort` values above.
            'when': s['expiry'], 'sort': -days if days is not None else 0,
            'href': url_for('admin.subscriptions'),
        })

    for item in items:
        item['icon'], item['tint'] = NOTIF_STYLE[item['kind']]

    # Sorts by kind first (NOTIF_PRIORITY), then most-urgent-within-that-
    # kind second. `sort` only ever gets compared against other items of
    # the SAME kind (ties on the primary key), so mixing what it measures
    # across kinds -- a timestamp here, `-days_left` there -- is safe.
    items.sort(key=lambda it: (NOTIF_PRIORITY[it['kind']], -it['sort']))
    return items


_PHOTO_CACHE = {}   # {user_id: (time read, photoBase64 or None)}


def _admin_photo(user_id):
    """The admin's profile photo for the top bar, kept for a minute so
    every page load doesn't re-read the account."""
    hit = _PHOTO_CACHE.get(user_id)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        doc = db.collection('user_account').document(user_id).get()
        photo = ((doc.to_dict() or {}) if doc.exists else {}).get('photoBase64')
    except Exception:
        photo = None
    _PHOTO_CACHE[user_id] = (time.time(), photo)
    return photo


@admin_bp.context_processor
def _admin_layout_context():
    """Values every admin page needs for the sidebar and top bar."""
    if session.get('role') != 'admin':
        return {}
    items = _notif_items()
    return {
        'admin_name': session.get('full_name', 'Administrator'),
        'admin_initial': _initials(session.get('full_name', 'Administrator'))[:1],
        'admin_photo': _admin_photo(session['user_id']),
        'pending_count': len(items),
        # The bell shows the eight most urgent; the rest are one click
        # away on each item's own page (Users, Emergency Logs, etc.).
        'notif_items': items[:8],
    }


# ---------- DASHBOARD ----------


# ---------- READING THE WHOLE SYSTEM (all barangays) ----------

# Alert types the device can send, grouped by how urgent they are. A document may
# set its own "severity" field, and that always wins over this table.
SEVERITY_BY_TYPE = {
    'emergency': 'high',
    'fall': 'high',
    'help': 'medium',
    'health_check': 'low',
    'medication': 'low',
    'false_alarm': 'low',
}
SEVERITY_ORDER = ('high', 'medium', 'low')


def _barangay_of(address):
    """Best guess at the barangay from an address of either shape."""
    if isinstance(address, dict):
        return (address.get('barangay') or '').strip()
    parts = _address_parts(address)
    # "house, street, barangay, city, province, zip"
    return parts[2] if len(parts) >= 4 else ''


def _bhw_by_barangay():
    """{barangay: {'id':..., 'name':...}} for every APPROVED BHW -- a BHW
    covers a whole barangay (see bhw_routes.py's _elders_in_barangay()),
    not one elder each, so "the BHW for this resident" really means
    "whoever is approved for their barangay". A barangay with more than
    one approved BHW just keeps the first one found -- good enough to
    show a name on the map pin, not meant as the authoritative roster."""
    out = {}
    try:
        profiles = {p.get('user_id'): p for p in
                    (d.to_dict() or {} for d in db.collection('bhw_profile').stream())}
        docs = (db.collection('user_account')
                .where('role', '==', 'bhw')
                .where('approval_status', '==', 'approved')
                .stream())
    except Exception:
        return out
    for doc in docs:
        user = doc.to_dict() or {}
        barangay = (profiles.get(doc.id, {}).get(
            'barangay_assigned') or '').strip()
        if barangay and barangay not in out:
            out[barangay] = {'id': doc.id, 'name': user.get(
                'full_name') or 'Unnamed BHW'}
    return out


def _all_elders():
    return {
        doc.id: (doc.to_dict() or {})
        for doc in db.collection('elder_profile').stream()
        if not (doc.to_dict() or {}).get('deleted_at')
    }


def _devices_by_elder():
    by_elder = {}
    for doc in db.collection('device').stream():
        device = doc.to_dict() or {}
        if device.get('elder_id'):
            by_elder.setdefault(device['elder_id'],
                                []).append((doc.id, device))
    return by_elder


def _age(date_of_birth):
    try:
        born = datetime.strptime(date_of_birth or '', '%Y-%m-%d').date()
    except ValueError:
        return None
    today = datetime.now().date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _alert_code(doc_id, created_at):
    """A short human-readable reference, e.g. AL-2026-3F9C."""
    moment = ac.local(created_at)
    year = moment.year if moment else datetime.now().year
    return f'AL-{year}-{doc_id[-4:].upper()}'


def _all_alert_rows(since=None):
    """Every alert in the system, newest first, with the resident joined in."""
    elders = _all_elders()
    devices_by_elder = _devices_by_elder()

    docs = list(db.collection(ac.ALERT_COLLECTION).stream())
    alerts = [(doc.id, doc.to_dict() or {}) for doc in docs]
    if since:
        alerts = [(i, a) for i, a in alerts
                  if not (ac.local(a.get('date_triggered')) and ac.local(a.get('date_triggered')) < since)]

    # Batched once for the whole page instead of 3 Firestore round trips
    # PER alert -- that N+1 pattern was clearly noticeable once there was
    # more than a handful of alerts.
    locations = ac.alert_locations_bulk(
        db, (a.get('location_id') for _, a in alerts))
    phrases = ac.alert_phrases_bulk(
        db, (a.get('phrase_id') for _, a in alerts))

    rows = []
    for alert_id, alert in alerts:
        elder = elders.get(alert.get('elder_id'), {})
        name = elder.get('full_name') or 'Unknown resident'
        # The device only ever writes real emergencies -- the paper's
        # EMERGENCY_ALERT table has no per-category type column at all.
        type_key = 'emergency'
        status = ac.row_status(alert)
        severity = SEVERITY_BY_TYPE.get(type_key, 'medium')
        serial = alert.get('device_id') or ''
        if not serial:
            owned = devices_by_elder.get(alert.get('elder_id'), [])
            serial = owned[0][0] if owned else ''
        lat, lng, _address = locations.get(
            alert.get('location_id'), (None, None, None))

        rows.append({
            'id': alert_id,
            'code': _alert_code(alert_id, alert.get('date_triggered')),
            'elder_id': alert.get('elder_id'),
            'name': name,
            'initials': _initials(name),
            'age': _age(elder.get('date_of_birth')),
            'device': serial or '—',
            'barangay': _barangay_of(elder.get('address')),
            'location': _short_address(elder.get('address')),
            'type': type_key,
            'type_label': ac.type_label(type_key),
            'type_icon': ac.type_icon(type_key),
            'type_colour': ac.type_colour(type_key),
            'status': status,
            'status_label': ac.status_label(status),
            'status_class': ac.status_class(status),
            'severity': severity,
            'created_at': alert.get('date_triggered'),
            'time': ac.fmt_time(alert.get('date_triggered')),
            'full_time': ac.local(alert.get('date_triggered')).strftime('%b %d, %Y %I:%M %p')
            if ac.local(alert.get('date_triggered')) else '—',
            'notes': alert.get('resolution_note') or '',
            'trigger_phrase': phrases.get(alert.get('phrase_id'), ''),
            'response_minutes': ac.response_minutes(alert),
            'is_open': status in ac.OPEN_STATUSES,
            'latitude': lat,
            'longitude': lng,
        })

    oldest = datetime.min.replace(tzinfo=ac.PH_TIME)
    rows.sort(key=lambda r: ac.local(r['created_at']) or oldest, reverse=True)
    return rows


def _resident_rows():
    """Every elder with their device status and whether they have an open alert."""
    elders = _all_elders()
    devices_by_elder = _devices_by_elder()
    open_by_elder = {r['elder_id']: r for r in reversed(
        _all_alert_rows()) if r['is_open']}
    order = ('online', 'weak', 'offline')

    rows = []
    for elder_id, elder in elders.items():
        devices = devices_by_elder.get(elder_id, [])
        if devices:
            (state, last_seen), serial, device = min(
                ((_device_state(d), d_id, d) for d_id, d in devices),
                key=lambda triple: order.index(triple[0][0]),
            )
        else:
            state, last_seen, serial, device = 'none', None, None, {}

        # The device's own doc never carries its location -- write_location()
        # on the Pi only ever appends to device_location. See
        # alerts_common.latest_device_location()'s doc comment.
        lat, lng, _address, _recorded_at = ac.latest_device_location(
            db, serial)

        open_alert = open_by_elder.get(elder_id)
        name = elder.get('full_name') or 'Unnamed'

        rows.append({
            'id': elder_id,
            'name': name,
            'initials': _initials(name),
            'age': _age(elder.get('date_of_birth')),
            'barangay': _barangay_of(elder.get('address')),
            'address': _address_text(elder.get('address')),
            'short_address': _short_address(elder.get('address')),
            'device_status': state,
            'device_ids': [d_id for d_id, _ in devices],
            'last_seen': ac.fmt_time(last_seen),
            'latitude': lat,
            'longitude': lng,
            'open_alert': open_alert,
            'map_status': 'alert' if open_alert else state,
        })

    rows.sort(key=lambda r: (not r['open_alert'], r['name'].casefold()))
    return rows


def _avg_response(rows):
    answered = [r['response_minutes']
                for r in rows if r['response_minutes'] is not None]
    return round(sum(answered) / len(answered)) if answered else None


def _fmt_minutes(minutes):
    if minutes is None:
        return '—'
    if minutes < 60:
        return f'{int(minutes)}m'
    return f'{int(minutes // 60)}h {int(minutes % 60)}m'


@admin_bp.route('/')
@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    users = _active_docs('user_account')
    elders = _active_docs('elder_profile')
    devices = list(db.collection('device').stream())
    alerts = _all_alert_rows()
    residents = _resident_rows()

    stats = {
        'active_alerts': sum(1 for a in alerts if a['is_open']),
        'total_elderly': len(elders),
        'total_bhws': sum(1 for _, u in users
                          if u.get('role') == 'bhw' and u.get('approval_status') == 'approved'),
        'total_devices': len(devices),
    }

    month_start = datetime.now(ac.PH_TIME).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    this_month = [a for a in alerts if (
        ac.local(a['created_at']) or month_start) >= month_start]
    summary = {
        'active': sum(1 for a in this_month if a['is_open']),
        'acknowledged': sum(1 for a in this_month if a['status'] == 'acknowledged'),
        'resolved': sum(1 for a in this_month if a['status'] in ac.RESOLVED_STATUSES),
    }
    summary['total'] = sum(summary.values())
    total = summary['total'] or 1

    return render_template(
        'admin/dashboard.html', active_page='dashboard',
        stats=stats, recent_alerts=alerts[:3], alert_summary=summary,
        donut={
            'active_pct': summary['active'] / total * 100,
            'ack_pct': (summary['active'] + summary['acknowledged']) / total * 100,
            'empty': summary['total'] == 0,
        },
        map_points=_map_points(residents),
        system={
            'devices_online': sum(1 for r in residents if r['device_status'] in ('online', 'weak')),
            'barangays': len({r['barangay'] for r in residents if r['barangay']}),
        },
    )


# ---------- USERS ----------

@admin_bp.route('/users')
@admin_required
def users():
    accounts = _active_docs('user_account')
    elders = _active_docs('elder_profile')

    devices_per_elder = {}
    # elder_id -> [serial, ...] (the device doc ID is the serial)
    serials_per_elder = {}
    for doc in db.collection('device').stream():
        elder_id = (doc.to_dict() or {}).get('elder_id')
        if elder_id:
            devices_per_elder[elder_id] = devices_per_elder.get(
                elder_id, 0) + 1
            serials_per_elder.setdefault(elder_id, []).append(doc.id)

    elders_per_family = {}
    families_per_elder = {}  # elder_id -> [family user_id, ...]
    for doc in db.collection('family_elder_link').stream():
        link = doc.to_dict() or {}
        elders_per_family.setdefault(
            link.get('family_user_id'), []).append(link.get('elder_id'))
        families_per_elder.setdefault(
            link.get('elder_id'), []).append(link.get('family_user_id'))

    # For the "View details" popup: who is linked to whom, and which device.
    elder_names = {eid: e.get('full_name') or 'Unnamed' for eid, e in elders}
    family_names = {uid: u.get('full_name')
                    or 'Unnamed' for uid, u in accounts}

    def serial_text(elder_id):
        return ', '.join(sorted(serials_per_elder.get(elder_id, []))) or 'No device'

    bhw_profiles = {}
    for doc in db.collection('bhw_profile').stream():
        profile = doc.to_dict() or {}
        bhw_profiles[profile.get('user_id')] = profile

    rows = []

    for elder_id, elder in elders:
        rows.append({
            'id': elder_id, 'display_id': _short_id('ELD', elder_id), 'type': 'elderly',
            'name': elder.get('full_name') or 'Unnamed', 'initials': _initials(elder.get('full_name')),
            'phone': None, 'email': None,
            'devices': devices_per_elder.get(elder_id, 0),
            'status': 'archived' if elder.get('is_archived') else 'active',
            'joined': _fmt_date(elder.get('created_at')), 'sort': _sort_key(elder.get('created_at')),
            # A list (not a dict) so the details keep this order in the modal.
            'details': [
                ['Full Name', elder.get('full_name') or '—'],
                ['Date of Birth', elder.get('date_of_birth') or '—'],
                ['Address', elder.get('address') or '—'],
                # One name per line (the popup keeps line breaks).
                ['Device Serial', serial_text(elder_id)],
                ['Linked Family', '\n'.join(sorted(
                    family_names[f] for f in set(families_per_elder.get(elder_id, []))
                    if f in family_names)) or '—'],
            ],
        })

    for user_id, user in accounts:
        role = user.get('role')
        if role not in ('family', 'bhw'):
            continue  # admins are not listed

        row = {
            'id': user_id, 'type': role,
            'name': user.get('full_name') or 'Unnamed', 'initials': _initials(user.get('full_name')),
            'phone': user.get('contact_number') or None, 'email': user.get('email') or None,
            'joined': _fmt_date(user.get('created_at')), 'sort': _sort_key(user.get('created_at')),
            'details': [
                ['Full Name', user.get('full_name') or '—'],
                ['Email Address', user.get('email') or '—'],
                ['Phone Number', user.get('contact_number') or '—'],
            ],
        }

        if role == 'family':
            row['display_id'] = _short_id('FAM', user_id)
            row['devices'] = sum(devices_per_elder.get(e, 0)
                                 for e in elders_per_family.get(user_id, []))
            row['status'] = 'archived' if user.get('is_archived') else 'active'
            # The elder(s) this family member watches over and the device
            # serial for each, one per line and in the same order so the
            # two rows line up. A link to an elder that no longer exists is
            # left out.
            linked = [e for e in dict.fromkeys(elders_per_family.get(user_id, []))
                      if e in elder_names]
            row['details'] += [
                ['Linked Elder', '\n'.join(elder_names[e]
                                           for e in linked) or '—'],
                ['Device Serial', '\n'.join(serial_text(e)
                                            for e in linked) or '—'],
            ]
        else:
            approval = user.get('approval_status', 'pending')
            profile = bhw_profiles.get(user_id, {})
            row['display_id'] = _short_id('BHW', user_id)
            row['devices'] = 0
            if user.get('is_archived'):
                row['status'] = 'archived'
            else:
                row['status'] = {'approved': 'active',
                                 'rejected': 'rejected'}.get(approval, 'pending')
            row['details'] += [
                ['Date of Birth', profile.get('date_of_birth') or '—'],
                ['Barangay', profile.get('barangay_assigned') or '—'],
                ['Health Center', profile.get('health_center') or '—'],
                ['Years of Service', profile.get('years_of_service') or '—'],
                ['BHW ID Number', profile.get('bhw_id_number') or '—'],
            ]
            if profile.get('valid_id_stored_as'):
                row['valid_id_url'] = url_for(
                    'admin.bhw_valid_id', user_id=user_id)
                row['valid_id_name'] = profile.get(
                    'valid_id_filename') or 'View file'

        rows.append(row)

    # Pending BHWs first so the admin sees them right away, then newest first.
    rows.sort(key=lambda r: (r['status'] != 'pending', -r['sort']))

    stats = {
        'total_users': len(rows),
        'elderly': sum(1 for r in rows if r['type'] == 'elderly'),
        'family': sum(1 for r in rows if r['type'] == 'family'),
        'bhw': sum(1 for r in rows if r['type'] == 'bhw'),
        'pending_bhw': sum(1 for r in rows if r['status'] == 'pending'),
        'total_devices': sum(devices_per_elder.values()),
    }

    return render_template(
        'admin/users.html', active_page='users', rows=rows, stats=stats,
        years_of_service_options=YEARS_OF_SERVICE_OPTIONS,
        cebu_city_barangays=CEBU_CITY_BARANGAYS,
    )


@admin_bp.route('/users/<user_id>/approval', methods=['POST'])
@admin_required
def update_bhw_approval(user_id):
    action = request.form.get('action')
    if action not in ('approve', 'reject'):
        flash('Invalid action.', 'error')
        return redirect(url_for('admin.users'))

    ref = db.collection('user_account').document(user_id)
    doc = ref.get()
    user = doc.to_dict() if doc.exists else None
    if not user or user.get('role') != 'bhw':
        flash('That BHW account could not be found.', 'error')
        return redirect(url_for('admin.users'))

    ref.update({
        'approval_status': 'approved' if action == 'approve' else 'rejected',
        'approval_reviewed_at': firestore.SERVER_TIMESTAMP,
        'approval_reviewed_by': session['user_id'],
    })

    name = user.get('full_name', 'The BHW')
    if action == 'approve':
        flash(f'{name} is approved and can now sign in.', 'success')
    else:
        flash(f"{name}'s registration was rejected.", 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<user_id>/archive', methods=['POST'])
@admin_required
def archive_user(user_id):
    """Toggles is_archived for a family/BHW account (user_account) or an
    elder profile (elder_profile) -- soft-hide only, matching this app's
    existing convention everywhere else (unregister_device(), the various
    _active_docs() filters): a user's alerts, subscriptions, and links all
    stay attributed to their real ID rather than being orphaned the way a
    hard delete would leave them, since Emergency Logs/Reports/Subscriptions
    all still reference these IDs directly. Archiving just hides them from
    the default Users view and tags them 'Archived' (already a status
    filter option); calling this again restores them. `type` (from the row
    the button was on) says which collection actually owns this ID."""
    collection = 'elder_profile' if request.form.get(
        'type') == 'elderly' else 'user_account'
    ref = db.collection(collection).document(user_id)
    doc = ref.get()
    if not doc.exists:
        flash('That user could not be found.', 'error')
        return redirect(url_for('admin.users'))

    data = doc.to_dict() or {}
    name = data.get('full_name') or 'This user'
    now_archived = not data.get('is_archived')
    ref.update({
        'is_archived': now_archived,
        'archived_at': firestore.SERVER_TIMESTAMP if now_archived else None,
        'archived_by': session['user_id'] if now_archived else None,
    })
    flash(f"{name} was {'archived' if now_archived else 'restored'}.", 'success')
    return redirect(url_for('admin.users'))


def _delete_matching(collection, field, value):
    """Deletes every document in `collection` whose `field` equals `value`
    (in batches, so a long list can't hit Firestore's 500-write limit).
    Returns how many were deleted."""
    deleted = 0
    batch = db.batch()
    pending = 0
    for doc in db.collection(collection).where(field, '==', value).stream():
        batch.delete(doc.reference)
        pending += 1
        deleted += 1
        if pending >= 400:
            batch.commit()
            batch = db.batch()
            pending = 0
    if pending:
        batch.commit()
    return deleted


@admin_bp.route('/users/<user_id>/delete', methods=['POST'])
@admin_required
def delete_user(user_id):
    """PERMANENTLY deletes a user. Unlike archive_user() this removes the
    data, so it is for freeing space and removing accounts for good.

    Family / BHW account: the Firebase Authentication login, the user_account
    document, the BHW profile and uploaded valid ID, and their links to loved
    ones. Their loved ones (elder profiles) and devices are NOT deleted.

    Elderly profile: the profile, its emergency contacts, medicine reminders
    and family links; any device registered to them is unregistered (same as
    Unregister on the Devices page) so the serial can be reused.

    Alerts, subscriptions and payments are deliberately KEPT: Emergency Logs,
    Reports and the Subscriptions page still reference them, and payments are
    financial records.
    """
    kind = request.form.get('type')
    if user_id == session.get('user_id'):
        flash("You can't delete your own account.", 'error')
        return redirect(url_for('admin.users'))
    if kind == 'elderly':
        return _delete_elder(user_id)

    ref = db.collection('user_account').document(user_id)
    doc = ref.get()
    user = doc.to_dict() if doc.exists else None
    if not user:
        flash('That user could not be found.', 'error')
        return redirect(url_for('admin.users'))
    if user.get('role') not in ('family', 'bhw'):
        flash('Only family and BHW accounts can be deleted here.', 'error')
        return redirect(url_for('admin.users'))

    name = user.get('full_name') or 'This user'

    # 1) The sign-in account first. If this fails we stop BEFORE touching the
    #    data, so a user is never left with a login but no profile.
    try:
        fb_auth.delete_user(user_id)
    except fb_auth.UserNotFoundError:
        pass  # no login account left (already removed in Firebase): carry on
    except Exception as e:
        flash(
            f"Could not delete {name}'s login in Firebase ({e}). Nothing was deleted.", 'error')
        return redirect(url_for('admin.users'))

    # 2) Their data.
    try:
        _delete_matching('bhw_profile', 'user_id', user_id)
        try:
            valid_id_store.delete(user_id)
        except Exception as e:
            print(
                f'[delete user] could not remove valid ID for {user_id}: {e}')
        _delete_matching('family_elder_link', 'family_user_id', user_id)
        ref.delete()
    except Exception as e:
        flash(f"{name}'s login was deleted but some data could not be removed ({e}). "
              'Try Delete again.', 'error')
        return redirect(url_for('admin.users'))

    flash(f'{name} was permanently deleted, including their Firebase login.', 'success')
    return redirect(url_for('admin.users'))


def _delete_elder(elder_id):
    ref = db.collection('elder_profile').document(elder_id)
    doc = ref.get()
    elder = doc.to_dict() if doc.exists else None
    if not elder:
        flash('That user could not be found.', 'error')
        return redirect(url_for('admin.users'))
    name = elder.get('full_name') or 'This person'

    try:
        for device in db.collection('device').where('elder_id', '==', elder_id).stream():
            device.reference.update({
                'is_registered': False,
                'elder_id': None,
                'registered_at': None,
                'previous_elder_id': elder_id,
                'unregistered_at': firestore.SERVER_TIMESTAMP,
                'unregistered_by': session['user_id'],
            })
        _delete_matching('family_elder_link', 'elder_id', elder_id)
        _delete_matching('emergency_contact', 'elder_id', elder_id)
        _delete_matching('medication_reminder', 'elder_id', elder_id)
        ref.delete()
    except Exception as e:
        flash(f'Could not finish deleting {name} ({e}). Try again.', 'error')
        return redirect(url_for('admin.users'))

    flash(f'{name} was permanently deleted and their device was unregistered.', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<user_id>/valid-id')
@admin_required
def bhw_valid_id(user_id):
    """Show a BHW's uploaded ID to admins only (never a public link)."""
    matches = list(db.collection('bhw_profile').where(
        'user_id', '==', user_id).limit(1).stream())
    stored = (matches[0].to_dict() or {}).get(
        'valid_id_stored_as') if matches else None
    if not stored:
        abort(404)

    if valid_id_store.is_firestore_path(stored):
        found = valid_id_store.load(user_id)
        if not found:
            abort(404)
        raw, content_type, filename = found
        return Response(raw, mimetype=content_type, headers={
            'Content-Disposition': f'inline; filename="{filename}"',
            'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
        })

    # Older sign-ups whose file is still in the local uploads folder
    # (run migrate_bhw_ids.py on that computer to move them to Firestore).
    folder = os.path.join(current_app.root_path, 'uploads', 'bhw_ids')
    if not os.path.exists(os.path.join(folder, os.path.basename(stored))):
        abort(404)
    return send_from_directory(folder, os.path.basename(stored))


# Mirrors app.py's own BHW_REGISTRATION_SETTINGS -- kept as a separate copy
# rather than importing from app.py, which would be a circular import
# (app.py is what imports admin_bp from this module in the first place).
HEALTH_CENTERS = ['Sudlon II Health Center']
YEARS_OF_SERVICE_OPTIONS = [
    'Less than 1 year', '1-3 years', '4-6 years', '7-10 years', 'More than 10 years'
]

# All 80 Cebu City barangays (Cebu City proper only -- no other Cebu
# province municipality), extracted from the same boundary data the Pi's
# own reverse_geocode.py uses for GPS-to-barangay lookups
# (alisto-iot/pi/data/cebu_city_barangays.json, ADM4_EN field, "(Pob.)"
# suffix stripped the same way that module does) -- kept as a plain list
# here rather than reading that file at request time so this page never
# depends on the Pi's bundled data folder existing on the web server.
CEBU_CITY_BARANGAYS = [
    'Adlaon', 'Agsungot', 'Apas', 'Babag', 'Bacayan', 'Banilad', 'Basak Pardo',
    'Basak San Nicolas', 'Binaliw', 'Bonbon', 'Budla-an', 'Buhisan', 'Bulacao',
    'Buot-Taup Pardo', 'Busay', 'Calamba', 'Cambinocot', 'Camputhaw', 'Capitol Site',
    'Carreta', 'Central', 'Cogon Pardo', 'Cogon Ramos', 'Day-as', 'Duljo', 'Ermita',
    'Guadalupe', 'Guba', 'Hippodromo', 'Inayawan', 'Kalubihan', 'Kalunasan',
    'Kamagayan', 'Kasambagan', 'Kinasang-an Pardo', 'Labangon', 'Lahug',
    'Lorega (Lorega San Miguel)', 'Lusaran', 'Luz', 'Mabini', 'Mabolo', 'Malubog',
    'Mambaling', 'Pahina Central', 'Pahina San Nicolas', 'Pamutan', 'Pardo',
    'Pari-an', 'Paril', 'Pasil', 'Pit-os', 'Pulangbato', 'Pung-ol-Sibugay',
    'Punta Princesa', 'Quiot Pardo', 'Sambag I', 'Sambag II', 'San Antonio',
    'San Jose', 'San Nicolas Central', 'San Roque (Ciudad)', 'Santa Cruz',
    'Sapangdaku', 'Sawang Calero', 'Sinsin', 'Sirao', 'Suba Pob. (Suba San Nicolas)',
    'Sudlon I', 'Sudlon II', 'T. Padilla', 'Tabunan', 'Tagbao', 'Talamban', 'Taptap',
    'Tejero (Villa Gonzalo)', 'Tinago', 'Tisa', 'To-ong Pardo', 'Zapatera',
]


@admin_bp.route('/users/new', methods=['GET', 'POST'])
@admin_required
def add_user():
    """Creates a BHW account directly, already approved -- skips the
    self-signup + review flow a real BHW goes through (see app.py's
    _register_bhw()), for when an admin is onboarding someone they've
    already vetted in person. Family and Elderly accounts still only ever
    come from the app/website's own self-service registration (which also
    needs a device to claim or join) -- this route doesn't touch that.

    POSTed via fetch() (see setupAddUser() in admin.js), not a plain form
    submit -- a validation error used to only ever show up as a flash
    banner at the top of the page after the modal had already closed on
    redirect, easy to miss while scrolled down filling a long form (real
    testing: an admin's duplicate-email submission looked like it had
    silently done nothing). Errors are returned as JSON instead, so the
    modal can show the message inline and keep what was typed. Success
    still flashes (shown after the page reload the client does next)."""
    if request.method == 'GET':
        return redirect(url_for('admin.users'))

    full_name = request.form.get('full_name', '').strip()
    email = request.form.get('email', '').strip().lower()
    contact_number = request.form.get('contact_number', '').strip()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')
    dob = request.form.get('bhw_dob', '').strip()
    barangay_assigned = request.form.get('barangay_assigned', '').strip()
    # Only one health center exists system-wide (HEALTH_CENTERS), so there's
    # nothing for the admin to actually choose -- assigned automatically
    # instead of showing a single-option dropdown.
    health_center = HEALTH_CENTERS[0]
    years_of_service = request.form.get('years_of_service', '').strip()
    bhw_id_number = request.form.get('bhw_id_number', '').strip()

    if not all([full_name, email, password, dob, barangay_assigned, years_of_service, bhw_id_number]):
        return jsonify(success=False, message='Please fill up all required fields.'), 400
    if password != confirm_password:
        return jsonify(success=False, message='Password and confirm password do not match.'), 400
    missing = [label for rule, label in PASSWORD_RULES if not rule(password)]
    if missing:
        return jsonify(success=False, message='Password needs ' + ', '.join(missing) + '.'), 400
    if barangay_assigned not in CEBU_CITY_BARANGAYS:
        return jsonify(success=False, message='Please select a valid barangay.'), 400
    if years_of_service not in YEARS_OF_SERVICE_OPTIONS:
        return jsonify(success=False, message='Please select a valid years of service option.'), 400
    try:
        if datetime.strptime(dob, '%Y-%m-%d').date() > datetime.now().date():
            return jsonify(success=False, message='Date of birth cannot be in the future.'), 400
    except ValueError:
        return jsonify(success=False, message='Please enter a valid date of birth.'), 400

    if list(db.collection('user_account').where('email', '==', email).limit(1).stream()):
        return jsonify(success=False, message='An account with this email already exists.'), 400
    if list(db.collection('bhw_profile').where('bhw_id_number', '==', bhw_id_number).limit(1).stream()):
        return jsonify(success=False, message='This BHW ID number is already registered.'), 400

    user_ref = db.collection('user_account').document()
    user_ref.set({
        'full_name': full_name, 'email': email,
        'password_hash': generate_password_hash(password),
        'role': 'bhw', 'contact_number': contact_number,
        'approval_status': 'approved',
        'approval_reviewed_at': firestore.SERVER_TIMESTAMP,
        'approval_reviewed_by': session['user_id'],
        'created_at': firestore.SERVER_TIMESTAMP, 'deleted_at': None, 'is_archived': 0,
    })
    db.collection('bhw_profile').add({
        'user_id': user_ref.id,
        'barangay_assigned': barangay_assigned,
        'date_of_birth': dob,
        'health_center': health_center,
        'years_of_service': years_of_service,
        'bhw_id_number': bhw_id_number,
        'valid_id_filename': None,
        'valid_id_stored_as': None,
        'created_at': firestore.SERVER_TIMESTAMP,
    })

    flash(f'{full_name} was added as a BHW and can sign in immediately.', 'success')
    return jsonify(success=True, message='BHW added.')


# ---------- DEVICES ----------

# Serial numbers are pre-registered here by the admin. A family can only sign up
# with a serial number that already exists in this collection.
SERIAL_PATTERN = re.compile(r'^[A-Z0-9][A-Z0-9\-]{3,31}$')
MAX_SERIALS_PER_SUBMIT = 200


def _clean_serial(raw):
    """Normalize one typed serial number, or return None if it is unusable."""
    serial = re.sub(r'\s+', '', (raw or '')).upper()
    return serial if serial and SERIAL_PATTERN.match(serial) else None


@admin_bp.route('/devices')
@admin_required
def devices():
    elders = {doc.id: (doc.to_dict() or {}).get('full_name') or 'Unnamed'
              for doc in db.collection('elder_profile').stream()}

    rows = []
    for doc in db.collection('device').stream():
        device = doc.to_dict() or {}
        elder_id = device.get('elder_id')
        registered = bool(device.get('is_registered'))
        rows.append({
            'serial': doc.id,
            'assigned_to': elders.get(elder_id) if registered else None,
            'elder_id': elder_id if registered else None,
            'status': 'registered' if registered else 'available',
            'added': _fmt_date(device.get('created_at')),
            'registered_at': _fmt_date(device.get('registered_at')),
            'batch': device.get('batch') or '',
            'sort': _sort_key(device.get('created_at')),
        })

    rows.sort(key=lambda r: (r['status'] !=
              'available', -r['sort'], r['serial']))

    stats = {
        'total': len(rows),
        'registered': sum(1 for r in rows if r['status'] == 'registered'),
        'available': sum(1 for r in rows if r['status'] == 'available'),
    }

    return render_template('admin/devices.html', active_page='devices', rows=rows, stats=stats)


@admin_bp.route('/devices/add', methods=['POST'])
@admin_required
def add_devices():
    """Pre-register one or many serial numbers. One serial per line."""
    raw_lines = (request.form.get('serial_numbers')
                 or '').replace(',', '\n').splitlines()
    batch = (request.form.get('batch') or '').strip()

    typed = [line for line in (l.strip() for l in raw_lines) if line]
    if not typed:
        flash('Please enter at least one serial number.', 'error')
        return redirect(url_for('admin.devices'))
    if len(typed) > MAX_SERIALS_PER_SUBMIT:
        flash(
            f'Please add at most {MAX_SERIALS_PER_SUBMIT} serial numbers at a time.', 'error')
        return redirect(url_for('admin.devices'))

    invalid, seen, to_add = [], set(), []
    for line in typed:
        serial = _clean_serial(line)
        if not serial:
            invalid.append(line)
        elif serial not in seen:
            seen.add(serial)
            to_add.append(serial)

    added, existing = [], []
    for serial in to_add:
        ref = db.collection('device').document(serial)
        if ref.get().exists:
            existing.append(serial)
            continue
        ref.set({
            'serial_number': serial,
            'is_registered': False,
            'elder_id': None,
            'batch': batch,
            'created_at': firestore.SERVER_TIMESTAMP,
            'added_by': session['user_id'],
        })
        added.append(serial)

    if added:
        flash(
            f"Pre-registered {len(added)} serial number{'s' if len(added) != 1 else ''}.", 'success')
    if existing:
        flash(
            f"Already in the system, skipped: {_join_sample(existing)}", 'info')
    if invalid:
        flash(
            f"Not a valid serial number, skipped: {_join_sample(invalid)}", 'error')
    return redirect(url_for('admin.devices'))


def _notify_family_device_unregistered(elder_id, elder_name):
    """Pushes a normal (non-emergency) notification to every family account
    linked to this elder when their device gets unregistered -- otherwise
    this is completely silent: no SMS/email gateway exists on the website,
    and the family app would only ever surface it passively (a "No Device"
    status the family has to happen to notice), while emergency-trigger
    capability is gone for that elder in the meantime.

    Sent WITH a `notification` payload (unlike
    firestore_backend.send_emergency_push()'s deliberately data-only
    emergency pushes) so Android auto-displays it in the system tray while
    the app is backgrounded/killed -- the common case -- without needing
    the app's own alarm machinery. Best-effort per-token, mirrors
    get_family_fcm_tokens()'s dedup (same phone can be linked more than
    once)."""
    try:
        links = db.collection('family_elder_link').where(
            'elder_id', '==', elder_id).stream()
        seen = set()
        tokens = []
        for link in links:
            family_user_id = (link.to_dict() or {}).get('family_user_id')
            if not family_user_id:
                continue
            user_doc = db.collection(
                'user_account').document(family_user_id).get()
            token = (user_doc.to_dict() or {}).get(
                'fcm_token') if user_doc.exists else None
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
    except Exception as e:
        print(f'[admin_routes] failed to read family FCM tokens: {e}')
        return

    if not tokens:
        return

    title = 'ALISTO Device Unregistered'
    body = (
        f"{elder_name}'s ALISTO device was unregistered and no longer sends emergency alerts. "
        'Please set up a new device to keep protection active.'
    )
    for token in tokens:
        try:
            messaging.send(messaging.Message(
                token=token,
                notification=messaging.Notification(title=title, body=body),
                data={'type': 'device_unregistered', 'elder_id': elder_id},
                android=messaging.AndroidConfig(priority='high'),
            ))
        except Exception as e:
            print(
                f'[admin_routes] failed to push device-unregistered notice to a token: {e}')


@admin_bp.route('/devices/<serial>/unregister', methods=['POST'])
@admin_required
def unregister_device(serial):
    """Unlink a device from its elder so the serial can be handed to someone else.

    The elder profile and the family accounts linked to it are NOT deleted — they
    simply stop having a device until a new one is registered to them.
    """
    ref = db.collection('device').document(serial)
    doc = ref.get()
    device = doc.to_dict() if doc.exists else None

    if not device:
        flash('That serial number is not in the system.', 'error')
        return redirect(url_for('admin.devices'))
    if not device.get('is_registered'):
        flash('That device is not registered to anyone.', 'error')
        return redirect(url_for('admin.devices'))

    elder_id = device.get('elder_id')
    elder_doc = db.collection('elder_profile').document(
        elder_id).get() if elder_id else None
    elder_name = (elder_doc.to_dict() or {}).get(
        'full_name') if elder_doc and elder_doc.exists else None

    ref.update({
        'is_registered': False,
        'elder_id': None,
        'registered_at': None,
        # Kept for history so you can see who the device used to belong to.
        'previous_elder_id': elder_id,
        'unregistered_at': firestore.SERVER_TIMESTAMP,
        'unregistered_by': session['user_id'],
    })

    if elder_id and elder_name:
        _notify_family_device_unregistered(elder_id, elder_name)

    flash(
        f"{serial} is unregistered and available again."
        + (f" {elder_name} no longer has a device linked." if elder_name else ''),
        'success',
    )
    return redirect(url_for('admin.devices'))


@admin_bp.route('/devices/<serial>/delete', methods=['POST'])
@admin_required
def delete_device(serial):
    """Remove a serial number that was never claimed (typo, wrong batch)."""
    ref = db.collection('device').document(serial)
    doc = ref.get()
    if not doc.exists:
        flash('That serial number is not in the system.', 'error')
    elif (doc.to_dict() or {}).get('is_registered'):
        flash(
            'That device is already linked to an elder, so it cannot be removed.', 'error')
    else:
        ref.delete()
        flash(f'Removed serial number {serial}.', 'success')
    return redirect(url_for('admin.devices'))


def _join_sample(items, limit=5):
    head = ', '.join(items[:limit])
    return head if len(items) <= limit else f'{head} and {len(items) - limit} more'


# ---------- MAP VIEW ----------

MAP_ALERT_FRESH_MINUTES = 120  # see _is_fresh_alert()'s doc comment


def _is_fresh_alert(alert):
    """Whether an open alert happened recently enough to still count as
    "an emergency is happening right now" for the LIVE map specifically --
    an alert nobody ever got around to marking Resolved stays 'is_open'
    (by design, elsewhere in this app that's exactly what should keep
    showing it to a BHW) but that's different from "this elder's pin
    should be pulsing red on the live map days later." Real testing found
    an alert from over a week earlier still rendering as an in-progress
    emergency for exactly that reason. Unlike is_open, this is only ever
    used to decide the map's own coloring/pulsing -- it doesn't change
    whether the alert shows up anywhere else (Alerts pages, dashboard
    counts, reports)."""
    when = ac.local(alert.get('created_at'))
    if not when:
        return False
    return (datetime.now(ac.PH_TIME) - when) <= timedelta(minutes=MAP_ALERT_FRESH_MINUTES)


def _map_points(residents):
    """One dict per resident CURRENTLY in a fresh, unacknowledged
    emergency -- shared by the page's first render and the auto-refresh
    polling endpoint below, so the two can never drift apart in what they
    consider "fresh". Only plotting active emergencies (not every
    resident with a known location) is deliberate: with enough registered
    devices, a marker for every online/offline one buries the ones that
    actually need attention in visual noise. A resident's marker
    disappears again the moment their alert is acknowledged (open_alert
    already becomes None then -- see ac.row_status()/OPEN_STATUSES) or
    goes stale (_is_fresh_alert()) -- there's nothing to plot for someone
    who hasn't pressed anything. The side list and its search/barangay/
    status filters still show everyone, for when an admin wants that."""
    bhw_by_barangay = _bhw_by_barangay()
    points = []
    for r in residents:
        if not (r['latitude'] and r['longitude']):
            continue
        if not (r['open_alert'] and _is_fresh_alert(r['open_alert'])):
            continue
        live_alert = r['open_alert']
        points.append({
            'id': r['id'], 'name': r['name'], 'initials': r['initials'], 'age': r['age'],
            'address': r['short_address'], 'barangay': r['barangay'],
            'latitude': r['latitude'], 'longitude': r['longitude'],
            'status': 'alert',
            'time': r['last_seen'],
            'alert_label': live_alert['type_label'],
            'alert_id': live_alert['id'],
            'bhw_name': (bhw_by_barangay.get(r['barangay']) or {}).get('name'),
        })
    return points


@admin_bp.route('/map')
@admin_required
def map_view():
    residents = _resident_rows()
    alerts = _all_alert_rows()
    barangays = sorted({r['barangay'] for r in residents if r['barangay']})
    located = [r for r in residents if r['latitude'] and r['longitude']]

    stats = {
        'active_alerts': sum(1 for a in alerts if a['is_open']),
        'online': sum(1 for r in residents if r['device_status'] in ('online', 'weak')),
        'offline': sum(1 for r in residents if r['device_status'] == 'offline'),
        'areas': len(barangays),
    }

    return render_template(
        'admin/map_view.html', active_page='map_view',
        residents=residents, points=_map_points(residents), barangays=barangays, stats=stats,
        recent=alerts[:4], missing_location=len(residents) - len(located),
    )


def _live_map_points():
    """Same points as _map_points(_resident_rows()), but cheap enough to poll
    every few seconds: it reads only alerts that are still PENDING or SENT
    (one read when nothing is happening) and then just the residents who are
    in an emergency right now, instead of every elder, device, and alert."""
    newest = {}
    for alert_id, alert in ac.open_alerts(db).items():
        if ac.row_status(alert) not in ac.OPEN_STATUSES or not alert.get('elder_id'):
            continue
        row = {'id': alert_id, 'created_at': alert.get('date_triggered'),
               'device_id': alert.get('device_id') or ''}
        when = ac.local(row['created_at'])
        best = newest.get(alert['elder_id'])
        if not best or (when and (not ac.local(best['created_at']) or when > ac.local(best['created_at']))):
            newest[alert['elder_id']] = row

    fresh = {eid: a for eid, a in newest.items() if _is_fresh_alert(a)}
    if not fresh:
        return []

    bhw_by_barangay = _bhw_by_barangay()
    points = []
    for elder_id, alert in fresh.items():
        elder_doc = db.collection('elder_profile').document(elder_id).get()
        elder = (elder_doc.to_dict() or {}) if elder_doc.exists else {}
        if not elder or elder.get('deleted_at'):
            continue

        serial, device = alert['device_id'], {}
        if serial:
            device_doc = db.collection('device').document(serial).get()
            device = (device_doc.to_dict() or {}) if device_doc.exists else {}
        else:
            for d in db.collection('device').where('elder_id', '==', elder_id).limit(1).stream():
                serial, device = d.id, d.to_dict() or {}
        lat, lng, _address, _recorded_at = ac.latest_device_location(
            db, serial)
        if not (lat and lng):
            continue

        _state, last_seen = _device_state(device)
        name = elder.get('full_name') or 'Unnamed'
        barangay = _barangay_of(elder.get('address'))
        points.append({
            'id': elder_id, 'name': name, 'initials': _initials(name),
            'age': _age(elder.get('date_of_birth')),
            'address': _short_address(elder.get('address')), 'barangay': barangay,
            'latitude': lat, 'longitude': lng,
            'status': 'alert',
            'time': ac.fmt_time(last_seen),
            'alert_label': ac.type_label('emergency'),
            'alert_id': alert['id'],
            'bhw_name': (bhw_by_barangay.get(barangay) or {}).get('name'),
        })
    points.sort(key=lambda p: p['name'].casefold())
    return points


_MAP_CACHE = {'key': None, 'at': 0.0, 'points': []}


@admin_bp.route('/map/points')
@admin_required
def map_points():
    key = tuple(sorted((i, bool(a.get('acknowledged')))
                       for i, a in ac.open_alerts(db).items()))
    now = time.time()
    if _MAP_CACHE['key'] != key or now - _MAP_CACHE['at'] > 15:
        _MAP_CACHE.update(key=key, at=now, points=_live_map_points())
    return jsonify(success=True, points=_MAP_CACHE['points'])


@admin_bp.route('/map/flag/<alert_id>', methods=['POST'])
@admin_required
def flag_alert_for_bhw(alert_id):
    """Admin escalates an alert from the map's popup -- there's no SMS/email
    gateway on this website (only the Pi's own GSM modem can send SMS), so
    this can't push anything to a phone. What it CAN do: mark the alert so
    the BHW portal (which the assigned BHW is already expected to be
    watching) shows it prominently the next time they load it -- see
    bhw_routes.py's alert rows reading admin_flagged."""
    ref = db.collection(ac.ALERT_COLLECTION).document(alert_id)
    doc = ref.get()
    if not doc.exists:
        return jsonify(success=False, message='Alert not found.'), 404

    ref.update({
        'admin_flagged': True,
        'admin_flagged_by': session.get('full_name'),
        'admin_flagged_at': firestore.SERVER_TIMESTAMP,
    })
    ac.write_status_history(db, alert_id, 'FLAGGED', session['user_id'],
                            remarks='Flagged by admin for BHW attention')
    return jsonify(success=True, message='BHW will see this flagged on their portal.')


# ---------- EMERGENCY LOGS ----------

LOG_RANGES = {
    'this_month': 'This Month',
    'last_7': 'Last 7 Days',
    'last_30': 'Last 30 Days',
    'all': 'All Time',
}


def _range_start(key):
    now = datetime.now(ac.PH_TIME)
    if key == 'last_7':
        return now - timedelta(days=7)
    if key == 'last_30':
        return now - timedelta(days=30)
    if key == 'all':
        return None
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@admin_bp.route('/emergency-logs')
@admin_required
def emergency_logs():
    range_key = request.args.get('range', 'all')
    if range_key not in LOG_RANGES:
        range_key = 'all'

    rows = _all_alert_rows(since=_range_start(range_key))
    everything = _all_alert_rows()
    month_start = datetime.now(ac.PH_TIME).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)

    stats = {
        'total': len(everything),
        'active': sum(1 for a in everything if a['is_open']),
        'resolved': sum(1 for a in everything if a['status'] in ac.RESOLVED_STATUSES),
        'avg_response': _fmt_minutes(_avg_response(everything)),
        'this_month': sum(1 for a in everything
                          if (ac.local(a['created_at']) or month_start) >= month_start),
        'month_label': month_start.strftime('%B %Y'),
    }

    return render_template(
        'admin/emergency_logs.html', active_page='emergency_logs',
        rows=rows, stats=stats, ranges=LOG_RANGES, range_key=range_key,
        barangays=sorted({r['barangay'] for r in rows if r['barangay']}),
    )


@admin_bp.route('/emergency-logs/<alert_id>/<action>', methods=['POST'])
@admin_required
def update_alert_status(alert_id, action):
    """Acknowledge / Resolve / False alarm from the Emergency Logs table, the
    same actions the BHW has. Acknowledging stops the alarm and the "needs a
    response" banner on every portal; Resolve and False alarm close it."""
    back = request.referrer or url_for('admin.emergency_logs')
    if action not in ('acknowledge', 'resolve', 'false-alarm'):
        flash('Unknown action.', 'error')
        return redirect(back)

    ref = db.collection(ac.ALERT_COLLECTION).document(alert_id)
    doc = ref.get()
    if not doc.exists:
        flash('That alert no longer exists.', 'error')
        return redirect(back)
    status = ac.row_status(doc.to_dict() or {})
    if status in ac.RESOLVED_STATUSES:
        flash('That alert is already closed.', 'info')
        return redirect(back)

    if action == 'acknowledge':
        if not ac.acknowledge(db, ref, alert_id, session['user_id']):
            flash('That alert was already handled.', 'info')
        else:
            flash('Alert acknowledged. The alarm has stopped for everyone.', 'success')
    else:
        ac.resolve(db, ref, alert_id, session['user_id'],
                   false_alarm=(action == 'false-alarm'),
                   notes=(request.form.get('resolution_note') or '').strip()[:300] or None)
        flash('Alert marked as a false alarm.' if action == 'false-alarm'
              else 'Alert resolved.', 'success')
    return redirect(back)


@admin_bp.route('/emergency-logs.csv')
@admin_required
def download_logs():
    rows = _all_alert_rows(since=_range_start(
        request.args.get('range', 'all')))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Alert ID', 'Device', 'Resident', 'Age', 'Location', 'Barangay',
                     'Type', 'Severity', 'Status', 'Date & Time', 'Response (minutes)', 'Notes'])
    for r in rows:
        writer.writerow([r['code'], r['device'], r['name'], r['age'] or '', r['location'],
                         r['barangay'], r['type_label'], r['severity'].title(
        ), r['status_label'],
            r['full_time'],
            '' if r['response_minutes'] is None else round(
                             r['response_minutes']),
            r['notes']])
    return _csv_response(buffer, 'emergency-logs')


def _csv_response(buffer, name):
    filename = f"alisto-{name}-{datetime.now(ac.PH_TIME).strftime('%Y-%m-%d')}.csv"
    return Response(
        '\ufeff' + buffer.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


# ---------- REPORTS ----------

@admin_bp.route('/reports')
@admin_required
def reports():
    range_key = request.args.get('range', 'last_30')
    if range_key not in LOG_RANGES:
        range_key = 'last_30'

    start = _range_start(range_key)
    rows = _all_alert_rows(since=start)
    residents = _resident_rows()
    devices = list(db.collection('device').stream())
    users = _active_docs('user_account')

    stats = {
        'total_devices': len(devices),
        'online_devices': sum(1 for r in residents if r['device_status'] in ('online', 'weak')),
        'total_alerts': len(rows),
        'resolved': sum(1 for r in rows if r['status'] in ac.RESOLVED_STATUSES),
        'avg_response': _fmt_minutes(_avg_response(rows)),
        'active_users': len(users),
    }
    stats['online_pct'] = round(
        stats['online_devices'] / (stats['total_devices'] or 1) * 100, 1)
    stats['resolved_pct'] = round(
        stats['resolved'] / (len(rows) or 1) * 100, 1)

    # Alerts per day across the range, for the line chart.
    now = datetime.now(ac.PH_TIME)
    window_start = start or (now - timedelta(days=30))
    days = max((now - window_start).days, 1)
    step = max(days // 12, 1)
    buckets, labels = [], []
    for i in range(0, days + 1, step):
        day = (window_start + timedelta(days=i)).date()
        buckets.append(day)
        labels.append(day.strftime('%b %d'))

    per_bucket = [0] * len(buckets)
    resolved_bucket = [0] * len(buckets)
    for row in rows:
        moment = ac.local(row['created_at'])
        if not moment:
            continue
        index = min(max((moment.date() - window_start.date()
                         ).days // step, 0), len(buckets) - 1)
        per_bucket[index] += 1
        if row['status'] in ac.RESOLVED_STATUSES:
            resolved_bucket[index] += 1

    by_type = Counter(r['type'] for r in rows)
    type_breakdown = [{
        'label': ac.type_label(key),
        'count': count,
        'pct': round(count / (len(rows) or 1) * 100, 1),
        'colour': ac.type_colour(key),
    } for key, count in by_type.most_common()]

    by_place = Counter(r['barangay'] or 'Unknown' for r in rows)
    top_places = [{
        'name': place,
        'count': count,
        'pct': round(count / (len(rows) or 1) * 100, 1),
    } for place, count in by_place.most_common(5)]

    # One summary row per barangay.
    summary = defaultdict(lambda: {'devices': 0, 'online': 0, 'alerts': 0,
                                   'resolved': 0, 'residents': 0, 'minutes': []})
    for r in residents:
        place = summary[r['barangay'] or 'Unknown']
        place['residents'] += 1
        place['devices'] += len(r['device_ids'])
        if r['device_status'] in ('online', 'weak'):
            place['online'] += len(r['device_ids'])
    for r in rows:
        place = summary[r['barangay'] or 'Unknown']
        place['alerts'] += 1
        if r['status'] in ac.RESOLVED_STATUSES:
            place['resolved'] += 1
        if r['response_minutes'] is not None:
            place['minutes'].append(r['response_minutes'])

    barangay_rows = []
    for name, data in sorted(summary.items(), key=lambda kv: -kv[1]['alerts']):
        minutes = data['minutes']
        barangay_rows.append({
            'name': name,
            'devices': data['devices'],
            'online': data['online'],
            'online_pct': round(data['online'] / (data['devices'] or 1) * 100, 1),
            'alerts': data['alerts'],
            'resolved': data['resolved'],
            'resolved_pct': round(data['resolved'] / (data['alerts'] or 1) * 100, 1),
            'residents': data['residents'],
            'avg_response': _fmt_minutes(round(sum(minutes) / len(minutes)) if minutes else None),
        })

    peak = max(per_bucket + [1])
    return render_template(
        'admin/reports.html', active_page='reports',
        stats=stats, ranges=LOG_RANGES, range_key=range_key,
        chart={
            'labels': labels,
            'alerts': per_bucket,
            'resolved': resolved_bucket,
            'peak': peak,
            'points': _spark_points(per_bucket, peak),
            'resolved_points': _spark_points(resolved_bucket, peak),
        },
        type_breakdown=type_breakdown, top_places=top_places, barangay_rows=barangay_rows,
    )


def _spark_points(values, peak):
    """Turn counts into 'x,y x,y ...' for an SVG polyline in a 100x100 box."""
    if len(values) < 2:
        values = values * 2 or [0, 0]
    step = 100 / (len(values) - 1)
    return ' '.join(f'{i * step:.2f},{100 - (v / peak * 100):.2f}' for i, v in enumerate(values))


@admin_bp.route('/reports.csv')
@admin_required
def download_report():
    rows = _all_alert_rows(since=_range_start(
        request.args.get('range', 'last_30')))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Alert ID', 'Resident', 'Barangay', 'Type', 'Severity', 'Status',
                     'Date & Time', 'Response (minutes)'])
    for r in rows:
        writer.writerow([r['code'], r['name'], r['barangay'], r['type_label'],
                         r['severity'].title(), r['status_label'], r['full_time'],
                         '' if r['response_minutes'] is None else round(r['response_minutes'])])
    return _csv_response(buffer, 'report')


# ---------- SUBSCRIPTIONS ----------

# Plans are defined here rather than in the database so the prices stay in one
# place. A subscription document keeps the plan key, and the price is read back
# from this table.
PLANS = {
    'basic': {'name': 'Basic Plan', 'price': 299, 'devices': 1, 'blurb': '1 Device'},
    'premium': {'name': 'Premium Plan', 'price': 599, 'devices': 5, 'blurb': 'Up to 5 Devices'},
}
EXPIRING_SOON_DAYS = 30


def _plan(key):
    return PLANS.get(key or 'basic', PLANS['basic'])


def _subscription_rows():
    """Subscriptions joined to whoever they belong to.

    A document in the `subscription` collection looks like:
        elder_id | user_id   who it is for (one of the two)
        plan                 'basic' or 'premium'
        start_date           timestamp
        expiry_date          timestamp
        auto_renew           boolean
        device_serials       list of strings, optional
    """
    elders = _all_elders()
    accounts = dict(_active_docs('user_account'))
    devices_by_elder = _devices_by_elder()
    today = datetime.now(ac.PH_TIME).date()

    rows = []
    for doc in db.collection('subscription').stream():
        sub = doc.to_dict() or {}
        elder_id, user_id = sub.get('elder_id'), sub.get('user_id')

        if elder_id and elder_id in elders:
            owner, kind, prefix = elders[elder_id], 'elderly', 'ELD'
            owner_id = elder_id
            serials = sub.get('device_serials') or [
                d for d, _ in devices_by_elder.get(elder_id, [])]
        elif user_id and user_id in accounts:
            owner = accounts[user_id]
            kind = owner.get('role') or 'family'
            prefix = 'BHW' if kind == 'bhw' else 'FAM'
            owner_id = user_id
            serials = sub.get('device_serials') or []
        else:
            continue  # points at someone who no longer exists

        expiry = ac.local(sub.get('expiry_date'))
        days_left = (expiry.date() - today).days if expiry else None

        if days_left is None:
            status = 'active'
        elif days_left < 0:
            status = 'expired'
        elif days_left <= EXPIRING_SOON_DAYS:
            status = 'expiring'
        else:
            status = 'active'

        plan = _plan(sub.get('plan'))
        name = owner.get('full_name') or 'Unnamed'
        rows.append({
            'id': doc.id,
            'name': name,
            'initials': _initials(name),
            'display_id': _short_id(prefix, owner_id),
            'type': kind,
            'type_label': 'BHW' if kind == 'bhw' else kind.title(),
            'plan_key': sub.get('plan') or 'basic',
            'plan': plan['name'],
            'plan_blurb': plan['blurb'],
            'price': plan['price'],
            'devices': serials,
            'start': _fmt_date(sub.get('start_date')),
            'expiry': _fmt_date(sub.get('expiry_date')),
            'days_left': days_left,
            'status': status,
            'auto_renew': bool(sub.get('auto_renew')),
            'sort': _sort_key(sub.get('expiry_date')),
        })

    rows.sort(key=lambda r: (('expired', 'expiring', 'active').index(
        r['status']), r['name'].casefold()))
    return rows


def _pending_payments():
    """Every payment a family submitted a GCash reference number for but
    that hasn't been reviewed yet -- see app.py's /care-plan/pay (writes
    these as status='pending') and care_plan_service.apply_approved_payment()
    (what actually applies one, called from update_payment() below).
    Nothing wired these together before now, so a submitted payment sat in
    Firestore forever with no admin-facing way to see or act on it."""
    rows = []
    try:
        docs = (db.collection('payment')
                .where('status', '==', 'pending').stream())
    except Exception as e:
        print(f'[subscriptions] could not read pending payments: {e}')
        return rows
    for doc in docs:
        p = doc.to_dict() or {}
        rows.append({
            'id': doc.id,
            'payment_no': p.get('payment_no') or doc.id[:6].upper(),
            'elder_name': p.get('elder_name') or 'Unknown',
            'payer_name': p.get('payer_name') or '—',
            'reference_no': p.get('reference_no') or '—',
            'method': (p.get('method') or 'gcash').upper(),
            'months': p.get('months') or 1,
            'amount': p.get('amount') or 0,
            'currency': p.get('currency') or 'PHP',
            'submitted': ac.fmt_time(p.get('created_at')),
            'sort': _sort_key(p.get('created_at')),
        })
    rows.sort(key=lambda r: r['sort'], reverse=True)
    return rows


@admin_bp.route('/inbox')
@admin_required
def inbox():
    """Support tickets, problem reports, care plan payments, messages, and
    announcements. The page talks to the JSON API in inbox_routes.py."""
    return render_template('admin/inbox.html', active_page='inbox')


@admin_bp.route('/subscriptions')
@admin_required
def subscriptions():
    rows = _subscription_rows()
    active = [r for r in rows if r['status'] in ('active', 'expiring')]

    stats = {
        'total': len(rows),
        'active': len(active),
        'active_pct': round(len(active) / (len(rows) or 1) * 100, 1),
        'expiring': sum(1 for r in rows if r['status'] == 'expiring'),
        'expired': sum(1 for r in rows if r['status'] == 'expired'),
        'revenue': sum(r['price'] for r in active),
    }

    return render_template(
        'admin/subscriptions.html', active_page='subscriptions',
        rows=rows, stats=stats, plans=PLANS, expiring_days=EXPIRING_SOON_DAYS,
        pending_payments=_pending_payments(), reject_reasons=cps.REJECT_REASONS,
    )


@admin_bp.route('/subscriptions/payments/<payment_id>/<action>', methods=['POST'])
@admin_required
def update_payment(payment_id, action):
    if action not in ('approve', 'reject'):
        flash('Invalid action.', 'error')
        return redirect(url_for('admin.subscriptions'))

    ref = db.collection('payment').document(payment_id)
    doc = ref.get()
    payment = doc.to_dict() if doc.exists else None
    if not payment or payment.get('status') != 'pending':
        flash('That payment is no longer pending.', 'error')
        return redirect(url_for('admin.subscriptions'))

    if action == 'reject':
        ok, error = cps.reject_payment(
            ref, payment, session['user_id'], session.get('full_name'),
            request.form.get('reason_key'), request.form.get('admin_note'))
        if not ok:
            flash(error, 'error')
        else:
            flash(f"Payment {payment.get('payment_no') or payment_id[:6].upper()} rejected. "
                  "The family can see your message.", 'success')
        return redirect(url_for('admin.subscriptions'))

    try:
        cps.apply_approved_payment(
            ref, payment, session['user_id'], session.get('full_name'))
        flash(
            f"Payment {payment.get('payment_no') or payment_id[:6].upper()} "
            f"approved -- {payment.get('elder_name') or 'the resident'}'s "
            f"plan has been extended.", 'success')
    except Exception as e:
        flash(f'Could not apply this payment: {e}', 'error')
    return redirect(url_for('admin.subscriptions'))


@admin_bp.route('/subscriptions.csv')
@admin_required
def download_subscriptions():
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Subscriber', 'ID', 'User Type', 'Plan', 'Price (PHP/month)',
                     'Devices', 'Status', 'Start Date', 'Expiry Date', 'Auto Renew'])
    for r in _subscription_rows():
        writer.writerow([r['name'], r['display_id'], r['type_label'], r['plan'], r['price'],
                         ', '.join(
                             r['devices']) or 'None', r['status'].title(),
                         r['start'], r['expiry'], 'Yes' if r['auto_renew'] else 'No'])
    return _csv_response(buffer, 'subscriptions')


# ---------- SETTINGS (account settings) ----------

# Timezone is fixed: ALISTO only runs in Cebu, so there is nothing to choose.
TIMEZONES = ['Asia/Manila (GMT+08:00)']
DATE_FORMATS = ['MMM DD, YYYY', 'DD/MM/YYYY', 'YYYY-MM-DD']
SYSTEM_DOC = 'system_settings'


def _system_settings():
    doc = db.collection('app_settings').document(SYSTEM_DOC).get()
    saved = (doc.to_dict() or {}) if doc.exists else {}
    last_backup_at = saved.get('last_backup_at')
    return {
        'timezone': TIMEZONES[0],
        'date_format': saved.get('date_format') or DATE_FORMATS[0],
        'auto_backup': bool(saved.get('auto_backup', True)),
        # Deliberately NOT _fmt_date() -- that reads these very settings
        # (via _display_settings()'s g-cache) to know how to format a
        # date, which would make this call recurse into itself. This one
        # field stays a plain fixed format, and also needs to work when
        # called from the auto-backup loop in app.py, which runs on a
        # background thread with no Flask request/g to cache onto at all.
        'last_backup': last_backup_at.strftime('%b %d, %Y %I:%M %p') if hasattr(last_backup_at, 'strftime') else '—',
    }


# Core collections a restore would actually need -- device_location and the
# various log/history collections are left out (high write volume, low
# value to restore from a snapshot) to keep each backup file a reasonable
# size.
BACKUP_DIR = os.path.join(os.path.dirname(__file__), 'backups')
BACKUP_COLLECTIONS = [
    'user_account', 'elder_profile', 'device', 'bhw_profile',
    'family_elder_link', 'emergency_alert', 'subscription',
]


def _json_default(value):
    """json.dump()'s fallback for anything it can't serialize natively --
    Firestore timestamps mainly, which come back as DatetimeWithNanoseconds
    objects."""
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def _run_backup():
    """The actual job behind the 'Auto Backup' toggle -- previously that
    toggle just saved a preference with nothing reading it, so 'Last
    backup' never had a real date to show. Dumps BACKUP_COLLECTIONS to one
    timestamped JSON file on disk and records when/where. Best-effort
    is deliberately NOT used here -- if this fails, the caller (the manual
    "Backup Now" button, or the periodic auto-backup loop in app.py) needs
    to know, so it's allowed to raise. Returns (filename, total_doc_count)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    snapshot = {}
    total = 0
    for name in BACKUP_COLLECTIONS:
        docs = {doc.id: doc.to_dict() for doc in db.collection(name).stream()}
        snapshot[name] = docs
        total += len(docs)

    filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(os.path.join(BACKUP_DIR, filename), 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, default=_json_default, indent=2)

    db.collection('app_settings').document(SYSTEM_DOC).set({
        'last_backup_at': firestore.SERVER_TIMESTAMP,
        'last_backup_file': filename,
    }, merge=True)
    return filename, total


@admin_bp.route('/settings/backup-now', methods=['POST'])
@admin_required
def backup_now():
    try:
        filename, total = _run_backup()
        flash(f'Backup created: {filename} ({total} records).', 'success')
    except Exception as e:
        flash(f'Backup failed: {e}', 'error')
    return redirect(url_for('admin.settings'))


@admin_bp.route('/settings')
@admin_required
def settings():
    return render_template(
        'admin/settings.html', active_page='settings',
        system=_system_settings(), date_formats=DATE_FORMATS,
        phone=(db.collection('user_account').document(session['user_id']).get()
               .to_dict() or {}).get('contact_number') or '',
    )


@admin_bp.route('/settings', methods=['POST'])
@admin_required
def save_settings():
    date_choice = request.form.get('date_format')

    db.collection('app_settings').document(SYSTEM_DOC).set({
        'date_format': date_choice if date_choice in DATE_FORMATS else DATE_FORMATS[0],
        'auto_backup': request.form.get('auto_backup') == 'on',
        'updated_at': firestore.SERVER_TIMESTAMP,
        'updated_by': session['user_id'],
    }, merge=True)  # merge, not overwrite -- this doc also carries
    # last_backup_at/last_backup_file (see _run_backup()), which a plain
    # .set() here would silently wipe out on every ordinary Save Changes.

    flash('Your settings have been saved.', 'success')
    return redirect(url_for('admin.settings'))


@admin_bp.route('/alerts/poll')
@admin_required
def alerts_poll():
    """Unanswered emergencies anywhere in the system. The admin pages ask for
    this every 15 seconds (bhw_alert_watch.js, same alarm as BHW/Family) so
    the admin hears an emergency even if no one has responded. Only reads
    alerts that are still PENDING or SENT."""
    elders = _cached('poll_elders', _all_elders)
    rows = []
    for alert_id, a in ac.open_alerts(db).items():
        if ac.row_status(a) in ac.OPEN_STATUSES:
            rows.append((ac.local(a.get('date_triggered')), alert_id, a))
    oldest = datetime.min.replace(tzinfo=ac.PH_TIME)
    rows.sort(key=lambda r: r[0] or oldest, reverse=True)
    return jsonify(success=True, count=len(rows), alerts=[{
        'id': alert_id,
        'resident': (elders.get(a.get('elder_id')) or {}).get('full_name') or 'A resident',
        'time': ac.fmt_time(a.get('date_triggered')),
        'trigger': (a.get('trigger_type') or 'VOICE').lower(),
        'url': url_for('admin.emergency_logs'),
    } for _, alert_id, a in rows[:20]])

# ---------- MY PROFILE ----------


@admin_bp.route('/profile')
@admin_required
def profile():
    """Same behaviour as the family My Profile page (see account_profile.py)."""
    doc = db.collection('user_account').document(session['user_id']).get()
    user = doc.to_dict() if doc.exists else {}
    admins = sum(1 for _, u in _active_docs(
        'user_account') if u.get('role') == 'admin')

    return render_template(
        'admin/profile.html', active_page='profile',
        me=account_profile.view(user),
        role_label='System Administrator',
        member_since=_fmt_date(user.get('created_at')),
        details=[
            ('Role', 'System Administrator'),
            ('Account Status', 'Active'),
            ('Last Login', session.get('previous_login') or 'First sign-in'),
            ('Password Last Changed', _fmt_date(user.get('password_changed_at'))),
            ('Administrators', admins),
            ('Two-Factor Authentication', 'Disabled'),
        ],
        urls={
            'update': url_for('admin.edit_profile'),
            'password': url_for('admin.change_password'),
            'photo': url_for('admin.upload_photo'),
            'photo_remove': url_for('admin.remove_photo'),
        },
    )


@admin_bp.route('/profile/password', methods=['POST'])
@admin_required
def change_password():
    ok, message = account_profile.change_password(
        session['user_id'], request.form)
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('admin.profile'))


@admin_bp.route('/profile/edit', methods=['POST'])
@admin_required
def edit_profile():
    ok, message, name = account_profile.update_details(
        session['user_id'], request.form)
    if ok:
        session['full_name'] = name
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('admin.profile'))


@admin_bp.route('/profile/photo', methods=['POST'])
@admin_required
def upload_photo():
    ok, message = account_profile.save_photo(
        session['user_id'], request.form.get('photo_base64'))
    _PHOTO_CACHE.pop(session['user_id'], None)
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('admin.profile'))


@admin_bp.route('/profile/photo/remove', methods=['POST'])
@admin_required
def remove_photo():
    ok, message = account_profile.remove_photo(session['user_id'])
    _PHOTO_CACHE.pop(session['user_id'], None)
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('admin.profile'))
