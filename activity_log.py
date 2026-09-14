# ============================================================
# ACTIVITY LOG  (history)
# Paste into app.py, replacing the existing history() route.
# Keep it above `if __name__ == '__main__':`.
#
# There was no history collection before this: schema.sql declared
# emergency_status_history / device_health_log / voice_detection_log
# but nothing ever wrote to them, and medication_reminder only holds
# the current schedule, not a record of it firing.
#
#   activity_log/{id}
#     elder_id      -- the anchor every role scopes on
#     type          -- 'alert' | 'med_reminder' | 'device'
#     title, detail -- what to show in the row
#     badge, badge_class
#     location_address, device_id
#     actor_user_id -- who did it, when a person did
#     created_at    -- REQUIRED, this is the sort key
#
# Family sees elders they are linked to, a BHW sees elders in their
# barangay, an admin sees everything — all through _activity_feed().
# ============================================================

ACTIVITY_PAGE_SIZE = 20

ACTIVITY_TYPES = {
    'alert': {
        'label': 'Emergency Alert',
        'icon': 'triangle-alert',
        'class': 'alert',
        'person_icon': 'user-round',
    },
    'med_reminder': {
        'label': 'Med Reminder',
        'icon': 'pill',
        'class': 'med',
        'person_icon': 'user-round',
    },
    'device': {
        'label': 'Device Activity',
        'icon': 'smartphone',
        'class': 'device',
        'person_icon': 'smartphone',
    },
}


def _log_activity(elder_id, activity_type, title, detail='',
                  badge='', badge_class='', location_address='',
                  device_id=None, actor_user_id=None):
    """Append one row to the shared history.

    Deliberately swallows its own errors: a history write failing should
    never break the action the user actually asked for.
    """
    if not elder_id or activity_type not in ACTIVITY_TYPES:
        return

    try:
        db.collection('activity_log').add({
            'elder_id': elder_id,
            'type': activity_type,
            'title': title,
            'detail': detail,
            'badge': badge,
            'badge_class': badge_class,
            'location_address': location_address,
            'device_id': device_id,
            'actor_user_id': actor_user_id,
            'created_at': firestore.SERVER_TIMESTAMP,
        })
    except Exception:
        pass


def _elders_for_role():
    """The elder ids the signed-in account may see, and their display info.

    family -> elders linked to this account
    bhw    -> elders whose address is in this BHW's barangay
    admin  -> every elder
    """
    role = session.get('role')
    elders = {}

    if role == 'family':
        links = list(
            db.collection('family_elder_link')
            .where('family_user_id', '==', session['user_id'])
            .stream()
        )
        wanted = {(l.to_dict() or {}).get('elder_id') for l in links}
        relationships = {
            (l.to_dict() or {}).get('elder_id'): (l.to_dict() or {}).get('relationship')
            for l in links
        }
    else:
        wanted = None          # admin and BHW filter below instead
        relationships = {}

    barangay = None
    if role == 'bhw':
        matches = list(
            db.collection('bhw_profile')
            .where('user_id', '==', session['user_id']).limit(1).stream()
        )
        barangay = (matches[0].to_dict() or {}).get('barangay_assigned') if matches else None

    for doc in db.collection('elder_profile').stream():
        elder = doc.to_dict() or {}
        if elder.get('deleted_at'):
            continue
        if wanted is not None and doc.id not in wanted:
            continue
        if role == 'bhw' and not _in_elder_barangay(elder.get('address'), barangay):
            continue

        elders[doc.id] = {
            'name': elder.get('full_name') or 'Unnamed',
            'photo_url': elder.get('photo_url') or None,
            'relationship': relationships.get(doc.id) or 'Elderly',
        }

    return elders


def _in_elder_barangay(address, barangay):
    """Whole-part match so 'Sudlon I' never matches 'Sudlon II'."""
    if not address or not barangay:
        return False
    parts = [p.strip().casefold() for p in address.split(',')]
    return barangay.strip().casefold() in parts


def _activity_feed(elders):
    """Every activity row for the given elders, newest first."""
    ph_tz = timezone(timedelta(hours=8))
    rows = []

    for elder_id, elder in elders.items():
        try:
            docs = list(
                db.collection('activity_log')
                .where('elder_id', '==', elder_id)
                .stream()
            )
        except Exception:
            docs = []

        for doc in docs:
            item = doc.to_dict() or {}
            meta = ACTIVITY_TYPES.get(item.get('type')) or ACTIVITY_TYPES['device']
            created = item.get('created_at')
            local = created.astimezone(ph_tz) if hasattr(created, 'astimezone') else None

            rows.append({
                'id': doc.id,
                'elder_id': elder_id,
                'date': local.strftime('%b %d, %Y') if local else '—',
                'time': local.strftime('%I:%M %p') if local else '',
                'sort_key': created.timestamp() if hasattr(created, 'timestamp') else 0,

                'type_filter': item.get('type') or 'device',
                'type_label': meta['label'],
                'type_icon': meta['icon'],
                'type_class': meta['class'],

                'person_name': elder['name'],
                'person_photo_url': elder['photo_url'],
                'person_relationship': elder['relationship'],
                'person_icon': meta['person_icon'],

                'detail_main': item.get('title') or '—',
                'detail_badge': item.get('badge') or '',
                'detail_badge_class': item.get('badge_class') or '',
                'location': item.get('location_address') or 'Not recorded',
            })

    rows.sort(key=lambda r: r['sort_key'], reverse=True)
    return rows


def _paginate(rows, page, per_page=ACTIVITY_PAGE_SIZE):
    """Slices rows and builds the page-number list the template renders."""
    total = len(rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    window = rows[start:start + per_page]

    if total_pages <= 7:
        pages = list(range(1, total_pages + 1))
    else:
        pages = [1]
        if page > 3:
            pages.append('...')
        for p in range(max(2, page - 1), min(total_pages, page + 1) + 1):
            pages.append(p)
        if page < total_pages - 2:
            pages.append('...')
        if total_pages not in pages:
            pages.append(total_pages)

    return window, {
        'start': start + 1 if window else 0,
        'end': start + len(window),
        'total': total,
        'current_page': page,
        'total_pages': total_pages,
        'pages': pages,
    }


@app.route('/history')
def history():
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1

    all_rows = _activity_feed(_elders_for_role())
    activities, pagination = _paginate(all_rows, page)

    return render_template(
        'family/history.html',
        full_name=session['full_name'], role=session['role'],
        care_plan=_get_care_plan(),
        activities=activities,
        alert_count=sum(1 for r in all_rows if r['type_filter'] == 'alert'),
        med_reminder_count=sum(1 for r in all_rows if r['type_filter'] == 'med_reminder'),
        device_activity_count=sum(1 for r in all_rows if r['type_filter'] == 'device'),
        total_activity_count=len(all_rows),
        notification_count=0, date_range_label='All Time',
        pagination=pagination, current_year=datetime.now().year,
    )


@app.route('/history/export')
def export_history():
    """Downloads the whole feed as CSV — not just the page on screen."""
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    rows = _activity_feed(_elders_for_role())

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Date', 'Time', 'Type', 'Person',
                     'Relationship', 'Details', 'Status', 'Location'])
    for r in rows:
        writer.writerow([
            r['date'], r['time'], r['type_label'], r['person_name'],
            r['person_relationship'], r['detail_main'],
            r['detail_badge'], r['location'],
        ])

    filename = f"alisto-history-{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@app.route('/history/<activity_id>')
def activity_details(activity_id):
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))
    flash('Activity details page not implemented yet.', 'info')
    return redirect(url_for('history'))