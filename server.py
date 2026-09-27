#!/usr/bin/env python3
"""ShiftTable: multi-branch shift roster server for teams of 5 to 30 staff.

Python standard library only. Start it with:

    python3 server.py                 # http://127.0.0.1:8080
    python3 server.py --host 0.0.0.0  # also reachable from phones on the same Wi-Fi

The SQLite database (roster.db) and the backups/ folder live next to this file. A
database from an older version is upgraded in place on start, after a safety snapshot.

On Vercel, api/index.py calls serverless_setup(): the database lives in /tmp and is
re-created with the demo data on each cold start (a live demo, not permanent storage).
"""
import argparse
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import demo_sync
import rules
import sample_data

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = {
    'db': os.path.join(APP_DIR, 'roster.db'),
    'backups': os.path.join(APP_DIR, 'backups'),
    'index': os.path.join(APP_DIR, 'index.html'),
    'demo': False,
}
SCHEMA_VERSION = 2
SESSION_HOURS = 12
MAX_BODY = 25 * 1024 * 1024
COOKIE = 'st_session'
CSRF_HEADER = 'X-ShiftTable'
PBKDF2_ITER = int(os.environ.get('SHIFTTABLE_PBKDF2_ITER', '200000'))
RANK = {'staff': 1, 'manager': 2, 'admin': 3}
LEAVE_KINDS = ['Annual leave', 'Medical leave', 'Childcare leave', 'Unpaid leave', 'Other']
EVENT_KINDS = ('holiday', 'event', 'closed')

