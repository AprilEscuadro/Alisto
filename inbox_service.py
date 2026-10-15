"""
ALISTO Inbox: support tickets, problem reports, care plan updates, direct
messages, and announcements in one place. Shared by the family, BHW, and
admin pages (see inbox_routes.py).

Firestore
---------
inbox_thread/{id}
    thread_no        'TCK-XXXXXX' (tickets) or 'MSG-XXXXXX' (direct messages)
    type             'support' | 'report' | 'care_plan' | 'direct'
    subject, status  status is 'open' or 'resolved' (direct messages stay 'open')
    participant_ids  [user_id, ...]; the whole admin team is the single key 'admins'
    participants     {key: {name, role}}
    unread           {key: number of messages that key hasn't seen}
    created_by, elder_id, elder_name, related_id, meta
    last_message_at, last_message_preview, last_sender_id
    created_at, updated_at

inbox_thread/{id}/messages/{id}
    sender_id, sender_key, sender_name, sender_role, body, is_system, created_at

announcement/{id}
    title, body, audience_roles ['family', 'bhw'], barangay ('' = everyone),
    created_by, created_by_name, created_by_role, read_by [user_id], created_at

Only single-field queries are used, so no composite Firestore index is needed.
"""
import time
from datetime import datetime, timezone

from firebase_admin import firestore
from google.cloud.firestore_v1.field_path import FieldPath

from database import db

THREADS = 'inbox_thread'
MESSAGES = 'messages'
ANNOUNCEMENTS = 'announcement'

ADMIN_KEY = 'admins'
ADMIN_NAME = 'ALISTO Support'

TYPES = {
    'support': 'Support',
    'report': 'Problem report',
    'care_plan': 'Care plan',
    'direct': 'Message',
}
TICKET_TYPES = ('support', 'report', 'care_plan')
ROLE_LABELS = {'family': 'Family', 'bhw': 'BHW',
               'admin': 'Admin', 'system': 'ALISTO'}

BODY_MAX = 2000
SUBJECT_MAX = 120
SEND_LIMIT_PER_MIN = 15
TITLE_MAX = 120
AUDIENCES = {'family': ['family'], 'bhw': ['bhw'], 'both': ['family', 'bhw']}

# The sidebar badge is on every page; caching it for a few seconds keeps
# page-to-page clicking from re-reading Firestore each time.
UNREAD_CACHE_SECONDS = 15
_unread_cache = {}


# ---------- small helpers ----------

def unread_field(key):
    """'unread.<key>', quoted when needed (ids that start with a digit)."""
    return FieldPath('unread', key).to_api_repr()


def me_key(user_id, role):
    """Every admin shares one inbox, so their key is 'admins'."""
    return ADMIN_KEY if role == 'admin' else user_id


def to_ms(value):
    """Firestore timestamp -> milliseconds since epoch (0 if missing).

    Keeps microseconds as a fraction so '?after=' polling never sends the
    last message back again."""
    if hasattr(value, 'timestamp'):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return round(value.timestamp() * 1000, 3)
    return 0


def clean(text, limit):
    return (text or '').strip()[:limit]


def preview(text):
    text = ' '.join((text or '').split())
    return text[:90] + ('…' if len(text) > 90 else '')


def too_fast(session):
    """True if this login sent too many messages in the last minute."""
    now = time.time()
    recent = [t for t in session.get('inbox_sent', []) if now - t < 60]
    if len(recent) >= SEND_LIMIT_PER_MIN:
        session['inbox_sent'] = recent
        return True
    recent.append(now)
    session['inbox_sent'] = recent
    return False


# ---------- threads ----------

def thread_view(doc_id, t, key):
    """Thread dict -> what the page needs (JSON safe)."""
    others = [p for k, p in (t.get('participants') or {}).items() if k != key]
    return {
        'id': doc_id,
        'thread_no': t.get('thread_no') or '',
        'type': t.get('type') or 'support',
        'type_label': TYPES.get(t.get('type'), 'Message'),
        'subject': t.get('subject') or '(no subject)',
        'status': t.get('status') or 'open',
        'with': ', '.join(p.get('name') or '' for p in others) or ADMIN_NAME,
        'with_role': others[0].get('role') if others else 'admin',
        'elder_name': t.get('elder_name') or '',
        'related_id': t.get('related_id') or '',
        'preview': t.get('last_message_preview') or '',
        'last_at': to_ms(t.get('last_message_at') or t.get('created_at')),
        'created_at': to_ms(t.get('created_at')),
        'created_by': t.get('created_by') or '',
        'unread': int((t.get('unread') or {}).get(key) or 0),
    }


def list_threads(key):
    """Every thread this key is part of, newest activity first."""
    rows = []
    for doc in db.collection(THREADS).where('participant_ids', 'array_contains', key).stream():
        rows.append(thread_view(doc.id, doc.to_dict() or {}, key))
    rows.sort(key=lambda r: r['last_at'], reverse=True)
    return rows


