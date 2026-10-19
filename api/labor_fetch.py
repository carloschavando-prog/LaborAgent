"""Reconcile approved 7shifts labor, including late approvals and edits.

The nightly run refreshes 28 completed local days, all pending dates, and older
punch dates modified within the last seven days. Explicit ?date=YYYY-MM-DD
reimports one date. ?dry_run=1 validates without writing or reporting lifecycle.
"""
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as day_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

try:
    from control_board import report
except ImportError:
    def report(*_args, **_kwargs):
        return

SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_KEY']
CRON_SECRET = os.environ.get('CRON_SECRET', '')
SEVEN_SHIFTS_TOKEN = os.environ['SEVEN_SHIFTS_TOKEN']
COMPANY_ID = '286488'
LOCATION_ID = '354876'
LOCAL_TZ = ZoneInfo('America/New_York')
LOOKBACK_DAYS = 28
ROLE_DEPT = {
    1760490: 'foh', 1760495: 'kit', 1760496: 'kit', 1761419: 'foh',
    1780081: 'kit', 2045831: 'foh', 2103857: 'kit', 2215217: 'foh',
    2332059: 'kit', 2686259: 'kit', 2754779: 'kit',
}
EXCLUDED_ROLES = {1760491}  # Salaried managers, intentionally excluded.
METRICS = ('kit_hours', 'kit_cost', 'kit_sched', 'foh_hours', 'foh_cost',
           'foh_sched', 'total_hours', 'total_cost', 'total_sched')
_rate_lock = threading.Lock()
_last_request = 0.0


class IncompleteData(ValueError):
    pass


def _request_json(req, rate_limit=False):
    global _last_request
    for attempt in range(3):
        if rate_limit:
            with _rate_lock:
                time.sleep(max(0, 0.13 - (time.monotonic() - _last_request)))
                _last_request = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def _7shifts_get(path, params):
    req = urllib.request.Request(
        'https://api.7shifts.com' + path + '?' + urlencode(params),
        headers={'Authorization': 'Bearer ' + SEVEN_SHIFTS_TOKEN,
                 'accept': 'application/json'},
    )
    return _request_json(req, rate_limit=True)


def _number(value):
    if value is None or isinstance(value, bool):
        raise IncompleteData('Missing numeric hours or wages')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise IncompleteData('Invalid hours or wages')
    return result


