"""Notification bell for BHW and Admin (same behavior as the Family bell).

    * Items STAY in the list. A read item turns gray.
    * The red badge counts UNREAD items only.
    * Emergency: red, pinned on top, and it does NOT turn gray when clicked.
      It stays unread until a BHW/admin responds (it leaves the open-alert list).
    * A message opens its conversation in Inbox (?t=<thread id>), and the
      Inbox marks it read.
    * Other items (offline device, pending BHW, expiring subscription) are
      marked read when clicked. Read ids are saved in
      user_account/{id}.notif_read_ids -- the same field the Family bell uses.
    * "Mark all as read" marks events + messages. Emergencies are skipped.

    BHW sees   emergencies in their barangay, offline devices in their
               barangay, messages.
    Admin sees emergencies, pending BHW registrations, offline devices,
               expiring subscriptions, messages (tickets are inbox threads).

Reads (kept small, same idea as notifications.py):
    /rbell/summary   page load + every 60s. Emergencies come from memory (0
                     reads); the rest is cached for 50s (~2 reads).
    /rbell           only when the bell is opened.
    /rbell/read      clicking an event item (1 write) or "Mark all as read".
"""
import hashlib
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, session, url_for
from firebase_admin import firestore

import alerts_common as ac
import inbox_service as inbox
import notifications as fam          # reuses its per-user "already read" set
from database import db

rbell_bp = Blueprint('role_bell', __name__, url_prefix='/rbell')

FEED_DAYS = 7
PANEL_LIMIT = 25
SUMMARY_CACHE_SECONDS = 50
BARANGAY_CACHE_SECONDS = 300
EVENT_CACHE_SECONDS = 120

_barangay_cache = {}     # user_id -> (time, barangay)
_event_cache = {}        # user_id -> (time, [event items])
_summary_cache = {}      # user_id -> (time, count of unread events + threads)


# ---------- helpers ----------

def _role():
    role = session.get('role')
    return role if 'user_id' in session and role in ('bhw', 'admin') else None


def _rid(kind, *parts):
    """Stable id for an event, e.g. 'device:3fa91c0b22'. The same device going
    offline AGAIN (new last-seen time) gets a new id, so it shows up unread."""
    raw = '|'.join(str(p) for p in parts)
    return f"{kind}:{hashlib.md5(raw.encode()).hexdigest()[:10]}"


def _barangay(user_id):
    hit = _barangay_cache.get(user_id)
    if hit and time.time() - hit[0] < BARANGAY_CACHE_SECONDS:
        return hit[1]
    from bhw_routes import _bhw_profile
    value = _bhw_profile(user_id).get('barangay_assigned') or ''
    _barangay_cache[user_id] = (time.time(), value)
    return value


def _cached_events(user_id, role):
    hit = _event_cache.get(user_id)
    if hit and time.time() - hit[0] < EVENT_CACHE_SECONDS:
        return hit[1]
    events = _bhw_events(user_id) if role == 'bhw' else _admin_events()
    _event_cache[user_id] = (time.time(), events)
    return events


# ---------- emergencies (from memory, 0 reads) ----------

def _emergency_items(user_id, role):
    items = []
    if role == 'bhw':
        from bhw_routes import _elders_in_barangay, _open_alerts
        elders = _elders_in_barangay(_barangay(user_id))
        for a in _open_alerts(elders):
            who = (elders.get(a.get('elder_id')) or {}).get(
                'full_name') or 'A resident'
            how = ('Pressed the ALISTO device'
                   if (a.get('trigger_type') or '').upper() == 'BUTTON'
                   else 'Called for help')
            items.append(_item(
                f"alert:{a['id']}", 'emergency', 'siren', 'tint-red', True,
                who, f'{how} — still active', ac.fmt_time(
                    a.get('date_triggered')),
                inbox.to_ms(a.get('date_triggered')),
                url_for('bhw.alerts', alert=a['id']), pinned=True))
    else:
        import admin_routes as ar
        elders = ar._cached('poll_elders', ar._all_elders)
        for aid, a in ac.open_alerts(db).items():
            if ac.row_status(a) not in ac.OPEN_STATUSES:
                continue
            who = (elders.get(a.get('elder_id')) or {}).get(
                'full_name') or 'Unknown resident'
            items.append(_item(
                f'alert:{aid}', 'emergency', 'siren', 'tint-red', True,
                who, f"{ac.type_label('emergency')} — still active",
                ac.fmt_time(a.get('date_triggered')),
                inbox.to_ms(a.get('date_triggered')),
                url_for('admin.emergency_logs'), pinned=True))
    return items


