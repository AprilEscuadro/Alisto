"""
ALISTO Inbox API (JSON), shared by the family, BHW, and admin pages.

Registered in app.py with:
    from inbox_routes import inbox_bp
    app.register_blueprint(inbox_bp)

Who can start a conversation with whom:
    family -> ALISTO Support (admins), BHWs assigned to their elder's barangay
    bhw    -> ALISTO Support (admins), families of elders in their barangay
    admin  -> any approved BHW, any family account
Replies only need you to be part of the thread.

Storage is in inbox_service.py.
"""
from functools import wraps

from flask import Blueprint, g, jsonify, request, session

import inbox_service as inbox
from bhw_routes import _in_barangay
from database import db

inbox_bp = Blueprint('inbox_api', __name__, url_prefix='/inbox/api')

ROLES = ('family', 'bhw', 'admin')


def login_required_json(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session or session.get('role') not in ROLES:
            return jsonify(success=False, message='Please log in first.'), 401
        return view(*args, **kwargs)
    return wrapped


def _me():
    role = session.get('role')
    return {
        'id': session['user_id'],
        'key': inbox.me_key(session['user_id'], role),
        'name': session.get('full_name') or 'User',
        'role': role,
    }


def _fail(message, code=400):
    return jsonify(success=False, message=message), code


# ---------- who you can talk to ----------

def _family_elders(user_id):
    """{elder_id: {name, address}} linked to this family account."""
    elders = {}
    for link in db.collection('family_elder_link').where('family_user_id', '==', user_id).stream():
        elder_id = (link.to_dict() or {}).get('elder_id')
        if not elder_id or elder_id in elders:
            continue
        doc = db.collection('elder_profile').document(elder_id).get()
        data = (doc.to_dict() or {}) if doc.exists else {}
        if data and not data.get('deleted_at'):
            elders[elder_id] = {'name': data.get('full_name') or 'Unnamed',
                                'address': data.get('address')}
    return elders


def _bhw_barangay(user_id):
    for doc in db.collection('bhw_profile').where('user_id', '==', user_id).limit(1).stream():
        return ((doc.to_dict() or {}).get('barangay_assigned') or '').strip()
    return ''


def _elders_in_barangay(barangay):
    elders = {}
    if not barangay:
        return elders
    for doc in db.collection('elder_profile').stream():
        data = doc.to_dict() or {}
        if not data.get('deleted_at') and _in_barangay(data.get('address'), barangay):
            elders[doc.id] = {'name': data.get('full_name') or 'Unnamed',
                              'address': data.get('address')}
    return elders


def _active_users(role):
    """{user_id: user dict} for non-deleted accounts with this role."""
    users = {}
    for doc in db.collection('user_account').where('role', '==', role).stream():
        data = doc.to_dict() or {}
        if data.get('deleted_at') or data.get('is_archived'):
            continue
        if role == 'bhw' and data.get('approval_status') != 'approved':
            continue
        users[doc.id] = data
    return users


def _approved_bhws():
    """[{id, name, barangay}] for every approved BHW."""
    barangays = {}
    for doc in db.collection('bhw_profile').stream():
        p = doc.to_dict() or {}
        if p.get('user_id'):
            barangays[p['user_id']] = (
                p.get('barangay_assigned') or '').strip()
    return [{'id': uid, 'name': u.get('full_name') or 'BHW', 'barangay': barangays.get(uid, '')}
            for uid, u in _active_users('bhw').items()]


def _scope():
    """(recipients, elders) the signed-in user may use. Cached per request."""
    if hasattr(g, '_inbox_scope'):
        return g._inbox_scope
    me = _me()
    recipients, elders = [], {}

    if me['role'] != 'admin':
        recipients.append({'key': inbox.ADMIN_KEY, 'name': inbox.ADMIN_NAME,
                           'role': 'admin', 'note': 'Admin team'})

    if me['role'] == 'family':
        elders = _family_elders(me['id'])
        addresses = [e['address'] for e in elders.values()]
        for b in _approved_bhws():
            if b['barangay'] and any(_in_barangay(a, b['barangay']) for a in addresses):
                recipients.append({'key': b['id'], 'name': b['name'], 'role': 'bhw',
                                   'note': f"Barangay Health Worker, {b['barangay']}"})

    elif me['role'] == 'bhw':
        barangay = _bhw_barangay(me['id'])
        elders = _elders_in_barangay(barangay)
        families = _active_users('family')
        seen = set()
        for link in db.collection('family_elder_link').stream():
            data = link.to_dict() or {}
            uid, elder_id = data.get('family_user_id'), data.get('elder_id')
            if elder_id in elders and uid in families and uid not in seen:
                seen.add(uid)
                recipients.append({'key': uid, 'name': families[uid].get('full_name') or 'Family',
                                   'role': 'family', 'note': f"Family of {elders[elder_id]['name']}"})

    else:  # admin
        for b in _approved_bhws():
            note = f"Barangay Health Worker, {b['barangay']}" if b['barangay'] else 'Barangay Health Worker'
            recipients.append(
                {'key': b['id'], 'name': b['name'], 'role': 'bhw', 'note': note})
        for uid, u in _active_users('family').items():
            recipients.append({'key': uid, 'name': u.get('full_name') or 'Family',
                               'role': 'family', 'note': 'Family member'})

    support = [r for r in recipients if r['key'] == inbox.ADMIN_KEY]
    people = sorted((r for r in recipients if r['key'] != inbox.ADMIN_KEY),
                    key=lambda r: (r['role'], r['name'].lower()))
    recipients = support + people
    g._inbox_scope = (recipients, elders)
    return g._inbox_scope


# ---------- sidebar badge (every page) ----------

@inbox_bp.app_context_processor
def inject_inbox_unread():
    if 'user_id' not in session or session.get('role') not in ROLES:
        return {}
    if not hasattr(g, '_inbox_unread'):
        try:
            g._inbox_unread = inbox.unread_total(
                inbox.me_key(session['user_id'], session['role']), use_cache=True)
        except Exception as e:
            print(f'[inbox] could not count unread: {e}')
            g._inbox_unread = 0
    return {'inbox_unread': g._inbox_unread}


# ---------- API ----------

@inbox_bp.route('/unread')
@login_required_json
def unread():
    return jsonify(success=True, unread=inbox.unread_total(_me()['key'], use_cache=True))


@inbox_bp.route('/threads')
@login_required_json
def threads():
    rows = inbox.list_threads(_me()['key'])
    return jsonify(success=True, threads=rows, unread=sum(r['unread'] for r in rows))


@inbox_bp.route('/threads/<thread_id>')
@login_required_json
def thread(thread_id):
    me = _me()
    ref, data = inbox.get_thread(thread_id, me['key'])
    if not ref:
        return _fail('Conversation not found.', 404)
    try:
        after = max(0.0, float(request.args.get('after') or 0))
    except ValueError:
        after = 0.0
    if after != after or after > 1e14:  # NaN or nonsense
        after = 0.0
    rows = inbox.messages(ref, after_ms=after)
    inbox.mark_read(ref, data, me['key'])
    view = inbox.thread_view(ref.id, data, me['key'])
    view['unread'] = 0
    return jsonify(success=True, thread=view, messages=rows, me=me['key'])


@inbox_bp.route('/threads/<thread_id>/messages', methods=['POST'])
@login_required_json
def send(thread_id):
    me = _me()
    ref, data = inbox.get_thread(thread_id, me['key'])
    if not ref:
        return _fail('Conversation not found.', 404)
    body = inbox.clean((request.get_json(silent=True) or {}
                        ).get('body'), inbox.BODY_MAX + 1)
    if not body:
        return _fail('Type a message first.')
    if len(body) > inbox.BODY_MAX:
        return _fail(f'Keep messages under {inbox.BODY_MAX} characters.')
    if inbox.too_fast(session):
        return _fail('You are sending messages too quickly. Wait a moment.', 429)
    inbox.add_message(ref, data, me, body)
    return jsonify(success=True)


@inbox_bp.route('/threads/<thread_id>/status', methods=['POST'])
@login_required_json
def status(thread_id):
    me = _me()
    ref, data = inbox.get_thread(thread_id, me['key'])
    if not ref:
        return _fail('Conversation not found.', 404)
    new_status = (request.get_json(silent=True) or {}).get('status')
    if new_status not in ('open', 'resolved') or data.get('type') not in inbox.TICKET_TYPES:
        return _fail('That status can’t be set here.')
    if new_status != data.get('status'):
        inbox.set_status(ref, data, new_status, me)
    return jsonify(success=True)


@inbox_bp.route('/recipients')
@login_required_json
def recipients():
    people, elders = _scope()
    me = _me()
    if me['role'] == 'admin':
        barangays = sorted(
            {b['barangay'] for b in _approved_bhws() if b['barangay']}, key=str.lower)
    elif me['role'] == 'bhw':
        barangays = [b for b in [_bhw_barangay(me['id'])] if b]
    else:
        barangays = []
    return jsonify(success=True, recipients=people,
                   elders=[{'id': k, 'name': v['name']}
                           for k, v in elders.items()],
                   ticket_types=[{'key': k, 'label': inbox.TYPES[k]}
                                 for k in inbox.TICKET_TYPES],
                   barangays=barangays)


@inbox_bp.route('/threads', methods=['POST'])
@login_required_json
def create():
    me = _me()
    data = request.get_json(silent=True) or {}
    people, elders = _scope()

    recipient = next((p for p in people if p['key'] == data.get('to')), None)
    if not recipient:
        return _fail('Choose who to send this to.')

    wanted = data.get('type')
    if recipient['key'] == inbox.ADMIN_KEY:
        thread_type = wanted if wanted in inbox.TICKET_TYPES else 'support'
    elif me['role'] == 'admin' and wanted in inbox.TICKET_TYPES:
        thread_type = wanted
    else:
        thread_type = 'direct'

    subject = inbox.clean(data.get('subject'), inbox.SUBJECT_MAX + 1)
    body = inbox.clean(data.get('body'), inbox.BODY_MAX + 1)
    if not subject or not body:
        return _fail('Add a subject and a message.')
    if len(subject) > inbox.SUBJECT_MAX or len(body) > inbox.BODY_MAX:
        return _fail('Your subject or message is too long.')

    elder = None
    if data.get('elder_id'):
        if data['elder_id'] not in elders:
            return _fail('Choose one of your loved ones.')
        elder = {'id': data['elder_id'],
                 'name': elders[data['elder_id']]['name']}

    if inbox.too_fast(session):
        return _fail('You are sending messages too quickly. Wait a moment.', 429)

    related = inbox.clean(data.get('related_id'), 40)
    thread_id = inbox.create_thread(thread_type, subject, body, me, recipient,
                                    elder=elder, related_id=related)
    return jsonify(success=True, thread_id=thread_id)


@inbox_bp.route('/announcements')
@login_required_json
def announcements():
    me = _me()
    barangay_ok = None
    if me['role'] == 'family':
        addresses = [e['address'] for e in _family_elders(me['id']).values()]
        def barangay_ok(b): return any(_in_barangay(a, b) for a in addresses)  # noqa: E731
    elif me['role'] == 'bhw':
        mine = _bhw_barangay(me['id']).casefold()
        def barangay_ok(b): return b.casefold() == mine  # noqa: E731
    rows = inbox.announcements_for(me['role'], me['id'], barangay_ok)
    return jsonify(success=True, announcements=rows,
                   unread=sum(1 for r in rows if not r['is_read']))


@inbox_bp.route('/announcements/<announcement_id>/read', methods=['POST'])
@login_required_json
def announcement_read(announcement_id):
    inbox.mark_announcement_read(announcement_id, session['user_id'])
    return jsonify(success=True)


@inbox_bp.route('/announcements', methods=['POST'])
@login_required_json
def announcement_create():
    """Admins post to families, BHWs, or both (optionally one barangay).
    BHWs post only to the families in their own barangay."""
    me = _me()
    if me['role'] not in ('admin', 'bhw'):
        return _fail('Only admins and BHWs can post announcements.', 403)
    data = request.get_json(silent=True) or {}
    title = inbox.clean(data.get('title'), inbox.TITLE_MAX + 1)
    body = inbox.clean(data.get('body'), inbox.BODY_MAX + 1)
    if not title or not body:
        return _fail('Add a title and a message.')
    if len(title) > inbox.TITLE_MAX or len(body) > inbox.BODY_MAX:
        return _fail('Your title or message is too long.')

    if me['role'] == 'bhw':
        barangay = _bhw_barangay(me['id'])
        if not barangay:
            return _fail('Your barangay is not set. Contact the admin.')
        audience = ['family']
    else:
        audience = inbox.AUDIENCES.get(data.get('audience'))
        if not audience:
            return _fail('Choose who should see this.')
        barangay = inbox.clean(data.get('barangay'), 80)

    if inbox.too_fast(session):
        return _fail('You are posting too quickly. Wait a moment.', 429)
    new_id = inbox.create_announcement(title, body, audience, barangay, me)
    return jsonify(success=True, id=new_id)


@inbox_bp.route('/announcements/<announcement_id>', methods=['DELETE'])
@login_required_json
def announcement_delete(announcement_id):
    if not inbox.delete_announcement(announcement_id, session['user_id'], session.get('role')):
        return _fail('You can only delete your own announcements.', 403)
    return jsonify(success=True)
