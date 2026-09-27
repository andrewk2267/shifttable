"""ShiftTable tests: rule engine (pure) and the HTTP API against throwaway databases.

Run from the ShiftTable folder:  python3 -m unittest discover -s tests -v
"""
import base64
import hashlib
import http.cookiejar
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

os.environ.setdefault('SHIFTTABLE_PBKDF2_ITER', '2000')  # fast hashing for tests only
os.environ['SHIFTTABLE_QUIET'] = '1'
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import demo_sync  # noqa: E402
import rules  # noqa: E402
import sample_data  # noqa: E402
import server  # noqa: E402

TODAY = date.today()
MON = rules.monday_of(TODAY)


def d(offset):
    return (MON + timedelta(days=offset)).isoformat()


# ================================================================ rule engine

SETTINGS = {k: (float(v[1]) if v[0] == 'num' else v[1]) for k, v in server.SETTINGS.items()}
BRANCHES = {1: {'id': 1, 'name': 'North', 'code': 'N', 'active': 1}, 2: {'id': 2, 'name': 'South', 'code': 'S', 'active': 1}}


def emp(i, **kw):
    e = {'id': i, 'name': f'E{i}', 'position_id': 1, 'branch_id': 1, 'branches': {1}, 'employment_type': 'full_time',
         'hourly_rate': 10.0, 'target_hours': 40, 'max_hours': 48, 'active': 1, 'skills': {1}, 'availability': {}}
    e.update(kw)
    return e


def shift(i, day, start, end, eid=1, brk=60, pos=1, status='published', branch=1):
    return {'id': i, 'date': d(day), 'start_time': start, 'end_time': end, 'break_minutes': brk,
            'employee_id': eid, 'position_id': pos, 'branch_id': branch, 'status': status, 'notes': ''}


def need(i, wd, start, end, headcount=1, branch=1, pos=1, brk=60):
    return {'id': i, 'weekday': wd, 'position_id': pos, 'branch_id': branch, 'start_time': start, 'end_time': end,
            'break_minutes': brk, 'headcount': headcount, 'label': ''}


def event(i, day, kind, branch=None, days=1):
    return {'id': i, 'start_date': d(day), 'end_date': d(day + days - 1), 'kind': kind, 'name': f'{kind} {i}', 'branch_id': branch, 'notes': ''}


def ctx(shifts, emps=None, needs=None, leave=None, forecasts=None, events=None, branches=None, attendance=None, **settings):
    st = dict(SETTINGS, **settings)
    return {'settings': st, 'employees': {e['id']: e for e in (emps or [emp(1)])},
            'positions': {1: {'id': 1, 'name': 'Server', 'department': 'Floor'}, 2: {'id': 2, 'name': 'Cook', 'department': 'Kitchen'}},
            'branches': branches or {1: BRANCHES[1]}, 'needs': needs or [], 'shifts': shifts, 'leave': leave or [],
            'forecasts': forecasts or {}, 'events': events or [], 'attendance': attendance or []}


def codes(ev, severity=None):
    return sorted(i['code'] for i in ev['issues'] if severity is None or i['severity'] == severity)