def unread_total(key, use_cache=False):
    """Number of unread messages for this key. Only threads that actually
    have unread messages are read from Firestore."""
    if use_cache:
        hit = _unread_cache.get(key)
        if hit and time.time() - hit[0] < UNREAD_CACHE_SECONDS:
            return hit[1]
    total = 0
    for doc in db.collection(THREADS).where(unread_field(key), '>', 0).stream():
        total += int(((doc.to_dict() or {}).get('unread') or {}).get(key) or 0)
    _unread_cache[key] = (time.time(), total)
    return total


def _forget_unread(*keys):
    for k in keys:
        _unread_cache.pop(k, None)


def get_thread(thread_id, key):
    """(ref, data) if this key belongs to the thread, else (None, None)."""
    if not thread_id or '/' in thread_id:
        return None, None
    ref = db.collection(THREADS).document(thread_id)
    doc = ref.get()
    data = (doc.to_dict() or {}) if doc.exists else None
    if not data or key not in (data.get('participant_ids') or []):
        return None, None
    return ref, data


def _message(sender, body, is_system=False):
    return {
        'sender_id': sender.get('id') or '',
        'sender_key': sender.get('key') or '',
        'sender_name': sender.get('name') or '',
        'sender_role': 'system' if is_system else (sender.get('role') or ''),
        'body': body,
        'is_system': is_system,
        'created_at': firestore.SERVER_TIMESTAMP,
    }


def create_thread(thread_type, subject, body, sender, recipient, elder=None,
                  related_id='', meta=None, thread_id=None, is_system=False):
    """Starts a thread with its first message.

    sender / recipient: {'key', 'name', 'role'} (sender also has 'id').
    Returns the new thread id.
    """
    ref = db.collection(THREADS).document(
        thread_id) if thread_id else db.collection(THREADS).document()
    prefix = 'MSG' if thread_type == 'direct' else 'TCK'
    keys = [sender['key'], recipient['key']]
    batch = db.batch()
    batch.set(ref, {
        'thread_no': f'{prefix}-{ref.id[-6:].upper()}',
        'type': thread_type,
        'subject': subject,
        'status': 'open',
        'participant_ids': keys,
        'participants': {
            sender['key']: {'name': sender['name'], 'role': sender['role']},
            recipient['key']: {'name': recipient['name'], 'role': recipient['role']},
        },
        'unread': {sender['key']: 0, recipient['key']: 1},
        'created_by': sender.get('id') or '',
        'elder_id': (elder or {}).get('id') or '',
        'elder_name': (elder or {}).get('name') or '',
        'related_id': related_id,
        'meta': meta or {},
        'last_message_at': firestore.SERVER_TIMESTAMP,
        'last_message_preview': preview(body),
        'last_sender_id': sender.get('id') or '',
        'created_at': firestore.SERVER_TIMESTAMP,
        'updated_at': firestore.SERVER_TIMESTAMP,
    })
    batch.set(ref.collection(MESSAGES).document(),
              _message(sender, body, is_system))
    batch.commit()
    _forget_unread(*keys)
    return ref.id


def add_message(ref, thread, sender, body, is_system=False, status=None):
    """Adds a message, bumps everyone else's unread count, and reopens a
    resolved ticket when someone replies (unless a status is given)."""
    update = {
        'last_message_at': firestore.SERVER_TIMESTAMP,
        'last_message_preview': preview(body),
        'last_sender_id': sender.get('id') or '',
        'updated_at': firestore.SERVER_TIMESTAMP,
        unread_field(sender['key']): 0,
    }
    for k in thread.get('participant_ids') or []:
        if k != sender['key']:
            update[unread_field(k)] = firestore.Increment(1)
    if status:
        update['status'] = status
    elif not is_system and thread.get('status') == 'resolved':
        update['status'] = 'open'

    batch = db.batch()
    batch.set(ref.collection(MESSAGES).document(),
              _message(sender, body, is_system))
    batch.update(ref, update)
    batch.commit()
    _forget_unread(*(thread.get('participant_ids') or []))


def messages(ref, after_ms=0, limit=300):
    """Messages oldest first; only the ones newer than after_ms if given."""
    query = ref.collection(MESSAGES)
    if after_ms:
        after = datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)
        query = query.where('created_at', '>', after)
    query = query.order_by('created_at').limit(limit)
    rows = []
    for doc in query.stream():
        m = doc.to_dict() or {}
        rows.append({
            'id': doc.id,
            'sender_id': m.get('sender_id') or '',
            'sender_key': m.get('sender_key') or '',
            'sender_name': m.get('sender_name') or '',
            'sender_role': m.get('sender_role') or '',
            'role_label': ROLE_LABELS.get(m.get('sender_role'), ''),
            'body': m.get('body') or '',
            'is_system': bool(m.get('is_system')),
            'created_at': to_ms(m.get('created_at')),
        })
    return rows