# ---------- events (Firestore, cached) ----------

def _bhw_events(user_id):
    from bhw_routes import (_alerts_for, _device_state, _devices_by_elder,
                            _elders_in_barangay)
    elders = _elders_in_barangay(_barangay(user_id))
    out = []
    for elder_id, devices in _devices_by_elder(elders).items():
        name = (elders.get(elder_id) or {}).get('full_name') or 'A resident'
        for doc_id, device in devices:
            state, last_seen = _device_state(device)
            if state != 'offline':
                continue
            ms = inbox.to_ms(last_seen)
            out.append(_item(
                _rid('device', doc_id, ms), 'device', 'wifi-off', 'tint-orange',
                False, name, f'Device {doc_id} is offline',
                ac.fmt_time(last_seen) if last_seen else 'Unknown', ms,
                url_for('bhw.elderly_users')))

    # False alarms the resident cancelled on the device (module 3.3).
    # Last 7 days only. Alerts a BHW marked as a false alarm themselves
    # (resolved_by is set) are skipped, since that BHW already knows.
    since = datetime.now(timezone.utc) - timedelta(days=FEED_DAYS)
    for a in _alerts_for(set(elders), since=since, limit=20):
        if ac.row_status(a) != 'false_alarm' or a.get('resolved_by'):
            continue
        name = (elders.get(a.get('elder_id')) or {}).get(
            'full_name') or 'A resident'
        out.append(_item(
            f"cancel:{a['id']}", 'false_alarm', 'flag', 'tint-orange', False,
            name, 'False alarm \u2014 cancelled on the device',
            ac.fmt_time(a.get('date_triggered')),
            inbox.to_ms(a.get('date_triggered')),
            url_for('bhw.alerts', alert=a['id'])))
    return out


def _admin_events():
    import admin_routes as ar
    out = []
    # ar._notif_items() already builds BHW registrations, offline devices and
    # expiring subscriptions (cached 5 min). Emergencies are skipped here --
    # they come from _emergency_items with their own ids.
    for it in ar._notif_items():
        if it['kind'] == 'emergency':
            continue
        # 'sort' = a real timestamp
        timed = it['kind'] in ('bhw', 'device')
        ms = int(it['sort'] * 1000) if timed else 0
        # 'when' and 'subtitle' change over time ("5 min ago", "expires in 9
        # days"), so they can't be part of the id or a read item would turn
        # unread again. Use the fixed timestamp / expiry date instead.
        out.append(_item(
            _rid(it['kind'], it['title'], it['sort'] if timed else it['when']),
            it['kind'], it['icon'], it['tint'], False,
            it['title'], it['subtitle'], it['when'], ms, it['href']))
    return out


def _message_items(user_id, role, only_unread=False):
    key = inbox.me_key(user_id, role)
    page = 'bhw.inbox' if role == 'bhw' else 'admin.inbox'
    window_ms = (datetime.now(timezone.utc) -
                 timedelta(days=FEED_DAYS)).timestamp() * 1000
    items = []
    for t in inbox.list_threads(key):
        if only_unread and not t['unread']:
            continue
        if not t['unread'] and t['last_at'] < window_ms:
            continue
        items.append(_item(
            f"msg:{t['id']}", 'message', 'message-circle', 'tint-blue',
            bool(t['unread']), t['with'] or t['subject'],
            t['preview'] or t['subject'],
            ac.fmt_time(datetime.fromtimestamp(
                t['last_at'] / 1000, tz=timezone.utc))
            if t['last_at'] else '',
            t['last_at'], url_for(page, t=t['id'])))
    return items


