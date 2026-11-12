"""Family notification bell (the dropdown in every family page's header).

The panel lists the last 7 days, newest first. Items STAY in the list; an
item that has been read turns gray. The count goes down one by one:

    message   counts while the conversation has unread messages. Clicking it
              opens the conversation in Inbox, which marks it read.
    medicine  (Taken / Missed) counts until it is clicked once.
    alert     counts while the emergency is still waiting for a response.
              Clicking does NOT clear it -- only a BHW/admin responding does.

Emergencies never depend on this bell: the siren, banner and the red Alerts
badge in the sidebar come from /alerts/poll every 15 seconds.

Reads (kept small on purpose):
    /notifications/summary  on page load + every 60s, ~2 reads (cached 50s)
    /notifications          only when the bell is opened, ~10-30 reads
    /notifications/read     clicking a medicine item, 0 reads (1 write)
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, session, url_for
from firebase_admin import firestore

import alerts_common as ac
import inbox_service as inbox
from database import db

notif_bp = Blueprint('notifications', __name__, url_prefix='/notifications')

FEED_DAYS = 7            # the panel shows the last 7 days
PANEL_LIMIT = 20         # items in the panel
SUMMARY_CACHE_SECONDS = 50
ELDERS_CACHE_SECONDS = 60
MED_TITLES = ('Medicine taken', 'Missed medicine')   # see DEVICE_MED_STATUSES

_lock = threading.Lock()
_elders_cache = {}       # user_id -> (time, {elder_id: name})
_summary_cache = {}      # user_id -> (time, count)
_read_ids = {}           # user_id -> set of medicine item ids already clicked
_med_cache = {}          # user_id -> {'rows': {doc_id: row}, 'newest': datetime}


# ---------- helpers ----------

def _is_family():
    return 'user_id' in session and session.get('role') == 'family'


def _window():
    return datetime.now(timezone.utc) - timedelta(days=FEED_DAYS)


def _ms(value):
    return inbox.to_ms(value) if value else 0


def _elders(user_id):
    """{elder_id: name} of this family's loved ones, kept for a minute."""
    hit = _elders_cache.get(user_id)
    if hit and time.time() - hit[0] < ELDERS_CACHE_SECONDS:
        return hit[1]
    names = {}
    for link in (db.collection('family_elder_link')
                 .where('family_user_id', '==', user_id).stream()):
        elder_id = (link.to_dict() or {}).get('elder_id')
        if not elder_id or elder_id in names:
            continue
        doc = db.collection('elder_profile').document(elder_id).get()
        data = (doc.to_dict() or {}) if doc.exists else {}
        if data and not data.get('deleted_at'):
            names[elder_id] = data.get('full_name') or 'Your loved one'
    _elders_cache[user_id] = (time.time(), names)
    return names


def _read_set(user_id):
    """Medicine items this account already clicked. 1 read per server start."""
    with _lock:
        if user_id in _read_ids:
            return _read_ids[user_id]
    doc = db.collection('user_account').document(user_id).get()
    ids = set(((doc.to_dict() or {}).get('notif_read_ids') or [])
              if doc.exists else [])
    with _lock:
        _read_ids[user_id] = ids
    return ids


def _med_rows(user_id, elders):
    """{doc_id: row} of medicine Taken/Missed in the last 7 days.

    Kept in memory and topped up with ONLY the rows newer than the newest
    one already known, so a poll normally costs 1 read (an empty query)."""
    window = _window()
    cache = _med_cache.get(user_id) or {'rows': {}, 'newest': window}
    since = max(cache['newest'], window)
    docs = (db.collection('activity_log')
            .where('elder_id', 'in', list(elders)[:30])
            .where('created_at', '>', since)
            .order_by('created_at', direction=firestore.Query.DESCENDING)
            .limit(50).stream())
    for d in docs:
        item = d.to_dict() or {}
        created = item.get('created_at')
        if hasattr(created, 'timestamp') and created > cache['newest']:
            cache['newest'] = created
        if item.get('type') == 'med_reminder' and \
                str(item.get('title') or '').startswith(MED_TITLES):
            cache['rows'][d.id] = item
    # Forget rows that fell out of the 7-day window.
    cache['rows'] = {i: r for i, r in cache['rows'].items()
                     if hasattr(r.get('created_at'), 'timestamp')
                     and r['created_at'] >= window}
    _med_cache[user_id] = cache
    return cache['rows']


def _active_alert_ids(elders):
    """Alerts for these loved ones still waiting for a response (memory)."""
    return {aid for aid, a in ac.open_alerts(db).items()
            if a.get('elder_id') in elders and not a.get('deleted_at')
            and ac.row_status(a) in ac.OPEN_STATUSES}


# ---------- count (polled) ----------

def _count(user_id, fresh=False):
    hit = _summary_cache.get(user_id)
    if hit and not fresh and time.time() - hit[0] < SUMMARY_CACHE_SECONDS:
        return hit[1]

    key = inbox.me_key(user_id, 'family')
    total = 0
    try:   # conversations with unread messages -- one COUNT, ~1 read
        result = (db.collection(inbox.THREADS)
                  .where(inbox.unread_field(key), '>', 0).count().get())
        total += int(result[0][0].value or 0)
    except Exception as e:
        print(f'[notifications] messages: {e}')

    elders = _elders(user_id)
    if elders:
        total += len(_active_alert_ids(elders))
        try:
            read = _read_set(user_id)
            total += sum(1 for i in _med_rows(user_id, elders)
                         if f'med:{i}' not in read)
        except Exception as e:
            print(f'[notifications] medicine (index?): {e}')

    _summary_cache[user_id] = (time.time(), total)
    return total