TABLES_SQL = [
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS branches (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, code TEXT NOT NULL DEFAULT '',
      address TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '#0e6b5c', sort INTEGER NOT NULL DEFAULT 0,
      active INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS positions (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, department TEXT NOT NULL DEFAULT '',
      color TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS employees (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT NOT NULL DEFAULT '',
      position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL,
      branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
      employment_type TEXT NOT NULL CHECK (employment_type IN ('full_time','part_time','casual')),
      hourly_rate REAL NOT NULL DEFAULT 0, target_hours REAL NOT NULL DEFAULT 0, max_hours REAL NOT NULL DEFAULT 44,
      phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS employee_skills (
      employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
      position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
      PRIMARY KEY (employee_id, position_id))""",
    """CREATE TABLE IF NOT EXISTS employee_branches (
      employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
      branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
      PRIMARY KEY (employee_id, branch_id))""",
    """CREATE TABLE IF NOT EXISTS availability (
      employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
      weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6), available INTEGER NOT NULL DEFAULT 1,
      start_time TEXT, end_time TEXT, PRIMARY KEY (employee_id, weekday))""",
    """CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, display_name TEXT NOT NULL,
      password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin','manager','staff')),
      employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
      active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_login_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS shift_templates (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
      break_minutes INTEGER NOT NULL DEFAULT 0, department TEXT NOT NULL DEFAULT '', sort INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS staffing_needs (
      id INTEGER PRIMARY KEY, branch_id INTEGER REFERENCES branches(id) ON DELETE CASCADE,
      weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
      position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
      start_time TEXT NOT NULL, end_time TEXT NOT NULL, break_minutes INTEGER NOT NULL DEFAULT 0,
      headcount INTEGER NOT NULL DEFAULT 1, label TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS shifts (
      id INTEGER PRIMARY KEY, date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
      break_minutes INTEGER NOT NULL DEFAULT 0,
      employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
      position_id INTEGER REFERENCES positions(id) ON DELETE SET NULL,
      branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
      notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published')),
      published_at TEXT, changed_after_publish INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS forecasts (
      branch_id INTEGER NOT NULL DEFAULT 0, date TEXT NOT NULL, sales REAL NOT NULL DEFAULT 0,
      note TEXT NOT NULL DEFAULT '', PRIMARY KEY (branch_id, date))""",
    """CREATE TABLE IF NOT EXISTS calendar_events (
      id INTEGER PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('holiday','event','closed')), name TEXT NOT NULL,
      branch_id INTEGER REFERENCES branches(id) ON DELETE CASCADE, notes TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS leave_requests (
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
      start_date TEXT NOT NULL, end_date TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','cancelled')),
      created_by INTEGER, decided_by INTEGER, decided_at TEXT, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS shift_requests (
      id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('cover','drop','pickup')),
      shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
      from_employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
      to_employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
      note TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','cancelled')),
      created_by INTEGER, decided_by INTEGER, decided_at TEXT, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS attendance (
      id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
      branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL, date TEXT NOT NULL,
      clock_in TEXT NOT NULL, clock_out TEXT, break_minutes INTEGER NOT NULL DEFAULT 0,
      source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS audit_log (
      id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER, username TEXT, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')""",
]
INDEXES_SQL = [
    'CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(date)',
    'CREATE INDEX IF NOT EXISTS idx_shifts_emp ON shifts(employee_id, date)',
    'CREATE INDEX IF NOT EXISTS idx_shifts_branch ON shifts(branch_id, date)',
    'CREATE INDEX IF NOT EXISTS idx_att_emp ON attendance(employee_id, date)',
    'CREATE INDEX IF NOT EXISTS idx_att_date ON attendance(date)',
    'CREATE INDEX IF NOT EXISTS idx_events_date ON calendar_events(start_date, end_date)',
    'CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)',
]

# Tables in insert order (parents first). Sessions are never backed up.
TABLES = ['settings', 'branches', 'positions', 'employees', 'employee_skills', 'employee_branches', 'availability',
          'shift_templates', 'staffing_needs', 'shifts', 'forecasts', 'calendar_events', 'leave_requests', 'shift_requests',
          'attendance', 'users', 'audit_log']
V1_TABLES = {'settings', 'positions', 'employees', 'employee_skills', 'availability', 'shift_templates', 'staffing_needs',
             'shifts', 'forecasts', 'leave_requests', 'shift_requests', 'users', 'audit_log'}
CORE_COLUMNS = {'settings': {'key', 'value'}, 'users': {'id', 'username', 'password_hash', 'role', 'active'},
                'employees': {'id', 'name', 'position_id', 'employment_type'}, 'positions': {'id', 'name', 'color'},
                'shifts': {'id', 'date', 'start_time', 'end_time', 'employee_id', 'position_id', 'status'}}

# key: (type, default, min, max)
SETTINGS = {
    'business_name': ('str', 'Harbour Lane Group', 1, 80),
    'currency_symbol': ('str', 'S$', 1, 5),
    'forecast_label': ('str', 'Forecast sales', 1, 30),
    'max_daily_hours': ('num', 12, 1, 24),
    'weekly_ot_threshold': ('num', 44, 1, 168),
    'max_monthly_ot': ('num', 72, 0, 400),
    'ot_multiplier': ('num', 1.5, 1, 5),
    'holiday_pay_multiplier': ('num', 2, 1, 5),
    'min_rest_hours': ('num', 11, 0, 24),
    'max_consecutive_days': ('int', 6, 1, 14),
    'break_after_hours': ('num', 6, 1, 24),
    'min_break_minutes': ('int', 45, 0, 180),
    'part_time_max_hours': ('num', 35, 1, 168),
    'target_labour_pct': ('num', 30, 1, 100),
    'publish_notice_days': ('int', 7, 0, 60),
    'late_grace_minutes': ('int', 5, 0, 120),
    'show_demo_logins': ('bool', 1, 0, 1),
}


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class FileResponse:
    def __init__(self, data, content_type, filename):
        self.data = data
        self.content_type = content_type
        self.filename = filename


# ---------------------------------------------------------------- basics

def local_now():
    """Wall-clock time for the business. SHIFTTABLE_UTC_OFFSET (hours, e.g. 8 for
    Singapore) overrides the server's own time zone, which is UTC on cloud hosts."""
    offset = os.environ.get('SHIFTTABLE_UTC_OFFSET')
    if offset:
        return (datetime.now(timezone.utc) + timedelta(hours=float(offset))).replace(tzinfo=None)
    return datetime.now()


def today():
    override = os.environ.get('SHIFTTABLE_TODAY')
    return date.fromisoformat(override) if override else local_now().date()


def now_iso():
    return local_now().strftime('%Y-%m-%d %H:%M:%S')


def now_abs():
    n = local_now()
    return today().toordinal() * rules.DAY + n.hour * 60 + n.minute


def has_started(s):
    return rules.span(s)[0] <= now_abs()


def has_ended(s):
    return rules.span(s)[1] <= now_abs()


def db():
    conn = sqlite3.connect(CONFIG['db'], timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def rows(cur):
    return [dict(r) for r in cur]


def one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def audit(conn, user, action, detail=''):
    conn.execute('INSERT INTO audit_log (ts, user_id, username, action, detail) VALUES (?,?,?,?,?)',
                 (now_iso(), user['id'] if user else None, user['username'] if user else None, action, str(detail)[:1000]))


def get_settings(conn):
    raw = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM settings')}
    out = {}
    for key, (kind, default, _lo, _hi) in SETTINGS.items():
        v = raw.get(key)
        try:
            if kind == 'str':
                out[key] = v if v is not None else default
            elif kind == 'int' or kind == 'bool':
                out[key] = int(float(v)) if v is not None else default
            else:
                out[key] = float(v) if v is not None else float(default)
        except ValueError:
            out[key] = default
    return out


def set_setting(conn, key, value):
    conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                 (key, str(value)))


# ---------------------------------------------------------------- validation

TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def v_str(b, key, label, maxlen=200, required=False, default=''):
    v = b.get(key, default)
    v = '' if v is None else str(v).strip()
    if required and not v:
        raise ApiError(400, f'{label} is required.')
    if len(v) > maxlen:
        raise ApiError(400, f'{label} must be {maxlen} characters or fewer.')
    return v


def v_num(b, key, label, lo, hi, default=None, integer=False):
    v = b.get(key, default)
    if v is None or v == '':
        if default is None:
            raise ApiError(400, f'{label} is required.')
        v = default
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, f'{label} must be a number.')
    if v != v or v < lo or v > hi:
        raise ApiError(400, f'{label} must be between {lo:g} and {hi:g}.')
    return int(round(v)) if integer else v


def v_date(v, label='Date'):
    try:
        return date.fromisoformat(str(v)).isoformat()
    except (TypeError, ValueError):
        raise ApiError(400, f'{label} must be a date (YYYY-MM-DD).')


def v_time(v, label='Time'):
    v = str(v or '').strip()
    if not TIME_RE.match(v):
        raise ApiError(400, f'{label} must be a time like 09:30.')
    return v


def v_id(conn, table, v, label, allow_none=False):
    if v in (None, '', 0, '0'):
        if allow_none:
            return None
        raise ApiError(400, f'{label} is required.')
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f'{label} is not valid.')
    if not conn.execute(f'SELECT 1 FROM {table} WHERE id = ?', (v,)).fetchone():
        raise ApiError(400, f'{label} does not exist.')
    return v


def v_color(b):
    c = v_str(b, 'color', 'Colour', 7, required=True)
    if not COLOR_RE.match(c):
        raise ApiError(400, 'Colour must look like #1a2b3c.')
    return c


def week_of(v):
    d = date.fromisoformat(v) if v else today()
    return rules.monday_of(d).isoformat()


def q_week(req):
    try:
        return week_of(req.query.get('week'))
    except ValueError:
        raise ApiError(400, 'Week must be a date (YYYY-MM-DD).')


def q_branch(req, value=None):
    v = value if value is not None else req.query.get('branch')
    if v in (None, '', 'all'):
        return None
    return v_id(req.conn, 'branches', v, 'Branch')


def q_employees(req):
    raw = req.query.get('employees') or ''
    if not raw:
        return None
    try:
        ids = {int(x) for x in raw.split(',') if x.strip()}
    except ValueError:
        raise ApiError(400, 'Employees must be a list of ids.')
    return ids or None


def q_range(req, limit=92):
    start = v_date(req.query.get('from'), 'From')
    end = v_date(req.query.get('to'), 'To')
    if end < start:
        raise ApiError(400, '"To" must be on or after "From".')
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > limit:
        raise ApiError(400, 'Reports cover up to 3 months at a time.')
    return start, end


# ---------------------------------------------------------------- passwords and sessions

def hash_password(pw, iterations=None):
    it = iterations or PBKDF2_ITER
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, it)
    return f'pbkdf2_sha256${it}${salt.hex()}${dk.hex()}'


def verify_password(pw, stored):
    try:
        algo, it, salt, digest = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, AttributeError):
        return False


DUMMY_HASH = hash_password('not-a-real-password')
_fails = {}
_fails_lock = threading.Lock()
LOCK_ATTEMPTS, LOCK_SECONDS = 5, 900


def throttled(key):
    with _fails_lock:
        now = time.time()
        recent = [t for t in _fails.get(key, []) if now - t < LOCK_SECONDS]
        _fails[key] = recent
        return len(recent) >= LOCK_ATTEMPTS


def record_fail(key):
    with _fails_lock:
        _fails.setdefault(key, []).append(time.time())


def clear_fails(key):
    with _fails_lock:
        _fails.pop(key, None)


# Sessions are signed cookies, not database rows, so they keep working when a cloud host
# spreads requests over several copies of the app. A token is "user_id.expiry.signature";
# the signature covers the user's password hash (changing a password signs them out
# everywhere) and a session epoch (a restore signs everyone out). In the hosted demo each
# copy has its own database, so the signature covers only what every copy shares.

def session_secret(conn):
    env = os.environ.get('SHIFTTABLE_SECRET')
    if env:
        return env.encode()
    row = conn.execute("SELECT value FROM settings WHERE key = '_session_secret'").fetchone()
    if row:
        return row[0].encode()
    value = secrets.token_hex(32)
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('_session_secret', ?)", (value,))
    conn.commit()
    return conn.execute("SELECT value FROM settings WHERE key = '_session_secret'").fetchone()[0].encode()


def session_epoch(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = '_session_epoch'").fetchone()
    return row[0] if row else '0'


def sign_session(conn, user, exp):
    if CONFIG['demo']:
        msg = f"demo.{user['id']}.{exp}.{user['username']}"
    else:
        msg = f"{user['id']}.{exp}.{session_epoch(conn)}.{hashlib.sha256(user['password_hash'].encode()).hexdigest()}"
    return hmac.new(session_secret(conn), msg.encode(), hashlib.sha256).hexdigest()


def make_session_token(conn, user):
    exp = int(time.time()) + SESSION_HOURS * 3600
    return f"{user['id']}.{exp}.{sign_session(conn, user, exp)}"


def read_session_token(conn, token):
    try:
        uid, exp, sig = token.split('.')
        uid, exp = int(uid), int(exp)
    except ValueError:
        return None
    if exp < time.time():
        return None
    u = one(conn, 'SELECT * FROM users WHERE id = ? AND active = 1', (uid,))
    if not u or not hmac.compare_digest(sig, sign_session(conn, u, exp)):
        return None
    return u


def end_all_sessions(conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('_session_epoch', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (secrets.token_hex(8),))


DEMO_USERNAMES = {u for u, *_ in sample_data.DEMO_USERS}


def ensure_demo_logins(conn):
    """Create the demo logins, or put them back as they should be (the hosted demo does
    this after every restore, so nobody can lock other visitors out)."""
    for username, password, display, role, _label in sample_data.DEMO_USERS:
        u = one(conn, 'SELECT id FROM users WHERE username = ?', (username,))
        if u:
            conn.execute('UPDATE users SET password_hash = ?, role = ?, active = 1 WHERE id = ?', (hash_password(password), role, u['id']))
            continue
        emp = one(conn, 'SELECT id FROM employees WHERE name = ?', (display,))
        conn.execute('INSERT INTO users (username, display_name, password_hash, role, employee_id, active, created_at) VALUES (?,?,?,?,?,1,?)',
                     (username, display, hash_password(password), role, emp['id'] if emp else None, now_iso()))
    set_setting(conn, 'show_demo_logins', 1)


def guard_demo_login(username):
    if CONFIG['demo'] and username in DEMO_USERNAMES:
        raise ApiError(400, "The demo logins can't be changed on the live demo. Add a login of your own to try this.")


def validate_password(pw):
    if len(pw) < 8:
        raise ApiError(400, 'Password must be at least 8 characters.')
    if len(pw) > 128:
        raise ApiError(400, 'Password must be 128 characters or fewer.')


def user_json(u):
    return {k: u[k] for k in ('id', 'username', 'display_name', 'role', 'employee_id', 'active', 'created_at', 'last_login_at')}


# ---------------------------------------------------------------- loading data for the rule engine

def load_positions(conn):
    return {r['id']: dict(r) for r in conn.execute('SELECT * FROM positions ORDER BY sort, name')}


def load_branches(conn):
    return {r['id']: dict(r) for r in conn.execute('SELECT * FROM branches ORDER BY sort, name')}


def load_employees(conn):
    emps = {}
    for r in conn.execute('SELECT * FROM employees ORDER BY name COLLATE NOCASE'):
        e = dict(r)
        e['skills'] = set()
        e['branches'] = {e['branch_id']} if e['branch_id'] else set()
        e['availability'] = {}
        emps[e['id']] = e
    for r in conn.execute('SELECT employee_id, position_id FROM employee_skills'):
        if r['employee_id'] in emps:
            emps[r['employee_id']]['skills'].add(r['position_id'])
    for r in conn.execute('SELECT employee_id, branch_id FROM employee_branches'):
        if r['employee_id'] in emps:
            emps[r['employee_id']]['branches'].add(r['branch_id'])
    for r in conn.execute('SELECT * FROM availability'):
        if r['employee_id'] in emps:
            emps[r['employee_id']]['availability'][r['weekday']] = dict(r)
    return emps


def events_in(conn, start, end, branch_id=None):
    return rows(conn.execute('SELECT * FROM calendar_events WHERE end_date >= ? AND start_date <= ? AND (branch_id IS NULL OR ? IS NULL OR branch_id = ?) ORDER BY start_date, kind, name',
                             (start, end, branch_id, branch_id)))


def load_ctx(conn, start, end):
    return {
        'settings': get_settings(conn),
        'employees': load_employees(conn),
        'positions': load_positions(conn),
        'branches': load_branches(conn),
        'needs': rows(conn.execute('SELECT * FROM staffing_needs ORDER BY branch_id, weekday, start_time')),
        'shifts': rows(conn.execute('SELECT * FROM shifts WHERE date BETWEEN ? AND ? ORDER BY date, start_time, id', (start, end))),
        'leave': rows(conn.execute("SELECT * FROM leave_requests WHERE status = 'approved' AND end_date >= ? AND start_date <= ?", (start, end))),
        'forecasts': {(r['branch_id'], r['date']): r['sales'] for r in conn.execute('SELECT branch_id, date, sales FROM forecasts WHERE date BETWEEN ? AND ?', (start, end))},
        'events': events_in(conn, start, end),
    }


def week_ctx(conn, week_start):
    ws = date.fromisoformat(week_start)
    lo = min(ws - timedelta(days=13), ws.replace(day=1))
    return load_ctx(conn, lo.isoformat(), (ws + timedelta(days=7)).isoformat())


def range_ctx(conn, start, end, published_only=True):
    lo = rules.monday_of(date.fromisoformat(start)) - timedelta(days=1)
    hi = rules.monday_of(date.fromisoformat(end)) + timedelta(days=7)
    ctx = load_ctx(conn, lo.isoformat(), hi.isoformat())
    if published_only:
        ctx['shifts'] = [s for s in ctx['shifts'] if s['status'] == 'published']
    return ctx, lo.isoformat(), hi.isoformat()


def emp_json(e, full=True):
    out = {'id': e['id'], 'name': e['name'], 'position_id': e['position_id'], 'branch_id': e['branch_id'],
           'branches': sorted(e['branches']), 'employment_type': e['employment_type'], 'active': e['active'], 'skills': sorted(e['skills'])}
    if full:
        out.update({k: e[k] for k in ('code', 'hourly_rate', 'target_hours', 'max_hours', 'phone', 'email', 'notes', 'created_at')})
        out['availability'] = [
            {'weekday': wd, 'available': e['availability'].get(wd, {}).get('available', 1),
             'start_time': e['availability'].get(wd, {}).get('start_time') or '',
             'end_time': e['availability'].get(wd, {}).get('end_time') or ''}
            for wd in range(7)]
    return out


def shift_label(conn, s):
    emp = one(conn, 'SELECT name FROM employees WHERE id = ?', (s['employee_id'],)) if s.get('employee_id') else None
    pos = one(conn, 'SELECT name FROM positions WHERE id = ?', (s['position_id'],)) if s.get('position_id') else None
    br = one(conn, 'SELECT name FROM branches WHERE id = ?', (s['branch_id'],)) if s.get('branch_id') else None
    return (f"{emp['name'] if emp else 'Open shift'} · {pos['name'] if pos else '-'} · {br['name'] + ' · ' if br else ''}"
            f"{rules.day_label(s['date'])} {s['date']} {s['start_time']}–{s['end_time']}")


# ---------------------------------------------------------------- routing

ROUTES = []


def route(method, pattern, role='staff'):
    def deco(fn):
        ROUTES.append((method, re.compile('^' + pattern + '$'), fn, role))
        return fn
    return deco


class Req:
    def __init__(self, conn, user, params, query, body, ip, secure=False):
        self.conn = conn
        self.user = user
        self.params = params
        self.query = query
        self.body = body
        self.ip = ip
        self.secure = secure
        self.cookies = []

    def set_session(self, user):
        flag = '; Secure' if self.secure else ''
        if user:
            self.cookies.append(f'{COOKIE}={make_session_token(self.conn, user)}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_HOURS * 3600}{flag}')
        else:
            self.cookies.append(f'{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{flag}')

    @property
    def is_manager(self):
        return self.user and RANK[self.user['role']] >= RANK['manager']


# ---------------------------------------------------------------- auth endpoints

@route('GET', '/api/public', role=None)
def api_public(req):
    st = get_settings(req.conn)
    out = {'business_name': st['business_name'], 'demo': CONFIG['demo']}
    if st['show_demo_logins']:
        out['demo_logins'] = [{'username': u, 'password': p, 'label': label} for u, p, _n, _r, label in sample_data.DEMO_USERS]
    return out


@route('POST', '/api/login', role=None)
def api_login(req):
    conn = req.conn
    username = str(req.body.get('username', '')).strip()[:80]
    password = str(req.body.get('password', ''))[:200]
    key = username.lower()
    if not username or not password:
        raise ApiError(400, 'Enter your username and password.')
    if throttled(key):
        raise ApiError(429, 'Too many failed attempts. Try again in 15 minutes.')
    u = one(conn, 'SELECT * FROM users WHERE username = ?', (username,))
    ok = verify_password(password, u['password_hash'] if u else DUMMY_HASH)
    if not (u and ok and u['active']):
        record_fail(key)
        audit(conn, None, 'login.failed', f'username={username}')
        conn.commit()
        raise ApiError(401, 'Wrong username or password.')
    clear_fails(key)
    conn.execute('UPDATE users SET last_login_at = ? WHERE id = ?', (now_iso(), u['id']))
    audit(conn, u, 'login', '')
    req.set_session(u)
    return {'user': user_json(u)}


@route('POST', '/api/logout', role=None)
def api_logout(req):
    req.set_session(None)
    return {'ok': True}


@route('GET', '/api/me', role=None)
def api_me(req):
    return {'user': user_json(req.user) if req.user else None}


@route('POST', '/api/me/password')
def api_me_password(req):
    conn = req.conn
    current = str(req.body.get('current', ''))
    new = str(req.body.get('new', ''))
    u = one(conn, 'SELECT * FROM users WHERE id = ?', (req.user['id'],))
    guard_demo_login(u['username'])
    if not verify_password(current, u['password_hash']):
        raise ApiError(400, 'Your current password is not correct.')
    validate_password(new)
    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (hash_password(new), u['id']))
    req.set_session(one(conn, 'SELECT * FROM users WHERE id = ?', (u['id'],)))  # other devices are signed out
    set_setting(conn, 'show_demo_logins', 0)
    audit(conn, req.user, 'user.password_changed', u['username'])
    return {'ok': True}


# ---------------------------------------------------------------- bootstrap, settings

@route('GET', '/api/bootstrap')
def api_bootstrap(req):
    conn = req.conn
    full = req.is_manager
    emps = load_employees(conn)
    out = {
        'user': user_json(req.user),
        'settings': get_settings(conn),
        'branches': list(load_branches(conn).values()),
        'positions': list(load_positions(conn).values()),
        'templates': rows(conn.execute('SELECT * FROM shift_templates ORDER BY sort, start_time')),
        'employees': [emp_json(e, full) for e in emps.values() if full or e['active']],
        'leave_kinds': LEAVE_KINDS,
        'today': today().isoformat(),
        'demo': CONFIG['demo'],
    }
    if req.user['employee_id'] and req.user['employee_id'] in emps and not full:
        out['me'] = emp_json(emps[req.user['employee_id']], True)
    if full:
        out['needs'] = rows(conn.execute('SELECT * FROM staffing_needs ORDER BY branch_id, weekday, start_time, position_id'))
    return out


@route('PUT', '/api/settings', role='admin')
def api_settings(req):
    changed = []
    for key, (kind, _d, lo, hi) in SETTINGS.items():
        if key not in req.body:
            continue
        if kind == 'str':
            v = v_str(req.body, key, key.replace('_', ' ').capitalize(), maxlen=hi, required=True)
        elif kind == 'bool':
            v = 1 if req.body[key] in (True, 1, '1', 'true') or (key == 'show_demo_logins' and CONFIG['demo']) else 0
        else:
            v = v_num(req.body, key, key.replace('_', ' ').capitalize(), lo, hi, integer=(kind == 'int'))
        set_setting(req.conn, key, v)
        changed.append(f'{key}={v}')
    audit(req.conn, req.user, 'settings.updated', ', '.join(changed))
    return {'settings': get_settings(req.conn)}


# ---------------------------------------------------------------- branches

def branch_body(b):
    code = v_str(b, 'code', 'Short code', 6).upper()
    if code and not re.match(r'^[A-Z0-9]{1,6}$', code):
        raise ApiError(400, 'Short code must be up to 6 letters or numbers.')
    return (v_str(b, 'name', 'Name', 60, required=True), code, v_str(b, 'address', 'Address', 120), v_color(b),
            v_num(b, 'sort', 'Sort', 0, 999, 0, True), 0 if b.get('active') in (0, False, '0') else 1)


@route('POST', '/api/branches', role='admin')
def api_branch_create(req):
    vals = branch_body(req.body)
    if one(req.conn, 'SELECT id FROM branches WHERE name = ?', (vals[0],)):
        raise ApiError(409, f'A branch called {vals[0]} already exists.')
    cur = req.conn.execute('INSERT INTO branches (name, code, address, color, sort, active) VALUES (?,?,?,?,?,?)', vals)
    audit(req.conn, req.user, 'branch.created', vals[0])
    return one(req.conn, 'SELECT * FROM branches WHERE id = ?', (cur.lastrowid,))


@route('PUT', r'/api/branches/(\d+)', role='admin')
def api_branch_update(req):
    bid = v_id(req.conn, 'branches', req.params[0], 'Branch')
    vals = branch_body(req.body)
    if one(req.conn, 'SELECT id FROM branches WHERE name = ? AND id != ?', (vals[0], bid)):
        raise ApiError(409, f'A branch called {vals[0]} already exists.')
    if not vals[5] and not req.conn.execute('SELECT COUNT(*) FROM branches WHERE active = 1 AND id != ?', (bid,)).fetchone()[0]:
        raise ApiError(400, 'At least one branch must stay active.')
    req.conn.execute('UPDATE branches SET name=?, code=?, address=?, color=?, sort=?, active=? WHERE id=?', (*vals, bid))
    audit(req.conn, req.user, 'branch.updated', vals[0])
    return one(req.conn, 'SELECT * FROM branches WHERE id = ?', (bid,))


@route('DELETE', r'/api/branches/(\d+)', role='admin')
def api_branch_delete(req):
    bid = v_id(req.conn, 'branches', req.params[0], 'Branch')
    if req.conn.execute('SELECT COUNT(*) FROM branches').fetchone()[0] <= 1:
        raise ApiError(400, "You can't delete the only branch.")
    n_emp = req.conn.execute('SELECT COUNT(*) FROM employees WHERE branch_id = ?', (bid,)).fetchone()[0]
    n_shift = req.conn.execute('SELECT COUNT(*) FROM shifts WHERE branch_id = ?', (bid,)).fetchone()[0]
    if n_emp or n_shift:
        raise ApiError(409, f'This branch is home to {n_emp} team member(s) and has {n_shift} shift(s). Move them, or mark the branch inactive instead.')
    name = one(req.conn, 'SELECT name FROM branches WHERE id = ?', (bid,))['name']
    req.conn.execute('DELETE FROM forecasts WHERE branch_id = ?', (bid,))
    req.conn.execute('DELETE FROM branches WHERE id = ?', (bid,))
    audit(req.conn, req.user, 'branch.deleted', name)
    return {'ok': True}


# ---------------------------------------------------------------- positions, templates, staffing needs

def position_body(b):
    return (v_str(b, 'name', 'Name', 40, required=True), v_str(b, 'department', 'Department', 30), v_color(b),
            v_num(b, 'sort', 'Sort', 0, 999, 0, True))


@route('POST', '/api/positions', role='manager')
def api_position_create(req):
    vals = position_body(req.body)
    if one(req.conn, 'SELECT id FROM positions WHERE name = ?', (vals[0],)):
        raise ApiError(400, f'A position called {vals[0]} already exists.')
    cur = req.conn.execute('INSERT INTO positions (name, department, color, sort) VALUES (?,?,?,?)', vals)
    audit(req.conn, req.user, 'position.created', vals[0])
    return one(req.conn, 'SELECT * FROM positions WHERE id = ?', (cur.lastrowid,))


@route('PUT', r'/api/positions/(\d+)', role='manager')
def api_position_update(req):
    pid = v_id(req.conn, 'positions', req.params[0], 'Position')
    vals = position_body(req.body)
    if one(req.conn, 'SELECT id FROM positions WHERE name = ? AND id != ?', (vals[0], pid)):
        raise ApiError(400, f'A position called {vals[0]} already exists.')
    req.conn.execute('UPDATE positions SET name=?, department=?, color=?, sort=? WHERE id=?', (*vals, pid))
    audit(req.conn, req.user, 'position.updated', vals[0])
    return one(req.conn, 'SELECT * FROM positions WHERE id = ?', (pid,))


@route('DELETE', r'/api/positions/(\d+)', role='manager')
def api_position_delete(req):
    pid = v_id(req.conn, 'positions', req.params[0], 'Position')
    n_emp = req.conn.execute('SELECT COUNT(*) FROM employees WHERE position_id = ?', (pid,)).fetchone()[0]
    n_shift = req.conn.execute('SELECT COUNT(*) FROM shifts WHERE position_id = ?', (pid,)).fetchone()[0]
    if n_emp or n_shift:
        raise ApiError(409, f'This position is used by {n_emp} team member(s) and {n_shift} shift(s). Reassign them first.')
    name = one(req.conn, 'SELECT name FROM positions WHERE id = ?', (pid,))['name']
    req.conn.execute('DELETE FROM positions WHERE id = ?', (pid,))
    audit(req.conn, req.user, 'position.deleted', name)
    return {'ok': True}


def template_body(b):
    start, end = v_time(b.get('start_time'), 'Start'), v_time(b.get('end_time'), 'End')
    if start == end:
        raise ApiError(400, 'Start and end cannot be the same time.')
    brk = v_num(b, 'break_minutes', 'Break', 0, 240, 0, True)
    if brk >= rules.paid_minutes({'date': '2000-01-03', 'start_time': start, 'end_time': end, 'break_minutes': 0}):
        raise ApiError(400, 'The break is longer than the shift.')
    return (v_str(b, 'name', 'Name', 40, required=True), start, end, brk, v_str(b, 'department', 'Department', 30),
            v_num(b, 'sort', 'Sort', 0, 999, 0, True))


@route('POST', '/api/templates', role='manager')
def api_template_create(req):
    vals = template_body(req.body)
    cur = req.conn.execute('INSERT INTO shift_templates (name, start_time, end_time, break_minutes, department, sort) VALUES (?,?,?,?,?,?)', vals)
    audit(req.conn, req.user, 'template.created', vals[0])
    return one(req.conn, 'SELECT * FROM shift_templates WHERE id = ?', (cur.lastrowid,))


@route('PUT', r'/api/templates/(\d+)', role='manager')
def api_template_update(req):
    tid = v_id(req.conn, 'shift_templates', req.params[0], 'Template')
    vals = template_body(req.body)
    req.conn.execute('UPDATE shift_templates SET name=?, start_time=?, end_time=?, break_minutes=?, department=?, sort=? WHERE id=?', (*vals, tid))
    audit(req.conn, req.user, 'template.updated', vals[0])
    return one(req.conn, 'SELECT * FROM shift_templates WHERE id = ?', (tid,))


@route('DELETE', r'/api/templates/(\d+)', role='manager')
def api_template_delete(req):
    tid = v_id(req.conn, 'shift_templates', req.params[0], 'Template')
    req.conn.execute('DELETE FROM shift_templates WHERE id = ?', (tid,))
    audit(req.conn, req.user, 'template.deleted', str(tid))
    return {'ok': True}


def need_body(conn, b):
    start, end = v_time(b.get('start_time'), 'Start'), v_time(b.get('end_time'), 'End')
    if start == end:
        raise ApiError(400, 'Start and end cannot be the same time.')
    return (v_id(conn, 'branches', b.get('branch_id'), 'Branch'), v_num(b, 'weekday', 'Weekday', 0, 6, integer=True),
            v_id(conn, 'positions', b.get('position_id'), 'Position'), start, end,
            v_num(b, 'break_minutes', 'Break', 0, 240, 0, True), v_num(b, 'headcount', 'Headcount', 1, 30, 1, True),
            v_str(b, 'label', 'Label', 40))


@route('POST', '/api/needs', role='manager')
def api_need_create(req):
    vals = need_body(req.conn, req.body)
    cur = req.conn.execute('INSERT INTO staffing_needs (branch_id, weekday, position_id, start_time, end_time, break_minutes, headcount, label) VALUES (?,?,?,?,?,?,?,?)', vals)
    audit(req.conn, req.user, 'need.created', f'{rules.DAY_NAMES[vals[1]]} {vals[3]}–{vals[4]} x{vals[6]}')
    return one(req.conn, 'SELECT * FROM staffing_needs WHERE id = ?', (cur.lastrowid,))


@route('PUT', r'/api/needs/(\d+)', role='manager')
def api_need_update(req):
    nid = v_id(req.conn, 'staffing_needs', req.params[0], 'Staffing need')
    vals = need_body(req.conn, req.body)
    req.conn.execute('UPDATE staffing_needs SET branch_id=?, weekday=?, position_id=?, start_time=?, end_time=?, break_minutes=?, headcount=?, label=? WHERE id=?', (*vals, nid))
    audit(req.conn, req.user, 'need.updated', str(nid))
    return one(req.conn, 'SELECT * FROM staffing_needs WHERE id = ?', (nid,))


@route('DELETE', r'/api/needs/(\d+)', role='manager')
def api_need_delete(req):
    nid = v_id(req.conn, 'staffing_needs', req.params[0], 'Staffing need')
    req.conn.execute('DELETE FROM staffing_needs WHERE id = ?', (nid,))
    audit(req.conn, req.user, 'need.deleted', str(nid))
    return {'ok': True}


def copy_need(conn, n, branch_id, weekday):
    conn.execute('INSERT INTO staffing_needs (branch_id, weekday, position_id, start_time, end_time, break_minutes, headcount, label) VALUES (?,?,?,?,?,?,?,?)',
                 (branch_id, weekday, n['position_id'], n['start_time'], n['end_time'], n['break_minutes'], n['headcount'], n['label']))


@route('POST', '/api/needs/copy', role='manager')
def api_need_copy(req):
    """Copy one weekday to other weekdays in the same branch, or a whole branch to another."""
    conn = req.conn
    bid = v_id(conn, 'branches', req.body.get('branch_id'), 'Branch')
    to_branch = req.body.get('to_branch_id')
    if to_branch:
        tb = v_id(conn, 'branches', to_branch, 'Copy to branch')
        if tb == bid:
            raise ApiError(400, 'Pick a different branch to copy to.')
        src = rows(conn.execute('SELECT * FROM staffing_needs WHERE branch_id = ?', (bid,)))
        conn.execute('DELETE FROM staffing_needs WHERE branch_id = ?', (tb,))
        for n in src:
            copy_need(conn, n, tb, n['weekday'])
        audit(conn, req.user, 'need.copied_branch', f'{bid} -> {tb}')
        return {'copied': len(src), 'days': 7}
    src_wd = v_num(req.body, 'from_weekday', 'From day', 0, 6, integer=True)
    targets = req.body.get('to_weekdays') or []
    if not isinstance(targets, list) or not targets:
        raise ApiError(400, 'Pick at least one day to copy to.')
    targets = sorted({int(t) for t in targets if str(t).isdigit() and 0 <= int(t) <= 6 and int(t) != src_wd})
    src = rows(conn.execute('SELECT * FROM staffing_needs WHERE branch_id = ? AND weekday = ?', (bid, src_wd)))
    for wd in targets:
        conn.execute('DELETE FROM staffing_needs WHERE branch_id = ? AND weekday = ?', (bid, wd))
        for n in src:
            copy_need(conn, n, bid, wd)
    audit(conn, req.user, 'need.copied', f"{rules.DAY_NAMES[src_wd]} -> {', '.join(rules.DAY_NAMES[t] for t in targets)}")
    return {'copied': len(src), 'days': len(targets)}


# ---------------------------------------------------------------- employees

def first_branch(conn):
    r = conn.execute('SELECT id FROM branches ORDER BY active DESC, sort, id LIMIT 1').fetchone()
    return r[0] if r else None


def employee_body(conn, b, eid=None):
    etype = v_str(b, 'employment_type', 'Employment type', 20, required=True)
    if etype not in ('full_time', 'part_time', 'casual'):
        raise ApiError(400, 'Employment type must be full-time, part-time or casual.')
    target = v_num(b, 'target_hours', 'Target hours', 0, 84, 0)
    mx = v_num(b, 'max_hours', 'Max hours', 1, 84, 44)
    if target > mx:
        raise ApiError(400, 'Target hours cannot be more than max hours.')
    email = v_str(b, 'email', 'Email', 120)
    if email and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        raise ApiError(400, 'Email does not look right.')
    code = v_str(b, 'code', 'Staff code', 20)
    if code and one(conn, "SELECT id FROM employees WHERE code = ? COLLATE NOCASE AND id != ?", (code, eid or 0)):
        raise ApiError(409, f'Staff code {code} is already used by someone else.')
    skills = b.get('skills') or []
    branches = b.get('branches') or []
    if not isinstance(skills, list) or not isinstance(branches, list):
        raise ApiError(400, 'Skills and branches must be lists.')
    skills = {v_id(conn, 'positions', s, 'Skill') for s in skills}
    pid = v_id(conn, 'positions', b.get('position_id'), 'Main position')
    skills.add(pid)
    home = v_id(conn, 'branches', b.get('branch_id') or first_branch(conn), 'Home branch')
    branches = {v_id(conn, 'branches', x, 'Branch') for x in branches} - {home}
    return {
        'name': v_str(b, 'name', 'Name', 80, required=True), 'code': code, 'position_id': pid, 'branch_id': home, 'employment_type': etype,
        'hourly_rate': v_num(b, 'hourly_rate', 'Hourly rate', 0, 1000, 0), 'target_hours': target, 'max_hours': mx,
        'phone': v_str(b, 'phone', 'Phone', 30), 'email': email, 'notes': v_str(b, 'notes', 'Notes', 500),
        'active': 0 if b.get('active') in (0, False, '0') else 1,
    }, skills, branches


def save_links(conn, eid, skills, branches):
    conn.execute('DELETE FROM employee_skills WHERE employee_id = ?', (eid,))
    conn.executemany('INSERT INTO employee_skills (employee_id, position_id) VALUES (?, ?)', [(eid, p) for p in sorted(skills)])
    conn.execute('DELETE FROM employee_branches WHERE employee_id = ?', (eid,))
    conn.executemany('INSERT INTO employee_branches (employee_id, branch_id) VALUES (?, ?)', [(eid, b) for b in sorted(branches)])


@route('GET', '/api/employees', role='manager')
def api_employees(req):
    return [emp_json(e) for e in load_employees(req.conn).values()]


@route('POST', '/api/employees', role='manager')
def api_employee_create(req):
    f, skills, branches = employee_body(req.conn, req.body)
    cur = req.conn.execute('INSERT INTO employees (name, code, position_id, branch_id, employment_type, hourly_rate, target_hours, max_hours, phone, email, notes, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (*f.values(), now_iso()))
    save_links(req.conn, cur.lastrowid, skills, branches)
    audit(req.conn, req.user, 'employee.created', f['name'])
    return emp_json(load_employees(req.conn)[cur.lastrowid])


@route('PUT', r'/api/employees/(\d+)', role='manager')
def api_employee_update(req):
    eid = v_id(req.conn, 'employees', req.params[0], 'Team member')
    f, skills, branches = employee_body(req.conn, req.body, eid)
    req.conn.execute('UPDATE employees SET name=?, code=?, position_id=?, branch_id=?, employment_type=?, hourly_rate=?, target_hours=?, max_hours=?, phone=?, email=?, notes=?, active=? WHERE id=?',
                     (*f.values(), eid))
    save_links(req.conn, eid, skills, branches)
    audit(req.conn, req.user, 'employee.updated' if f['active'] else 'employee.archived', f['name'])
    return emp_json(load_employees(req.conn)[eid])


@route('DELETE', r'/api/employees/(\d+)', role='manager')
def api_employee_delete(req):
    eid = v_id(req.conn, 'employees', req.params[0], 'Team member')
    n = req.conn.execute('SELECT COUNT(*) FROM shifts WHERE employee_id = ?', (eid,)).fetchone()[0]
    if n:
        raise ApiError(409, f'This person has {n} shift(s) on record. Archive them instead so the history is kept.')
    name = one(req.conn, 'SELECT name FROM employees WHERE id = ?', (eid,))['name']
    req.conn.execute('DELETE FROM employees WHERE id = ?', (eid,))
    audit(req.conn, req.user, 'employee.deleted', name)
    return {'ok': True}


@route('PUT', r'/api/employees/(\d+)/availability')
def api_availability(req):
    eid = v_id(req.conn, 'employees', req.params[0], 'Team member')
    if not req.is_manager and req.user['employee_id'] != eid:
        raise ApiError(403, 'You can only change your own availability.')
    days = req.body.get('days')
    if not isinstance(days, list) or len(days) != 7:
        raise ApiError(400, 'Send availability for all 7 days.')
    clean = []
    for d in days:
        if not isinstance(d, dict):
            raise ApiError(400, 'Availability is not valid.')
        wd = v_num(d, 'weekday', 'Weekday', 0, 6, integer=True)
        avail = 0 if d.get('available') in (0, False, '0') else 1
        s, e = (d.get('start_time') or '').strip(), (d.get('end_time') or '').strip()
        if avail and (s or e):
            s, e = v_time(s, f'{rules.DAY_NAMES[wd]} from'), v_time(e, f'{rules.DAY_NAMES[wd]} to')
            if s == e:
                raise ApiError(400, f'{rules.DAY_NAMES[wd]}: from and to cannot be the same.')
        else:
            s = e = None
        clean.append((eid, wd, avail, s, e))
    if len({c[1] for c in clean}) != 7:
        raise ApiError(400, 'Each weekday must appear once.')
    req.conn.execute('DELETE FROM availability WHERE employee_id = ?', (eid,))
    req.conn.executemany('INSERT INTO availability (employee_id, weekday, available, start_time, end_time) VALUES (?,?,?,?,?)', clean)
    audit(req.conn, req.user, 'availability.updated', one(req.conn, 'SELECT name FROM employees WHERE id = ?', (eid,))['name'])
    return emp_json(load_employees(req.conn)[eid], True)


# ---------------------------------------------------------------- roster

def publish_state(week_shifts, week_start):
    pub = [s['published_at'] for s in week_shifts if s['status'] == 'published' and s['published_at']]
    first = min(pub) if pub else None
    return {
        'first_published_at': first,
        'notice_days': (date.fromisoformat(week_start) - date.fromisoformat(first[:10])).days if first else None,
        'drafts': sum(1 for s in week_shifts if s['status'] == 'draft'),
        'published': sum(1 for s in week_shifts if s['status'] == 'published'),
        'changed': sum(1 for s in week_shifts if s['status'] == 'published' and s['changed_after_publish']),
    }


STAFF_SHIFT_KEYS = ('id', 'date', 'start_time', 'end_time', 'break_minutes', 'employee_id', 'position_id', 'branch_id', 'notes', 'changed_after_publish')


@route('GET', '/api/roster')
def api_roster(req):
    conn = req.conn
    ws = q_week(req)
    bid = q_branch(req)
    days = rules.week_dates(ws)
    ctx = week_ctx(conn, ws)
    week_all = [s for s in ctx['shifts'] if s['date'] in days]
    in_scope = (lambda s: True) if bid is None else (lambda s: s['branch_id'] == bid)
    events = events_in(conn, days[0], days[6], bid)
    if not req.is_manager:
        me = req.user['employee_id']
        vis = [s for s in week_all if s['status'] == 'published' and (in_scope(s) or s['employee_id'] == me)]
        return {
            'week_start': ws, 'days': days, 'branch_id': bid, 'events': events,
            'shifts': [{k: s[k] for k in STAFF_SHIFT_KEYS} | {'hours': rules.paid_hours(s), 'elsewhere': not in_scope(s)} for s in vis],
            'leave': rows(conn.execute("SELECT id, employee_id, start_date, end_date, kind, status FROM leave_requests WHERE employee_id = ? AND status IN ('approved','pending') AND end_date >= ? AND start_date <= ?",
                                       (me, days[0], days[6]))) if me else [],
            'my_hours': round(sum(rules.paid_hours(s) for s in vis if s['employee_id'] == me), 2),
        }
    ev = rules.evaluate(ctx, ws, bid)
    by_shift = defaultdict(list)
    for i in ev['issues']:
        for key in ('shift_id', 'related_id'):
            if i.get(key):
                by_shift[i[key]].append(i['id'])
    in_view = {r['employee_id'] for r in ev['employees']}
    shifts = [dict(s, cost=ev['shift_cost'].get(s['id'], 0), hours=rules.paid_hours(s), issue_ids=by_shift.get(s['id'], []), elsewhere=False)
              for s in week_all if in_scope(s)]
    shifts += [dict(s, hours=rules.paid_hours(s), issue_ids=[], elsewhere=True) for s in week_all if not in_scope(s) and s['employee_id'] in in_view]
    leave = rows(conn.execute("SELECT id, employee_id, start_date, end_date, kind, status FROM leave_requests WHERE status IN ('approved','pending') AND end_date >= ? AND start_date <= ?",
                              (days[0], days[6])))
    return {
        'week_start': ws, 'days': days, 'branch_id': bid, 'shifts': shifts, 'leave': leave, 'events': events,
        'issues': ev['issues'], 'employees': ev['employees'], 'day_stats': ev['days'], 'totals': ev['totals'],
        'publish': publish_state([s for s in week_all if in_scope(s)], ws),
    }


@route('GET', '/api/calendar')
def api_calendar(req):
    """A month grid: per-day roster summary plus holidays and events."""
    conn = req.conn
    month = req.query.get('month') or today().strftime('%Y-%m')
    if not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', month):
        raise ApiError(400, 'Month must look like 2026-10.')
    bid = q_branch(req)
    first = date.fromisoformat(month + '-01')
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    start, end = rules.monday_of(first), rules.monday_of(last) + timedelta(days=6)
    days = {}
    if req.is_manager:
        ws = start
        while ws <= end:
            ev = rules.evaluate(week_ctx(conn, ws.isoformat()), ws.isoformat(), bid)
            for d in ev['days']:
                days[d['date']] = {k: d[k] for k in ('hours', 'cost', 'sales', 'labour_pct', 'need_total', 'staffed', 'open', 'shifts', 'drafts')}
            ws += timedelta(days=7)
    else:
        me = req.user['employee_id']
        for s in rows(conn.execute("SELECT * FROM shifts WHERE status = 'published' AND employee_id = ? AND date BETWEEN ? AND ? ORDER BY date, start_time",
                                   (me, start.isoformat(), end.isoformat()))):
            days.setdefault(s['date'], {'mine': []})['mine'].append({k: s[k] for k in STAFF_SHIFT_KEYS})
    return {'month': month, 'start': start.isoformat(), 'end': end.isoformat(), 'days': days,
            'events': events_in(conn, start.isoformat(), end.isoformat(), bid)}


def shift_body(conn, b):
    d = v_date(b.get('date'))
    start, end = v_time(b.get('start_time'), 'Start'), v_time(b.get('end_time'), 'End')
    if start == end:
        raise ApiError(400, 'Start and end cannot be the same time.')
    brk = v_num(b, 'break_minutes', 'Break', 0, 240, 0, True)
    total = rules.paid_minutes({'date': d, 'start_time': start, 'end_time': end, 'break_minutes': 0})
    if brk >= total:
        raise ApiError(400, 'The break is longer than the shift.')
    eid = v_id(conn, 'employees', b.get('employee_id'), 'Team member', allow_none=True)
    pid = v_id(conn, 'positions', b.get('position_id'), 'Position', allow_none=True)
    emp = one(conn, 'SELECT position_id, branch_id FROM employees WHERE id = ?', (eid,)) if eid else None
    if pid is None:
        if not emp:
            raise ApiError(400, 'Pick a position for an open shift.')
        pid = emp['position_id']
    bid = v_id(conn, 'branches', b.get('branch_id') or (emp['branch_id'] if emp else None) or first_branch(conn), 'Branch')
    return {'date': d, 'start_time': start, 'end_time': end, 'break_minutes': brk, 'employee_id': eid,
            'position_id': pid, 'branch_id': bid, 'notes': v_str(b, 'notes', 'Notes', 300)}


def shift_issues(conn, s):
    ws = week_of(s['date'])
    ev = rules.evaluate(week_ctx(conn, ws), ws, s['branch_id'])
    return [i for i in ev['issues'] if i.get('shift_id') == s['id'] or i.get('related_id') == s['id']]


def short_notice(conn, s):
    return (date.fromisoformat(s['date']) - today()).days < get_settings(conn)['publish_notice_days']


@route('POST', '/api/shifts', role='manager')
def api_shift_create(req):
    f = shift_body(req.conn, req.body)
    now = now_iso()
    cur = req.conn.execute("INSERT INTO shifts (date, start_time, end_time, break_minutes, employee_id, position_id, branch_id, notes, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'draft', ?, ?)",
                           (*f.values(), now, now))
    s = one(req.conn, 'SELECT * FROM shifts WHERE id = ?', (cur.lastrowid,))
    audit(req.conn, req.user, 'shift.created', shift_label(req.conn, s))
    return {'shift': s, 'issues': shift_issues(req.conn, s)}


@route('PUT', r'/api/shifts/(\d+)', role='manager')
def api_shift_update(req):
    sid = v_id(req.conn, 'shifts', req.params[0], 'Shift')
    old = one(req.conn, 'SELECT * FROM shifts WHERE id = ?', (sid,))
    f = shift_body(req.conn, req.body)
    material = any(old[k] != f[k] for k in ('date', 'start_time', 'end_time', 'break_minutes', 'employee_id', 'position_id', 'branch_id'))
    changed = 1 if (old['status'] == 'published' and material) or old['changed_after_publish'] else 0
    req.conn.execute('UPDATE shifts SET date=?, start_time=?, end_time=?, break_minutes=?, employee_id=?, position_id=?, branch_id=?, notes=?, changed_after_publish=?, updated_at=? WHERE id=?',
                     (*f.values(), changed, now_iso(), sid))
    s = one(req.conn, 'SELECT * FROM shifts WHERE id = ?', (sid,))
    audit(req.conn, req.user, 'shift.updated', f'{shift_label(req.conn, old)}  ->  {shift_label(req.conn, s)}')
    if old['status'] == 'published' and material and (short_notice(req.conn, old) or short_notice(req.conn, s)):
        audit(req.conn, req.user, 'shift.short_notice', shift_label(req.conn, s))
    return {'shift': s, 'issues': shift_issues(req.conn, s)}


@route('DELETE', r'/api/shifts/(\d+)', role='manager')
def api_shift_delete(req):
    sid = v_id(req.conn, 'shifts', req.params[0], 'Shift')
    s = one(req.conn, 'SELECT * FROM shifts WHERE id = ?', (sid,))
    label = shift_label(req.conn, s)
    req.conn.execute('DELETE FROM shifts WHERE id = ?', (sid,))
    audit(req.conn, req.user, 'shift.deleted', label)
    if s['status'] == 'published' and short_notice(req.conn, s):
        audit(req.conn, req.user, 'shift.short_notice', 'Deleted: ' + label)
    return {'ok': True}


@route('GET', r'/api/shifts/(\d+)/candidates', role='manager')
def api_shift_candidates(req):
    sid = v_id(req.conn, 'shifts', req.params[0], 'Shift')
    s = one(req.conn, 'SELECT * FROM shifts WHERE id = ?', (sid,))
    ctx = week_ctx(req.conn, week_of(s['date']))
    out = rules.rank_candidates(ctx, s, ignore_id=sid)
    for r in out:
        r['current'] = r['employee_id'] == s['employee_id']
    return out


def scope_sql(bid):
    return ('', ()) if bid is None else (' AND branch_id = ?', (bid,))


@route('POST', '/api/roster/copy', role='manager')
def api_roster_copy(req):
    conn = req.conn
    ws = week_of(v_date(req.body.get('week'), 'Week'))
    fw = week_of(v_date(req.body.get('from_week'), 'Copy from'))
    bid = q_branch(req, req.body.get('branch_id') or '')
    if ws == fw:
        raise ApiError(400, 'Pick a different week to copy from.')
    offset = (date.fromisoformat(ws) - date.fromisoformat(fw)).days
    src_days = rules.week_dates(fw)
    days = rules.week_dates(ws)
    where, args = scope_sql(bid)
    if req.body.get('replace_drafts'):
        conn.execute(f"DELETE FROM shifts WHERE date BETWEEN ? AND ? AND status = 'draft'{where}", (days[0], days[6], *args))
    ctx = week_ctx(conn, ws)
    existing = {(s['date'], s['start_time'], s['end_time'], s['employee_id'], s['position_id'], s['branch_id']) for s in ctx['shifts']}
    now = now_iso()
    made = opened = skipped_closed = 0
    for s in rows(conn.execute(f'SELECT * FROM shifts WHERE date BETWEEN ? AND ?{where} ORDER BY date, start_time', (src_days[0], src_days[6], *args))):
        nd = (date.fromisoformat(s['date']) + timedelta(days=offset)).isoformat()
        if rules.closure(ctx, nd, s['branch_id']):
            skipped_closed += 1
            continue
        eid = s['employee_id']
        emp = ctx['employees'].get(eid) if eid else None
        if eid and (not emp or not emp['active'] or rules.on_leave(ctx, eid, nd)):
            eid = None
            opened += 1
        key = (nd, s['start_time'], s['end_time'], eid, s['position_id'], s['branch_id'])
        if key in existing:
            continue
        existing.add(key)
        conn.execute("INSERT INTO shifts (date, start_time, end_time, break_minutes, employee_id, position_id, branch_id, notes, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'draft', ?, ?)",
                     (nd, s['start_time'], s['end_time'], s['break_minutes'], eid, s['position_id'], s['branch_id'], s['notes'], now, now))
        made += 1
    audit(conn, req.user, 'roster.copied', f'{fw} -> {ws}: {made} shifts')
    return {'created': made, 'opened': opened, 'skipped_closed': skipped_closed}


@route('POST', '/api/roster/autofill', role='manager')
def api_roster_autofill(req):
    conn = req.conn
    ws = week_of(v_date(req.body.get('week'), 'Week'))
    bid = q_branch(req, req.body.get('branch_id') or '')
    days = rules.week_dates(ws)
    t = today().isoformat()
    if days[6] < t:
        raise ApiError(400, 'That week is already over. Auto-fill works on today and later.')
    new, assigned = rules.autofill(week_ctx(conn, ws), ws, from_date=max(days[0], t), branch_id=bid)
    now = now_iso()
    for s in new:
        conn.execute("INSERT INTO shifts (date, start_time, end_time, break_minutes, employee_id, position_id, branch_id, notes, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?, '', 'draft', ?, ?)",
                     (s['date'], s['start_time'], s['end_time'], s['break_minutes'], s['employee_id'], s['position_id'], s['branch_id'], now, now))
    for sid, eid in assigned.items():
        conn.execute("UPDATE shifts SET employee_id = ?, changed_after_publish = CASE WHEN status = 'published' THEN 1 ELSE changed_after_publish END, updated_at = ? WHERE id = ?",
                     (eid, now, sid))
    filled = len(assigned) + sum(1 for s in new if s['employee_id'])
    where, args = scope_sql(bid)
    still_open = conn.execute(f'SELECT COUNT(*) FROM shifts WHERE date BETWEEN ? AND ? AND employee_id IS NULL{where}', (max(days[0], t), days[6], *args)).fetchone()[0]
    audit(conn, req.user, 'roster.autofilled', f'{ws}: {len(new)} created, {filled} assigned, {still_open} still open')
    return {'created': len(new), 'assigned': filled, 'still_open': still_open}


@route('POST', '/api/roster/publish', role='manager')
def api_roster_publish(req):
    ws = week_of(v_date(req.body.get('week'), 'Week'))
    bid = q_branch(req, req.body.get('branch_id') or '')
    days = rules.week_dates(ws)
    now = now_iso()
    where, args = scope_sql(bid)
    cur = req.conn.execute(f"UPDATE shifts SET status = 'published', published_at = ?, changed_after_publish = 0, updated_at = ? WHERE date BETWEEN ? AND ? AND (status = 'draft' OR changed_after_publish = 1){where}",
                           (now, now, days[0], days[6], *args))
    notice = (date.fromisoformat(ws) - today()).days
    audit(req.conn, req.user, 'roster.published', f'{ws}: {cur.rowcount} shifts, {notice} days before the week')
    return {'published': cur.rowcount, 'notice_days': notice}


@route('POST', '/api/roster/clear', role='manager')
def api_roster_clear(req):
    ws = week_of(v_date(req.body.get('week'), 'Week'))
    bid = q_branch(req, req.body.get('branch_id') or '')
    days = rules.week_dates(ws)
    where, args = scope_sql(bid)
    cur = req.conn.execute(f"DELETE FROM shifts WHERE date BETWEEN ? AND ? AND status = 'draft'{where}", (days[0], days[6], *args))
    audit(req.conn, req.user, 'roster.drafts_cleared', f'{ws}: {cur.rowcount} shifts')
    return {'deleted': cur.rowcount}


@route('PUT', '/api/forecasts', role='manager')
def api_forecast(req):
    d = v_date(req.body.get('date'))
    bid = v_id(req.conn, 'branches', req.body.get('branch_id'), 'Branch')
    sales = v_num(req.body, 'sales', 'Forecast', 0, 10_000_000, 0)
    req.conn.execute('INSERT INTO forecasts (branch_id, date, sales) VALUES (?, ?, ?) ON CONFLICT(branch_id, date) DO UPDATE SET sales = excluded.sales', (bid, d, sales))
    audit(req.conn, req.user, 'forecast.updated', f'{bid} {d}: {sales:g}')
    return {'date': d, 'branch_id': bid, 'sales': sales}


# ---------------------------------------------------------------- holidays and events

def event_body(conn, b):
    kind = v_str(b, 'kind', 'Type', 10, required=True)
    if kind not in EVENT_KINDS:
        raise ApiError(400, 'Type must be public holiday, event or closed.')
    start = v_date(b.get('start_date'), 'From')
    end = v_date(b.get('end_date') or start, 'To')
    if end < start:
        raise ApiError(400, '"To" must be on or after "From".')
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 366:
        raise ApiError(400, 'Events can last at most a year.')
    return (start, end, kind, v_str(b, 'name', 'Name', 80, required=True),
            v_id(conn, 'branches', b.get('branch_id'), 'Branch', allow_none=True), v_str(b, 'notes', 'Notes', 300))


@route('GET', '/api/events')
def api_events(req):
    start = v_date(req.query.get('from'), 'From')
    end = v_date(req.query.get('to'), 'To')
    return events_in(req.conn, start, end, q_branch(req))


@route('POST', '/api/events', role='manager')
def api_event_create(req):
    vals = event_body(req.conn, req.body)
    cur = req.conn.execute('INSERT INTO calendar_events (start_date, end_date, kind, name, branch_id, notes) VALUES (?,?,?,?,?,?)', vals)
    audit(req.conn, req.user, f'event.created', f'{vals[2]} {vals[3]} {vals[0]}..{vals[1]}')
    return one(req.conn, 'SELECT * FROM calendar_events WHERE id = ?', (cur.lastrowid,))


@route('PUT', r'/api/events/(\d+)', role='manager')
def api_event_update(req):
    eid = v_id(req.conn, 'calendar_events', req.params[0], 'Event')
    vals = event_body(req.conn, req.body)
    req.conn.execute('UPDATE calendar_events SET start_date=?, end_date=?, kind=?, name=?, branch_id=?, notes=? WHERE id=?', (*vals, eid))
    audit(req.conn, req.user, 'event.updated', f'{vals[2]} {vals[3]} {vals[0]}..{vals[1]}')
    return one(req.conn, 'SELECT * FROM calendar_events WHERE id = ?', (eid,))


@route('DELETE', r'/api/events/(\d+)', role='manager')
def api_event_delete(req):
    eid = v_id(req.conn, 'calendar_events', req.params[0], 'Event')
    e = one(req.conn, 'SELECT * FROM calendar_events WHERE id = ?', (eid,))
    req.conn.execute('DELETE FROM calendar_events WHERE id = ?', (eid,))
    audit(req.conn, req.user, 'event.deleted', f"{e['kind']} {e['name']} {e['start_date']}")
    return {'ok': True}


@route('POST', '/api/events/holidays', role='manager')
def api_event_holidays(req):
    year = v_num(req.body, 'year', 'Year', 2000, 2100, integer=True)
    items = sample_data.holiday_events(year)
    if not items:
        raise ApiError(400, f'Singapore holidays for {year} are not built in yet. Add them by hand as "Public holiday" events.')
    added = 0
    for d, kind, name in items:
        if not one(req.conn, 'SELECT id FROM calendar_events WHERE start_date = ? AND name = ?', (d, name)):
            req.conn.execute('INSERT INTO calendar_events (start_date, end_date, kind, name, branch_id, notes) VALUES (?,?,?,?,NULL,?)',
                             (d, d, kind, name, 'Singapore public holiday' if kind == 'holiday' else ''))
            added += 1
    audit(req.conn, req.user, 'event.holidays_loaded', f'{year}: {added} added')
    return {'added': added, 'year': year}


# ---------------------------------------------------------------- requests

def names(conn):
    return {r['id']: r['name'] for r in conn.execute('SELECT id, name FROM employees')}


@route('GET', '/api/requests')
def api_requests(req):
    conn = req.conn
    status = req.query.get('status', 'pending')
    where, args = ('status = ?', [status]) if status in ('pending', 'approved', 'rejected', 'cancelled') else ('1=1', [])
    me = req.user['employee_id']
    if not req.is_manager:
        if not me:
            return {'leave': [], 'shift': []}
        leave = rows(conn.execute(f'SELECT * FROM leave_requests WHERE {where} AND employee_id = ? ORDER BY created_at DESC LIMIT 300', (*args, me)))
        shift = rows(conn.execute(f'SELECT * FROM shift_requests WHERE {where} AND (from_employee_id = ? OR to_employee_id = ?) ORDER BY created_at DESC LIMIT 300', (*args, me, me)))
    else:
        leave = rows(conn.execute(f'SELECT * FROM leave_requests WHERE {where} ORDER BY created_at DESC LIMIT 300', args))
        shift = rows(conn.execute(f'SELECT * FROM shift_requests WHERE {where} ORDER BY created_at DESC LIMIT 300', args))
    nm = names(conn)
    for lv in leave:
        lv['employee_name'] = nm.get(lv['employee_id'], '?')
        if req.is_manager and lv['status'] == 'pending':
            lv['conflicts'] = rows(conn.execute('SELECT id, date, start_time, end_time, branch_id, status FROM shifts WHERE employee_id = ? AND date BETWEEN ? AND ? ORDER BY date',
                                                (lv['employee_id'], lv['start_date'], lv['end_date'])))
    for r in shift:
        s = one(conn, 'SELECT * FROM shifts WHERE id = ?', (r['shift_id'],))
        r['shift'] = s
        r['from_name'] = nm.get(r['from_employee_id'])
        r['to_name'] = nm.get(r['to_employee_id'])
        if req.is_manager and r['status'] == 'pending' and s and r['to_employee_id']:
            ctx = week_ctx(conn, week_of(s['date']))
            emp = ctx['employees'].get(r['to_employee_id'])
            if emp:
                reasons, info = rules.check_candidate(ctx, emp, s, rules.group_by_employee(ctx['shifts']), ignore_id=s['id'])
                r['check'] = {'reasons': reasons, **info}
    return {'leave': leave, 'shift': shift}


@route('POST', '/api/requests/leave')
def api_leave_create(req):
    conn = req.conn
    if req.is_manager:
        eid = v_id(conn, 'employees', req.body.get('employee_id') or req.user['employee_id'], 'Team member')
    else:
        eid = req.user['employee_id']
        if not eid:
            raise ApiError(400, "Your login isn't linked to a team member. Ask a manager to link it.")
    start, end = v_date(req.body.get('start_date'), 'From'), v_date(req.body.get('end_date'), 'To')
    if end < start:
        raise ApiError(400, '"To" must be on or after "From".')
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 60:
        raise ApiError(400, 'Leave requests are limited to 60 days. Split longer leave into parts.')
    kind = v_str(req.body, 'kind', 'Leave type', 40, required=True)
    if kind not in LEAVE_KINDS:
        raise ApiError(400, 'Pick a leave type from the list.')
    if not req.is_manager and start < today().isoformat():
        raise ApiError(400, 'Leave has to start today or later.')
    if one(conn, "SELECT id FROM leave_requests WHERE employee_id = ? AND status IN ('pending','approved') AND end_date >= ? AND start_date <= ?", (eid, start, end)):
        raise ApiError(409, 'There is already a pending or approved leave request over those dates.')
    approve = bool(req.body.get('approve')) and req.is_manager
    cur = conn.execute('INSERT INTO leave_requests (employee_id, start_date, end_date, kind, reason, status, created_by, decided_by, decided_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (eid, start, end, kind, v_str(req.body, 'reason', 'Reason', 300), 'approved' if approve else 'pending', req.user['id'],
                        req.user['id'] if approve else None, now_iso() if approve else None, now_iso()))
    audit(conn, req.user, 'leave.created' + ('_approved' if approve else ''), f"{names(conn).get(eid)} {kind} {start}..{end}")
    return one(conn, 'SELECT * FROM leave_requests WHERE id = ?', (cur.lastrowid,))


@route('POST', '/api/requests/shift')
def api_shift_request_create(req):
    conn = req.conn
    kind = v_str(req.body, 'kind', 'Request type', 10, required=True)
    if kind not in ('cover', 'drop', 'pickup'):
        raise ApiError(400, 'Request type must be cover, drop or pickup.')
    sid = v_id(conn, 'shifts', req.body.get('shift_id'), 'Shift')
    s = one(conn, 'SELECT * FROM shifts WHERE id = ?', (sid,))
    me = req.user['employee_id']
    if not req.is_manager and not me:
        raise ApiError(400, "Your login isn't linked to a team member. Ask a manager to link it.")
    if s['status'] != 'published':
        raise ApiError(400, 'Only published shifts can be swapped or picked up.')
    if has_started(s):
        raise ApiError(400, 'That shift has already started.')
    to_emp = None
    if kind in ('cover', 'drop'):
        if not s['employee_id']:
            raise ApiError(400, 'That shift is already open.')
        if not req.is_manager and s['employee_id'] != me:
            raise ApiError(403, 'You can only ask for cover on your own shifts.')
        if one(conn, "SELECT id FROM shift_requests WHERE shift_id = ? AND status = 'pending' AND kind IN ('cover','drop')", (sid,)):
            raise ApiError(409, 'There is already a pending request for this shift.')
        if kind == 'cover':
            to_emp = v_id(conn, 'employees', req.body.get('to_employee_id'), 'Colleague')
            if to_emp == s['employee_id']:
                raise ApiError(400, 'Pick a different colleague.')
            if not one(conn, 'SELECT id FROM employees WHERE id = ? AND active = 1', (to_emp,)):
                raise ApiError(400, 'That colleague is archived.')
    else:
        if s['employee_id']:
            raise ApiError(400, 'That shift is no longer open.')
        to_emp = me if not req.is_manager else v_id(conn, 'employees', req.body.get('to_employee_id') or me, 'Team member')
        if one(conn, "SELECT id FROM shift_requests WHERE shift_id = ? AND status = 'pending' AND kind = 'pickup' AND to_employee_id = ?", (sid, to_emp)):
            raise ApiError(409, 'You have already asked for this shift.')
    cur = conn.execute('INSERT INTO shift_requests (kind, shift_id, from_employee_id, to_employee_id, note, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)',
                       (kind, sid, s['employee_id'], to_emp, v_str(req.body, 'note', 'Note', 300), 'pending', req.user['id'], now_iso()))
    audit(conn, req.user, f'request.{kind}', shift_label(conn, s))
    return one(conn, 'SELECT * FROM shift_requests WHERE id = ?', (cur.lastrowid,))


@route('POST', r'/api/requests/(leave|shift)/(\d+)/decide', role='manager')
def api_request_decide(req):
    conn = req.conn
    typ, rid = req.params[0], int(req.params[1])
    decision = req.body.get('decision')
    if decision not in ('approve', 'reject'):
        raise ApiError(400, 'Decision must be approve or reject.')
    table = 'leave_requests' if typ == 'leave' else 'shift_requests'
    r = one(conn, f'SELECT * FROM {table} WHERE id = ?', (rid,))
    if not r:
        raise ApiError(404, 'Request not found.')
    if r['status'] != 'pending':
        raise ApiError(409, f"This request was already {r['status']}.")
    status = 'approved' if decision == 'approve' else 'rejected'
    now = now_iso()
    out = {'status': status}
    if typ == 'leave':
        if status == 'approved' and req.body.get('release_shifts'):
            released = rows(conn.execute('SELECT * FROM shifts WHERE employee_id = ? AND date BETWEEN ? AND ? AND date >= ?',
                                         (r['employee_id'], r['start_date'], r['end_date'], today().isoformat())))
            for s in released:
                conn.execute("UPDATE shifts SET employee_id = NULL, changed_after_publish = CASE WHEN status = 'published' THEN 1 ELSE 0 END, updated_at = ? WHERE id = ?", (now, s['id']))
                audit(conn, req.user, 'shift.released_for_leave', shift_label(conn, s))
            out['released'] = len(released)
        audit(conn, req.user, f'leave.{status}', f"{names(conn).get(r['employee_id'])} {r['kind']} {r['start_date']}..{r['end_date']}")
    else:
        s = one(conn, 'SELECT * FROM shifts WHERE id = ?', (r['shift_id'],))
        if status == 'approved':
            if r['kind'] in ('cover', 'drop') and s['employee_id'] != r['from_employee_id']:
                raise ApiError(409, 'The shift has changed since this request was made. Reject it and ask for a new one.')
            if r['kind'] == 'pickup' and s['employee_id']:
                raise ApiError(409, 'That shift has already been filled.')
            new_emp = None if r['kind'] == 'drop' else r['to_employee_id']
            conn.execute("UPDATE shifts SET employee_id = ?, changed_after_publish = CASE WHEN status = 'published' THEN 1 ELSE 0 END, updated_at = ? WHERE id = ?",
                         (new_emp, now, s['id']))
            if r['kind'] == 'pickup':
                conn.execute("UPDATE shift_requests SET status = 'rejected', decided_by = ?, decided_at = ? WHERE shift_id = ? AND kind = 'pickup' AND status = 'pending' AND id != ?",
                             (req.user['id'], now, s['id'], rid))
            if short_notice(conn, s):
                audit(conn, req.user, 'shift.short_notice', f"{r['kind']} approved: " + shift_label(conn, s))
        audit(conn, req.user, f"request.{r['kind']}.{status}", shift_label(conn, s))
    conn.execute(f'UPDATE {table} SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?', (status, req.user['id'], now, rid))
    return out


@route('POST', r'/api/requests/(leave|shift)/(\d+)/cancel')
def api_request_cancel(req):
    conn = req.conn
    typ, rid = req.params[0], int(req.params[1])
    table = 'leave_requests' if typ == 'leave' else 'shift_requests'
    r = one(conn, f'SELECT * FROM {table} WHERE id = ?', (rid,))
    if not r:
        raise ApiError(404, 'Request not found.')
    me = req.user['employee_id']
    if typ == 'leave':
        owner = r['employee_id']
    else:
        owner = r['to_employee_id'] if r['kind'] == 'pickup' else r['from_employee_id']
    if not req.is_manager and r['created_by'] != req.user['id'] and not (me and owner == me):
        raise ApiError(403, 'You can only cancel your own requests.')
    if r['status'] != 'pending':
        raise ApiError(409, f"This request was already {r['status']}.")
    conn.execute(f"UPDATE {table} SET status = 'cancelled', decided_by = ?, decided_at = ? WHERE id = ?", (req.user['id'], now_iso(), rid))
    audit(conn, req.user, f'{typ}_request.cancelled', str(rid))
    return {'status': 'cancelled'}


# ---------------------------------------------------------------- dashboard and reports

TOTAL_KEYS = ('hours', 'cost', 'sales', 'labour_pct', 'open', 'shifts', 'people', 'errors', 'warnings', 'coverage_pct', 'staffed', 'need_total', 'drafts')


@route('GET', '/api/dashboard')
def api_dashboard(req):
    conn = req.conn
    t = today()
    ws = rules.monday_of(t).isoformat()
    bid = q_branch(req)
    upcoming = events_in(conn, t.isoformat(), (t + timedelta(days=60)).isoformat(), bid)
    if not req.is_manager:
        me = req.user['employee_id']
        if not me:
            return {'linked': False}
        until = (t + timedelta(days=27)).isoformat()
        mine = rows(conn.execute("SELECT * FROM shifts WHERE employee_id = ? AND status = 'published' AND date BETWEEN ? AND ? ORDER BY date, start_time",
                                 (me, t.isoformat(), until)))
        mine = [s for s in mine if not has_ended(s)]
        for s in mine:
            s['hours'] = rules.paid_hours(s)
        wk = rules.week_dates(ws)
        week_hours = sum(rules.paid_hours(s) for s in rows(conn.execute("SELECT * FROM shifts WHERE employee_id = ? AND status = 'published' AND date BETWEEN ? AND ?", (me, wk[0], wk[6]))))
        emps = load_employees(conn)
        e = emps.get(me)
        skills = rules.skills_of(e) if e else set()
        where = rules.branches_of(e) if e else set()
        open_shifts = [s for s in rows(conn.execute("SELECT * FROM shifts WHERE employee_id IS NULL AND status = 'published' AND date BETWEEN ? AND ? ORDER BY date, start_time",
                                                    (t.isoformat(), until))) if s['position_id'] in skills and s['branch_id'] in where and not has_started(s)]
        pending = conn.execute("SELECT COUNT(*) FROM leave_requests WHERE employee_id = ? AND status = 'pending'", (me,)).fetchone()[0] + \
            conn.execute("SELECT COUNT(*) FROM shift_requests WHERE (from_employee_id = ? OR to_employee_id = ?) AND status = 'pending'", (me, me)).fetchone()[0]
        requested = [r[0] for r in conn.execute("SELECT shift_id FROM shift_requests WHERE status = 'pending' AND ((kind IN ('cover','drop') AND from_employee_id = ?) OR (kind = 'pickup' AND to_employee_id = ?))", (me, me))]
        return {'linked': True, 'shifts': mine, 'week_hours': round(week_hours, 2), 'open_shifts': open_shifts, 'pending': pending,
                'target': e['target_hours'] if e else 0, 'events': upcoming[:6], 'requested': requested}
    ctx = week_ctx(conn, ws)
    ev = rules.evaluate(ctx, ws, bid)
    nws = (date.fromisoformat(ws) + timedelta(days=7)).isoformat()
    nctx = week_ctx(conn, nws)
    nev = rules.evaluate(nctx, nws, bid)
    nm = names(conn)
    in_scope = (lambda s: True) if bid is None else (lambda s: s['branch_id'] == bid)
    todays = [dict(s, employee_name=nm.get(s['employee_id']), hours=rules.paid_hours(s)) for s in ctx['shifts'] if s['date'] == t.isoformat() and in_scope(s)]
    since = (local_now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    branches = []
    active = [b for b in ctx['branches'].values() if b['active']]
    if bid is None and len(active) > 1:
        for b in active:
            bev = rules.evaluate(ctx, ws, b['id'])
            branches.append({'branch_id': b['id'], **{k: bev['totals'][k] for k in TOTAL_KEYS}})
    next_scope = [s for s in nctx['shifts'] if nws <= s['date'] < (date.fromisoformat(nws) + timedelta(days=7)).isoformat() and in_scope(s)]
    return {
        'week_start': ws, 'today': t.isoformat(), 'branch_id': bid,
        'totals': ev['totals'], 'days': [{k: d[k] for k in ('date', 'hours', 'cost', 'sales', 'labour_pct', 'need_total', 'staffed', 'open', 'holiday')} for d in ev['days']],
        'issues': ev['issues'][:8], 'branches': branches, 'events': upcoming[:8],
        'today_shifts': sorted(todays, key=lambda s: (s['start_time'], s['employee_name'] or '')),
        'pending': {
            'leave': conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status = 'pending'").fetchone()[0],
            'shift': conn.execute("SELECT COUNT(*) FROM shift_requests WHERE status = 'pending'").fetchone()[0],
        },
        'next_week': {'week_start': nws, 'totals': nev['totals'], 'publish': publish_state(next_scope, nws)},
        'short_notice_changes': conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'shift.short_notice' AND ts >= ?", (since,)).fetchone()[0],
        'publish': publish_state([s for s in ctx['shifts'] if s['date'] in rules.week_dates(ws) and in_scope(s)], ws),
    }


@route('GET', '/api/reports', role='manager')
def api_reports(req):
    start, end = q_range(req)
    ctx, _lo, _hi = range_ctx(req.conn, start, end, req.query.get('drafts') != '1')
    return rules.summarize_range(ctx, start, end, q_branch(req), q_employees(req))


@route('GET', '/api/reports/actual', role='manager')
def api_reports_actual(req):
    conn = req.conn
    start, end = q_range(req)
    bid = q_branch(req)
    ctx, lo, hi = range_ctx(conn, start, end, req.query.get('drafts') != '1')
    ctx['attendance'] = rows(conn.execute('SELECT * FROM attendance WHERE date BETWEEN ? AND ?', (lo, hi)))
    out = rules.compare_actual(ctx, start, end, now_abs(), bid, q_employees(req))
    where, args = scope_sql(bid)
    out['records'] = conn.execute(f'SELECT COUNT(*) FROM attendance WHERE date BETWEEN ? AND ?{where}', (start, end, *args)).fetchone()[0]
    return out


@route('GET', '/api/audit', role='manager')
def api_audit(req):
    try:
        limit = max(1, min(1000, int(req.query.get('limit', 200))))
    except ValueError:
        limit = 200
    return rows(req.conn.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', (limit,)))


# ---------------------------------------------------------------- users (admin)

def active_admins(conn, exclude=None):
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id != ?", (exclude or 0,)).fetchone()[0]


def user_body(conn, b):
    role = v_str(b, 'role', 'Role', 10, required=True)
    if role not in RANK:
        raise ApiError(400, 'Role must be admin, manager or staff.')
    eid = v_id(conn, 'employees', b.get('employee_id'), 'Linked team member', allow_none=True)
    if role == 'staff' and not eid:
        raise ApiError(400, 'Staff logins must be linked to a team member.')
    return {'display_name': v_str(b, 'display_name', 'Name', 80, required=True), 'role': role, 'employee_id': eid,
            'active': 0 if b.get('active') in (0, False, '0') else 1}


@route('GET', '/api/users', role='admin')
def api_users(req):
    return [user_json(u) for u in rows(req.conn.execute('SELECT * FROM users ORDER BY role, username'))]


@route('POST', '/api/users', role='admin')
def api_user_create(req):
    conn = req.conn
    username = v_str(req.body, 'username', 'Username', 40, required=True)
    if not re.match(r'^[A-Za-z0-9._-]{3,40}$', username):
        raise ApiError(400, 'Username must be 3 to 40 letters, numbers, dots, dashes or underscores.')
    if one(conn, 'SELECT id FROM users WHERE username = ?', (username,)):
        raise ApiError(409, 'That username is taken.')
    f = user_body(conn, req.body)
    pw = str(req.body.get('password', ''))
    validate_password(pw)
    cur = conn.execute('INSERT INTO users (username, display_name, password_hash, role, employee_id, active, created_at) VALUES (?,?,?,?,?,?,?)',
                       (username, f['display_name'], hash_password(pw), f['role'], f['employee_id'], f['active'], now_iso()))
    audit(conn, req.user, 'user.created', f"{username} ({f['role']})")
    return user_json(one(conn, 'SELECT * FROM users WHERE id = ?', (cur.lastrowid,)))


@route('PUT', r'/api/users/(\d+)', role='admin')
def api_user_update(req):
    conn = req.conn
    uid = v_id(conn, 'users', req.params[0], 'User')
    guard_demo_login(one(conn, 'SELECT username FROM users WHERE id = ?', (uid,))['username'])
    f = user_body(conn, req.body)
    if uid == req.user['id'] and (f['role'] != 'admin' or not f['active']):
        raise ApiError(400, "You can't remove your own admin access or deactivate yourself.")
    if (f['role'] != 'admin' or not f['active']) and not active_admins(conn, exclude=uid):
        raise ApiError(400, 'There must be at least one active admin.')
    conn.execute('UPDATE users SET display_name=?, role=?, employee_id=?, active=? WHERE id=?', (*f.values(), uid))
    audit(conn, req.user, 'user.updated', one(conn, 'SELECT username FROM users WHERE id = ?', (uid,))['username'])
    return user_json(one(conn, 'SELECT * FROM users WHERE id = ?', (uid,)))


@route('POST', r'/api/users/(\d+)/password', role='admin')
def api_user_password(req):
    conn = req.conn
    uid = v_id(conn, 'users', req.params[0], 'User')
    guard_demo_login(one(conn, 'SELECT username FROM users WHERE id = ?', (uid,))['username'])
    pw = str(req.body.get('password', ''))
    validate_password(pw)
    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (hash_password(pw), uid))
    if uid == req.user['id']:
        req.set_session(one(conn, 'SELECT * FROM users WHERE id = ?', (uid,)))
    set_setting(conn, 'show_demo_logins', 0)
    audit(conn, req.user, 'user.password_reset', one(conn, 'SELECT username FROM users WHERE id = ?', (uid,))['username'])
    return {'ok': True}


@route('DELETE', r'/api/users/(\d+)', role='admin')
def api_user_delete(req):
    conn = req.conn
    uid = v_id(conn, 'users', req.params[0], 'User')
    if uid == req.user['id']:
        raise ApiError(400, "You can't delete your own login.")
    u = one(conn, 'SELECT * FROM users WHERE id = ?', (uid,))
    guard_demo_login(u['username'])
    if u['role'] == 'admin' and not active_admins(conn, exclude=uid):
        raise ApiError(400, 'There must be at least one active admin.')
    conn.execute('DELETE FROM users WHERE id = ?', (uid,))
    audit(conn, req.user, 'user.deleted', u['username'])
    return {'ok': True}


# ---------------------------------------------------------------- data: backup, restore, import

SNAP_RE = re.compile(r'^[A-Za-z0-9._-]+\.db$')
SNAP_TIME_RE = re.compile(r'^shifttable-(\d{8}-\d{6})-')


def snapshot(conn, reason):
    os.makedirs(CONFIG['backups'], exist_ok=True)
    conn.commit()
    base = f"shifttable-{local_now():%Y%m%d-%H%M%S}-{reason}"
    name, n = base + '.db', 1
    while os.path.exists(os.path.join(CONFIG['backups'], name)):
        n += 1
        name = f'{base}-{n}.db'
    dst = sqlite3.connect(os.path.join(CONFIG['backups'], name))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    autos = sorted(f for f in os.listdir(CONFIG['backups']) if SNAP_RE.match(f) and '-manual' not in f)
    for old in autos[:-(5 if CONFIG['demo'] else 20)]:  # the hosted demo shares its snapshots, so keeps fewer
        os.remove(os.path.join(CONFIG['backups'], old))
    return name


def snap_path(name):
    if not SNAP_RE.match(name or ''):
        raise ApiError(400, 'Invalid backup name.')
    p = os.path.join(CONFIG['backups'], name)
    if not os.path.isfile(p):
        raise ApiError(404, 'Backup not found.')
    return p


def table_columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]


def export_json(conn):
    return {
        'app': 'ShiftTable', 'schema_version': SCHEMA_VERSION, 'exported_at': now_iso(),
        'tables': {t: rows(conn.execute(f'SELECT * FROM {t}')) for t in TABLES},
    }


def export_db_bytes(conn):
    conn.commit()
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    try:
        dst = sqlite3.connect(path)
        try:
            conn.backup(dst)
        finally:
            dst.close()
        with open(path, 'rb') as f:
            return f.read()
    finally:
        os.remove(path)


def check_db_file(path):
    """Validate an uploaded or stored SQLite file before it replaces the live database.
    Backups from version 1 are accepted and upgraded after the restore."""
    with open(path, 'rb') as f:
        if f.read(16) != b'SQLite format 3\x00':
            raise ApiError(400, 'That file is not a SQLite database.')
    try:
        src = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ApiError(400, 'The database file is damaged (integrity check failed).')
            if src.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
                raise ApiError(400, 'This backup was made by a newer version of ShiftTable.')
            have_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = sorted(V1_TABLES - have_tables)
            if missing:
                raise ApiError(400, f'This is not a ShiftTable backup (table "{missing[0]}" is missing).')
            for t, need in CORE_COLUMNS.items():
                gap = need - set(table_columns(src, t))
                if gap:
                    raise ApiError(400, f'This backup is missing columns in "{t}": {", ".join(sorted(gap))}.')
            if not src.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]:
                raise ApiError(400, 'This backup has no active admin login, so restoring it would lock everyone out.')
        finally:
            src.close()
    except sqlite3.DatabaseError as e:
        raise ApiError(400, f'That file could not be read as a database ({e}).')


def restore_from_db_file(conn, path):
    check_db_file(path)
    conn.commit()
    src = sqlite3.connect(path)
    try:
        src.backup(conn)
    finally:
        src.close()
    # A backup should only carry data. Drop any triggers or views it brought along.
    for name, kind in conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchall():
        conn.execute(f'DROP {kind.upper()} IF EXISTS "{name.replace(chr(34), chr(34) * 2)}"')
    conn.commit()
    migrate(conn)
    end_all_sessions(conn)
    if CONFIG['demo']:
        ensure_demo_logins(conn)


def restore_from_json(conn, data):
    if not isinstance(data, dict) or data.get('app') != 'ShiftTable' or not isinstance(data.get('tables'), dict):
        raise ApiError(400, 'That file is not a ShiftTable JSON backup.')
    if int(data.get('schema_version') or 0) > SCHEMA_VERSION:
        raise ApiError(400, 'This backup was made by a newer version of ShiftTable.')
    tables = data['tables']
    for t in TABLES:
        v = tables.get(t)
        if v is None and t not in V1_TABLES:
            continue
        if not isinstance(v, list) or not all(isinstance(r, dict) for r in v):
            raise ApiError(400, f'The backup is missing the "{t}" table.')
    if not any(u.get('role') == 'admin' and u.get('active', 1) for u in tables['users']):
        raise ApiError(400, 'This backup has no active admin login, so restoring it would lock everyone out.')
    area_map = {'FOH': 'Front of house', 'BOH': 'Kitchen'}
    try:
        for t in reversed(TABLES):
            conn.execute(f'DELETE FROM {t}')
        for t in TABLES:
            cols = table_columns(conn, t)
            for r in tables.get(t) or []:
                if t in ('positions', 'shift_templates') and 'area' in r and 'department' not in r:
                    r = dict(r, department=area_map.get(r['area'], r['area'] or ''))
                keys = [k for k in r if k in cols]
                if not keys:
                    continue
                conn.execute(f'INSERT INTO {t} ({", ".join(keys)}) VALUES ({", ".join("?" for _ in keys)})', [r[k] for k in keys])
        bad = conn.execute('PRAGMA foreign_key_check').fetchall()
        if bad:
            raise ApiError(400, f'The backup has {len(bad)} broken reference(s) and was not restored.')
    except sqlite3.Error as e:
        raise ApiError(400, f'The backup could not be restored: {e}. Nothing was changed.')
    ensure_settings(conn)
    ensure_branch_defaults(conn)
    end_all_sessions(conn)
    if CONFIG['demo']:
        ensure_demo_logins(conn)


def decode_upload(b):
    content = b.get('content')
    if not isinstance(content, str) or not content:
        raise ApiError(400, 'No file received.')
    try:
        return base64.b64decode(content, validate=True)
    except ValueError:
        raise ApiError(400, 'The file could not be read.')


@route('GET', '/api/data/stats', role='admin')
def api_data_stats(req):
    counts = {t: req.conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in TABLES}
    return {'counts': counts, 'db_file': os.path.basename(CONFIG['db']), 'db_bytes': os.path.getsize(CONFIG['db']),
            'schema_version': SCHEMA_VERSION, 'samples': sample_data.catalogue()}


@route('GET', '/api/data/backups', role='admin')
def api_backups(req):
    if not os.path.isdir(CONFIG['backups']):
        return []
    out = []
    for f in os.listdir(CONFIG['backups']):
        if SNAP_RE.match(f):
            p = os.path.join(CONFIG['backups'], f)
            # The name holds the business-time moment it was taken; a file's own time can
            # be a copy's unpacking time in the hosted demo, and UTC on cloud hosts.
            m = SNAP_TIME_RE.match(f)
            created = datetime.strptime(m.group(1), '%Y%m%d-%H%M%S') if m else datetime.fromtimestamp(os.path.getmtime(p))
            out.append({'name': f, 'bytes': os.path.getsize(p), 'created': created.strftime('%Y-%m-%d %H:%M:%S')})
    return sorted(out, key=lambda r: r['name'], reverse=True)


@route('POST', '/api/data/backups', role='admin')
def api_backup_create(req):
    name = snapshot(req.conn, 'manual')
    audit(req.conn, req.user, 'data.snapshot', name)
    return {'name': name}


@route('GET', r'/api/data/backups/([^/]+)', role='admin')
def api_backup_download(req):
    p = snap_path(req.params[0])
    with open(p, 'rb') as f:
        return FileResponse(f.read(), 'application/vnd.sqlite3', req.params[0])


@route('DELETE', r'/api/data/backups/([^/]+)', role='admin')
def api_backup_delete(req):
    p = snap_path(req.params[0])
    os.remove(p)
    audit(req.conn, req.user, 'data.snapshot_deleted', req.params[0])
    return {'ok': True}


@route('POST', r'/api/data/backups/([^/]+)/restore', role='admin')
def api_backup_restore(req):
    p = snap_path(req.params[0])
    check_db_file(p)
    safety = snapshot(req.conn, 'pre-restore')
    restore_from_db_file(req.conn, p)
    audit(req.conn, req.user, 'data.restored', f'{req.params[0]} (safety copy {safety})')
    return {'restored': req.params[0], 'safety_backup': safety, 'relogin': True}


@route('GET', '/api/data/export', role='admin')
def api_export(req):
    stamp = local_now().strftime('%Y%m%d-%H%M')
    audit(req.conn, req.user, 'data.exported', req.query.get('format', 'json'))
    if req.query.get('format') == 'db':
        return FileResponse(export_db_bytes(req.conn), 'application/vnd.sqlite3', f'shifttable-backup-{stamp}.db')
    return FileResponse(json.dumps(export_json(req.conn), indent=1).encode(), 'application/json', f'shifttable-backup-{stamp}.json')


@route('POST', '/api/data/restore', role='admin')
def api_restore(req):
    raw = decode_upload(req.body)
    fname = v_str(req.body, 'filename', 'File name', 200)
    if raw.startswith(b'SQLite format 3\x00'):
        fd, path = tempfile.mkstemp(suffix='.db')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(raw)
            check_db_file(path)
            safety = snapshot(req.conn, 'pre-restore')
            restore_from_db_file(req.conn, path)
        finally:
            os.remove(path)
    else:
        try:
            data = json.loads(raw.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, 'That file is neither a ShiftTable .db nor a .json backup.')
        safety = snapshot(req.conn, 'pre-restore')
        restore_from_json(req.conn, data)
    audit(req.conn, None, 'data.restored', f'{fname} by {req.user["username"]} (safety copy {safety})')
    return {'restored': fname, 'safety_backup': safety, 'relogin': True}


@route('POST', '/api/data/sample', role='admin')
def api_sample(req):
    industry = req.body.get('industry') or 'fnb'
    if industry not in sample_data.INDUSTRIES:
        raise ApiError(400, 'Pick one of the sample businesses.')
    safety = snapshot(req.conn, 'pre-sample')
    sample_data.load(req.conn, today(), load_ctx, keep_users=True, industry=industry, now=local_now())
    audit(req.conn, req.user, 'data.sample_loaded', f'{industry} (safety copy {safety})')
    return {'ok': True, 'industry': industry, 'safety_backup': safety}


@route('POST', '/api/data/reset', role='admin')
def api_reset(req):
    if req.body.get('confirm') != 'RESET':
        raise ApiError(400, 'Type RESET to confirm.')
    safety = snapshot(req.conn, 'pre-reset')
    clear_business_data(req.conn)
    audit(req.conn, req.user, 'data.reset', f'safety copy {safety}')
    return {'ok': True, 'safety_backup': safety}


def clear_business_data(conn):
    conn.execute('UPDATE users SET employee_id = NULL')
    for t in sample_data.BUSINESS_TABLES:
        conn.execute(f'DELETE FROM {t}')
    ensure_branch_defaults(conn)


# CSV import ------------------------------------------------------------

def norm_header(h):
    return re.sub(r'[^a-z]', '', (h or '').lower())


EMP_ALIASES = {'name': 'name', 'fullname': 'name', 'employee': 'name', 'position': 'position', 'role': 'position', 'mainposition': 'position',
               'skills': 'skills', 'canwork': 'skills', 'type': 'type', 'employmenttype': 'type', 'hourlyrate': 'rate', 'rate': 'rate',
               'targethours': 'target', 'target': 'target', 'maxhours': 'max', 'max': 'max', 'phone': 'phone', 'mobile': 'phone',
               'email': 'email', 'notes': 'notes', 'code': 'code', 'staffcode': 'code', 'employeecode': 'code', 'staffid': 'code',
               'branch': 'branch', 'homebranch': 'branch', 'outlet': 'branch', 'site': 'branch', 'location': 'branch',
               'branches': 'branches', 'otherbranches': 'branches', 'alsoworksat': 'branches'}
SHIFT_ALIASES = {'date': 'date', 'start': 'start', 'starttime': 'start', 'end': 'end', 'endtime': 'end', 'finish': 'end',
                 'break': 'break', 'breakminutes': 'break', 'breakmins': 'break', 'employee': 'employee', 'name': 'employee',
                 'staff': 'employee', 'position': 'position', 'role': 'position', 'notes': 'notes',
                 'branch': 'branch', 'outlet': 'branch', 'site': 'branch', 'location': 'branch'}
CLOCK_ALIASES = {'employee': 'employee', 'name': 'employee', 'staff': 'employee', 'staffname': 'employee', 'employeename': 'employee',
                 'code': 'code', 'staffcode': 'code', 'employeecode': 'code', 'staffid': 'code', 'employeeid': 'code', 'badge': 'code', 'id': 'code',
                 'date': 'date', 'workdate': 'date', 'clockin': 'in', 'in': 'in', 'timein': 'in', 'checkin': 'in', 'punchin': 'in',
                 'clockout': 'out', 'out': 'out', 'timeout': 'out', 'checkout': 'out', 'punchout': 'out',
                 'break': 'break', 'breakminutes': 'break', 'breakmins': 'break',
                 'branch': 'branch', 'outlet': 'branch', 'site': 'branch', 'location': 'branch'}
TYPE_MAP = {'fulltime': 'full_time', 'ft': 'full_time', 'full': 'full_time', 'parttime': 'part_time', 'pt': 'part_time',
            'part': 'part_time', 'casual': 'casual'}
TYPE_DEFAULTS = {'full_time': (40, 44), 'part_time': (20, 30), 'casual': (12, 24)}
PALETTE = ['#2f7fd1', '#6d5bd0', '#a1662f', '#c2417a', '#5b8c2a', '#d4552f', '#e08a1e', '#b8932a', '#6b7280', '#0f8b8d']


def parse_csv(text, aliases):
    text = text.lstrip('﻿')
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ApiError(400, 'The CSV file is empty.')
    keys = [aliases.get(norm_header(h)) for h in header]
    out = []
    for n, row in enumerate(reader, start=2):
        if not any(c.strip() for c in row):
            continue
        rec = {}
        for k, v in zip(keys, row):
            if k and k not in rec:
                rec[k] = v.strip()
        out.append((n, rec))
        if len(out) > 20000:
            raise ApiError(400, 'That file has more than 20,000 rows. Split it up.')
    return [k for k in keys if k], out


def parse_date_any(v):
    v = (v or '').strip()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d/%m/%y', '%d.%m.%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f'"{v}" is not a date (use YYYY-MM-DD or DD/MM/YYYY)')


def parse_time_any(v):
    v = (v or '').strip().lower().replace('.', ':')
    m = re.match(r'^(\d{1,2})(?::?(\d{2}))?(?::\d{2})?\s*(am|pm)?$', v)
    if not m:
        raise ValueError(f'"{v}" is not a time (use HH:MM)')
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            raise ValueError(f'"{v}" is not a time')
        h = h % 12 + (12 if ap == 'pm' else 0)
    if h == 24 and mi == 0:
        h = 0
    if h > 23 or mi > 59:
        raise ValueError(f'"{v}" is not a time')
    return f'{h:02d}:{mi:02d}'


def parse_stamp_any(v, day=None):
    """A clock value: '07:58', '2026-09-21 07:58', '21/09/2026 7:58 am' or ISO 'T' form.
    Returns (date or None, 'HH:MM', whether the value carried its own date)."""
    v = (v or '').strip().replace('T', ' ')
    parts = v.split(None, 1)
    if len(parts) == 2 and re.search(r'\d[-/.]\d+[-/.]\d', parts[0]):
        return parse_date_any(parts[0]), parse_time_any(parts[1]), True
    return day, parse_time_any(v), False


def num_or(v, default, lo, hi, label):
    if v in (None, ''):
        return default
    try:
        x = float(str(v).replace('$', '').replace('S', '').replace(',', ''))
    except ValueError:
        raise ValueError(f'{label} "{v}" is not a number')
    if not lo <= x <= hi:
        raise ValueError(f'{label} must be between {lo} and {hi}')
    return x


def branch_lookup(conn):
    out = {}
    for b in rows(conn.execute('SELECT id, name, code FROM branches')):
        out[b['name'].lower()] = b['id']
        if b['code']:
            out[b['code'].lower()] = b['id']
    return out


def import_employees(conn, text, commit):
    keys, recs = parse_csv(text, EMP_ALIASES)
    if 'name' not in keys or 'position' not in keys:
        raise ApiError(400, 'The team CSV needs at least "name" and "position" columns.')
    positions = {p['name'].lower(): p['id'] for p in rows(conn.execute('SELECT id, name FROM positions'))}
    branches = branch_lookup(conn)
    default_branch = first_branch(conn)
    existing = {r['name'].lower(): r['id'] for r in conn.execute('SELECT id, name FROM employees')}
    codes = {r['code'].lower(): r['id'] for r in conn.execute("SELECT id, code FROM employees WHERE code != ''")}
    new_positions, new_branches, report, seen = [], [], [], set()
    for n, r in recs:
        try:
            name = r.get('name', '')
            if not name:
                raise ValueError('name is empty')
            if len(name) > 80:
                raise ValueError('name is longer than 80 characters')
            if name.lower() in seen:
                raise ValueError('this name appears twice in the file')
            pos_name = r.get('position', '')
            if not pos_name:
                raise ValueError('position is empty')
            skill_names = [s.strip() for s in re.split(r'[;|/]', r.get('skills', '')) if s.strip()]
            for pn in [pos_name] + skill_names:
                if len(pn) > 40:
                    raise ValueError(f'position "{pn[:20]}…" is too long')
                if pn.lower() not in positions and pn.lower() not in [x.lower() for x in new_positions]:
                    new_positions.append(pn)
            branch_names = [b for b in [r.get('branch', '')] + [s.strip() for s in re.split(r'[;|/]', r.get('branches', ''))] if b]
            for bn in branch_names:
                if len(bn) > 60:
                    raise ValueError(f'branch "{bn[:20]}…" is too long')
                if bn.lower() not in branches and bn.lower() not in [x.lower() for x in new_branches]:
                    new_branches.append(bn)
            code = r.get('code', '')[:20]
            owner = codes.get(code.lower()) if code else None
            if owner and owner != existing.get(name.lower()):
                raise ValueError(f'staff code {code} belongs to someone else')
            etype = TYPE_MAP.get(norm_header(r.get('type', '')) or 'fulltime')
            if not etype:
                raise ValueError(f'type "{r.get("type")}" should be full-time, part-time or casual')
            dt, dm = TYPE_DEFAULTS[etype]
            rate = num_or(r.get('rate'), 0, 0, 1000, 'hourly rate')
            mx = num_or(r.get('max'), dm, 1, 84, 'max hours')
            target = num_or(r.get('target'), min(dt, mx), 0, 84, 'target hours')
            if target > mx:
                raise ValueError('target hours are above max hours')
            email = r.get('email', '')
            if email and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
                raise ValueError(f'email "{email}" does not look right')
            seen.add(name.lower())
            report.append({'row': n, 'status': 'update' if name.lower() in existing else 'new', 'name': name, 'position': pos_name,
                           '_data': (name, code, pos_name, skill_names, r.get('branch', ''), [s.strip() for s in re.split(r'[;|/]', r.get('branches', '')) if s.strip()],
                                     etype, rate, target, mx, r.get('phone', '')[:30], email[:120], r.get('notes', '')[:500])})
        except ValueError as e:
            report.append({'row': n, 'status': 'error', 'name': r.get('name', ''), 'message': str(e)})
    summary = {s: sum(1 for x in report if x['status'] == s) for s in ('new', 'update', 'error')}
    if commit and (summary['new'] or summary['update']):
        for i, pn in enumerate(new_positions):
            if pn.lower() not in positions:
                positions[pn.lower()] = conn.execute('INSERT INTO positions (name, department, color, sort) VALUES (?, ?, ?, ?)', (pn, '', PALETTE[i % len(PALETTE)], 50 + i)).lastrowid
        for i, bn in enumerate(new_branches):
            if bn.lower() not in branches:
                branches[bn.lower()] = conn.execute('INSERT INTO branches (name, code, address, color, sort, active) VALUES (?, ?, ?, ?, ?, 1)', (bn, '', '', PALETTE[i % len(PALETTE)], 50 + i)).lastrowid
        for x in report:
            if x['status'] == 'error':
                continue
            name, code, pos_name, skill_names, home, others, etype, rate, target, mx, phone, email, notes = x['_data']
            pid = positions[pos_name.lower()]
            bid = branches[home.lower()] if home else None
            if x['status'] == 'update':
                eid = existing[name.lower()]
                conn.execute('UPDATE employees SET position_id=?, employment_type=?, hourly_rate=?, target_hours=?, max_hours=?, phone=?, email=?, notes=?, active=1 WHERE id=?',
                             (pid, etype, rate, target, mx, phone, email, notes, eid))
                if code:
                    conn.execute('UPDATE employees SET code = ? WHERE id = ?', (code, eid))
                if bid:
                    conn.execute('UPDATE employees SET branch_id = ? WHERE id = ?', (bid, eid))
            else:
                eid = conn.execute('INSERT INTO employees (name, code, position_id, branch_id, employment_type, hourly_rate, target_hours, max_hours, phone, email, notes, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)',
                                   (name, code, pid, bid or default_branch, etype, rate, target, mx, phone, email, notes, now_iso())).lastrowid
            home_id = one(conn, 'SELECT branch_id FROM employees WHERE id = ?', (eid,))['branch_id']
            save_links(conn, eid, {pid} | {positions[s.lower()] for s in skill_names}, {branches[o.lower()] for o in others} - {home_id})
    for x in report:
        x.pop('_data', None)
    return {'rows': report, 'summary': summary, 'new_positions': new_positions, 'new_branches': new_branches}


def import_shifts(conn, text, commit):
    keys, recs = parse_csv(text, SHIFT_ALIASES)
    for k in ('date', 'start', 'end'):
        if k not in keys:
            raise ApiError(400, 'The shift CSV needs at least "date", "start" and "end" columns.')
    positions = {p['name'].lower(): p['id'] for p in rows(conn.execute('SELECT id, name FROM positions'))}
    branches = branch_lookup(conn)
    default_branch = first_branch(conn)
    emps = {r['name'].lower(): dict(r) for r in conn.execute('SELECT id, name, position_id, branch_id FROM employees')}
    existing = {(s['date'], s['start_time'], s['end_time'], s['employee_id']) for s in conn.execute('SELECT date, start_time, end_time, employee_id FROM shifts')}
    report, batch = [], set()
    for n, r in recs:
        try:
            d = parse_date_any(r.get('date'))
            st, en = parse_time_any(r.get('start')), parse_time_any(r.get('end'))
            if st == en:
                raise ValueError('start and end are the same')
            brk = int(num_or(r.get('break'), 0, 0, 240, 'break'))
            if brk >= rules.paid_minutes({'date': d, 'start_time': st, 'end_time': en, 'break_minutes': 0}):
                raise ValueError('break is longer than the shift')
            emp_name = r.get('employee', '')
            emp = emps.get(emp_name.lower()) if emp_name else None
            if emp_name and not emp:
                raise ValueError(f'no team member called "{emp_name}"')
            pos_name = r.get('position', '')
            if pos_name:
                if pos_name.lower() not in positions:
                    raise ValueError(f'no position called "{pos_name}"')
                pid = positions[pos_name.lower()]
            elif emp:
                pid = emp['position_id']
            else:
                raise ValueError('an open shift needs a position')
            bname = r.get('branch', '')
            if bname:
                if bname.lower() not in branches:
                    raise ValueError(f'no branch called "{bname}"')
                bid = branches[bname.lower()]
            else:
                bid = (emp or {}).get('branch_id') or default_branch
            key = (d, st, en, emp['id'] if emp else None)
            if key in existing or key in batch:
                report.append({'row': n, 'status': 'duplicate', 'name': emp_name or 'Open shift', 'message': f'{d} {st}–{en} already exists'})
                continue
            batch.add(key)
            report.append({'row': n, 'status': 'new', 'name': emp_name or 'Open shift', 'message': f'{d} {st}–{en}',
                           '_data': (d, st, en, brk, key[3], pid, bid, r.get('notes', '')[:300])})
        except ValueError as e:
            report.append({'row': n, 'status': 'error', 'name': r.get('employee', ''), 'message': str(e)})
    summary = {s: sum(1 for x in report if x['status'] == s) for s in ('new', 'duplicate', 'error')}
    if commit:
        now = now_iso()
        for x in report:
            if x['status'] == 'new':
                conn.execute("INSERT INTO shifts (date, start_time, end_time, break_minutes, employee_id, position_id, branch_id, notes, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'draft', ?, ?)",
                             (*x['_data'], now, now))
    for x in report:
        x.pop('_data', None)
    return {'rows': report, 'summary': summary}


def import_clock(conn, text, commit, source):
    """Clock-in/clock-out records from a time clock or payroll export."""
    keys, recs = parse_csv(text, CLOCK_ALIASES)
    if 'in' not in keys or ('employee' not in keys and 'code' not in keys):
        raise ApiError(400, 'The clock CSV needs an employee name or staff code column and a clock-in column.')
    by_name = {r['name'].lower(): dict(r) for r in conn.execute('SELECT id, name, branch_id FROM employees')}
    by_code = {r['code'].lower(): dict(r) for r in conn.execute("SELECT id, name, code, branch_id FROM employees WHERE code != ''")}
    branches = branch_lookup(conn)
    existing = {(r['employee_id'], r['clock_in']) for r in conn.execute('SELECT employee_id, clock_in FROM attendance')}
    report, batch = [], set()
    for n, r in recs:
        label = r.get('employee') or r.get('code') or ''
        try:
            emp = by_code.get((r.get('code') or '').lower()) if r.get('code') else None
            if not emp and r.get('employee'):
                emp = by_name.get(r['employee'].lower())
            if not emp:
                raise ValueError(f'no team member matches "{label}"')
            day = parse_date_any(r['date']) if r.get('date') else None
            in_day, in_t, _ = parse_stamp_any(r.get('in'), day)
            if not in_day:
                raise ValueError('no date: add a date column or put the date in the clock-in value')
            clock_in = f'{in_day} {in_t}'
            clock_out = None
            if r.get('out'):
                out_day, out_t, dated = parse_stamp_any(r['out'], in_day)
                if not dated and out_t <= in_t:
                    out_day = (date.fromisoformat(in_day) + timedelta(days=1)).isoformat()
                clock_out = f'{out_day} {out_t}'
                dur = rules.abs_dt(clock_out) - rules.abs_dt(clock_in)
                if dur <= 0:
                    raise ValueError('clock-out is before clock-in')
                if dur > 24 * 60:
                    raise ValueError('shift is longer than 24 hours')
            brk = int(num_or(r.get('break'), 0, 0, 240, 'break'))
            bname = (r.get('branch') or '').lower()
            if bname and bname not in branches:
                raise ValueError(f'no branch called "{r.get("branch")}"')
            bid = branches[bname] if bname else emp['branch_id']
            key = (emp['id'], clock_in)
            if key in existing or key in batch:
                report.append({'row': n, 'status': 'duplicate', 'name': emp['name'], 'message': f'{clock_in} already imported'})
                continue
            batch.add(key)
            report.append({'row': n, 'status': 'new', 'name': emp['name'], 'message': f"{clock_in} → {clock_out[11:] if clock_out else 'no clock-out'}",
                           '_data': (emp['id'], bid, in_day, clock_in, clock_out, brk)})
        except ValueError as e:
            report.append({'row': n, 'status': 'error', 'name': label, 'message': str(e)})
    summary = {s: sum(1 for x in report if x['status'] == s) for s in ('new', 'duplicate', 'error')}
    if commit:
        now = now_iso()
        for x in report:
            if x['status'] == 'new':
                conn.execute('INSERT INTO attendance (employee_id, branch_id, date, clock_in, clock_out, break_minutes, source, created_at) VALUES (?,?,?,?,?,?,?,?)',
                             (*x['_data'], source, now))
    for x in report:
        x.pop('_data', None)
    dates = [x['message'][:10] for x in report if x['status'] == 'new']
    return {'rows': report, 'summary': summary, 'from': min(dates) if dates else None, 'to': max(dates) if dates else None}


@route('POST', '/api/data/import', role='manager')
def api_import(req):
    kind = req.body.get('type')
    text = req.body.get('csv')
    if kind not in ('employees', 'shifts', 'clock'):
        raise ApiError(400, 'Import type must be employees, shifts or clock.')
    if kind != 'clock' and req.user['role'] != 'admin':
        raise ApiError(403, 'Only an admin can import team members or shifts.')
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, 'The CSV file is empty.')
    commit = bool(req.body.get('commit'))
    safety = snapshot(req.conn, 'pre-import') if commit else None
    if kind == 'clock':
        result = import_clock(req.conn, text, commit, v_str(req.body, 'filename', 'File name', 120) or 'CSV import')
    else:
        result = (import_employees if kind == 'employees' else import_shifts)(req.conn, text, commit)
    if commit:
        audit(req.conn, req.user, f'data.imported_{kind}', f"{result['summary']} (safety copy {safety})")
        result['safety_backup'] = safety
    return result


@route('POST', '/api/attendance/delete', role='manager')
def api_attendance_delete(req):
    start = v_date(req.body.get('from'), 'From')
    end = v_date(req.body.get('to'), 'To')
    if end < start:
        raise ApiError(400, '"To" must be on or after "From".')
    bid = q_branch(req, req.body.get('branch_id') or '')
    where, args = scope_sql(bid)
    safety = snapshot(req.conn, 'pre-clock-delete')
    cur = req.conn.execute(f'DELETE FROM attendance WHERE date BETWEEN ? AND ?{where}', (start, end, *args))
    audit(req.conn, req.user, 'attendance.deleted', f'{start}..{end}: {cur.rowcount} records (safety copy {safety})')
    return {'deleted': cur.rowcount, 'safety_backup': safety}


# ---------------------------------------------------------------- database setup and upgrades

def ensure_settings(conn):
    for key, (_k, default, _lo, _hi) in SETTINGS.items():
        conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, str(default)))