class RuleEngineTests(unittest.TestCase):
    def test_compliant_week_has_no_issues(self):
        s = [shift(i + 1, i, '09:00', '18:00') for i in range(5)]
        ev = rules.evaluate(ctx(s), d(0))
        self.assertEqual(ev['issues'], [])
        self.assertEqual(ev['totals']['hours'], 40)
        self.assertEqual(ev['totals']['cost'], 400)

    def test_overnight_shift_hours(self):
        s = shift(1, 0, '18:00', '02:00', brk=30)
        self.assertEqual(rules.paid_hours(s), 7.5)
        self.assertTrue(rules.is_closing(s))

    def test_overlap_is_an_error_reported_once(self):
        ev = rules.evaluate(ctx([shift(1, 0, '09:00', '17:00'), shift(2, 0, '16:00', '20:00', brk=0)]), d(0))
        self.assertEqual(codes(ev, 'error'), ['overlap'])

    def test_clopen_detected_even_across_weeks(self):
        ev = rules.evaluate(ctx([shift(1, -1, '14:00', '23:00'), shift(2, 0, '07:30', '16:30')]), d(0))
        self.assertIn('rest', codes(ev))
        ev = rules.evaluate(ctx([shift(1, 0, '14:00', '23:00'), shift(2, 1, '10:00', '19:00')]), d(0))
        self.assertNotIn('rest', codes(ev))  # 11 h exactly is fine

    def test_split_shift_same_day_is_not_a_clopen(self):
        ev = rules.evaluate(ctx([shift(1, 0, '10:00', '14:00', brk=0), shift(2, 0, '17:00', '22:00', brk=0)]), d(0))
        self.assertNotIn('rest', codes(ev))

    def test_daily_limit(self):
        ev = rules.evaluate(ctx([shift(1, 0, '06:00', '20:00', brk=60)]), d(0))
        self.assertIn('daily', codes(ev, 'error'))

    def test_break_rule(self):
        ev = rules.evaluate(ctx([shift(1, 0, '09:00', '17:00', brk=15)]), d(0))
        self.assertIn('break', codes(ev))
        ev = rules.evaluate(ctx([shift(1, 0, '09:00', '14:00', brk=0)]), d(0))
        self.assertNotIn('break', codes(ev))

    def test_leave_availability_and_skill(self):
        e = emp(1, availability={0: {'available': 0}, 1: {'available': 1, 'start_time': '17:00', 'end_time': '23:59'}})
        s = [shift(1, 0, '09:00', '17:00'), shift(2, 1, '12:00', '21:00'), shift(3, 2, '09:00', '17:00', pos=2)]
        lv = [{'employee_id': 1, 'start_date': d(2), 'end_date': d(2), 'kind': 'Annual leave'}]
        ev = rules.evaluate(ctx(s, [e], leave=lv), d(0))
        self.assertEqual(codes(ev), ['availability', 'availability', 'leave', 'skill'])

    def test_weekly_limits_overtime_and_cost_premium(self):
        s = [shift(i + 1, i, '08:00', '17:00') for i in range(6)]  # 6 x 8 h = 48 h
        ev = rules.evaluate(ctx(s, [emp(1, max_hours=44)]), d(0))
        self.assertIn('max_hours', codes(ev))
        self.assertIn('overtime', codes(ev, 'info'))
        self.assertAlmostEqual(ev['totals']['cost'], 500)  # 44 h at 10 + 4 h at 15
        pt = rules.evaluate(ctx(s, [emp(1, employment_type='part_time', max_hours=48)]), d(0))
        self.assertIn('part_time', codes(pt))

    def test_rest_day_and_consecutive_days(self):
        s = [shift(i + 1, i, '09:00', '15:00', brk=0) for i in range(7)]
        self.assertIn('rest_day', codes(rules.evaluate(ctx(s), d(0)), 'error'))
        s = [shift(i + 10, i - 3, '09:00', '15:00', brk=0) for i in range(3)] + [shift(i + 1, i, '09:00', '15:00', brk=0) for i in range(5)]
        ev = rules.evaluate(ctx(s), d(0))
        self.assertIn('consecutive', codes(ev))
        self.assertNotIn('rest_day', codes(ev))

    def test_monthly_overtime_cap(self):
        ev = rules.evaluate(ctx([shift(i + 1, i, '06:00', '18:00', brk=0) for i in range(6)], max_monthly_ot=10), d(0))
        self.assertIn('monthly_ot', codes(ev, 'error'))

    def test_coverage_open_shift_and_labour(self):
        s = [shift(1, 0, '10:00', '16:00', brk=0), shift(2, 0, '11:00', '15:00', eid=None, brk=0)]
        ev = rules.evaluate(ctx(s, needs=[need(1, 0, '11:00', '15:00', 2, brk=0)], forecasts={(1, d(0)): 100}), d(0))
        day = ev['days'][0]
        self.assertEqual((day['need_total'], day['staffed'], day['open']), (2, 1, 1))
        self.assertIn('open', codes(ev))
        self.assertIn('labour', codes(ev))
        self.assertNotIn('coverage', codes(ev))  # the open shift is already counted as a slot

    def test_candidates_explain_why_not(self):
        c = ctx([shift(1, 0, '14:00', '23:00')], [emp(1), emp(2, skills={2}, position_id=2)])
        rows = {r['employee_id']: r for r in rules.rank_candidates(c, shift(99, 1, '07:30', '16:30', eid=None))}
        self.assertFalse(rows[1]['ok'])
        self.assertTrue(any('rest' in x for x in rows[1]['reasons']))
        self.assertTrue(any('Not trained' in x for x in rows[2]['reasons']))

    def test_autofill_respects_every_rule(self):
        emps = [emp(i, target_hours=24 if i > 4 else 40, max_hours=30 if i > 4 else 44,
                    employment_type='part_time' if i > 4 else 'full_time') for i in range(1, 9)]
        emps[5]['availability'] = {wd: {'available': 0} for wd in range(5)}
        needs = [need(n, wd, st, en, 2) for n, (wd, st, en) in enumerate([(wd, st, en) for wd in range(7) for st, en in (('07:30', '16:30'), ('14:00', '23:00'))], start=1)]
        lv = [{'employee_id': 1, 'start_date': d(2), 'end_date': d(3), 'kind': 'Annual leave'}]
        keep = shift(500, 0, '07:30', '16:30', eid=2)
        c = ctx([keep], emps, needs=needs, leave=lv)
        new, _ = rules.autofill(c, d(0))
        c['shifts'] = [keep] + new
        ev = rules.evaluate(c, d(0))
        self.assertEqual(codes(ev, 'error'), [])
        self.assertFalse({'rest', 'availability', 'leave', 'skill', 'max_hours', 'break', 'consecutive', 'overlap', 'branch'} & set(codes(ev)), codes(ev))
        self.assertEqual(ev['totals']['staffed'], 28)
        self.assertEqual(keep['employee_id'], 2)
        seen = set()
        for s in new:
            if s['employee_id']:
                self.assertNotIn((s['employee_id'], s['date']), seen)
                seen.add((s['employee_id'], s['date']))

    def test_autofill_fairness_prefers_people_under_target(self):
        c = ctx([], [emp(1), emp(2)], needs=[need(1, wd, '09:00', '18:00') for wd in range(6)])
        new, _ = rules.autofill(c, d(0))
        self.assertEqual(sorted(sum(1 for s in new if s['employee_id'] == i) for i in (1, 2)), [3, 3])

    def test_summarize_range_counts_clopens(self):
        r = rules.summarize_range(ctx([shift(1, 0, '14:00', '23:00'), shift(2, 1, '07:30', '16:30')]), d(0), d(6))
        self.assertEqual(r['employees'][0]['clopens'], 1)
        self.assertEqual(r['totals']['hours'], 16)

    # ---- branches
    def test_limits_count_hours_at_every_branch(self):
        s = [shift(1, 0, '07:00', '13:00', brk=0, branch=1), shift(2, 0, '13:00', '20:00', brk=0, branch=2),
             shift(3, 1, '09:00', '12:00', brk=0, branch=2), shift(4, 1, '11:00', '15:00', brk=0, branch=1)]
        c = ctx(s, [emp(1, branches={1, 2})], branches=BRANCHES)
        north = rules.evaluate(c, d(0), 1)
        self.assertIn('daily', codes(north, 'error'))  # 13 h across two branches on day 0
        self.assertIn('overlap', codes(north, 'error'))  # day 1 clash with the South shift
        self.assertEqual(north['totals']['shifts'], 2)
        self.assertEqual(north['employees'][0]['hours'], 20)
        self.assertEqual(north['employees'][0]['branch_hours'], 10)

    def test_branch_membership_and_scoped_coverage(self):
        s = [shift(1, 0, '09:00', '17:00', branch=2)]
        c = ctx(s, [emp(1)], needs=[need(1, 0, '09:00', '17:00', branch=1), need(2, 0, '09:00', '17:00', branch=2)], branches=BRANCHES)
        self.assertIn('branch', codes(rules.evaluate(c, d(0))))
        north, south = rules.evaluate(c, d(0), 1), rules.evaluate(c, d(0), 2)
        self.assertEqual((north['totals']['staffed'], north['totals']['need_total']), (0, 1))
        self.assertEqual((south['totals']['staffed'], south['totals']['need_total']), (1, 1))

    def test_autofill_keeps_people_at_home_and_respects_branch_setup(self):
        emps = [emp(1, branch_id=1, branches={1, 2}), emp(2, branch_id=2, branches={2}), emp(3, branch_id=1, branches={1})]
        c = ctx([], emps, needs=[need(1, 0, '09:00', '17:00', branch=1), need(2, 0, '09:00', '17:00', branch=2)], branches=BRANCHES)
        new, _ = rules.autofill(c, d(0), branch_id=None)
        by_branch = {s['branch_id']: s['employee_id'] for s in new}
        self.assertEqual(by_branch[2], 2)  # South's own person, not the floater from North
        self.assertIn(by_branch[1], (1, 3))
        new, _ = rules.autofill(ctx([], [emp(3, branch_id=1, branches={1})], needs=[need(2, 0, '09:00', '17:00', branch=2)], branches=BRANCHES), d(0), branch_id=2)
        self.assertIsNone(new[0]['employee_id'])  # nobody is set up for South
        rows = rules.rank_candidates(ctx([], [emp(3, branch_id=1, branches={1})], branches=BRANCHES), shift(9, 0, '09:00', '17:00', eid=None, branch=2))
        self.assertTrue(rows[0]['ok'])  # borrowing is allowed when finding cover, with a note
        self.assertTrue(rows[0]['notes'])

    # ---- holidays and closures
    def test_public_holiday_pay_and_closures(self):
        s = [shift(1, 0, '09:00', '17:00', brk=0), shift(2, 1, '09:00', '17:00', brk=0)]
        c = ctx(s, events=[event(1, 0, 'holiday'), event(2, 1, 'closed', branch=1)], needs=[need(1, 1, '09:00', '17:00')])
        ev = rules.evaluate(c, d(0))
        self.assertEqual(ev['shift_cost'][1], 160)  # 8 h x 10 x 2
        self.assertEqual(ev['shift_cost'][2], 80)
        self.assertIn('closed', codes(ev))
        self.assertEqual(ev['days'][1]['need_total'], 0)  # needs pause on a closed day
        self.assertTrue(ev['days'][0]['holiday'])
        new, _ = rules.autofill(ctx([], [emp(1)], needs=[need(1, 1, '09:00', '17:00')], events=[event(2, 1, 'closed')]), d(0))
        self.assertEqual(new, [])
        other = ctx(s, events=[event(3, 0, 'holiday', branch=2)])
        self.assertEqual(rules.evaluate(other, d(0))['shift_cost'][1], 80)  # a holiday at another branch doesn't apply

    # ---- actual vs rostered
    def test_compare_actual_statuses(self):
        s = [shift(1, -7, '09:00', '17:00'), shift(2, -6, '09:00', '17:00'), shift(3, -5, '22:00', '06:00'),
             shift(4, -4, '09:00', '17:00'), shift(5, -3, '09:00', '17:00')]
        day = lambda o, t: f'{d(o)} {t}'
        att = [{'id': 1, 'employee_id': 1, 'branch_id': 1, 'date': d(-7), 'clock_in': day(-7, '08:55'), 'clock_out': day(-7, '17:02'), 'break_minutes': 60},
               {'id': 2, 'employee_id': 1, 'branch_id': 1, 'date': d(-6), 'clock_in': day(-6, '09:20'), 'clock_out': day(-6, '16:30'), 'break_minutes': 60},
               {'id': 3, 'employee_id': 1, 'branch_id': 1, 'date': d(-5), 'clock_in': day(-5, '21:58'), 'clock_out': day(-4, '06:05'), 'break_minutes': 60},
               {'id': 4, 'employee_id': 1, 'branch_id': 1, 'date': d(-3), 'clock_in': day(-3, '09:00'), 'clock_out': None, 'break_minutes': 0},
               {'id': 5, 'employee_id': 1, 'branch_id': 1, 'date': d(-2), 'clock_in': day(-2, '10:00'), 'clock_out': day(-2, '14:00'), 'break_minutes': 0}]
        c = ctx(s, attendance=att)
        now = TODAY.toordinal() * rules.DAY + 23 * 60
        r = rules.compare_actual(c, d(-7), d(-1), now)
        st = {x['shift_id']: x['status'] for x in r['details']}
        self.assertEqual(st[1], ['ok'])
        self.assertEqual(st[2], ['late', 'early'])
        self.assertEqual(st[3], ['ok'])  # overnight shift matched to the overnight record
        self.assertEqual(st[4], ['no_show'])
        self.assertEqual(st[5], ['missing_out'])
        self.assertEqual([x['status'] for x in r['details'] if x['shift_id'] is None], [['unscheduled']])
        t = r['totals']
        self.assertEqual((t['sched_shifts'], t['attended'], t['no_show'], t['late'], t['early'], t['unscheduled']), (5, 4, 1, 1, 1, 1))
        self.assertEqual(t['late_minutes'], 20)
        self.assertEqual(t['attendance_pct'], 80)
        self.assertAlmostEqual(r['employees'][0]['actual_hours'], 7.12 + 6.17 + 7.12 + 4, places=1)
        only = rules.compare_actual(c, d(-7), d(-1), now, employee_ids={2})
        self.assertEqual(only['employees'], [])


# ================================================================ HTTP API