def _role(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise IncompleteData('Punch or shift has no assigned role')


def _report_shifts(start, end, punches):
    data = _7shifts_get('/v2/reports/hours_and_wages', {
        'company_id': COMPANY_ID, 'location_id': LOCATION_ID,
        'from': start, 'to': end, 'punches': str(punches).lower(),
        'include_tips': 'false',
    })
    if (not isinstance(data, dict) or not isinstance(data.get('users'), list)
            or data.get('start') != start or data.get('end') != end):
        raise IncompleteData('Invalid or wrong-date hours and wages response')
    shifts = []
    for user in data['users']:
        if not isinstance(user.get('weeks'), list):
            raise IncompleteData('Missing employee weeks')
        for week in user['weeks']:
            if not isinstance(week.get('shifts'), list):
                raise IncompleteData('Missing employee shifts')
            for shift in week['shifts']:
                shift_day = (shift.get('date') or '')[:10]
                if not start <= shift_day <= end:
                    raise IncompleteData('Unexpected shift date')
                if not isinstance(shift.get('total'), dict):
                    raise IncompleteData('Missing shift totals')
                _number(shift['total'].get('total_hours'))
                if punches:
                    _number(shift['total'].get('total_pay'))
                shifts.append(dict(shift, user_id=shift.get('user_id') or user['user']['id']))
    return shifts


def _punches(params):
    params = {'location_id': LOCATION_ID, 'limit': 100, **params}
    result, cursors = [], set()
    for _ in range(100):
        data = _7shifts_get(f'/v2/company/{COMPANY_ID}/time_punches', params)
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise IncompleteData('Invalid time punch response')
        cursor_info = (data.get('meta') or {}).get('cursor')
        if not isinstance(cursor_info, dict) or 'next' not in cursor_info:
            raise IncompleteData('Missing time punch pagination')
        result.extend(data['data'])
        cursor = cursor_info['next']
        if not cursor:
            return result
        if cursor in cursors:
            raise IncompleteData('Repeated time punch cursor')
        cursors.add(cursor)
        params['cursor'] = cursor
    raise IncompleteData('Time punch page limit exceeded')


def _punch_day(punch):
    return datetime.fromisoformat(punch['clocked_in'].replace('Z', '+00:00')).astimezone(LOCAL_TZ).date().isoformat()


def _range_punches(start, end):
    start_utc = datetime.combine(date.fromisoformat(start), day_time(), LOCAL_TZ).astimezone(timezone.utc)
    end_utc = datetime.combine(date.fromisoformat(end) + timedelta(days=1), day_time(), LOCAL_TZ).astimezone(timezone.utc)
    return _punches({'clocked_in[gte]': start_utc.isoformat(),
                     'clocked_in[lte]': (end_utc - timedelta(seconds=1)).isoformat()})


def _supa(path, rows=None):
    headers = {'apikey': SUPABASE_KEY, 'Authorization': 'Bearer ' + SUPABASE_KEY,
               'Content-Type': 'application/json', 'Accept': 'application/json'}
    if rows is not None:
        headers['Prefer'] = 'resolution=merge-duplicates,return=representation'
    req = urllib.request.Request(SUPABASE_URL + '/rest/v1/' + path, headers=headers,
                                 data=None if rows is None else json.dumps(rows).encode(),
                                 method='GET' if rows is None else 'POST')
    return _request_json(req)


def _day_row(target, actual, scheduled, punches, allow_empty=False):
    row = {'date': target, **{k: 0.0 for k in METRICS}}
    expected, received = Counter(), Counter()
    for punch in punches:
        if _punch_day(punch) != target or punch.get('deleted'):
            continue
        role = _role(punch.get('role_id'))
        if role in EXCLUDED_ROLES:
            continue
        if role not in ROLE_DEPT:
            raise IncompleteData(f'Unmapped role {role}; review department assignment')
        if not punch.get('clocked_out') or punch.get('approved') is not True:
            raise IncompleteData('Time punches are awaiting approval or clock-out')
        expected[(int(punch['user_id']), role)] += 1
    for source, is_actual in ((actual, True), (scheduled, False)):
        for shift in source:
            if shift['date'][:10] != target:
                continue
            role = _role(shift.get('role_id'))
            if role in EXCLUDED_ROLES:
                continue
            if role not in ROLE_DEPT:
                raise IncompleteData(f'Unmapped role {role}; review department assignment')
            dept = ROLE_DEPT[role]
            total = shift['total']
            key = f'{dept}_hours' if is_actual else f'{dept}_sched'
            row[key] = round(row[key] + _number(total.get('total_hours')), 2)
            if is_actual:
                row[f'{dept}_cost'] = round(row[f'{dept}_cost'] + _number(total.get('total_pay')), 2)
                received[(int(shift['user_id']), role)] += 1
    if expected != received:
        raise IncompleteData('Hours and wages report does not match approved time punches')
    # A fully absent department can also mean POS punches have not synced yet.
    # Only a deliberate single-day allow_empty=1 can confirm a scheduled closure.
    for dept in ('kit', 'foh'):
        if not allow_empty and row[f'{dept}_sched'] >= 8 and row[f'{dept}_hours'] < row[f'{dept}_sched'] * .25:
            raise IncompleteData(f'{dept.upper()} hours are below 25% of scheduled hours; verify punches')
    for metric in ('hours', 'cost', 'sched'):
        row[f'total_{metric}'] = round(row[f'kit_{metric}'] + row[f'foh_{metric}'], 2)
    checked = datetime.now(timezone.utc).isoformat()
    return dict(row, fetched_at=checked, import_status='complete', import_note=None,
                source_checked_at=checked)


def _pending_row(target, existing, reason):
    return {'date': target, **{k: float(existing.get(k) or 0) for k in METRICS},
            'fetched_at': existing.get('fetched_at'), 'import_status': 'pending',
            'import_note': reason, 'source_checked_at': datetime.now(timezone.utc).isoformat()}


def _batches(days):
    batches = []
    for value in sorted(set(days)):
        if (not batches or len(batches[-1]) == 7 or
                date.fromisoformat(value) != date.fromisoformat(batches[-1][-1]) + timedelta(days=1)):
            batches.append([])
        batches[-1].append(value)
    return batches


def _sync_batch(days, dry_run, allow_empty):
    start, end = days[0], days[-1]
    existing = {r['date']: r for r in _supa(f'labor_daily?date=gte.{start}&date=lte.{end}&select=*')}
    try:
        actual = _report_shifts(start, end, True)
        scheduled = _report_shifts(start, end, False)
        punches = _range_punches(start, end)
    except Exception as exc:
        # Preserve previous numbers while making uncertainty visible.
        reason = str(exc) if isinstance(exc, IncompleteData) else f'Source fetch failed ({type(exc).__name__}); retry required'
        rows = [_pending_row(d, existing.get(d, {}), reason) for d in days]
    else:
        rows = []
        for d in days:
            try:
                rows.append(_day_row(d, actual, scheduled, punches, allow_empty))
            except (IncompleteData, KeyError, ValueError, TypeError) as exc:
                rows.append(_pending_row(d, existing.get(d, {}), str(exc)))
    if not dry_run:
        saved = _supa('labor_daily?on_conflict=date', rows)
        by_date = {r['date']: r for r in saved}
        if set(by_date) != set(days):
            raise RuntimeError('Database did not return all imported dates')
        for row in rows:
            stored = by_date[row['date']]
            if stored['import_status'] != row['import_status'] or any(float(stored[k]) != row[k] for k in METRICS):
                raise RuntimeError('Database verification failed')
    return rows


def run(target_date=None, dry_run=False, allow_empty=False, today=None):
    today = today or datetime.now(LOCAL_TZ).date()
    if target_date is not None:
        target = date.fromisoformat(target_date)
        if target.isoformat() != target_date or target >= today:
            raise ValueError('date must be a completed day in YYYY-MM-DD format')
        days = {target_date}
    else:
        if allow_empty:
            raise ValueError('allow_empty requires an explicit date')
        days = {(today - timedelta(days=i)).isoformat() for i in range(1, LOOKBACK_DAYS + 1)}
        pending = _supa('labor_daily?import_status=eq.pending&select=date&order=date&limit=1000')
        if len(pending) >= 1000:
            raise RuntimeError('Too many pending dates; manual reconciliation required')
        days.update(r['date'] for r in pending if r['date'] < today.isoformat())
        # Include older edits/deletions so late approvals beyond the rolling window recover.
        modified_since = datetime.combine(today - timedelta(days=7), day_time(), LOCAL_TZ).astimezone(timezone.utc)
        changed = []
        for deleted in ('false', 'true'):
            changed.extend(_punches({'modified_since': modified_since.isoformat(), 'deleted': deleted}))
        days.update(_punch_day(p) for p in changed if _punch_day(p) < today.isoformat())
    rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for batch_rows in pool.map(lambda batch: _sync_batch(batch, dry_run, allow_empty), _batches(days)):
            rows.extend(batch_rows)
    pending = [r for r in rows if r['import_status'] == 'pending']
    return {'status': 'pending' if pending else 'ok', 'dry_run': dry_run,
            'checked_dates': len(rows), 'complete_dates': len(rows) - len(pending),
            'pending_dates': [{'date': r['date'], 'reason': r['import_note']} for r in pending],
            'rows': rows}


class handler(BaseHTTPRequestHandler):
    def _reply(self, status, payload):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        if not CRON_SECRET or self.headers.get('authorization', '') != 'Bearer ' + CRON_SECRET:
            self._reply(401, {'error': 'Unauthorized'})
            return
        qs = parse_qs(urlparse(self.path).query)
        dry_run = qs.get('dry_run', ['0'])[0] == '1'
        target = qs.get('date', [None])[0]
        try:
            if not dry_run:
                report('labor', 'started', current_task='Reconciling 7shifts labor and late approvals')
            result = run(target, dry_run, qs.get('allow_empty', ['0'])[0] == '1')
            summary = {k: v for k, v in result.items() if k != 'rows'}
            print(json.dumps(summary), flush=True)
            if not dry_run:
                report('labor', 'failed' if result['pending_dates'] else 'finished',
                       output=json.dumps(summary))
            self._reply(503 if result['pending_dates'] else 200, result)
        except ValueError as exc:
            self._reply(400, {'error': str(exc)})
        except Exception as exc:
            message = f'Labor reconciliation failed ({type(exc).__name__})'
            print(message, flush=True)
            if not dry_run:
                report('labor', 'failed', message=message)
            self._reply(503, {'error': message})

    def log_message(self, *_args):
        pass
