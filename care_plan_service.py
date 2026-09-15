"""
ALISTO Care Plan: shared logic for the family pages (app.py) and the admin panel
(admin_routes.py). Everything is stored in Firestore:

care_plan/default                the plan the admin manages
    name, price, currency, billing_period ('monthly'), description, is_active, updated_at

app_settings/payment_methods     where families send money (edited by the admin)
    gcash_name, gcash_number, maya_name, maya_number,
    bank_name, bank_account_name, bank_account_number, instructions

subscription/{elder_id}          one per elder (= one per ALISTO device)
    elder_id, device_id, plan_name, price,
    status ('active' | 'cancelled'),
    started_at, current_period_start, current_period_end,
    last_payment_id, updated_at, cancelled_at, cancelled_by

payment/{id}                     every payment, from a family or recorded by the admin
    payment_no, elder_id, elder_name, device_id,
    months, unit_price, amount, currency,
    method ('gcash' | 'maya' | 'bank' | 'cash'), reference_no, payer_name, note,
    paid_by_user_id, paid_by_name,
    status ('pending' | 'approved' | 'rejected'),
    admin_note, reviewed_by, reviewed_by_name, reviewed_at,
    period_start, period_end, created_at

An expired plan never switches off emergency alerts: safety first.
"""
import calendar
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore

from database import db

PH_TZ = timezone(timedelta(hours=8))
EXPIRING_SOON_DAYS = 5
PLAN_MONTH_OPTIONS = (1, 3, 6, 12)

PAYMENT_METHODS = {
    'gcash': 'GCash',
    'maya': 'Maya',
    'bank': 'Bank Transfer',
    'cash': 'Cash (Barangay Hall)',
}

DEFAULT_PLAN = {
    'name': 'Alisto Care Plan',
    'price': 55,
    'currency': 'PHP',
    'billing_period': 'monthly',
    'description': 'Emergency alerts, live location, and medicine reminders for one ALISTO device.',
    'is_active': True,
}

DEFAULT_PAYMENT_METHODS = {
    'gcash_name': '', 'gcash_number': '',
    'maya_name': '', 'maya_number': '',
    'bank_name': '', 'bank_account_name': '', 'bank_account_number': '',
    'instructions': 'Send the exact amount, then enter the reference number from your receipt.',
}


# ---------- time helpers ----------

def now_ph():
    return datetime.now(PH_TZ)


def to_ph(value):
    if not hasattr(value, 'astimezone'):
        return None
    if getattr(value, 'tzinfo', None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PH_TZ)


def fmt_date(value):
    local = to_ph(value)
    return local.strftime('%B %d, %Y') if local else '—'


def fmt_datetime(value):
    local = to_ph(value)
    return local.strftime('%b %d, %Y %I:%M %p') if local else '—'


def add_months(dt, months):
    """Same day N months later (Jan 31 + 1 month = Feb 28/29)."""
    index = dt.month - 1 + months
    year = dt.year + index // 12
    month = index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


# ---------- settings ----------

def get_plan():
    plan = dict(DEFAULT_PLAN)
    try:
        doc = db.collection('care_plan').document('default').get()
        if doc.exists:
            plan.update(
                {k: v for k, v in (doc.to_dict() or {}).items() if v is not None})
    except Exception as e:
        print(f'[care plan] could not read plan: {e}')
    try:
        plan['price'] = float(plan['price'])
    except (TypeError, ValueError):
        plan['price'] = float(DEFAULT_PLAN['price'])
    return plan


def get_payment_methods():
    methods = dict(DEFAULT_PAYMENT_METHODS)
    try:
        doc = db.collection('app_settings').document('payment_methods').get()
        if doc.exists:
            methods.update(
                {k: v for k, v in (doc.to_dict() or {}).items() if v is not None})
    except Exception as e:
        print(f'[care plan] could not read payment methods: {e}')
    return methods


def available_methods(methods):
    """Only offer e-wallets / bank that the admin has actually filled in."""
    out = []
    if methods.get('gcash_number'):
        out.append({'key': 'gcash', 'label': PAYMENT_METHODS['gcash'],
                    'lines': [methods.get('gcash_name') or '', methods['gcash_number']]})
    if methods.get('maya_number'):
        out.append({'key': 'maya', 'label': PAYMENT_METHODS['maya'],
                    'lines': [methods.get('maya_name') or '', methods['maya_number']]})
    if methods.get('bank_account_number'):
        out.append({'key': 'bank', 'label': PAYMENT_METHODS['bank'],
                    'lines': [methods.get('bank_name') or '', methods.get('bank_account_name') or '',
                              methods['bank_account_number']]})
    out.append({'key': 'cash', 'label': PAYMENT_METHODS['cash'],
                'lines': ['Pay at the Barangay Hall. Use the receipt number as the reference.']})
    for m in out:
        m['lines'] = [line for line in m['lines'] if line]
    return out


def format_money(amount):
    amount = float(amount or 0)
    return f'{amount:,.0f}' if amount == int(amount) else f'{amount:,.2f}'


# ---------- subscription state ----------

