"""Firestore READ METER -- shows where the daily read quota is going.

Every document Firestore returns costs 1 read, and a query that returns
nothing still costs 1. This counts them as the web app makes them and
prints one line per page/API call in the terminal, e.g.

    [reads]   38  GET /dashboard            (since start: 1,204)

Open /dev/reads in the browser for the totals per page since the server
started. It only COUNTS -- it never blocks or changes a read, so it is
safe to leave on. Turn it off with READ_METER=0 in .env.

Not counted: the open-alert listener in alerts_common.py (its own
reads are small: 1 per open alert at start + 1 per change).
"""
import os
import threading
import time
from collections import defaultdict

from flask import g, jsonify, request, session
from google.cloud.firestore_v1.client import Client
from google.cloud.firestore_v1.document import DocumentReference
from google.cloud.firestore_v1.query import Query

ENABLED = os.environ.get('READ_METER', '1') != '0'

_lock = threading.Lock()
_total = 0                       # every read since the server started
_by_page = defaultdict(lambda: [0, 0])   # 'GET /dashboard' -> [calls, reads]
_started = time.time()


def _add(n):
    global _total
    with _lock:
        _total += n


def _counted_stream(gen):
    """Pass every document through, counting as we go (1 if none)."""
    seen = 0
    try:
        for doc in gen:
            seen += 1
            yield doc
    finally:
        _add(seen or 1)


def install(app):
    if not ENABLED:
        return

    original_stream = Query.stream
    original_get = DocumentReference.get
    original_get_all = Client.get_all

    def stream(self, *args, **kwargs):
        return _counted_stream(original_stream(self, *args, **kwargs))

    def get(self, *args, **kwargs):
        _add(1)
        return original_get(self, *args, **kwargs)

    def get_all(self, references, *args, **kwargs):
        references = list(references)
        _add(len(references) or 1)
        return original_get_all(self, references, *args, **kwargs)

    Query.stream = stream            # also covers .get() and collection.stream()
    DocumentReference.get = get
    Client.get_all = get_all

    @app.before_request
    def _meter_start():
        g._reads_at_start = _total

    @app.after_request
    def _meter_end(response):
        start = getattr(g, '_reads_at_start', None)
        if start is None or request.path.startswith('/static'):
            return response
        used = _total - start
        page = f'{request.method} {request.url_rule.rule if request.url_rule else request.path}'
        with _lock:
            _by_page[page][0] += 1
            _by_page[page][1] += used
        if used:
            print(f'[reads] {used:>5}  {page:<45} (since start: {_total:,})')
        return response

    @app.route('/dev/reads')
    def dev_reads():
        if session.get('role') != 'admin' and request.remote_addr not in ('127.0.0.1', '::1'):
            return jsonify(success=False, message='Admins only.'), 403
        rows = sorted(_by_page.items(), key=lambda kv: kv[1][1], reverse=True)
        hours = max((time.time() - _started) / 3600, 1 / 60)
        return jsonify(
            success=True,
            total_reads=_total,
            running_minutes=round(hours * 60),
            reads_per_hour=round(_total / hours),
            free_quota_per_day=50000,
            pages=[{'page': p, 'calls': c, 'reads': r,
                    'avg_per_call': round(r / c, 1) if c else 0}
                   for p, (c, r) in rows],
        )