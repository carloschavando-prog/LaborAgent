import copy
import io
import json
import os
import unittest
import urllib.error
import urllib.request
from datetime import date
from unittest.mock import patch

os.environ.setdefault('SUPABASE_URL', 'https://example.invalid')
os.environ.setdefault('SUPABASE_SERVICE_KEY', 'test')
os.environ.setdefault('SEVEN_SHIFTS_TOKEN', 'test')
from api import labor_fetch as f
from api import labor_report as r

DAY = '2026-10-15'


def shift(user=1, role=2754779, hours=8, pay=144, day=DAY):
    return {'user_id': user, 'role_id': role, 'date': day + ' 09:00:00',
            'total': {'total_hours': hours, 'total_pay': pay}}


def punch(user=1, role=2754779, approved=True, day=DAY):
    return {'id': user, 'user_id': user, 'role_id': role, 'approved': approved,
            'clocked_in': day + 'T13:00:00+00:00',
            'clocked_out': day + 'T21:00:00+00:00', 'deleted': None}


def payload(shifts=None):
    return {'start': DAY, 'end': DAY, 'users': [
        {'user': {'id': 1}, 'weeks': [{'shifts': [shift()] if shifts is None else shifts}]}]}


class ImportTests(unittest.TestCase):
    def test_complete_day_preserves_hours_and_wages(self):
        row = f._day_row(DAY, [shift()], [shift()], [punch()])
        self.assertEqual((row['kit_hours'], row['kit_cost'], row['total_hours']), (8, 144, 8))
        self.assertEqual(row['import_status'], 'complete')

    def test_late_approval_then_recovery(self):
        with self.assertRaisesRegex(f.IncompleteData, 'approval'):
            f._day_row(DAY, [], [shift()], [punch(approved=False)])
        self.assertEqual(f._day_row(DAY, [shift()], [shift()], [punch()])['import_status'], 'complete')

    def test_partial_report_is_rejected_even_when_nonzero(self):
        with self.assertRaisesRegex(f.IncompleteData, 'does not match'):
            f._day_row(DAY, [shift()], [shift()], [punch(), punch(user=2)])

    def test_open_punch_is_pending(self):
        p = punch(); p['clocked_out'] = None
        with self.assertRaisesRegex(f.IncompleteData, 'clock-out'):
            f._day_row(DAY, [], [shift()], [p])

    def test_unknown_role_is_not_silently_excluded(self):
        with self.assertRaisesRegex(f.IncompleteData, 'Unmapped role'):
            f._day_row(DAY, [shift(role=999)], [], [punch(role=999)])

    def test_manager_exclusion_and_string_role(self):
        row = f._day_row(DAY, [shift(role='2754779'), shift(2, 1760491)], [],
                         [punch(role='2754779'), punch(2, 1760491, False)])
        self.assertEqual(row['total_hours'], 8)

    def test_empty_scheduled_day_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(f.IncompleteData, '25%'):
            f._day_row(DAY, [], [shift()], [])
        self.assertEqual(f._day_row(DAY, [], [shift()], [], allow_empty=True)['total_hours'], 0)
        self.assertEqual(f._day_row(DAY, [], [], [])['total_hours'], 0)

    def test_pending_preserves_last_values(self):
        prior = f._day_row(DAY, [shift()], [], [punch()])
        pending = f._pending_row(DAY, prior, 'Awaiting approval')
        self.assertEqual(pending['kit_cost'], 144)
        self.assertEqual(pending['fetched_at'], prior['fetched_at'])
        self.assertEqual(pending['import_status'], 'pending')

    def test_utc_overnight_is_local_previous_day(self):
        p = punch(); p['clocked_in'] = '2026-10-16T01:00:00Z'
        self.assertEqual(f._punch_day(p), DAY)

    def test_local_day_bounds_handle_dst(self):
        with patch.object(f, '_punches', return_value=[]) as get:
            f._range_punches('2026-11-01', '2026-11-01')
        params = get.call_args.args[0]
        self.assertEqual(params['clocked_in[gte]'], '2026-11-01T04:00:00+00:00')
        self.assertEqual(params['clocked_in[lte]'], '2026-11-02T04:59:59+00:00')

    def test_invalid_report_or_missing_metrics_rejected(self):
        examples = [{}, dict(payload(), start='2026-10-14'), payload([dict(shift(), total={})]),
                    payload([shift(hours=float('nan'))]), payload([shift(day='2026-10-14')])]
        for data in examples:
            with self.subTest(data=data), patch.object(f, '_7shifts_get', return_value=data):
                with self.assertRaises((f.IncompleteData, ValueError)):
                    f._report_shifts(DAY, DAY, True)

    def test_punch_pagination_reads_all_pages(self):
        pages = [{'data': [punch()], 'meta': {'cursor': {'next': 'page2'}}},
                 {'data': [punch(2)], 'meta': {'cursor': {'next': None}}}]
        with patch.object(f, '_7shifts_get', side_effect=pages):
            self.assertEqual(len(f._punches({})), 2)

    def test_truncated_or_repeating_pagination_fails(self):
        for response in [{'data': []}, {'data': [], 'meta': {'cursor': {'next': 'same'}}}]:
            with patch.object(f, '_7shifts_get', return_value=response):
                with self.assertRaises(f.IncompleteData): f._punches({})

    def test_retry_transient_http_failure(self):
        failure = urllib.error.HTTPError('https://example.invalid', 503, 'Unavailable', {}, None)
        with patch.object(f.urllib.request, 'urlopen', side_effect=[failure, io.BytesIO(b'{"ok":true}')]) as get, patch.object(f.time, 'sleep'):
            self.assertEqual(f._request_json(urllib.request.Request('https://example.invalid')), {'ok': True})
            self.assertEqual(get.call_count, 2)

    def test_dry_run_cannot_write(self):
        with patch.object(f, '_supa', return_value=[]) as db, patch.object(f, '_report_shifts', return_value=[shift()]), patch.object(f, '_range_punches', return_value=[punch()]):
            rows = f._sync_batch([DAY], True, False)
        self.assertEqual(rows[0]['import_status'], 'complete')
        self.assertEqual(db.call_count, 1)
        self.assertEqual(len(db.call_args.args), 1)

    def test_partial_response_cannot_overwrite_good_metrics(self):
        prior = f._day_row(DAY, [shift()], [], [punch()])
        saved = []
        def db(path, rows=None):
            if rows is None: return [prior]
            saved.extend(rows); return copy.deepcopy(rows)
        with patch.object(f, '_supa', side_effect=db), patch.object(f, '_report_shifts', side_effect=f.IncompleteData('Missing report')), patch.object(f, '_range_punches', return_value=[]):
            f._sync_batch([DAY], False, False)
        self.assertEqual((saved[0]['import_status'], saved[0]['kit_cost']), ('pending', 144))

    def test_rolling_window_older_edits_and_pending_dates(self):
        picked = []
        def sync(days, dry, empty):
            picked.extend(days); return []
        old = punch(day='2026-08-01')
        with patch.object(f, '_supa', return_value=[{'date':'2026-07-01'}]), patch.object(f, '_punches', return_value=[old]) as modified, patch.object(f, '_sync_batch', side_effect=sync):
            f.run(dry_run=True, today=date(2026,10,19))
        self.assertEqual(len(picked), 30)
        self.assertTrue({DAY, '2026-09-21', '2026-10-18', '2026-07-01', '2026-08-01'} <= set(picked))
        self.assertNotIn('2026-10-19', picked)
        self.assertEqual({call.args[0]['deleted'] for call in modified.call_args_list}, {'true', 'false'})

    def test_manual_dates_validated_before_writes(self):
        for d in ['bad', '2026-10-19', '2026-10-20']:
            with self.assertRaises(ValueError): f.run(d, today=date(2026,10,19))
        with self.assertRaises(ValueError): f.run(allow_empty=True)

    def test_batches_are_contiguous_and_at_most_week(self):
        batches=f._batches(['2026-10-%02d'%i for i in range(1,17)]+['2026-08-01'])
        self.assertEqual([len(b) for b in batches], [1,7,7,2])