def mark_read(ref, thread, key):
    if int((thread.get('unread') or {}).get(key) or 0):
        ref.update({unread_field(key): 0})
        _forget_unread(key)


def set_status(ref, thread, status, actor):
    """Marks a ticket resolved or reopens it, leaving a note in the chat."""
    word = 'resolved' if status == 'resolved' else 'reopened'
    add_message(ref, thread, actor, f"{actor['name']} marked this as {word}.",
                is_system=True, status=status)


# ---------- care plan payment updates ----------

def payment_update(payment_id, payment, text, status='open', notify='family'):
    """Posts a care plan payment update between the family member who paid
    and the admin team.

    One thread per payment (id 'pay_<payment id>'), so the submit, the
    admin's decision, and any dispute all stay together. notify says whose
    unread count goes up: 'family' (admin decided) or 'admins' (family
    submitted). Never raises.
    """
    try:
        user_id = payment.get('paid_by_user_id')
        if not user_id:
            return None
        family = {'id': user_id, 'key': user_id,
                  'name': payment.get('paid_by_name') or 'Family', 'role': 'family'}
        admins = {'id': '', 'key': ADMIN_KEY,
                  'name': ADMIN_NAME, 'role': 'admin'}
        sender, recipient = (
            family, admins) if notify == 'admins' else (admins, family)

        thread_id = f'pay_{payment_id}'
        ref = db.collection(THREADS).document(thread_id)
        doc = ref.get()
        if doc.exists:
            add_message(ref, doc.to_dict() or {}, sender,
                        text, is_system=True, status=status)
        else:
            payment_no = payment.get('payment_no') or payment_id[:6].upper()
            create_thread(
                'care_plan', f'Care plan payment {payment_no}', text, sender, recipient,
                elder={'id': payment.get('elder_id'),
                       'name': payment.get('elder_name')},
                related_id=payment_no, thread_id=thread_id, is_system=True)
            if status != 'open':
                ref.update({'status': status})
        return thread_id
    except Exception as e:
        print(f'[inbox] could not post payment update: {e}')
        return None


# ---------- announcements ----------

def announcements_for(role, user_id, barangay_ok=None):
    """Announcements for this role, newest first.

    barangay_ok(barangay) -> bool decides if a barangay-only announcement
    applies to this user; None means show everything (admins).
    """
    rows = []
    query = db.collection(ANNOUNCEMENTS)
    if role != 'admin':
        query = query.where('audience_roles', 'array_contains', role)
    for doc in query.stream():
        a = doc.to_dict() or {}
        barangay = (a.get('barangay') or '').strip()
        if barangay and barangay_ok is not None and not barangay_ok(barangay):
            continue
        rows.append({
            'id': doc.id,
            'title': a.get('title') or '',
            'body': a.get('body') or '',
            'barangay': barangay,
            'from_name': a.get('created_by_name') or ADMIN_NAME,
            'from_role': ROLE_LABELS.get(a.get('created_by_role'), ''),
            'created_at': to_ms(a.get('created_at')),
            'created_by': a.get('created_by') or '',
            'audience_roles': a.get('audience_roles') or [],
            'is_read': user_id in (a.get('read_by') or []),
            'read_count': len(a.get('read_by') or []),
        })
    rows.sort(key=lambda r: r['created_at'], reverse=True)
    return rows


def mark_announcement_read(announcement_id, user_id):
    if not announcement_id or '/' in announcement_id:
        return
    ref = db.collection(ANNOUNCEMENTS).document(announcement_id)
    if ref.get().exists:
        ref.update({'read_by': firestore.ArrayUnion([user_id])})


def create_announcement(title, body, audience_roles, barangay, author):
    """author: {'id', 'name', 'role'}. Returns the new id."""
    ref = db.collection(ANNOUNCEMENTS).document()
    ref.set({
        'title': title,
        'body': body,
        'audience_roles': audience_roles,
        'barangay': barangay,
        'created_by': author['id'],
        'created_by_name': author['name'],
        'created_by_role': author['role'],
        'read_by': [author['id']],
        'created_at': firestore.SERVER_TIMESTAMP,
    })
    return ref.id


def delete_announcement(announcement_id, user_id, role):
    """Admins can delete any announcement; others only their own."""
    if not announcement_id or '/' in announcement_id:
        return False
    ref = db.collection(ANNOUNCEMENTS).document(announcement_id)
    doc = ref.get()
    data = (doc.to_dict() or {}) if doc.exists else None
    if not data or (role != 'admin' and data.get('created_by') != user_id):
        return False
    ref.delete()
    return True