def _item(item_id, kind, icon, tint, unread, title, subtitle, when, ms, href,
          pinned=False):
    return {'id': item_id, 'kind': kind, 'icon': icon, 'tint': tint,
            'unread': unread, 'pinned': pinned, 'title': title,
            'subtitle': subtitle, 'when': when, 'ms': ms, 'href': href}


# ---------- count (polled) ----------

def _count(user_id, role, fresh=False):
    """Unread = open emergencies (memory) + unread events + threads with unread."""
    emergencies = len(_emergency_items(user_id, role))

    hit = _summary_cache.get(user_id)
    if hit and not fresh and time.time() - hit[0] < SUMMARY_CACHE_SECONDS:
        return emergencies + hit[1]

    rest = 0
    key = inbox.me_key(user_id, role)
    try:   # conversations with unread messages: one COUNT, ~1 read
        result = (db.collection(inbox.THREADS)
                  .where(inbox.unread_field(key), '>', 0).count().get())
        rest += int(result[0][0].value or 0)
    except Exception as e:
        print(f'[role_bell] messages: {e}')
    try:
        read = fam._read_set(user_id)
        rest += sum(1 for e in _cached_events(user_id, role)
                    if e['id'] not in read)
    except Exception as e:
        print(f'[role_bell] events: {e}')

    _summary_cache[user_id] = (time.time(), rest)
    return emergencies + rest


@rbell_bp.route('/summary')
def summary():
    role = _role()
    if not role:
        return jsonify(success=False), 401
    fresh = request.args.get('fresh') == '1'
    return jsonify(success=True, count=_count(session['user_id'], role, fresh))


# ---------- the panel (on open) ----------

@rbell_bp.route('')
def panel():
    role = _role()
    if not role:
        return jsonify(success=False), 401
    user_id = session['user_id']

    items = _emergency_items(user_id, role)
    try:
        read = fam._read_set(user_id)
        for e in _cached_events(user_id, role):
            items.append({**e, 'unread': e['id'] not in read})
    except Exception as e:
        print(f'[role_bell] events: {e}')
    try:
        items += _message_items(user_id, role)
    except Exception as e:
        print(f'[role_bell] messages: {e}')

    # Emergencies always first, then newest first.
    items.sort(key=lambda i: (not i['pinned'], -i['ms']))
    items = items[:PANEL_LIMIT]
    return jsonify(success=True, items=items,
                   unread_count=sum(i['unread'] for i in items))


# ---------- mark read ----------

@rbell_bp.route('/read', methods=['POST'])
def mark_read():
    """{'id': 'device:..'}  -> mark that event read.
       {'all': true}        -> mark every event + every conversation read.
    Emergencies are never marked here, and messages are normally marked read
    by the Inbox itself when the conversation opens."""
    role = _role()
    if not role:
        return jsonify(success=False), 401
    user_id = session['user_id']
    body = request.get_json(silent=True) or {}

    ids = []
    if body.get('all'):
        ids = [e['id'] for e in _cached_events(user_id, role)]
        key = inbox.me_key(user_id, role)
        for t in _message_items(user_id, role, only_unread=True):
            ref, data = inbox.get_thread(t['id'][4:], key)
            if ref:
                inbox.mark_read(ref, data, key)
    else:
        one = str(body.get('id') or '')
        if one and not one.startswith(('alert:', 'msg:')) and len(one) <= 120:
            ids = [one]

    read = fam._read_set(user_id)
    new = [i for i in ids if i not in read]
    if new:
        db.collection('user_account').document(user_id).update(
            {'notif_read_ids': firestore.ArrayUnion(new)})
        with fam._lock:
            read.update(new)
    _summary_cache.pop(user_id, None)
    return jsonify(success=True)
