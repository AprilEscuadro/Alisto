"""
Responder portal (read-only analytics).

A responder is an approved account from CDRRMO, BFP, an ambulance crew, the
police, etc. They are NOT assigned to a barangay (they go wherever the
emergency is), so this page defaults to ALL of Cebu City and lets them pick
any barangay.

Privacy: everything here is a count. No resident name, address, phone or
device serial ever leaves this file.

user_account/{id}        role 'responder', approval_status 'pending' | 'approved' | 'rejected'
responder_profile/{id}   user_id, agency, position, agency_id_number,
                         valid_id_filename, valid_id_stored_as
"""
import time
from collections import Counter
from datetime import datetime
from functools import wraps

from flask import (Blueprint, render_template, session, redirect, url_for,
                   flash, request, jsonify, g)

import account_profile
import alerts_common as ac
import barangays as bg
from bhw_routes import _address_parts, _fmt_date
from database import db

responder_bp = Blueprint('responder', __name__, url_prefix='/responder')

DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
             'Saturday', 'Sunday']
# 8 blocks of 3 hours: fits a phone screen (24 columns would not).
BLOCK_LABELS = ['12-3 AM', '3-6 AM', '6-9 AM', '9-12 NN',
                '12-3 PM', '3-6 PM', '6-9 PM', '9-12 MN']


# ---------- ACCESS GUARD ----------

def responder_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'error')
            return redirect(url_for('login'))
        if session.get('role') != 'responder':
            flash('That page is only for responders.', 'error')
            return redirect(url_for('login'))

        doc = db.collection('user_account').document(session['user_id']).get()
        user = doc.to_dict() if doc.exists else None
        if (not user or user.get('deleted_at') or user.get('is_archived')
                or user.get('approval_status') != 'approved'):
            session.clear()
            flash('Your responder account is not active. '
                  'Please contact the system administrator.', 'error')
            return redirect(url_for('login'))

        g.responder_user = user
        g.responder_profile = _profile_of(session['user_id'])
        return view(*args, **kwargs)
    return wrapped


def _profile_of(user_id):
    matches = list(db.collection('responder_profile').where(
        'user_id', '==', user_id).limit(1).stream())
    return (matches[0].to_dict() or {}) if matches else {}


@responder_bp.context_processor
def _responder_layout_context():
    """What responder/base.html needs for the top bar."""
    user = getattr(g, 'responder_user', None)
    if not user:
        return {}
    me = account_profile.view(user)
    return {
        'rsp_name': me['full_name'] or 'Responder',
        'rsp_initials': me['initials'],
        'rsp_photo': me['photo_base64'],
        'now_year': datetime.now().year,
    }


# ---------- DATA (read once a minute, not once per tap) ----------

_CACHE = {'at': 0.0, 'alerts': [], 'barangay_of': {}}
_CACHE_SECONDS = 60


def _load():
    if time.time() - _CACHE['at'] < _CACHE_SECONDS and _CACHE['alerts']:
        return _CACHE['alerts'], _CACHE['barangay_of']

    barangay_of = {}
    for doc in db.collection('elder_profile').stream():
        elder = doc.to_dict() or {}
        barangay_of[doc.id] = bg.find_in_parts(
            _address_parts(elder.get('address')))

    alerts = []
    for doc in db.collection(ac.ALERT_COLLECTION).stream():
        alert = doc.to_dict() or {}
        if alert.get('deleted_at'):
            continue
        # Keep only what the counts need.
        alerts.append({
            'when': alert.get('date_triggered'),
            'elder_id': alert.get('elder_id'),
            'trigger': (alert.get('trigger_type') or '').upper(),
            'false_alarm': (alert.get('alert_status') or '').upper() == 'CANCELLED',
        })

    _CACHE.update(at=time.time(), alerts=alerts, barangay_of=barangay_of)
    return alerts, barangay_of