def ensure_branch_defaults(conn):
    """Every database has at least one branch, and every row that needs a branch has one."""
    if not conn.execute('SELECT COUNT(*) FROM branches').fetchone()[0]:
        conn.execute("INSERT INTO branches (name, code, address, color, sort, active) VALUES ('Main branch', 'MAIN', '', '#0e6b5c', 0, 1)")
    first = first_branch(conn)
    valid = 'SELECT id FROM branches'
    for t in ('employees', 'shifts', 'staffing_needs'):
        conn.execute(f'UPDATE {t} SET branch_id = ? WHERE branch_id IS NULL OR branch_id NOT IN ({valid})', (first,))
    conn.execute(f'UPDATE OR REPLACE forecasts SET branch_id = ? WHERE branch_id NOT IN ({valid})', (first,))


def migrate(conn):
    """Create missing tables and upgrade a version 1 database in place (idempotent)."""
    conn.commit()
    level = conn.isolation_level
    conn.isolation_level = None
    conn.execute('PRAGMA foreign_keys = OFF')
    try:
        conn.execute('BEGIN')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

        def cols(t):
            return {r[1] for r in conn.execute(f'PRAGMA table_info({t})')}

        if 'positions' in tables and 'department' not in cols('positions'):
            conn.execute("CREATE TABLE positions_v2 (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, department TEXT NOT NULL DEFAULT '', color TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO positions_v2 (id, name, department, color, sort) SELECT id, name, CASE area WHEN 'FOH' THEN 'Front of house' WHEN 'BOH' THEN 'Kitchen' ELSE COALESCE(area, '') END, color, sort FROM positions")
            conn.execute('DROP TABLE positions')
            conn.execute('ALTER TABLE positions_v2 RENAME TO positions')
        if 'shift_templates' in tables and 'department' not in cols('shift_templates'):
            conn.execute('ALTER TABLE shift_templates RENAME COLUMN area TO department')
            conn.execute("UPDATE shift_templates SET department = CASE department WHEN 'FOH' THEN 'Front of house' WHEN 'BOH' THEN 'Kitchen' ELSE department END")
        if 'forecasts' in tables and 'branch_id' not in cols('forecasts'):
            conn.execute("CREATE TABLE forecasts_v2 (branch_id INTEGER NOT NULL DEFAULT 0, date TEXT NOT NULL, sales REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', PRIMARY KEY (branch_id, date))")
            conn.execute('INSERT INTO forecasts_v2 (branch_id, date, sales, note) SELECT 0, date, sales, note FROM forecasts')
            conn.execute('DROP TABLE forecasts')
            conn.execute('ALTER TABLE forecasts_v2 RENAME TO forecasts')
        for stmt in TABLES_SQL:
            conn.execute(stmt)
        for table, col, ddl in (('employees', 'code', "TEXT NOT NULL DEFAULT ''"),
                                ('employees', 'branch_id', 'INTEGER REFERENCES branches(id) ON DELETE SET NULL'),
                                ('shifts', 'branch_id', 'INTEGER REFERENCES branches(id) ON DELETE SET NULL'),
                                ('staffing_needs', 'branch_id', 'INTEGER REFERENCES branches(id) ON DELETE CASCADE')):
            if col not in cols(table):
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}')
        for stmt in INDEXES_SQL:
            conn.execute(stmt)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.isolation_level = level
    ensure_settings(conn)
    ensure_branch_defaults(conn)
    conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
    conn.commit()