class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.headers = None  # of the last response

    def call(self, method, path, body=None, csrf=True, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        if csrf:
            req.add_header('X-ShiftTable', '1')
        try:
            with self.op.open(req) as r:
                self.headers = r.headers
                payload = r.read()
                return r.status, (payload if raw else json.loads(payload or b'null'))
        except urllib.error.HTTPError as e:
            self.headers = e.headers
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, payload

    def login(self, user, pw):
        st, body = self.call('POST', '/api/login', {'username': user, 'password': pw})
        assert st == 200, body
        return body['user']


PW = {u: p for u, p, *_ in sample_data.DEMO_USERS}


def start_server(tmp, name):
    server.CONFIG['db'] = os.path.join(tmp, name)
    server.CONFIG['backups'] = os.path.join(tmp, 'backups')
    httpd = server.make_server('127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f'http://127.0.0.1:{httpd.server_address[1]}'


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.httpd, cls.base = start_server(cls.tmp, 'test.db')

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def as_(self, user):
        c = Client(self.base)
        c.login(user, PW[user])
        return c

    def q(self, sql, args=()):
        conn = sqlite3.connect(server.CONFIG['db'])
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()

    def branches(self, c):
        return c.call('GET', '/api/bootstrap')[1]['branches']

    # ---- auth and access control
    def test_auth_flow_and_lockout(self):
        c = Client(self.base)
        self.assertEqual(c.call('GET', '/api/me')[1], {'user': None})
        self.assertEqual(c.call('GET', '/api/roster')[0], 401)
        self.assertEqual(c.call('POST', '/api/login', {'username': "' OR 1=1 --", 'password': 'x'})[0], 401)
        for _ in range(5):
            self.assertEqual(c.call('POST', '/api/login', {'username': 'priya', 'password': 'wrong'})[0], 401)
        self.assertEqual(c.call('POST', '/api/login', {'username': 'priya', 'password': PW['priya']})[0], 429)
        server.clear_fails('priya')
        self.assertEqual(c.login('priya', PW['priya'])['role'], 'staff')
        self.assertEqual(c.call('GET', '/api/me')[1]['user']['username'], 'priya')
        self.assertEqual(c.call('POST', '/api/logout', {})[0], 200)
        self.assertEqual(c.call('GET', '/api/bootstrap')[0], 401)

    def test_csrf_header_required(self):
        self.assertEqual(self.as_('manager').call('POST', '/api/roster/publish', {'week': d(0)}, csrf=False)[0], 403)

    def test_role_enforcement(self):
        staff, mgr = self.as_('marcus'), self.as_('manager')
        for method, path in [('GET', '/api/employees'), ('PUT', '/api/settings'), ('GET', '/api/users'), ('GET', '/api/reports?from=2026-01-01&to=2026-01-02'),
                             ('GET', '/api/reports/actual?from=2026-01-01&to=2026-01-02'), ('POST', '/api/shifts'), ('GET', '/api/data/export'),
                             ('GET', '/api/audit'), ('POST', '/api/events'), ('POST', '/api/data/import'), ('POST', '/api/attendance/delete')]:
            self.assertEqual(staff.call(method, path, {} if method != 'GET' else None)[0], 403, path)
        for method, path in [('GET', '/api/users'), ('PUT', '/api/settings'), ('GET', '/api/data/stats'), ('POST', '/api/data/reset'), ('POST', '/api/branches')]:
            self.assertEqual(mgr.call(method, path, {} if method != 'GET' else None)[0], 403, path)
        self.assertEqual(mgr.call('POST', '/api/data/import', {'type': 'employees', 'csv': 'name,position\nX,Server\n'})[0], 403)

    def test_staff_roster_is_published_only_and_hides_pay(self):
        staff = self.as_('marcus')
        self.assertEqual(staff.call('GET', f'/api/roster?week={d(14)}')[1]['shifts'], [])
        r = staff.call('GET', f'/api/roster?week={d(0)}')[1]
        self.assertTrue(r['shifts'])
        self.assertNotIn('cost', r['shifts'][0])
        self.assertNotIn('issues', r)
        boot = staff.call('GET', '/api/bootstrap')[1]
        self.assertNotIn('hourly_rate', boot['employees'][0])
        self.assertIn('hourly_rate', boot['me'])
        self.assertEqual(len(boot['branches']), 2)

    # ---- shifts and roster
    def test_shift_crud_validation_and_change_tracking(self):
        m = self.as_('manager')
        eid = m.call('GET', '/api/employees')[1][0]['id']
        bad = [{'date': 'nope'}, {'date': d(21), 'start_time': '25:00', 'end_time': '10:00'},
               {'date': d(21), 'start_time': '09:00', 'end_time': '10:00', 'break_minutes': 90, 'employee_id': eid},
               {'date': d(21), 'start_time': '09:00', 'end_time': '17:00', 'employee_id': 99999},
               {'date': d(21), 'start_time': '09:00', 'end_time': '17:00', 'employee_id': eid, 'branch_id': 999}]
        for b in bad:
            self.assertEqual(m.call('POST', '/api/shifts', b)[0], 400, b)
        st, res = m.call('POST', '/api/shifts', {'date': d(21), 'start_time': '09:00', 'end_time': '17:00', 'break_minutes': 15, 'employee_id': eid, 'notes': '<b>x</b>'})
        self.assertEqual(st, 200, res)
        sid = res['shift']['id']
        self.assertEqual(res['shift']['status'], 'draft')
        self.assertEqual(res['shift']['notes'], '<b>x</b>')
        self.assertIsNotNone(res['shift']['branch_id'])  # defaults to the person's home branch
        self.assertIn('break', [i['code'] for i in res['issues']])
        self.assertEqual(m.call('POST', '/api/roster/publish', {'week': d(21)})[0], 200)
        st, res = m.call('PUT', f'/api/shifts/{sid}', {'date': d(21), 'start_time': '10:00', 'end_time': '18:00', 'break_minutes': 60, 'employee_id': eid})
        self.assertEqual(res['shift']['changed_after_publish'], 1)
        self.assertEqual(m.call('DELETE', f'/api/shifts/{sid}')[0], 200)
        self.assertEqual(m.call('DELETE', f'/api/shifts/{sid}')[0], 400)

    def test_autofill_copy_publish_clear_per_branch(self):
        m = self.as_('manager')
        b1, b2 = [b['id'] for b in self.branches(m)]
        wk = d(28)
        self.assertEqual(m.call('POST', '/api/roster/autofill', {'week': d(-7)})[0], 400)
        r1 = m.call('POST', '/api/roster/autofill', {'week': wk, 'branch_id': b1})[1]
        self.assertGreater(r1['created'], 30)
        only = m.call('GET', f'/api/roster?week={wk}&branch={b2}')[1]
        self.assertEqual(only['totals']['shifts'], 0)  # the other branch was left alone
        m.call('POST', '/api/roster/autofill', {'week': wk})
        roster = m.call('GET', f'/api/roster?week={wk}')[1]
        self.assertEqual(roster['totals']['errors'], 0, [i['message'] for i in roster['issues'] if i['severity'] == 'error'])
        self.assertFalse({'rest', 'availability', 'leave', 'max_hours', 'branch'} & {i['code'] for i in roster['issues']})
        self.assertEqual(m.call('POST', '/api/roster/autofill', {'week': wk})[1]['created'], 0)  # idempotent
        total = roster['totals']['shifts']
        b1_count = m.call('GET', f'/api/roster?week={wk}&branch={b1}')[1]['totals']['shifts']
        self.assertEqual(m.call('POST', '/api/roster/copy', {'week': d(35), 'from_week': wk, 'branch_id': b1})[1]['created'], b1_count)
        self.assertEqual(m.call('POST', '/api/roster/copy', {'week': d(35), 'from_week': wk})[1]['created'], total - b1_count)
        self.assertEqual(m.call('POST', '/api/roster/publish', {'week': d(35), 'branch_id': b2})[1]['published'], total - b1_count)
        self.assertEqual(m.call('POST', '/api/roster/clear', {'week': d(35)})[1]['deleted'], b1_count)  # only drafts go
        self.assertEqual(m.call('POST', '/api/roster/clear', {'week': wk})[1]['deleted'], total)

    def test_branch_roster_shows_other_branch_commitments(self):
        m = self.as_('manager')
        b1, b2 = [b['id'] for b in self.branches(m)]
        emps = m.call('GET', '/api/employees')[1]
        floater = next(e for e in emps if e['branch_id'] == b1 and b2 in e['branches'])
        m.call('POST', '/api/shifts', {'date': d(42), 'start_time': '09:00', 'end_time': '13:00', 'employee_id': floater['id'], 'branch_id': b2})
        r = m.call('GET', f'/api/roster?week={d(42)}&branch={b1}')[1]
        self.assertTrue(any(s['elsewhere'] and s['employee_id'] == floater['id'] for s in r['shifts']))
        self.assertTrue(all(not s['elsewhere'] for s in r['shifts'] if s['branch_id'] == b1))
        m.call('POST', '/api/roster/clear', {'week': d(42)})

    def test_candidates_and_forecast(self):
        m = self.as_('manager')
        roster = m.call('GET', f'/api/roster?week={d(7)}')[1]
        open_shift = next(s for s in roster['shifts'] if not s['employee_id'])
        cands = m.call('GET', f"/api/shifts/{open_shift['id']}/candidates")[1]
        self.assertTrue(any(c['ok'] for c in cands))
        self.assertTrue(all(c['reasons'] for c in cands if not c['ok']))
        b1 = self.branches(m)[0]['id']
        self.assertEqual(m.call('PUT', '/api/forecasts', {'date': d(7), 'branch_id': b1, 'sales': -5})[0], 400)
        self.assertEqual(m.call('PUT', '/api/forecasts', {'date': d(7), 'sales': 5})[0], 400)  # branch is required
        self.assertEqual(m.call('PUT', '/api/forecasts', {'date': d(7), 'branch_id': b1, 'sales': 9999})[0], 200)
        r = m.call('GET', f'/api/roster?week={d(7)}&branch={b1}')[1]
        self.assertEqual(r['day_stats'][0]['sales'], 9999)

    # ---- holidays, events and the month calendar
    def test_events_holidays_and_calendar(self):
        m, staff = self.as_('manager'), self.as_('marcus')
        b1 = self.branches(m)[0]['id']
        bad = [{'name': 'x', 'kind': 'party', 'start_date': d(3)}, {'name': '', 'kind': 'event', 'start_date': d(3)},
               {'name': 'x', 'kind': 'event', 'start_date': d(3), 'end_date': d(1)}, {'name': 'x', 'kind': 'event', 'start_date': d(3), 'branch_id': 999}]
        for b in bad:
            self.assertEqual(m.call('POST', '/api/events', b)[0], 400, b)
        st, e = m.call('POST', '/api/events', {'name': 'Stocktake', 'kind': 'closed', 'start_date': d(50), 'end_date': d(50), 'branch_id': b1})
        self.assertEqual(st, 200, e)
        self.assertEqual(m.call('POST', '/api/roster/autofill', {'week': rules.monday_of(date.fromisoformat(d(50))).isoformat(), 'branch_id': b1})[0], 200)
        self.assertIsNone(self.q('SELECT id FROM shifts WHERE date = ? AND branch_id = ?', (d(50), b1)))  # closed day skipped
        ph = m.call('POST', '/api/events/holidays', {'year': 2027})[1]
        self.assertEqual(ph['added'], 0)  # the demo already carries next year's holidays
        self.assertEqual(m.call('POST', '/api/events/holidays', {'year': 1999})[0], 400)
        month = date.fromisoformat(d(50)).strftime('%Y-%m')
        cal = m.call('GET', f'/api/calendar?month={month}&branch={b1}')[1]
        self.assertTrue(any(ev['name'] == 'Stocktake' for ev in cal['events']))
        self.assertIn(d(50), cal['days'])
        self.assertIn('shifts', cal['days'][d(50)])
        scal = staff.call('GET', f'/api/calendar?month={TODAY.strftime("%Y-%m")}')[1]
        self.assertTrue(all('mine' in v for v in scal['days'].values()))
        self.assertEqual(staff.call('GET', '/api/calendar?month=2026-13')[0], 400)
        self.assertEqual(m.call('PUT', f"/api/events/{e['id']}", {'name': 'Stocktake', 'kind': 'event', 'start_date': d(50)})[1]['kind'], 'event')
        self.assertEqual(m.call('DELETE', f"/api/events/{e['id']}")[0], 200)
        m.call('POST', '/api/roster/clear', {'week': rules.monday_of(date.fromisoformat(d(50))).isoformat()})

    # ---- branches
    def test_branch_admin_guards(self):
        a = self.as_('admin')
        b1, b2 = [b['id'] for b in self.branches(a)]
        self.assertEqual(a.call('POST', '/api/branches', {'name': 'Robertson Quay', 'color': '#123456'})[0], 409)
        self.assertEqual(a.call('POST', '/api/branches', {'name': 'X', 'color': 'red'})[0], 400)
        self.assertEqual(a.call('POST', '/api/branches', {'name': 'X', 'code': 'TOO-LONG', 'color': '#123456'})[0], 400)
        st, nb = a.call('POST', '/api/branches', {'name': 'Jewel', 'code': 'jwl', 'color': '#123456'})
        self.assertEqual((st, nb['code']), (200, 'JWL'))
        self.assertEqual(a.call('DELETE', f'/api/branches/{b1}')[0], 409)  # has people and shifts
        self.assertEqual(a.call('PUT', f'/api/branches/{b1}', {'name': 'Robertson Quay', 'color': '#0e6b5c', 'active': 0})[0], 200)
        self.assertEqual(a.call('PUT', f'/api/branches/{b2}', {'name': 'Tampines Mall', 'color': '#b45f1c', 'active': 0})[0], 200)
        self.assertEqual(a.call('PUT', f"/api/branches/{nb['id']}", {'name': 'Jewel', 'color': '#123456', 'active': 0})[0], 400)  # last active
        a.call('PUT', f'/api/branches/{b1}', {'name': 'Robertson Quay', 'color': '#0e6b5c', 'active': 1})
        a.call('PUT', f'/api/branches/{b2}', {'name': 'Tampines Mall', 'color': '#b45f1c', 'active': 1})
        self.assertEqual(a.call('DELETE', f"/api/branches/{nb['id']}")[0], 200)

    def test_needs_are_per_branch_and_copy_between_branches(self):
        m = self.as_('manager')
        b1, b2 = [b['id'] for b in self.branches(m)]
        before = m.call('GET', '/api/bootstrap')[1]['needs']
        n1 = [n for n in before if n['branch_id'] == b1]
        self.assertEqual(m.call('POST', '/api/needs', {'weekday': 0, 'position_id': n1[0]['position_id'], 'start_time': '09:00', 'end_time': '10:00'})[0], 400)
        a = self.as_('admin')
        st, nb = a.call('POST', '/api/branches', {'name': 'Copy Target', 'color': '#123456'})
        self.assertEqual(m.call('POST', '/api/needs/copy', {'branch_id': b1, 'to_branch_id': nb['id']})[1]['copied'], len(n1))
        after = m.call('GET', '/api/bootstrap')[1]['needs']
        self.assertEqual(len([n for n in after if n['branch_id'] == nb['id']]), len(n1))
        self.assertEqual(len([n for n in after if n['branch_id'] == b2]), len([n for n in before if n['branch_id'] == b2]))
        self.assertEqual(a.call('DELETE', f"/api/branches/{nb['id']}")[0], 200)
        self.assertFalse([n for n in m.call('GET', '/api/bootstrap')[1]['needs'] if n['branch_id'] == nb['id']])  # needs go with it

    # ---- requests
    def test_cover_and_pickup_requests(self):
        staff, m = self.as_('marcus'), self.as_('manager')
        me = staff.call('GET', '/api/me')[1]['user']['employee_id']
        roster = staff.call('GET', f'/api/roster?week={d(7)}')[1]
        mine = [s for s in roster['shifts'] if s['employee_id'] == me]
        others = [s for s in roster['shifts'] if s['employee_id'] not in (me, None)]
        self.assertEqual(staff.call('POST', '/api/requests/shift', {'kind': 'drop', 'shift_id': others[0]['id']})[0], 403)
        target = mine[-1]
        st, req = staff.call('POST', '/api/requests/shift', {'kind': 'drop', 'shift_id': target['id'], 'note': 'exam'})
        self.assertEqual(st, 200, req)
        self.assertIn(target['id'], staff.call('GET', '/api/dashboard')[1]['requested'])
        self.assertEqual(staff.call('POST', '/api/requests/shift', {'kind': 'drop', 'shift_id': target['id']})[0], 409)
        self.assertEqual(m.call('POST', f"/api/requests/shift/{req['id']}/decide", {'decision': 'approve'})[1]['status'], 'approved')
        self.assertEqual(self.q('SELECT employee_id, changed_after_publish FROM shifts WHERE id = ?', (target['id'],)), (None, 1))
        self.assertEqual(m.call('POST', f"/api/requests/shift/{req['id']}/decide", {'decision': 'approve'})[0], 409)
        priya = self.as_('priya')
        r1 = staff.call('POST', '/api/requests/shift', {'kind': 'pickup', 'shift_id': target['id']})[1]
        r2 = priya.call('POST', '/api/requests/shift', {'kind': 'pickup', 'shift_id': target['id']})[1]
        self.assertEqual(m.call('POST', f"/api/requests/shift/{r2['id']}/decide", {'decision': 'approve'})[1]['status'], 'approved')
        self.assertEqual(self.q('SELECT status FROM shift_requests WHERE id = ?', (r1['id'],))[0], 'rejected')

    def test_leave_request_approve_releases_shifts(self):
        priya, m = self.as_('priya'), self.as_('manager')
        me = priya.call('GET', '/api/me')[1]['user']['employee_id']
        self.assertEqual(priya.call('POST', '/api/requests/leave', {'start_date': d(-3), 'end_date': d(-3), 'kind': 'Annual leave'})[0], 400)
        self.assertEqual(priya.call('POST', '/api/requests/leave', {'start_date': d(9), 'end_date': d(8), 'kind': 'Annual leave'})[0], 400)
        self.assertEqual(priya.call('POST', '/api/requests/leave', {'start_date': d(8), 'end_date': d(9), 'kind': 'Holiday'})[0], 400)
        mine = [s for s in priya.call('GET', f'/api/roster?week={d(7)}')[1]['shifts'] if s['employee_id'] == me and s['date'] >= TODAY.isoformat()]
        day = mine[0]['date']
        st, lv = priya.call('POST', '/api/requests/leave', {'start_date': day, 'end_date': day, 'kind': 'Medical leave', 'reason': 'MC'})
        self.assertEqual(st, 200, lv)
        pend = m.call('GET', '/api/requests?status=pending')[1]
        self.assertTrue(next(x for x in pend['leave'] if x['id'] == lv['id'])['conflicts'])
        self.assertEqual(self.as_('marcus').call('POST', f"/api/requests/leave/{lv['id']}/cancel", {})[0], 403)
        out = m.call('POST', f"/api/requests/leave/{lv['id']}/decide", {'decision': 'approve', 'release_shifts': True})[1]
        self.assertGreaterEqual(out['released'], 1)
        self.assertIsNone(self.q('SELECT employee_id FROM shifts WHERE id = ?', (mine[0]['id'],))[0])

    def test_staff_can_cancel_only_their_own_requests(self):
        marcus, m = self.as_('marcus'), self.as_('manager')
        me = marcus.call('GET', '/api/me')[1]['user']['employee_id']
        pend = m.call('GET', '/api/requests?status=pending')[1]
        mine = next(r for r in pend['shift'] if r['kind'] == 'cover' and r['from_employee_id'] == me)
        theirs = next(r for r in pend['shift'] if r['kind'] == 'pickup' and r['to_employee_id'] != me)
        self.assertEqual(marcus.call('POST', f"/api/requests/shift/{theirs['id']}/cancel", {})[0], 403)
        self.assertEqual(marcus.call('POST', f"/api/requests/shift/{mine['id']}/cancel", {})[1]['status'], 'cancelled')

    def test_availability_permissions(self):
        staff = self.as_('marcus')
        me = staff.call('GET', '/api/me')[1]['user']['employee_id']
        days = [{'weekday': i, 'available': 1, 'start_time': '', 'end_time': ''} for i in range(7)]
        days[6] = {'weekday': 6, 'available': 0}
        self.assertEqual(staff.call('PUT', f'/api/employees/{me}/availability', {'days': days})[0], 200)
        self.assertEqual(staff.call('PUT', f'/api/employees/{me + 1}/availability', {'days': days})[0], 403)
        days[0] = {'weekday': 0, 'available': 1, 'start_time': '10:00', 'end_time': '10:00'}
        self.assertEqual(staff.call('PUT', f'/api/employees/{me}/availability', {'days': days})[0], 400)
        self.assertEqual(staff.call('PUT', f'/api/employees/{me}/availability', {'days': days[:6]})[0], 400)

    # ---- team, settings, users
    def test_employee_crud_with_branches_and_codes(self):
        m = self.as_('manager')
        boot = m.call('GET', '/api/bootstrap')[1]
        pid = boot['positions'][0]['id']
        b1, b2 = [b['id'] for b in boot['branches']]
        taken = next(e['code'] for e in m.call('GET', '/api/employees')[1] if e['code'])
        base = {'name': '<img src=x onerror=alert(1)>', 'position_id': pid, 'employment_type': 'part_time', 'hourly_rate': 11, 'target_hours': 20, 'max_hours': 28,
                'skills': [], 'branch_id': b1, 'branches': [b1, b2], 'code': 'T-001'}
        self.assertEqual(m.call('POST', '/api/employees', dict(base, target_hours=30))[0], 400)
        self.assertEqual(m.call('POST', '/api/employees', dict(base, email='nope'))[0], 400)
        self.assertEqual(m.call('POST', '/api/employees', dict(base, code=taken.lower()))[0], 409)
        self.assertEqual(m.call('POST', '/api/employees', dict(base, branch_id=999))[0], 400)
        st, e = m.call('POST', '/api/employees', base)
        self.assertEqual(st, 200, e)
        self.assertEqual(e['name'], base['name'])  # stored verbatim; the page escapes on render
        self.assertEqual((e['skills'], e['branch_id'], e['branches']), ([pid], b1, sorted([b1, b2])))
        self.assertEqual(m.call('DELETE', f"/api/employees/{e['id']}")[0], 200)
        used = m.call('GET', '/api/employees')[1][0]['id']
        self.assertEqual(m.call('DELETE', f'/api/employees/{used}')[0], 409)
        self.assertEqual(m.call('DELETE', f'/api/positions/{pid}')[0], 409)

    def test_settings_validation(self):
        a = self.as_('admin')
        self.assertEqual(a.call('PUT', '/api/settings', {'max_daily_hours': 30})[0], 400)
        self.assertEqual(a.call('PUT', '/api/settings', {'business_name': ''})[0], 400)
        self.assertEqual(a.call('PUT', '/api/settings', {'holiday_pay_multiplier': 9})[0], 400)
        r = a.call('PUT', '/api/settings', {'min_rest_hours': 10.5, 'late_grace_minutes': 7, 'forecast_label': 'Revenue'})[1]
        self.assertEqual((r['settings']['min_rest_hours'], r['settings']['late_grace_minutes'], r['settings']['forecast_label']), (10.5, 7, 'Revenue'))
        a.call('PUT', '/api/settings', {'min_rest_hours': 11, 'late_grace_minutes': 5, 'forecast_label': 'Forecast sales'})

    def test_user_management_guards(self):
        a = self.as_('admin')
        me = a.call('GET', '/api/me')[1]['user']
        self.assertEqual(a.call('POST', '/api/users', {'username': 'x', 'display_name': 'X', 'role': 'manager', 'password': 'longenough'})[0], 400)
        self.assertEqual(a.call('POST', '/api/users', {'username': 'newmgr', 'display_name': 'X', 'role': 'manager', 'password': 'short'})[0], 400)
        self.assertEqual(a.call('POST', '/api/users', {'username': 'newstaff', 'display_name': 'X', 'role': 'staff', 'password': 'longenough'})[0], 400)
        st, u = a.call('POST', '/api/users', {'username': 'newmgr', 'display_name': 'New Manager', 'role': 'manager', 'password': 'longenough'})
        self.assertEqual(st, 200, u)
        self.assertEqual(a.call('POST', '/api/users', {'username': 'NEWMGR', 'display_name': 'Dup', 'role': 'manager', 'password': 'longenough'})[0], 409)
        nm = Client(self.base)
        nm.login('newmgr', 'longenough')
        self.assertEqual(a.call('POST', f"/api/users/{u['id']}/password", {'password': 'another-one'})[0], 200)
        self.assertEqual(nm.call('GET', '/api/bootstrap')[0], 401)
        self.assertEqual(a.call('DELETE', f"/api/users/{me['id']}")[0], 400)
        self.assertEqual(a.call('PUT', f"/api/users/{me['id']}", {'display_name': 'Grace', 'role': 'manager'})[0], 400)
        self.assertEqual(a.call('DELETE', f"/api/users/{u['id']}")[0], 200)
        self.assertEqual(self.as_('manager').call('POST', '/api/me/password', {'current': 'wrong', 'new': 'whatever1'})[0], 400)

    # ---- reports and dashboard
    def test_reports_filters_and_dashboards(self):
        m = self.as_('manager')
        b1, b2 = [b['id'] for b in self.branches(m)]
        self.assertEqual(m.call('GET', f'/api/reports?from={d(0)}&to={d(120)}')[0], 400)
        self.assertEqual(m.call('GET', f'/api/reports?from={d(6)}&to={d(0)}')[0], 400)
        self.assertEqual(m.call('GET', f'/api/reports?from={d(0)}&to={d(6)}&employees=a,b')[0], 400)
        rep = m.call('GET', f'/api/reports?from={d(0)}&to={d(6)}')[1]
        roster = m.call('GET', f'/api/roster?week={d(0)}')[1]
        published = sum(s['hours'] for s in roster['shifts'] if s['employee_id'] and s['status'] == 'published')
        self.assertAlmostEqual(rep['totals']['hours'], published, places=2)
        r1 = m.call('GET', f'/api/reports?from={d(0)}&to={d(6)}&branch={b1}')[1]
        r2 = m.call('GET', f'/api/reports?from={d(0)}&to={d(6)}&branch={b2}')[1]
        self.assertAlmostEqual(r1['totals']['hours'] + r2['totals']['hours'], rep['totals']['hours'], places=2)
        someone = rep['employees'][0]
        one = m.call('GET', f"/api/reports?from={d(0)}&to={d(6)}&employees={someone['employee_id']}")[1]
        self.assertEqual([e['employee_id'] for e in one['employees']], [someone['employee_id']])
        self.assertAlmostEqual(one['totals']['hours'], someone['hours'], places=2)
        self.assertIsNone(one['totals']['labour_pct'])  # a subset of people isn't compared with sales
        dash = m.call('GET', '/api/dashboard')[1]
        self.assertEqual(dash['week_start'], d(0))
        self.assertEqual(len(dash['branches']), 2)
        self.assertEqual(m.call('GET', f'/api/dashboard?branch={b1}')[1]['branches'], [])
        sd = self.as_('marcus').call('GET', '/api/dashboard')[1]
        self.assertTrue(sd['linked'])
        self.assertTrue(all(s['date'] >= TODAY.isoformat() for s in sd['shifts']))

    def test_actual_vs_rostered_report(self):
        m = self.as_('manager')
        a = m.call('GET', f'/api/reports/actual?from={d(-21)}&to={d(-1)}')[1]
        self.assertGreater(a['records'], 100)
        t = a['totals']
        self.assertGreater(t['sched_shifts'], 100)
        self.assertGreater(t['attendance_pct'], 85)
        self.assertGreater(t['late'], 0)
        self.assertEqual(t['sched_shifts'], t['attended'] + t['no_show'])
        b1 = self.branches(m)[0]['id']
        a1 = m.call('GET', f'/api/reports/actual?from={d(-21)}&to={d(-1)}&branch={b1}')[1]
        self.assertLess(a1['totals']['sched_shifts'], t['sched_shifts'])
        self.assertTrue(all(x['branch_id'] == b1 for x in a1['details']))


class DataTests(unittest.TestCase):
    """Backup, restore, import, samples and upgrades each get a fresh database."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.httpd, self.base = start_server(self.tmp, 'data.db')
        self.a = Client(self.base)
        self.a.login('admin', PW['admin'])

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def count(self, table, where=''):
        conn = sqlite3.connect(server.CONFIG['db'])
        try:
            return conn.execute(f'SELECT COUNT(*) FROM {table} {where}').fetchone()[0]
        finally:
            conn.close()

    def upload(self, name, data):
        return self.a.call('POST', '/api/data/restore', {'filename': name, 'content': base64.b64encode(data).decode()})

    def relogin(self):
        self.a = Client(self.base)
        self.a.login('admin', PW['admin'])

    def test_json_backup_round_trip(self):
        st, raw = self.a.call('GET', '/api/data/export?format=json', raw=True)
        self.assertEqual(st, 200)
        counts = {t: self.count(t) for t in ('shifts', 'branches', 'attendance', 'calendar_events')}
        self.a.call('POST', '/api/data/reset', {'confirm': 'RESET'})
        self.assertEqual(self.count('shifts'), 0)
        self.assertEqual(self.count('branches'), 1)  # a reset keeps one empty branch to work in
        st, r = self.upload('b.json', raw)
        self.assertEqual(st, 200, r)
        self.assertEqual({t: self.count(t) for t in counts}, counts)
        self.assertEqual(self.a.call('GET', '/api/bootstrap')[0], 401)
        self.relogin()
        self.assertTrue(os.path.exists(os.path.join(server.CONFIG['backups'], r['safety_backup'])))

    def test_db_backup_round_trip_and_snapshots(self):
        st, raw = self.a.call('GET', '/api/data/export?format=db', raw=True)
        self.assertTrue(raw.startswith(b'SQLite format 3\x00'))
        employees = self.count('employees')
        self.a.call('POST', '/api/data/reset', {'confirm': 'RESET'})
        st, r = self.upload('b.db', raw)
        self.assertEqual(st, 200, r)
        self.assertEqual(self.count('employees'), employees)
        self.relogin()
        snap = self.a.call('POST', '/api/data/backups', {})[1]['name']
        self.assertIn(snap, [b['name'] for b in self.a.call('GET', '/api/data/backups')[1]])
        self.assertTrue(self.a.call('GET', f'/api/data/backups/{snap}', raw=True)[1].startswith(b'SQLite format 3'))
        self.a.call('POST', '/api/data/reset', {'confirm': 'RESET'})
        self.assertEqual(self.a.call('POST', f'/api/data/backups/{snap}/restore', {})[0], 200)
        self.relogin()
        self.assertEqual(self.count('employees'), employees)
        self.assertEqual(self.a.call('GET', '/api/data/backups/..%2Froster.db')[0], 400)
        self.assertEqual(self.a.call('DELETE', f'/api/data/backups/{snap}')[0], 200)

    def test_bad_backups_are_rejected_and_change_nothing(self):
        shifts = self.count('shifts')
        self.assertEqual(self.upload('x.db', b'SQLite format 3\x00' + b'\x00' * 200)[0], 400)
        self.assertEqual(self.upload('x.json', b'{"app": "Other"}')[0], 400)
        self.assertEqual(self.upload('x.txt', b'hello')[0], 400)
        doc = json.loads(self.a.call('GET', '/api/data/export?format=json', raw=True)[1])
        for u in doc['tables']['users']:
            u['role'] = 'staff'
        self.assertIn('no active admin', self.upload('noadmin.json', json.dumps(doc).encode())[1]['error'])
        doc = json.loads(self.a.call('GET', '/api/data/export?format=json', raw=True)[1])
        doc['tables']['shifts'][0]['employee_id'] = 99999
        self.assertEqual(self.upload('broken.json', json.dumps(doc).encode())[0], 400)
        other = os.path.join(self.tmp, 'other.db')
        conn = sqlite3.connect(other)
        conn.execute('CREATE TABLE t (x)')
        conn.commit()
        conn.close()
        with open(other, 'rb') as f:
            self.assertIn('not a ShiftTable backup', self.upload('other.db', f.read())[1]['error'])
        self.assertEqual(self.count('shifts'), shifts)
        self.assertEqual(self.a.call('GET', '/api/me')[1]['user']['username'], 'admin')

    def test_csv_imports(self):
        csv_text = ('Name,Code,Position,Skills,Type,Hourly rate,Target hours,Max hours,Branch,Branches,Phone,Email\n'
                    'Zoe New,Z-9,Host,Server; Cashier,part-time,11,20,28,Orchard,Tampines Mall,,zoe@example.com\n'
                    'Rachel Tan,,Supervisor,,FT,18,40,44,,,,\n'
                    ',,Server,,,,,,,,,\n'
                    'Bad Type,,Server,,weekly,,,,,,,\n')
        r = self.a.call('POST', '/api/data/import', {'type': 'employees', 'csv': csv_text})[1]
        self.assertEqual(r['summary'], {'new': 1, 'update': 1, 'error': 2})
        self.assertEqual((r['new_positions'], r['new_branches']), (['Host'], ['Orchard']))
        before = self.count('employees')
        r = self.a.call('POST', '/api/data/import', {'type': 'employees', 'csv': csv_text, 'commit': True})[1]
        self.assertEqual(self.count('employees'), before + 1)
        self.assertTrue(r['safety_backup'])
        zoe = next(e for e in self.a.call('GET', '/api/employees')[1] if e['name'] == 'Zoe New')
        self.assertEqual((zoe['code'], len(zoe['branches'])), ('Z-9', 2))
        future = MON + timedelta(days=21)
        shifts_csv = ('date,start,end,break,employee,position,branch,notes\n'
                      f'{future.strftime("%d/%m/%Y")},9am,5:30pm,45,Zoe New,,,\n'
                      f'{future.isoformat()},17:30,22:30,0,,Server,Tampines Mall,Open\n'
                      f'{future.isoformat()},17:30,22:30,0,Nobody,Server,,\n'
                      f'{future.isoformat()},10:00,12:00,0,Zoe New,,Mars,\n'
                      f'31/02/2026,09:00,17:00,0,Zoe New,,,\n')
        r = self.a.call('POST', '/api/data/import', {'type': 'shifts', 'csv': shifts_csv, 'commit': True})[1]
        self.assertEqual(r['summary'], {'new': 2, 'duplicate': 0, 'error': 3})
        self.assertEqual(self.a.call('POST', '/api/data/import', {'type': 'shifts', 'csv': shifts_csv})[1]['summary']['duplicate'], 2)
        self.assertEqual(self.a.call('POST', '/api/data/import', {'type': 'shifts', 'csv': 'foo,bar\n1,2\n'})[0], 400)

    def test_clock_import_matching_and_delete(self):
        emps = self.a.call('GET', '/api/employees')[1]
        e0, e1 = emps[0], emps[1]
        day = (MON - timedelta(days=40))
        csv_text = ('Staff ID,Employee Name,Work Date,Time In,Time Out,Break Mins,Outlet\n'
                    f'{e0["code"]},,{day.strftime("%d/%m/%Y")},7:58 am,4:31 pm,60,\n'
                    f',{e1["name"]},{day.isoformat()},22:00,06:10,45,Tampines Mall\n'
                    f',{e1["name"]},,{day.isoformat()} 10:00,{day.isoformat()} 09:00,0,\n'
                    f'NOPE,,{day.isoformat()},09:00,17:00,0,\n'
                    f'{e0["code"]},,{day.isoformat()},09:00,17:00,0,Mars\n'
                    f'{e0["code"]},,,09:00,17:00,0,\n'
                    f',{e1["name"]},{(day + timedelta(days=1)).isoformat()},08:00,,0,\n')
        r = self.a.call('POST', '/api/data/import', {'type': 'clock', 'csv': csv_text})[1]
        self.assertEqual(r['summary'], {'new': 3, 'duplicate': 0, 'error': 4}, r['rows'])
        before = self.count('attendance')
        r = self.a.call('POST', '/api/data/import', {'type': 'clock', 'csv': csv_text, 'commit': True, 'filename': 'clock.csv'})[1]
        self.assertEqual(self.count('attendance'), before + 3)
        self.assertEqual((r['from'], r['to']), (day.isoformat(), (day + timedelta(days=1)).isoformat()))
        conn = sqlite3.connect(server.CONFIG['db'])
        overnight = conn.execute('SELECT clock_in, clock_out, source FROM attendance WHERE employee_id = ? AND date = ? ORDER BY id DESC', (e1['id'], day.isoformat())).fetchone()
        conn.close()
        self.assertEqual(overnight, (f'{day.isoformat()} 22:00', f'{(day + timedelta(days=1)).isoformat()} 06:10', 'clock.csv'))
        self.assertEqual(self.a.call('POST', '/api/data/import', {'type': 'clock', 'csv': csv_text})[1]['summary']['duplicate'], 3)
        mgr = Client(self.base)
        mgr.login('manager', PW['manager'])
        self.assertEqual(mgr.call('POST', '/api/data/import', {'type': 'clock', 'csv': 'foo\n1\n'})[0], 400)
        out = mgr.call('POST', '/api/attendance/delete', {'from': day.isoformat(), 'to': (day + timedelta(days=1)).isoformat()})[1]
        self.assertEqual(out['deleted'], 3)
        self.assertEqual(mgr.call('POST', '/api/attendance/delete', {'from': d(0), 'to': d(-1)})[0], 400)

    def test_three_sample_businesses(self):
        for industry, meta in sample_data.INDUSTRIES.items():
            self.assertEqual(self.a.call('POST', '/api/data/sample', {'industry': industry})[0], 200)
            self.assertEqual(self.count('employees'), len(meta['employees']))
            self.assertEqual(self.count('branches'), len(meta['branches']))
            self.assertGreater(self.count('attendance'), 100)
            self.assertGreater(self.count('calendar_events', "WHERE kind = 'holiday'"), 20)
            self.assertGreater(self.count('calendar_events', "WHERE kind = 'closed'"), 0)
            boot = self.a.call('GET', '/api/bootstrap')[1]
            self.assertEqual(boot['settings']['business_name'], meta['business_name'])
            self.assertTrue(5 <= len(boot['employees']) <= 30)
            staff = Client(self.base)
            self.assertIsNotNone(staff.login('marcus', PW['marcus'])['employee_id'])
            self.assertTrue(staff.call('GET', '/api/dashboard')[1]['linked'])
            for k in range(-3, 3):
                ev = self.a.call('GET', f'/api/roster?week={d(7 * k)}')[1]
                self.assertGreaterEqual(ev['totals']['coverage_pct'], 85, (industry, k))
                self.assertLessEqual(ev['totals']['errors'], 1, (industry, k, [i['message'] for i in ev['issues'] if i['severity'] == 'error']))
        self.assertEqual(self.a.call('POST', '/api/data/sample', {'industry': 'mining'})[0], 400)
        self.assertEqual(self.a.call('POST', '/api/data/reset', {'confirm': 'nope'})[0], 400)


V1_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE positions (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, area TEXT NOT NULL CHECK (area IN ('FOH','BOH')), color TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT NOT NULL, position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL,
  employment_type TEXT NOT NULL CHECK (employment_type IN ('full_time','part_time','casual')), hourly_rate REAL NOT NULL DEFAULT 0, target_hours REAL NOT NULL DEFAULT 0,
  max_hours REAL NOT NULL DEFAULT 44, phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE employee_skills (employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE, position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE, PRIMARY KEY (employee_id, position_id));
CREATE TABLE availability (employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE, weekday INTEGER NOT NULL, available INTEGER NOT NULL DEFAULT 1, start_time TEXT, end_time TEXT, PRIMARY KEY (employee_id, weekday));
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, display_name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL,
  employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_login_at TEXT);
CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE shift_templates (id INTEGER PRIMARY KEY, name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL, break_minutes INTEGER NOT NULL DEFAULT 0, area TEXT NOT NULL DEFAULT '', sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE staffing_needs (id INTEGER PRIMARY KEY, weekday INTEGER NOT NULL, position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  break_minutes INTEGER NOT NULL DEFAULT 0, headcount INTEGER NOT NULL DEFAULT 1, label TEXT NOT NULL DEFAULT '');
CREATE TABLE shifts (id INTEGER PRIMARY KEY, date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL, break_minutes INTEGER NOT NULL DEFAULT 0,
  employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL, position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL, notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft', published_at TEXT, changed_after_publish INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE forecasts (date TEXT PRIMARY KEY, sales REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '');
CREATE TABLE leave_requests (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  kind TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', created_by INTEGER, decided_by INTEGER, decided_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE shift_requests (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE, from_employee_id INTEGER, to_employee_id INTEGER,
  note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', created_by INTEGER, decided_by INTEGER, decided_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER, username TEXT, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
PRAGMA user_version = 1;
"""


def make_v1_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO settings VALUES ('business_name', 'Old Bistro')")
    conn.execute("INSERT INTO positions VALUES (1, 'Server', 'FOH', '#2f7fd1', 0), (2, 'Cook', 'BOH', '#d4552f', 1)")
    conn.execute("INSERT INTO employees (id, name, position_id, employment_type, hourly_rate, target_hours, max_hours, created_at) VALUES (1, 'Ann', 1, 'full_time', 12, 40, 44, '2026-01-01'), (2, 'Bo', 2, 'part_time', 11, 20, 30, '2026-01-01')")
    conn.execute('INSERT INTO employee_skills VALUES (1, 1), (2, 2)')
    conn.execute("INSERT INTO users (username, display_name, password_hash, role, employee_id, created_at) VALUES ('admin', 'Old Owner', ?, 'admin', NULL, '2026-01-01')",
                 (server.hash_password(PW['admin']),))
    conn.execute("INSERT INTO shift_templates (name, start_time, end_time, break_minutes, area) VALUES ('Open', '08:00', '16:00', 60, 'FOH')")
    conn.execute("INSERT INTO staffing_needs (weekday, position_id, start_time, end_time, headcount) VALUES (0, 1, '08:00', '16:00', 1)")
    for i in range(5):
        conn.execute("INSERT INTO shifts (date, start_time, end_time, break_minutes, employee_id, position_id, status, created_at, updated_at) VALUES (?, '08:00', '16:00', 60, 1, 1, 'published', 'x', 'x')", (d(i),))
    conn.execute('INSERT INTO forecasts VALUES (?, 2500, ?)', (d(0), ''))
    conn.commit()
    conn.close()


class UpgradeTests(unittest.TestCase):
    def test_version_1_database_upgrades_in_place(self):
        tmp = tempfile.mkdtemp()
        try:
            make_v1_db(os.path.join(tmp, 'old.db'))
            httpd, base = start_server(tmp, 'old.db')
            try:
                c = Client(base)
                c.login('admin', PW['admin'])
                boot = c.call('GET', '/api/bootstrap')[1]
                self.assertEqual([b['name'] for b in boot['branches']], ['Main branch'])
                self.assertEqual(boot['settings']['business_name'], 'Old Bistro')
                self.assertEqual({p['name']: p['department'] for p in boot['positions']}, {'Server': 'Front of house', 'Cook': 'Kitchen'})
                self.assertEqual(boot['templates'][0]['department'], 'Front of house')
                self.assertTrue(all(e['branch_id'] == boot['branches'][0]['id'] for e in boot['employees']))
                r = c.call('GET', f'/api/roster?week={d(0)}')[1]
                self.assertEqual(r['totals']['shifts'], 5)
                self.assertEqual(r['day_stats'][0]['sales'], 2500)
                self.assertEqual(r['totals']['need_total'], 1)
                snaps = os.listdir(os.path.join(tmp, 'backups'))
                self.assertTrue(any('pre-upgrade-v1' in s for s in snaps))
            finally:
                httpd.shutdown()
                httpd.server_close()
            conn = sqlite3.connect(os.path.join(tmp, 'old.db'))
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], server.SCHEMA_VERSION)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_version_1_backup_restores_into_version_2(self):
        tmp = tempfile.mkdtemp()
        try:
            make_v1_db(os.path.join(tmp, 'v1.db'))
            conn = sqlite3.connect(os.path.join(tmp, 'v1.db'))
            conn.row_factory = sqlite3.Row
            doc = {'app': 'ShiftTable', 'schema_version': 1, 'tables': {t: [dict(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in sorted(server.V1_TABLES)}}
            conn.close()
            httpd, base = start_server(tmp, 'new.db')
            try:
                c = Client(base)
                c.login('admin', PW['admin'])
                with open(os.path.join(tmp, 'v1.db'), 'rb') as f:
                    v1_bytes = f.read()
                for name, data in (('v1.json', json.dumps(doc).encode()), ('v1.db', v1_bytes)):
                    st, r = c.call('POST', '/api/data/restore', {'filename': name, 'content': base64.b64encode(data).decode()})
                    self.assertEqual(st, 200, (name, r))
                    c = Client(base)
                    c.login('admin', PW['admin'])
                    boot = c.call('GET', '/api/bootstrap')[1]
                    self.assertEqual((len(boot['employees']), len(boot['branches'])), (2, 1), name)
                    self.assertEqual({p['department'] for p in boot['positions']}, {'Front of house', 'Kitchen'}, name)
                    self.assertEqual(c.call('GET', f'/api/roster?week={d(0)}')[1]['totals']['shifts'], 5, name)
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SessionAndHostingTests(unittest.TestCase):
    """Signed-cookie sessions, the hosted-demo setup and the business clock."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = dict(server.CONFIG)

    def tearDown(self):
        server.CONFIG.clear()
        server.CONFIG.update(self.saved)
        for k in ('SHIFTTABLE_SECRET', 'SHIFTTABLE_DATA_DIR', 'SHIFTTABLE_UTC_OFFSET'):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_session_tokens_resist_tampering_and_expire(self):
        httpd, base = start_server(self.tmp, 's.db')
        try:
            c = Client(base)
            c.login('manager', PW['manager'])
            token = next(ck.value for ck in c.jar if ck.name == server.COOKIE)
            uid, exp, sig = token.split('.')
            def with_cookie(value):
                req = urllib.request.Request(base + '/api/me', headers={'Cookie': f'{server.COOKIE}={value}'})
                with urllib.request.urlopen(req) as r:
                    return json.loads(r.read())['user']
            self.assertEqual(with_cookie(token)['username'], 'manager')
            self.assertIsNone(with_cookie(f'1.{exp}.{sig}'))  # someone else's id
            self.assertIsNone(with_cookie(f'{uid}.{int(exp) + 999}.{sig}'))  # longer expiry
            self.assertIsNone(with_cookie(f'{uid}.{exp}.{"0" * 64}'))
            self.assertIsNone(with_cookie('garbage'))
            conn = server.db()
            u = server.one(conn, 'SELECT * FROM users WHERE username = ?', ('manager',))
            old = int(time.time()) - 10
            self.assertIsNone(server.read_session_token(conn, f"{u['id']}.{old}.{server.sign_session(conn, u, old)}"))
            conn.close()
            # changing your own password keeps you signed in here but ends other sessions
            other = Client(base)
            other.login('manager', PW['manager'])
            self.assertEqual(c.call('POST', '/api/me/password', {'current': PW['manager'], 'new': 'Brand-new-1'})[0], 200)
            self.assertEqual(c.call('GET', '/api/bootstrap')[0], 200)
            self.assertEqual(other.call('GET', '/api/bootstrap')[0], 401)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_hosted_demo_copies_share_logins(self):
        os.environ['SHIFTTABLE_SECRET'] = 'test-secret-for-two-copies'
        dbs = []
        for name in ('copy-a', 'copy-b'):
            os.environ['SHIFTTABLE_DATA_DIR'] = os.path.join(self.tmp, name)
            server.serverless_setup()
            self.assertTrue(server.CONFIG['demo'])
            dbs.append(server.CONFIG['db'])
        server.CONFIG['db'] = dbs[0]
        conn = server.db()
        token = server.make_session_token(conn, server.one(conn, "SELECT * FROM users WHERE username = 'admin'"))
        conn.close()
        server.CONFIG['db'] = dbs[1]
        conn = server.db()
        self.assertEqual(server.read_session_token(conn, token)['username'], 'admin')  # valid on the other copy
        conn.close()
        # a restore or password change on one copy must not sign people out on the others
        server.CONFIG['db'] = dbs[0]
        conn = server.db()
        server.end_all_sessions(conn)
        conn.execute("UPDATE users SET password_hash = ? WHERE username = 'admin'", (server.hash_password('Changed-on-A-1'),))
        conn.commit()
        token_a = server.make_session_token(conn, server.one(conn, "SELECT * FROM users WHERE username = 'admin'"))
        self.assertEqual(server.read_session_token(conn, token)['username'], 'admin')
        conn.close()
        server.CONFIG['db'] = dbs[1]
        conn = server.db()
        self.assertEqual(server.read_session_token(conn, token_a)['username'], 'admin')
        manager = server.one(conn, "SELECT * FROM users WHERE username = 'manager'")
        forged = f"{manager['id']}.{token_a.split('.', 1)[1]}"
        self.assertIsNone(server.read_session_token(conn, forged))  # still bound to the user
        conn.close()

    def test_vercel_entry_point(self):
        import importlib.util
        from http.server import BaseHTTPRequestHandler
        os.environ['SHIFTTABLE_DATA_DIR'] = os.path.join(self.tmp, 'vercel')
        path = os.path.join(os.path.dirname(__file__), '..', 'api', 'index.py')
        spec = importlib.util.spec_from_file_location('vercel_entry', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(issubclass(mod.handler, BaseHTTPRequestHandler))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'vercel', 'roster.db')))
        with open(os.path.join(os.path.dirname(__file__), '..', 'vercel.json')) as f:
            cfg = json.load(f)
        self.assertEqual(cfg['rewrites'][0]['destination'], '/api/index')

    def test_business_clock_offset(self):
        os.environ['SHIFTTABLE_UTC_OFFSET'] = '8'
        from datetime import datetime as dt, timezone
        expected = (dt.now(timezone.utc) + timedelta(hours=8)).replace(tzinfo=None)
        self.assertLess(abs((server.local_now() - expected).total_seconds()), 5)
        self.assertEqual(server.today(), expected.date())


class ScaleTest(unittest.TestCase):
    def test_thirty_people_two_branches_week_is_fast_and_clean(self):
        emps = [emp(i, target_hours=40 if i <= 20 else 20, max_hours=44 if i <= 20 else 28, employment_type='full_time' if i <= 20 else 'part_time',
                    skills={1, 2} if i % 3 else {1}, branch_id=1 if i % 2 else 2, branches={1, 2} if i % 5 == 0 else ({1} if i % 2 else {2}))
                for i in range(1, 31)]
        needs, n = [], 0
        for b in (1, 2):
            for wd in range(7):
                for pos_id, st, en, hc in ((1, '07:30', '16:30', 2), (1, '14:00', '23:00', 2), (2, '07:00', '16:00', 1), (2, '13:30', '22:30', 2)):
                    n += 1
                    needs.append(need(n, wd, st, en, hc, branch=b, pos=pos_id))
        c = ctx([], emps, needs=needs, branches=BRANCHES)
        t0 = time.time()
        new, _ = rules.autofill(c, d(0))
        c['shifts'] = new
        ev = rules.evaluate(c, d(0))
        self.assertLess(time.time() - t0, 5.0)
        self.assertEqual(ev['totals']['errors'], 0)
        self.assertFalse({'branch', 'rest', 'overlap'} & set(codes(ev)))
        self.assertGreaterEqual(ev['totals']['coverage_pct'], 90)


# ================================================================ hosted demo: one shared database

BLOB_TOKEN = 'vercel_blob_rw_teststore_s3cret'
RealBlobStore = demo_sync.BlobStore


class FakeBlob:
    """Stands in for Vercel Blob: files by pathname with ETags, conditional writes and deletes."""

    def __init__(self):
        self.files = {}
        self.puts = []  # pathnames uploaded, in order
        self.before_put = None  # run once as the shared database's next upload arrives
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=b'', headers=()):
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authorised(self):
                if self.headers.get('Authorization') == f'Bearer {BLOB_TOKEN}':
                    return True
                self.reply(403, b'{"error": {"code": "forbidden"}}')
                return False

            def do_GET(self):
                if not self.authorised():
                    return
                path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path[1:])
                if path not in fake.files:
                    return self.reply(404)
                tag = fake.etag(path)
                if self.headers.get('If-None-Match') == tag:
                    return self.reply(304, headers=[('ETag', tag)])
                self.reply(200, fake.files[path], [('ETag', tag)])

            def do_PUT(self):
                if not self.authorised():
                    return
                path = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)['pathname'][0]
                data = self.rfile.read(int(self.headers['Content-Length']))
                if fake.before_put and path == demo_sync.DB_BLOB:
                    hook, fake.before_put = fake.before_put, None
                    hook()
                want = self.headers.get('x-if-match')
                if want and (path not in fake.files or fake.etag(path) != want):
                    return self.reply(412, b'{"error": {"code": "precondition_failed"}}')
                fake.files[path] = data
                fake.puts.append(path)
                self.reply(200, json.dumps({'pathname': path, 'etag': fake.etag(path)}).encode())

            def do_POST(self):
                if not self.authorised():
                    return
                for url in json.loads(self.rfile.read(int(self.headers['Content-Length'])))['urls']:
                    fake.files.pop(urllib.parse.unquote(urllib.parse.urlparse(url).path[1:]), None)
                self.reply(200, b'{}')

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def etag(self, path):
        return '"%s"' % hashlib.sha1(self.files[path]).hexdigest()

    def store(self):
        return RealBlobStore(BLOB_TOKEN, self.url, self.url)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def read_setting(path, key):
    return demo_sync._read_setting(path, key)


def write_setting(path, key, value):
    conn = sqlite3.connect(path)
    conn.execute('UPDATE settings SET value = ? WHERE key = ?', (value, key))
    conn.commit()
    conn.close()


class SharedDemoTests(unittest.TestCase):
    """Every copy of the hosted demo works on one database kept in Vercel Blob (demo_sync)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = dict(server.CONFIG)
        self.blob = FakeBlob()
        os.environ.update(SHIFTTABLE_SECRET='test-secret', SHIFTTABLE_DATA_DIR=os.path.join(self.tmp, 'a'),
                          BLOB_READ_WRITE_TOKEN=BLOB_TOKEN)
        with mock.patch.object(demo_sync, 'BlobStore', lambda token: self.blob.store()):
            server.serverless_setup()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.blob.stop()
        server.SYNC = None
        server.CONFIG.clear()
        server.CONFIG.update(self.saved)
        for k in ('SHIFTTABLE_SECRET', 'SHIFTTABLE_DATA_DIR', 'BLOB_READ_WRITE_TOKEN'):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def admin(self):
        c = Client(self.base)
        c.login('admin', PW['admin'])
        return c

    def other_copy(self, name='b'):
        """Another copy of the app on the same store, with its own files."""
        folder = os.path.join(self.tmp, name)
        os.makedirs(folder, exist_ok=True)
        copy = demo_sync.DemoSync(self.blob.store(), os.path.join(folder, 'roster.db'), os.path.join(folder, 'backups'), seed=lambda: None)
        copy.pull()
        return copy

    def test_copies_share_one_demo(self):
        self.assertIn(demo_sync.DB_BLOB, self.blob.files)  # the first copy shared the demo it seeded
        c = self.admin()
        self.assertEqual(c.call('POST', '/api/data/sample', {'industry': 'wellness'})[0], 200)
        self.assertEqual(c.headers['X-Demo-Sync'], 'saved')
        self.assertIn(f'{demo_sync.COOKIE}={server.SYNC.version}', c.headers['Set-Cookie'])
        b = self.other_copy()
        self.assertEqual(read_setting(b.db_path, 'business_name'), 'Vitality Wellness')
        # another copy saves a change: a visitor who has seen it gets it here straight away
        write_setting(b.db_path, 'business_name', 'Changed elsewhere')
        self.assertEqual(b.after(), 'saved')
        session = next(f'{k.name}={k.value}' for k in c.jar if k.name == server.COOKIE)
        req = urllib.request.Request(self.base + '/api/public', headers={'Cookie': f'{session}; {demo_sync.COOKIE}={b.version}'})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.headers['X-Demo-Sync'], 'pulled')
            self.assertEqual(json.loads(r.read())['business_name'], 'Changed elsewhere')
        # reading changes nothing, so it uploads nothing
        uploaded = self.blob.files[demo_sync.DB_BLOB]
        self.assertEqual(c.call('GET', '/api/roster')[0], 200)
        self.assertIs(self.blob.files[demo_sync.DB_BLOB], uploaded)

    def test_a_new_copy_joins_without_uploading(self):
        shared = self.blob.files[demo_sync.DB_BLOB]
        os.environ['SHIFTTABLE_DATA_DIR'] = os.path.join(self.tmp, 'late')
        with mock.patch.object(demo_sync, 'BlobStore', lambda token: self.blob.store()):
            server.serverless_setup()
        self.assertIs(self.blob.files[demo_sync.DB_BLOB], shared)
        self.assertEqual(server.SYNC.etag, self.blob.etag(demo_sync.DB_BLOB))

    def test_a_save_that_loses_the_race_runs_again_on_the_newer_data(self):
        c = self.admin()
        b = self.other_copy()
        write_setting(b.db_path, 'business_name', 'Saved first')
        self.blob.before_put = b.after  # the other copy saves while this one is uploading
        st, r = c.call('POST', '/api/branches', {'name': 'Jewel Changi', 'code': 'JC', 'color': '#123456'})
        self.assertEqual(st, 200, r)
        final = self.other_copy('c')
        self.assertEqual(read_setting(final.db_path, 'business_name'), 'Saved first')
        conn = sqlite3.connect(final.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM branches WHERE name = 'Jewel Changi'").fetchone()[0], 1)
        conn.close()

    def test_snapshots_are_shared(self):
        c = self.admin()
        name = c.call('POST', '/api/data/backups', {})[1]['name']
        b = self.other_copy()
        self.assertEqual(os.listdir(b.backups), [name])
        path = json.loads(read_setting(b.db_path, '_demo_backups'))[name]
        self.assertIn(path, self.blob.files)
        # a second snapshot uploads only itself, and the other copy downloads only it
        self.blob.puts.clear()
        second = c.call('POST', '/api/data/backups', {})[1]['name']
        self.assertEqual([p for p in self.blob.puts if p != demo_sync.DB_BLOB], [json.loads(read_setting(server.CONFIG['db'], '_demo_backups'))[second]])
        b.pull()
        self.assertEqual(sorted(os.listdir(b.backups)), sorted([name, second]))
        self.assertEqual(c.call('DELETE', f'/api/data/backups/{name}')[0], 200)
        for _ in range(50):  # deleted in the background
            if path not in self.blob.files:
                break
            time.sleep(0.05)
        self.assertNotIn(path, self.blob.files)
        b.pull()
        self.assertEqual(os.listdir(b.backups), [second])

    def test_an_idle_demo_is_seeded_afresh(self):
        c = self.admin()
        self.assertEqual(c.call('POST', '/api/data/sample', {'industry': 'manufacturing'})[0], 200)
        b = self.other_copy()
        write_setting(b.db_path, '_demo_changed_at', repr(time.time() - demo_sync.IDLE_RESET - 60))
        with open(b.db_path, 'rb') as f:
            self.blob.files[demo_sync.DB_BLOB] = zlib.compress(f.read())
        server.SYNC.checked = 0  # as if this copy last checked long ago
        self.assertEqual(c.call('GET', '/api/public')[1]['business_name'], 'Harbour Lane Group')
        self.assertEqual(c.headers['X-Demo-Sync'], 'reset')
        self.assertEqual(c.call('GET', '/api/bootstrap')[0], 200)  # still signed in
        self.assertEqual(read_setting(self.other_copy('c').db_path, 'business_name'), 'Harbour Lane Group')

    def test_demo_logins_stay_usable(self):
        admin, mgr = self.admin(), Client(self.base)
        mgr.login('manager', PW['manager'])
        self.assertEqual(mgr.call('POST', '/api/me/password', {'current': PW['manager'], 'new': 'Brand-new-1'})[0], 400)
        mid = next(u['id'] for u in admin.call('GET', '/api/users')[1] if u['username'] == 'manager')
        self.assertEqual(admin.call('POST', f'/api/users/{mid}/password', {'password': 'Brand-new-1'})[0], 400)
        self.assertEqual(admin.call('PUT', f'/api/users/{mid}', {'display_name': 'X', 'role': 'staff', 'active': 0})[0], 400)
        self.assertEqual(admin.call('DELETE', f'/api/users/{mid}')[0], 400)
        admin.call('PUT', '/api/settings', {'show_demo_logins': False})
        self.assertTrue(Client(self.base).call('GET', '/api/public')[1]['demo_logins'])
        # visitors can still try logins of their own
        st, u = admin.call('POST', '/api/users', {'username': 'visitor', 'display_name': 'Visitor', 'role': 'manager', 'password': 'Visitor#2026'})
        self.assertEqual(st, 200, u)
        self.assertEqual(admin.call('POST', f"/api/users/{u['id']}/password", {'password': 'Visitor#2027'})[0], 200)
        # and a doctored backup can't lock anyone out
        doc = admin.call('GET', '/api/data/export?format=json')[1]
        for u in doc['tables']['users']:
            if u['username'] == 'manager':
                u['password_hash'] = server.hash_password('Only-I-know-this-1')
        for s in doc['tables']['settings']:
            if s['key'] == 'show_demo_logins':
                s['value'] = '0'
        st, r = admin.call('POST', '/api/data/restore', {'filename': 'x.json', 'content': base64.b64encode(json.dumps(doc).encode()).decode()})
        self.assertEqual(st, 200, r)
        Client(self.base).login('manager', PW['manager'])
        self.assertTrue(Client(self.base).call('GET', '/api/public')[1]['demo_logins'])

    def test_the_demo_keeps_working_when_the_store_is_unreachable(self):
        c = self.admin()
        self.blob.stop()
        server.SYNC.checked = 0
        st, r = c.call('POST', '/api/branches', {'name': 'Offline', 'code': 'OFF', 'color': '#123456'})
        self.assertEqual((st, c.headers['X-Demo-Sync']), (200, 'offline'), r)
        self.assertEqual(c.call('GET', '/api/bootstrap')[0], 200)


if __name__ == '__main__':
    unittest.main()