def aggregate(alerts, barangay_of, barangay=''):
    """Counts only. [alerts] items: {when, elder_id, trigger, false_alarm}."""
    heat = [[0] * 8 for _ in range(7)]          # weekday x 3-hour block
    by_day, by_block = [0] * 7, [0] * 8
    monthly, ranking = Counter(), Counter()
    total = false_alarms = voice = button = 0

    for a in alerts:
        moment = ac.local(a['when'])
        if not moment:
            continue
        place = barangay_of.get(a['elder_id']) or ''
        if place:
            ranking[place] += 1                 # ranking always uses everyone
        if barangay and place != barangay:
            continue

        total += 1
        false_alarms += 1 if a['false_alarm'] else 0
        voice += 1 if a['trigger'] == 'VOICE' else 0
        button += 1 if a['trigger'] == 'BUTTON' else 0
        day, block = moment.weekday(), moment.hour // 3
        heat[day][block] += 1
        by_day[day] += 1
        by_block[block] += 1
        monthly[moment.strftime('%Y-%m')] += 1

    months = sorted(monthly)[-12:]              # last 12 months that have data
    busiest_day = DAY_NAMES[by_day.index(max(by_day))] if total else None
    busiest_block = BLOCK_LABELS[by_block.index(
        max(by_block))] if total else None
    return {
        'barangay': barangay or 'All Cebu City',
        'total': total,
        'false_alarms': false_alarms,
        'false_alarm_rate': round(100 * false_alarms / total) if total else 0,
        'voice': voice,
        'button': button,
        'busiest_day': busiest_day,
        'busiest_block': busiest_block,
        'heat': heat,
        'heat_max': max((max(r) for r in heat), default=0),
        'day_names': DAY_NAMES,
        'block_labels': BLOCK_LABELS,
        'monthly': [{'month': datetime.strptime(m, '%Y-%m').strftime('%b %Y'),
                     'count': monthly[m]} for m in months],
        'ranking': [{'barangay': b, 'count': n}
                    for b, n in ranking.most_common(10)],
    }


# ---------- ROUTES ----------

@responder_bp.route('/')
@responder_required
def home():
    return redirect(url_for('responder.analytics'))


@responder_bp.route('/analytics')
@responder_required
def analytics():
    return render_template(
        'responder/analytics.html', active_page='analytics',
        barangays=bg.CEBU_CITY_BARANGAYS,
    )


@responder_bp.route('/analytics/data')
@responder_required
def analytics_data():
    raw = request.args.get('barangay', '').strip()
    barangay = bg.canonical(raw) or ''
    if raw and not barangay:
        return jsonify(success=False, message='Pick a barangay from the list.'), 400
    alerts, barangay_of = _load()
    return jsonify(success=True, **aggregate(alerts, barangay_of, barangay))


# ---------- MY PROFILE (same behaviour as the BHW/family page) ----------
# The shared logic lives in account_profile.py. Agency, position and agency ID
# stay read-only: the admin approved them.

@responder_bp.route('/profile')
@responder_required
def profile():
    p = g.responder_profile
    return render_template(
        'responder/profile.html', active_page='profile',
        me=account_profile.view(g.responder_user),
        role_label='Emergency Responder',
        member_since=_fmt_date(g.responder_user.get('created_at')),
        details=[
            ('Role', 'Emergency Responder'),
            ('Agency', p.get('agency') or '—'),
            ('Position', p.get('position') or '—'),
            ('Agency ID Number', p.get('agency_id_number') or '—'),
            ('Account Status', 'Active'),
            ('Last Login', session.get('previous_login') or 'First sign-in'),
        ],
        urls={
            'update': url_for('responder.edit_profile'),
            'password': url_for('responder.change_password'),
            'photo': url_for('responder.upload_photo'),
            'photo_remove': url_for('responder.remove_photo'),
        },
    )


@responder_bp.route('/profile/edit', methods=['POST'])
@responder_required
def edit_profile():
    ok, message, name = account_profile.update_details(
        session['user_id'], request.form)
    if ok:
        session['full_name'] = name
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('responder.profile'))


@responder_bp.route('/profile/password', methods=['POST'])
@responder_required
def change_password():
    ok, message = account_profile.change_password(
        session['user_id'], request.form)
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('responder.profile'))


@responder_bp.route('/profile/photo', methods=['POST'])
@responder_required
def upload_photo():
    ok, message = account_profile.save_photo(
        session['user_id'], request.form.get('photo_base64'))
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('responder.profile'))


@responder_bp.route('/profile/photo/remove', methods=['POST'])
@responder_required
def remove_photo():
    ok, message = account_profile.remove_photo(session['user_id'])
    flash(message, 'success' if ok else 'error')
    return redirect(url_for('responder.profile'))