def subscription_state(sub, has_pending=False, now=None):
    """Plain description of one elder's plan, used by both sides."""
    now = now or now_ph()
    end = to_ph((sub or {}).get('current_period_end'))
    cancelled = (sub or {}).get('status') == 'cancelled'

    if end and end > now and not cancelled:
        days_left = max(0, (end.date() - now.date()).days)
        expiring = days_left <= EXPIRING_SOON_DAYS
        state = {
            'status': 'expiring' if expiring else 'active',
            'label': 'Expiring' if expiring else 'Active',
            'badge': 'warning' if expiring else 'active',
            'icon': 'clock' if expiring else 'check-circle-2',
            'days_left': days_left,
            'headline': ('Renews today' if days_left == 0 else
                         f"Renews in {days_left} day{'s' if days_left != 1 else ''}"),
            'paid_until': fmt_date(end),
        }
    elif end:
        days_ago = max(0, (now.date() - end.date()).days)
        state = {
            'status': 'expired',
            'label': 'Cancelled' if cancelled else 'Expired',
            'badge': 'danger',
            'icon': 'circle-alert',
            'days_left': 0,
            'headline': ('Expired today' if days_ago == 0 else
                         f"Expired {days_ago} day{'s' if days_ago != 1 else ''} ago"),
            'paid_until': fmt_date(end),
        }
    else:
        state = {
            'status': 'none',
            'label': 'No Plan',
            'badge': 'muted',
            'icon': 'circle-dashed',
            'days_left': 0,
            'headline': 'No active plan yet',
            'paid_until': '—',
        }

    state['has_pending'] = bool(has_pending)
    if has_pending and state['status'] in ('none', 'expired'):
        state.update(label='Pending', badge='info', icon='hourglass',
                     headline='Payment under review')
    return state


def pending_elder_ids(elder_ids):
    pending = set()
    for elder_id in elder_ids:
        try:
            for doc in db.collection('payment').where('elder_id', '==', elder_id).stream():
                if (doc.to_dict() or {}).get('status') == 'pending':
                    pending.add(elder_id)
                    break
        except Exception as e:
            print(f'[care plan] could not read payments for {elder_id}: {e}')
    return pending


def device_for_elder(elder_id):
    try:
        docs = list(db.collection('device').where(
            'elder_id', '==', elder_id).limit(1).stream())
        return docs[0].id if docs else None
    except Exception:
        return None


# ---------- applying a payment ----------

def apply_approved_payment(payment_ref, payment, reviewer_id, reviewer_name, admin_note=''):
    """Marks the payment approved and extends that elder's subscription.

    The new period starts when the current one ends (or now, if it has
    already ended), so paying early never loses days.
    """
    elder_id = payment['elder_id']
    months = int(payment.get('months') or 1)
    now = now_ph()

    sub_ref = db.collection('subscription').document(elder_id)
    sub_doc = sub_ref.get()
    sub = (sub_doc.to_dict() or {}) if sub_doc.exists else {}

    current_end = to_ph(sub.get('current_period_end'))
    still_running = current_end and current_end > now and sub.get(
        'status') != 'cancelled'
    start = current_end if still_running else now
    end = add_months(start, months)

    sub_update = {
        'elder_id': elder_id,
        'device_id': payment.get('device_id') or sub.get('device_id') or device_for_elder(elder_id),
        'plan_name': payment.get('plan_name') or get_plan()['name'],
        'price': payment.get('unit_price'),
        'status': 'active',
        'current_period_end': end,
        'last_payment_id': payment_ref.id,
        'updated_at': firestore.SERVER_TIMESTAMP,
        'cancelled_at': None,
        'cancelled_by': None,
    }
    if not still_running:
        sub_update['current_period_start'] = start
    if not sub.get('started_at'):
        sub_update['started_at'] = start
    sub_ref.set(sub_update, merge=True)

    payment_ref.update({
        'status': 'approved',
        'admin_note': admin_note,
        'reviewed_by': reviewer_id,
        'reviewed_by_name': reviewer_name,
        'reviewed_at': firestore.SERVER_TIMESTAMP,
        'period_start': start,
        'period_end': end,
    })
    return start, end


def new_payment_no(doc_id):
    return f'PAY-{doc_id[:6].upper()}'

# ---------- per-plan QR codes ----------
# app_settings/plan_qr_codes: { "1": {gcash_name, gcash_number, qr_image_url},
#                                "3": {...}, "12": {...} }
# The admin uploads a QR image per duration; families see the QR that
# matches the plan length they picked, not one QR for everything.


def get_plan_qr_codes():
    """{months: {gcash_name, gcash_number, qr_image_url}} for each option."""
    out = {}
    try:
        doc = db.collection('app_settings').document('plan_qr_codes').get()
        data = (doc.to_dict() or {}) if doc.exists else {}
    except Exception as e:
        print(f'[care plan] could not read plan QR codes: {e}')
        data = {}

    # Falls back to this until the admin sets a different QR per plan length
    # in /admin/subscriptions.
    default_gcash_name = 'ALISTO CHUCHU'
    default_qr_image = '/static/images/alisto_qr.jpg'

    for months in PLAN_MONTH_OPTIONS:
        entry = data.get(str(months)) or {}
        out[months] = {
            'gcash_name': entry.get('gcash_name') or default_gcash_name,
            'gcash_number': entry.get('gcash_number') or '',
            'qr_image_url': entry.get('qr_image_url') or default_qr_image,
        }
    return out


def save_plan_qr_code(months, gcash_name, gcash_number, qr_image_url):
    """Admin sets/updates the QR + GCash details for one plan length."""
    if months not in PLAN_MONTH_OPTIONS:
        raise ValueError('Invalid plan length.')
    ref = db.collection('app_settings').document('plan_qr_codes')
    ref.set({
        str(months): {
            'gcash_name': gcash_name.strip(),
            'gcash_number': gcash_number.strip(),
            'qr_image_url': qr_image_url,
            'updated_at': firestore.SERVER_TIMESTAMP,
        }
    }, merge=True)
