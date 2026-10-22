"""SIM number, load balance, and connected-account counts for an ALISTO device.

Everything here is read from Firestore; nothing is hardcoded:
  - The Raspberry Pi writes to device/{serial}:
        simNumber      the SIM's own number (when the SIM reports it)
        loadBalance    remaining load in pesos (float), from a USSD check
        loadText       the carrier's raw reply, kept for when it can't be parsed
        loadCheckedAt  when the Pi last checked
  - The family can also type the SIM number in the app. Older code saved it
    as `sim_number`, so both spellings are read.
  - Connected accounts = family accounts linked to the elder that owns the
    device (family_elder_link), not counting deleted accounts.
"""
import alerts_common as ac

LOW_LOAD_PESOS = 10   # below this, the load is shown in red


def sim_number(device):
    device = device or {}
    return device.get('simNumber') or device.get('sim_number') or None


def load_info(device):
    """{'text', 'low', 'checked'} for display, or None if never checked."""
    device = device or {}
    amount = device.get('loadBalance')
    raw = device.get('loadText')
    checked = device.get('loadCheckedAt')
    if amount is None and not raw and not checked:
        return None

    if isinstance(amount, (int, float)):
        text = f'₱{amount:,.2f}'
        low = amount < LOW_LOAD_PESOS
    else:
        text = 'Could not read'
        low = False

    return {
        'text': text,
        'low': low,
        'checked': ac.fmt_time(checked) if checked else '—',
        'raw': raw or '',
    }


def family_counts(db):
    """{elder_id: number of active family accounts linked}. Two reads total,
    so it's safe for tables that list many devices at once."""
    active_family = {
        doc.id for doc in db.collection('user_account')
        .where('role', '==', 'family').stream()
        if not (doc.to_dict() or {}).get('deleted_at')
    }
    counts = {}
    for link in db.collection('family_elder_link').stream():
        data = link.to_dict() or {}
        elder_id = data.get('elder_id')
        if elder_id and data.get('family_user_id') in active_family:
            counts[elder_id] = counts.get(elder_id, 0) + 1
    return counts


def accounts_text(count):
    count = count or 0
    return f"{count} account{'' if count == 1 else 's'}"