class ReportTests(unittest.TestCase):
    def test_pending_day_never_displays_zero_cost(self):
        rows=r._build_rows(2026,date(2026,10,19),{DAY:{'import_status':'pending'}},
                           {DAY:{'total':11864,'food':5000}})
        row=next(x for x in rows if x.get('date')==DAY)
        html=r._tr(row)
        self.assertIn('Pending',html)
        self.assertNotIn('$0.00',html)
        self.assertIn('$11,864',html)

    def test_pending_subtotal_retains_sales_but_suppresses_percent(self):
        rows=[{'_type':'data','total_sales':100,'food_sales':30,'foh_sales':70,'kit_cost':20,'tot_cost':30},
              {'_type':'pending','total_sales':200,'food_sales':50,'foh_sales':150}]
        result=r._subtotal_row(rows,'Total','week_sub')
        self.assertEqual(result['total_sales'],300)
        self.assertEqual(result['kit_cost'],20)
        self.assertIsNone(result['tot_pct'])
        self.assertIn('partial (1 pending)', result['label'])

    def test_question_does_not_treat_pending_as_zero(self):
        answer=r._answer_labor_question('total kitchen hours this month',2026,date(2026,10,19),
                                         {DAY:{'import_status':'pending','kit_hours':0}}, {})
        self.assertIn('incomplete',answer)

    def test_existing_question_feature_still_calculates(self):
        answer=r._answer_labor_question('total kitchen hours this month',2026,date(2026,10,19),
                                         {DAY:{'import_status':'complete','kit_hours':45.51}}, {})
        self.assertIn('45.5',answer)

if __name__=='__main__': unittest.main()