def init_db(seed=True):
    os.makedirs(os.path.dirname(CONFIG['db']) or '.', exist_ok=True)
    conn = db()
    try:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        has_data = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()[0]
        if has_data and version < SCHEMA_VERSION:
            name = snapshot(conn, f'pre-upgrade-v{version}')
            print(f'Upgrading the database to version {SCHEMA_VERSION}. Safety copy: backups/{name}', flush=True)
        migrate(conn)
        if seed and not conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
            sample_data.load(conn, today(), load_ctx, keep_users=False, industry='fnb', now=local_now())
            ensure_demo_logins(conn)
            audit(conn, None, 'data.seeded', 'Demo business and demo logins created')
        if has_data and version < SCHEMA_VERSION:
            audit(conn, None, 'data.upgraded', f'Database upgraded from version {version} to {SCHEMA_VERSION}')
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- HTTP

_csp_cache = {}


def index_csp(html):
    """Allow exactly the inline scripts in index.html, by hash."""
    key = hashlib.sha256(html).hexdigest()
    if key not in _csp_cache:
        hashes = [base64.b64encode(hashlib.sha256(m.group(1)).digest()).decode()
                  for m in re.finditer(rb'<script>(.*?)</script>', html, re.S)]
        script = ' '.join(f"'sha256-{h}'" for h in hashes) or "'none'"
        _csp_cache.clear()
        _csp_cache[key] = (f"default-src 'none'; script-src {script}; style-src 'unsafe-inline'; img-src 'self' data: blob:; "
                           f"connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
    return _csp_cache[key]


SYNC = None  # the hosted demo's shared database (demo_sync.DemoSync), when a Blob store is connected


class Handler(BaseHTTPRequestHandler):
    server_version = 'ShiftTable'
    sys_version = ''
    protocol_version = 'HTTP/1.1'
    held = None  # responses kept back until the hosted demo has saved (see dispatch)

    def log_message(self, fmt, *args):
        if not os.environ.get('SHIFTTABLE_QUIET'):
            super().log_message(fmt, *args)

    def do_GET(self):
        self.dispatch('GET')

    def do_HEAD(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def send(self, status, body, ctype, extra=None):
        if self.held is not None:
            self.held.append((status, body, ctype, list(extra or [])))
            return
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_json(self, status, obj, extra=None):
        self.send(status, json.dumps(obj, default=list).encode(), 'application/json; charset=utf-8',
                  [('Cache-Control', 'no-store')] + (extra or []))

    def session_user(self, conn):
        raw = self.headers.get('Cookie')
        if not raw:
            return None
        try:
            c = SimpleCookie(raw)
        except Exception:
            return None
        if COOKIE not in c or not c[COOKIE].value:
            return None
        return read_session_token(conn, c[COOKIE].value)

    def dispatch(self, method):
        if SYNC is None or not urlparse(self.path).path.startswith('/api/'):
            return self.route_request(method)
        # Hosted demo: run the request on the shared database and answer once any change
        # is saved. If another copy saved first, run it again on their newer data.
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if 0 < length <= MAX_BODY else b''
        try:
            c = SimpleCookie(self.headers.get('Cookie') or '')
            seen = c[demo_sync.COOKIE].value if demo_sync.COOKIE in c else ''
        except Exception:
            seen = ''
        with SYNC.lock:
            status = SYNC.before(method, seen)
            for _attempt in range(2):
                self.rfile, self.held = io.BytesIO(body), []
                self.route_request(method)
                try:
                    status = SYNC.after() or status
                    break
                except demo_sync.Conflict:
                    status = 'retried'
                    try:
                        SYNC.pull()
                    except demo_sync.BlobError as e:
                        demo_sync.log(e)
                except demo_sync.BlobError as e:
                    demo_sync.log(e)
                    status = 'offline'
                    break
            else:
                status = 'busy'
                self.held = [(409, json.dumps({'error': 'Someone else changed the demo at the same moment. Please try again.'}).encode(),
                              'application/json; charset=utf-8', [('Cache-Control', 'no-store')])]
            held, self.held = self.held, None
            extra = [('X-Demo-Sync', status)]
            cookie = SYNC.cookie(seen, self.headers.get('X-Forwarded-Proto') == 'https')
            if cookie:
                extra.append(('Set-Cookie', cookie))
        for st, b, ct, ex in held:
            self.send(st, b, ct, ex + extra)

    def route_request(self, method):
        url = urlparse(self.path)
        path = url.path
        if method == 'GET' and path in ('/', '/index.html'):
            try:
                with open(CONFIG['index'], 'rb') as f:
                    html = f.read()
            except OSError:
                return self.send(500, b'index.html is missing', 'text/plain; charset=utf-8')
            return self.send(200, html, 'text/html; charset=utf-8',
                             [('Content-Security-Policy', index_csp(html)), ('Cache-Control', 'no-cache')])
        if not path.startswith('/api/'):
            return self.send(404, b'Not found', 'text/plain; charset=utf-8')
        match = None
        for m, rx, fn, role in ROUTES:
            mt = rx.match(path)
            if mt and m == method:
                match = (fn, role, mt.groups())
                break
        if not match:
            return self.send_json(404, {'error': 'Not found.'})
        fn, role, params = match
        if method != 'GET' and self.headers.get(CSRF_HEADER) != '1':
            return self.send_json(403, {'error': 'Missing request header.'})
        body = {}
        if method in ('POST', 'PUT'):
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                return self.send_json(400, {'error': 'Bad Content-Length.'})
            if length > MAX_BODY:
                return self.send_json(413, {'error': 'That upload is too large (25 MB limit).'})
            raw = self.rfile.read(length) if length else b''
            if raw:
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    return self.send_json(400, {'error': 'The request body is not valid JSON.'})
                if not isinstance(body, dict):
                    return self.send_json(400, {'error': 'The request body must be a JSON object.'})
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        conn = db()
        try:
            user = self.session_user(conn)
            if role and not user:
                raise ApiError(401, 'Please sign in.')
            if role and RANK[user['role']] < RANK[role]:
                raise ApiError(403, "You don't have access to that.")
            req = Req(conn, user, params, query, body, self.client_address[0], self.headers.get('X-Forwarded-Proto') == 'https')
            result = fn(req)
            conn.commit()
            extra = [('Set-Cookie', c) for c in req.cookies]
            if isinstance(result, FileResponse):
                safe = re.sub(r'[^A-Za-z0-9._-]', '_', result.filename)
                return self.send(200, result.data, result.content_type,
                                 [('Content-Disposition', f'attachment; filename="{safe}"'), ('Cache-Control', 'no-store')] + extra)
            return self.send_json(200, result, extra)
        except ApiError as e:
            conn.rollback()
            return self.send_json(e.status, {'error': e.message, **e.extra})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            return self.send_json(400, {'error': f'That change clashes with existing data ({e}).'})
        except Exception:
            conn.rollback()
            traceback.print_exc()
            return self.send_json(500, {'error': 'Something went wrong on the server. Check the terminal for details.'})
        finally:
            conn.close()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def make_server(host, port):
    init_db()
    return ThreadingHTTPServer((host, port), Handler)


def serverless_setup():
    """For hosts without a permanent disk (Vercel): keep the database in /tmp. With a Blob
    store connected, every copy of the app shares one demo (see demo_sync); without one,
    each copy seeds its own whenever it starts."""
    global SYNC
    data_dir = os.environ.get('SHIFTTABLE_DATA_DIR', '/tmp/shifttable')
    os.makedirs(data_dir, exist_ok=True)
    CONFIG.update(db=os.path.join(data_dir, 'roster.db'), backups=os.path.join(data_dir, 'backups'), demo=True)
    token = os.environ.get('BLOB_READ_WRITE_TOKEN')
    if token:
        SYNC = demo_sync.DemoSync(demo_sync.BlobStore(token), CONFIG['db'], CONFIG['backups'], init_db)
        SYNC.start()
    else:
        init_db()


def main():
    ap = argparse.ArgumentParser(description='ShiftTable roster server')
    ap.add_argument('--host', default='127.0.0.1', help='127.0.0.1 (this computer only) or 0.0.0.0 (your Wi-Fi too)')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--db', help='path to the SQLite file (default: roster.db next to server.py)')
    args = ap.parse_args()
    if args.db:
        CONFIG['db'] = os.path.abspath(args.db)
        CONFIG['backups'] = os.path.join(os.path.dirname(CONFIG['db']), 'backups')
    httpd = make_server(args.host, args.port)
    print(f'ShiftTable is running: http://127.0.0.1:{args.port}', flush=True)
    if args.host == '0.0.0.0' and lan_ip():
        print(f'On the same Wi-Fi:     http://{lan_ip()}:{args.port}', flush=True)
    print(f'Database: {CONFIG["db"]}', flush=True)
    print('Press Ctrl+C to stop.', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