@notif_bp.route('/summary')
def summary():
    if not _is_family():
        return jsonify(success=False), 401
    fresh = request.args.get('fresh') == '1'      # page load: skip the cache
    return jsonify(success=True, count=_count(session['user_id'], fresh))


# ---------- the panel (on open) ----------

@notif_bp.route('')
def panel():
    if not _is_family():
        return jsonify(success=False), 401

    user_id = session['user_id']
    key = inbox.me_key(user_id, 'family')
    window_ms = _window().timestamp() * 1000
    elders = _elders(user_id)
    items = []

    # Messages: every conversation active in the last 7 days, plus any that
    # still has unread messages however old. Read ones show gray.
    try:
        for t in inbox.list_threads(key):
            if not t['unread'] and t['last_at'] < window_ms:
                continue
            items.append({
                'id': f"msg:{t['id']}", 'kind': 'message', 'icon': 'message-circle',
                'tint': 'blue', 'unread': bool(t['unread']),
                'title': t['with'] or t['subject'],
                'subtitle': t['preview'] or t['subject'],
                'when': ac.fmt_time(datetime.fromtimestamp(t['last_at'] / 1000, tz=timezone.utc))
                if t['last_at'] else '',
                'ms': t['last_at'],
                'href': url_for('inbox_page', t=t['id']),
            })
    except Exception as e:
        print(f'[notifications] messages: {e}')

    if elders:
        # Alerts: colored while still waiting for a response.
        active = _active_alert_ids(elders)
        try:
            docs = (db.collection(ac.ALERT_COLLECTION)
                    .where('elder_id', 'in', list(elders)[:30])
                    .where('date_triggered', '>=', _window())
                    .order_by('date_triggered', direction=firestore.Query.DESCENDING)
                    .limit(PANEL_LIMIT).stream())
            for d in docs:
                a = d.to_dict() or {}
                status = ac.row_status(a)
                items.append({
                    'id': f'alert:{d.id}', 'kind': 'alert',
                    'icon': 'siren', 'tint': 'red', 'unread': d.id in active,
                    'active': d.id in active,
                    'title': elders.get(a.get('elder_id')) or 'Your loved one',
                    'subtitle': f"Emergency alert · {ac.status_label(status)}",
                    'when': ac.fmt_time(a.get('date_triggered')),
                    'ms': _ms(a.get('date_triggered')),
                    'href': url_for('alerts', open=d.id),
                })
        except Exception as e:
            print(f'[notifications] alerts (index?): {e}')
        # An emergency still waiting for a response always shows, however old.
        shown = {i['id'] for i in items}
        for aid, a in ac.open_alerts(db).items():
            if aid in active and f'alert:{aid}' not in shown:
                items.append({
                    'id': f'alert:{aid}', 'kind': 'alert',
                    'icon': 'siren', 'tint': 'red', 'unread': True,
                    'active': True,
                    'title': elders.get(a.get('elder_id')) or 'Your loved one',
                    'subtitle': 'Emergency alert · Active',
                    'when': ac.fmt_time(a.get('date_triggered')),
                    'ms': _ms(a.get('date_triggered')),
                    'href': url_for('alerts', open=aid),
                })

        # Medicine Taken / Missed: colored until clicked once.
        try:
            read = _read_set(user_id)
            for doc_id, m in _med_rows(user_id, elders).items():
                missed = str(m.get('title') or '').startswith('Missed')
                items.append({
                    'id': f'med:{doc_id}', 'kind': 'medicine', 'icon': 'pill',
                    'tint': 'red' if missed else 'green',
                    'unread': f'med:{doc_id}' not in read,
                    'title': elders.get(m.get('elder_id')) or 'Your loved one',
                    'subtitle': m.get('title') or 'Medicine update',
                    'when': ac.fmt_time(m.get('created_at')),
                    'ms': _ms(m.get('created_at')),
                    'href': url_for('medication_reminders'),
                })
        except Exception as e:
            print(f'[notifications] medicine (index?): {e}')

    # Active emergencies always on top (newest first), then everything else
    # newest first.
    items.sort(key=lambda i: (not i.get('active'), -(i['ms'] or 0)))
    items = items[:PANEL_LIMIT]
    return jsonify(success=True, items=items,
                   unread_count=sum(i['unread'] for i in items))


@notif_bp.route('/read', methods=['POST'])
def mark_read():
    """Clicking a medicine item marks it read. Messages are marked read by
    the Inbox itself; alerts only by a BHW/admin responding."""
    if not _is_family():
        return jsonify(success=False), 401
    item_id = str((request.get_json(silent=True) or {}).get('id') or '')
    if not item_id.startswith('med:') or len(item_id) > 120:
        return jsonify(success=True)          # nothing to store for this kind
    user_id = session['user_id']
    read = _read_set(user_id)
    if item_id not in read:
        db.collection('user_account').document(user_id).update(
            {'notif_read_ids': firestore.ArrayUnion([item_id])})
        with _lock:
            read.add(item_id)
    _summary_cache.pop(user_id, None)
    return jsonify(success=True)
