"""Roster rule engine.

Pure functions over plain dicts, so everything here can be tested without a database.

A shift is a dict with: id, date (YYYY-MM-DD), start_time, end_time (HH:MM; an end at or
before the start means the shift runs past midnight), break_minutes, employee_id (None for
an open shift), position_id, branch_id, status.

A context (ctx) is a dict with: settings, employees {id: emp}, positions {id: pos},
branches {id: branch}, needs [..], shifts [..] (every branch, and a window wider than the
week being checked, so rest gaps, weekly hours and consecutive days see everything an
employee works), leave [..] (approved only), forecasts {(branch_id, date): sales},
events [..] (holidays, events and closures) and, for the actual-vs-rostered report,
attendance [..] (clock records).

Labour-law checks always look at a person's shifts across all branches. Coverage, labour
cost and open shifts are counted for the branch being viewed.
"""
from collections import defaultdict
from datetime import date, timedelta

DAY = 1440
DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


# ---------------------------------------------------------------- time helpers

def mins(t):
    h, m = t.split(':')
    return int(h) * 60 + int(m)


def span(s):
    """Absolute (start, end) in minutes for a shift-like dict with date/start/end."""
    base = date.fromisoformat(s['date']).toordinal() * DAY
    start = base + mins(s['start_time'])
    end = base + mins(s['end_time'])
    if end <= start:
        end += DAY
    return start, end


def abs_dt(stamp):
    """'YYYY-MM-DD HH:MM' to absolute minutes."""
    d, t = stamp.split(' ')
    return date.fromisoformat(d).toordinal() * DAY + mins(t[:5])


def paid_minutes(s):
    a, b = span(s)
    return max(0, b - a - int(s.get('break_minutes') or 0))


def paid_hours(s):
    return paid_minutes(s) / 60


def week_dates(week_start):
    d = date.fromisoformat(week_start)
    return [(d + timedelta(days=i)).isoformat() for i in range(7)]


def monday_of(d):
    return d - timedelta(days=d.weekday())


def is_weekend(ds):
    return date.fromisoformat(ds).weekday() >= 5


def is_closing(s):
    a, b = span(s)
    return b // DAY > a // DAY or b % DAY >= 22 * 60


def is_opening(s):
    return mins(s['start_time']) < 9 * 60


def overlap(a1, b1, a2, b2):
    return max(0, min(b1, b2) - max(a1, a2))


def fmt_hours(h):
    return f'{h:.1f}'.rstrip('0').rstrip('.') + 'h'


def fmt_clock(abs_min):
    m = abs_min % DAY
    return f'{m // 60:02d}:{m % 60:02d}'


def day_label(ds):
    d = date.fromisoformat(ds)
    return f'{DAY_NAMES[d.weekday()]} {d.day}'


# ---------------------------------------------------------------- people, branches, calendar

def skills_of(emp):
    return emp.get('skills') or ({emp['position_id']} if emp.get('position_id') else set())


def branches_of(emp):
    return emp.get('branches') or ({emp['branch_id']} if emp.get('branch_id') else set())


def branch_name(ctx, bid):
    return (ctx.get('branches') or {}).get(bid, {}).get('name', 'this branch')


def multi_branch(ctx):
    return len(ctx.get('branches') or {}) > 1


def event_applies(e, ds, branch_id):
    return e['start_date'] <= ds <= e['end_date'] and (e.get('branch_id') is None or e.get('branch_id') == branch_id)


def holiday(ctx, ds, branch_id):
    return next((e for e in ctx.get('events', ()) if e['kind'] == 'holiday' and event_applies(e, ds, branch_id)), None)


def closure(ctx, ds, branch_id):
    return next((e for e in ctx.get('events', ()) if e['kind'] == 'closed' and event_applies(e, ds, branch_id)), None)


def on_leave(ctx, emp_id, ds):
    for lv in ctx.get('leave', []):
        if lv['employee_id'] == emp_id and lv['start_date'] <= ds <= lv['end_date']:
            return lv
    return None


def availability_problem(emp, s):
    """None if the shift sits inside the person's availability, else a reason."""
    wd = date.fromisoformat(s['date']).weekday()
    av = (emp.get('availability') or {}).get(wd)
    if not av:
        return None
    if not av.get('available'):
        return f'Unavailable on {DAY_NAMES[wd]}'
    if av.get('start_time') and av.get('end_time'):
        ws, we = span({'date': s['date'], 'start_time': av['start_time'], 'end_time': av['end_time']})
        a, b = span(s)
        if a < ws or b > we:
            return f"Outside availability ({av['start_time']}–{av['end_time']})"
    return None


def group_by_employee(shifts):
    by = defaultdict(list)
    for s in shifts:
        if s.get('employee_id'):
            by[s['employee_id']].append(s)
    for lst in by.values():
        lst.sort(key=span)
    return by


def consecutive_run(dates_worked, ds):
    """Length of the run of consecutive worked days that contains ds (ds counted as worked)."""
    d = date.fromisoformat(ds)
    run = 1
    x = d - timedelta(days=1)
    while x.isoformat() in dates_worked:
        run += 1
        x -= timedelta(days=1)
    x = d + timedelta(days=1)
    while x.isoformat() in dates_worked:
        run += 1
        x += timedelta(days=1)
    return run


def rest_neighbours(lst, s):
    """(end of the latest earlier-day shift, start of the earliest later-day shift)."""
    prev_end = next_start = None
    for o in lst:
        if o is s or o.get('id') == s.get('id'):
            continue
        if o['date'] < s['date']:
            e = span(o)[1]
            prev_end = e if prev_end is None else max(prev_end, e)
        elif o['date'] > s['date']:
            st = span(o)[0]
            next_start = st if next_start is None else min(next_start, st)
    return prev_end, next_start


def item_costs(ctx, emp, items):
    """Cost and overtime hours of each item (a shift or a clock record) for one person.

    Items are in time order and carry date, hours and branch_id. Overtime is the part of
    each Monday-to-Sunday week above the weekly threshold, paid at the overtime multiplier.
    Public-holiday hours are paid at the holiday multiplier.
    """
    st = ctx['settings']
    rate = emp.get('hourly_rate') or 0
    th = st['weekly_ot_threshold']
    acc = defaultdict(float)
    out = []
    for it in items:
        wk = monday_of(date.fromisoformat(it['date']))
        h = it['hours']
        before = acc[wk]
        ot = max(0.0, before + h - max(th, before))
        acc[wk] = before + h
        mult = st.get('holiday_pay_multiplier', 1) if holiday(ctx, it['date'], it.get('branch_id')) else 1
        out.append((h * rate * mult + ot * rate * (st['ot_multiplier'] - 1), ot))
    return out


def shift_items(lst):
    return [{'date': s['date'], 'hours': paid_hours(s), 'branch_id': s.get('branch_id')} for s in lst]


# ---------------------------------------------------------------- coverage

def needs_for(ctx, ds, branch_id):
    wd = date.fromisoformat(ds).weekday()
    return [n for n in ctx['needs'] if n['weekday'] == wd and n.get('branch_id') == branch_id]


def match_needs(needs, day_shifts, ds):
    """Match one branch's shifts for a day to that weekday's staffing needs.

    A shift counts toward a need when it has the same position and overlaps at least half
    of the need's window. Each shift fills at most one need. Assigned shifts are matched
    before open ones so 'staffed' is as high as it can be.
    """
    out, used = [], set()
    for n in sorted(needs, key=lambda n: (mins(n['start_time']), n['position_id'], n['id'])):
        ns, ne = span({'date': ds, 'start_time': n['start_time'], 'end_time': n['end_time']})
        dur = ne - ns
        cands = []
        for s in day_shifts:
            if s['id'] in used or s.get('position_id') != n['position_id']:
                continue
            a, b = span(s)
            ov = overlap(a, b, ns, ne)
            if ov > 0 and ov * 2 >= dur:
                cands.append((0 if s.get('employee_id') else 1, -ov, s['id']))
        cands.sort()
        take = cands[:n['headcount']]
        for c in take:
            used.add(c[2])
        out.append({
            'need_id': n['id'], 'branch_id': n.get('branch_id'), 'position_id': n['position_id'], 'label': n.get('label') or '',
            'start_time': n['start_time'], 'end_time': n['end_time'],
            'headcount': n['headcount'], 'matched': len(take), 'staffed': sum(1 for c in take if c[0] == 0),
            'shift_ids': [c[2] for c in take],
        })
    return out


def scope_branches(ctx, branch_id):
    if branch_id:
        return [branch_id]
    return sorted(b for b, v in (ctx.get('branches') or {}).items() if v.get('active', 1))


# ---------------------------------------------------------------- week evaluation

def weekly_hours(shifts, emp_id, dates):
    ds = set(dates)
    return sum(paid_hours(s) for s in shifts if s.get('employee_id') == emp_id and s['date'] in ds)


def evaluate(ctx, week_start, branch_id=None):
    """Check a week for one branch (or all). Returns issues, per-shift cost, and summaries."""
    st = ctx['settings']
    emps = ctx['employees']
    pos = ctx['positions']
    days = week_dates(week_start)
    wk = set(days)
    multi = multi_branch(ctx) and branch_id is None
    at = (lambda b: f' at {branch_name(ctx, b)}') if multi else (lambda b: '')
    in_branch = (lambda s: True) if branch_id is None else (lambda s: s.get('branch_id') == branch_id)
    shifts = ctx['shifts']
    scope = [s for s in shifts if s['date'] in wk and in_branch(s)]
    by_emp = group_by_employee(shifts)
    issues = []

    def add(severity, code, message, **kw):
        issues.append({'severity': severity, 'code': code, 'message': message, **kw})

    min_rest = st['min_rest_hours'] * 60
    ot_threshold = st['weekly_ot_threshold']
    shift_cost = {}
    emp_rows = []

    for eid, emp in emps.items():
        lst = by_emp.get(eid, [])
        week_all = [s for s in lst if s['date'] in wk]
        mine = [s for s in week_all if in_branch(s)]
        belongs = branch_id is None or branch_id in branches_of(emp)
        if not mine and not (emp.get('active') and belongs):
            continue
        name = emp['name']
        for s, (c, _ot) in zip(week_all, item_costs(ctx, emp, shift_items(week_all))):
            shift_cost[s['id']] = round(c, 2)
        mine_ids = {s['id'] for s in mine}
        for s in mine:
            a, b = span(s)
            sid = s['id']
            where = at(s.get('branch_id'))
            if not emp.get('active'):
                add('error', 'inactive', f'{name} is archived but still rostered', shift_id=sid, employee_id=eid, date=s['date'])
            for o in lst:
                if o['id'] != sid and (o['id'] not in mine_ids or o['id'] > sid) and overlap(a, b, *span(o)) > 0:
                    other = f" at {branch_name(ctx, o.get('branch_id'))}" if o.get('branch_id') != s.get('branch_id') else ''
                    add('error', 'overlap', f"{name} is double-booked on {day_label(s['date'])} ({s['start_time']}–{s['end_time']} and {o['start_time']}–{o['end_time']}{other})",
                        shift_id=sid, employee_id=eid, date=s['date'], related_id=o['id'])
            lv = on_leave(ctx, eid, s['date'])
            if lv:
                add('error', 'leave', f"{name} is on approved {lv['kind'].lower()} on {day_label(s['date'])}", shift_id=sid, employee_id=eid, date=s['date'])
            cl = closure(ctx, s['date'], s.get('branch_id'))
            if cl:
                add('warn', 'closed', f"{branch_name(ctx, s.get('branch_id'))} is closed on {day_label(s['date'])} ({cl['name']}) but {name} is rostered",
                    shift_id=sid, employee_id=eid, date=s['date'])
            why = availability_problem(emp, s)
            if why:
                add('warn', 'availability', f"{name}: {why.lower()} ({day_label(s['date'])})", shift_id=sid, employee_id=eid, date=s['date'])
            if s.get('position_id') and s['position_id'] not in skills_of(emp):
                pname = pos.get(s['position_id'], {}).get('name', 'this position')
                add('warn', 'skill', f"{name} is not trained as {pname} ({day_label(s['date'])})", shift_id=sid, employee_id=eid, date=s['date'])
            if s.get('branch_id') and branches_of(emp) and s['branch_id'] not in branches_of(emp):
                add('warn', 'branch', f"{name} is rostered at {branch_name(ctx, s['branch_id'])} but isn't set up to work there ({day_label(s['date'])})",
                    shift_id=sid, employee_id=eid, date=s['date'])
            span_min = b - a
            if span_min > st['break_after_hours'] * 60 and int(s.get('break_minutes') or 0) < st['min_break_minutes']:
                add('warn', 'break', f"{name}: {fmt_hours(span_min / 60)} shift on {day_label(s['date'])}{where} needs a {st['min_break_minutes']}-min break",
                    shift_id=sid, employee_id=eid, date=s['date'])
            prev_end, _ = rest_neighbours(lst, s)
            if prev_end is not None and 0 <= a - prev_end < min_rest:
                add('warn', 'rest', f"{name} gets only {fmt_hours((a - prev_end) / 60)} rest before {day_label(s['date'])} {s['start_time']} (previous shift ends {fmt_clock(prev_end)})",
                    shift_id=sid, employee_id=eid, date=s['date'])
        per_day = defaultdict(float)
        for s in week_all:
            per_day[s['date']] += paid_hours(s)
        hours = sum(per_day.values())
        ot = max(0.0, hours - ot_threshold)
        if mine:
            for ds, h in per_day.items():
                if h > st['max_daily_hours'] and any(s['date'] == ds for s in mine):
                    first = next(s for s in mine if s['date'] == ds)
                    add('error', 'daily', f"{name} works {fmt_hours(h)} on {day_label(ds)} (limit {fmt_hours(st['max_daily_hours'])})",
                        shift_id=first['id'], employee_id=eid, date=ds)
            if hours > emp['max_hours'] + 1e-9:
                add('warn', 'max_hours', f"{name} is rostered {fmt_hours(hours)}, above their {fmt_hours(emp['max_hours'])} maximum", employee_id=eid)
            if emp.get('employment_type') == 'part_time' and hours > st['part_time_max_hours'] + 1e-9:
                add('warn', 'part_time', f"{name} is part-time but rostered {fmt_hours(hours)} (part-time is under {fmt_hours(st['part_time_max_hours'])})", employee_id=eid)
            if ot > 0:
                add('info', 'overtime', f"{name} has {fmt_hours(ot)} overtime this week", employee_id=eid)
            if len(per_day) == 7:
                add('error', 'rest_day', f'{name} has no rest day this week', employee_id=eid)
            worked_all = {s['date'] for s in lst}
            runs = [consecutive_run(worked_all, ds) for ds in per_day]
            if runs and max(runs) > st['max_consecutive_days']:
                add('warn', 'consecutive', f"{name} works {max(runs)} days in a row (limit {st['max_consecutive_days']})", employee_id=eid)
            month_ot = monthly_overtime(ctx, eid, week_start)
            if month_ot > st['max_monthly_ot'] + 1e-9:
                add('error', 'monthly_ot', f"{name} reaches {fmt_hours(month_ot)} overtime this month (limit {fmt_hours(st['max_monthly_ot'])})", employee_id=eid)
        emp_rows.append({
            'employee_id': eid, 'hours': round(hours, 2), 'branch_hours': round(sum(paid_hours(s) for s in mine), 2),
            'cost': round(sum(shift_cost[s['id']] for s in mine), 2), 'ot_hours': round(ot, 2),
            'shifts': len(mine), 'elsewhere': len(week_all) - len(mine), 'days': len(per_day),
            'weekend': sum(1 for s in mine if is_weekend(s['date'])),
            'closing': sum(1 for s in mine if is_closing(s)),
            'opening': sum(1 for s in mine if is_opening(s)),
            'target': emp.get('target_hours') or 0, 'max': emp.get('max_hours') or 0,
        })

    branches = scope_branches(ctx, branch_id)
    day_rows = []
    for ds in days:
        todays = [s for s in scope if s['date'] == ds]
        for s in todays:
            if not s.get('employee_id'):
                pname = pos.get(s.get('position_id'), {}).get('name', 'Open')
                add('warn', 'open', f"Open {pname} shift {day_label(ds)} {s['start_time']}–{s['end_time']}{at(s.get('branch_id'))} has nobody assigned", shift_id=s['id'], date=ds)
        matches = []
        closed = []
        for b in branches:
            if closure(ctx, ds, b):
                closed.append(b)
                continue
            for m in match_needs(needs_for(ctx, ds, b), [s for s in todays if s.get('branch_id') == b], ds):
                matches.append(m)
                missing = m['headcount'] - m['matched']
                if missing > 0:
                    pname = pos.get(m['position_id'], {}).get('name', '?')
                    add('warn', 'coverage', f"{day_label(ds)}: {missing} more {pname} needed {m['start_time']}–{m['end_time']}{at(b)}", date=ds, need_id=m['need_id'])
        cost = sum(shift_cost.get(s['id'], 0) for s in todays)
        hours = sum(paid_hours(s) for s in todays if s.get('employee_id'))
        sales = sum(ctx['forecasts'].get((b, ds)) or 0 for b in branches)
        pct = round(cost / sales * 100, 1) if sales else None
        if pct is not None and pct > st['target_labour_pct']:
            add('warn', 'labour', f"{day_label(ds)}: labour is {pct}% of forecast (target {st['target_labour_pct']:g}%)", date=ds)
        need_total = sum(m['headcount'] for m in matches)
        staffed = sum(m['staffed'] for m in matches)
        day_rows.append({
            'date': ds, 'hours': round(hours, 2), 'cost': round(cost, 2), 'sales': sales, 'labour_pct': pct,
            'need_total': need_total, 'staffed': staffed, 'shifts': len(todays),
            'open': sum(1 for s in todays if not s.get('employee_id')),
            'drafts': sum(1 for s in todays if s.get('status') == 'draft'),
            'holiday': any(holiday(ctx, ds, b) for b in branches), 'closed_branches': closed,
            'coverage': matches,
        })

    total_cost = sum(r['cost'] for r in day_rows)
    total_sales = sum(r['sales'] for r in day_rows)
    need_total = sum(r['need_total'] for r in day_rows)
    totals = {
        'hours': round(sum(r['hours'] for r in day_rows), 2),
        'cost': round(total_cost, 2),
        'sales': total_sales,
        'labour_pct': round(total_cost / total_sales * 100, 1) if total_sales else None,
        'open': sum(r['open'] for r in day_rows),
        'shifts': len(scope),
        'people': len({s['employee_id'] for s in scope if s.get('employee_id')}),
        'drafts': sum(1 for s in scope if s.get('status') == 'draft'),
        'published': sum(1 for s in scope if s.get('status') == 'published'),
        'errors': sum(1 for i in issues if i['severity'] == 'error'),
        'warnings': sum(1 for i in issues if i['severity'] == 'warn'),
        'need_total': need_total,
        'staffed': sum(r['staffed'] for r in day_rows),
        'coverage_pct': round(sum(r['staffed'] for r in day_rows) / need_total * 100) if need_total else None,
    }
    order = {'error': 0, 'warn': 1, 'info': 2}
    issues.sort(key=lambda i: (order[i['severity']], i.get('date') or '', i['message']))
    for n, i in enumerate(issues):
        i['id'] = n + 1
    return {'issues': issues, 'shift_cost': shift_cost, 'employees': emp_rows, 'days': day_rows, 'totals': totals}


def monthly_overtime(ctx, emp_id, week_start):
    """Overtime summed over the weeks that start in the same month as week_start."""
    ws = date.fromisoformat(week_start)
    first = ws.replace(day=1)
    monday = first + timedelta(days=(7 - first.weekday()) % 7)
    total = 0.0
    th = ctx['settings']['weekly_ot_threshold']
    while monday.month == ws.month and monday <= ws:
        h = weekly_hours(ctx['shifts'], emp_id, week_dates(monday.isoformat()))
        total += max(0.0, h - th)
        monday += timedelta(days=7)
    return total


# ---------------------------------------------------------------- candidates and auto-fill

def check_candidate(ctx, emp, slot, by_emp, ignore_id=None, one_per_day=False, strict_branch=False):
    """Why this person cannot take this shift (empty list = they can), plus facts for ranking."""
    st = ctx['settings']
    eid = emp['id']
    reasons, notes = [], []
    if not emp.get('active'):
        reasons.append('Archived')
    if slot.get('position_id') and slot['position_id'] not in skills_of(emp):
        reasons.append(f"Not trained as {ctx['positions'].get(slot['position_id'], {}).get('name', 'this position')}")
    other_branch = bool(slot.get('branch_id') and branches_of(emp) and slot['branch_id'] not in branches_of(emp))
    if other_branch:
        home = branch_name(ctx, emp.get('branch_id'))
        if strict_branch:
            reasons.append(f"Not set up for {branch_name(ctx, slot['branch_id'])}")
        else:
            notes.append(f'Usually at {home}')
    cl = closure(ctx, slot['date'], slot.get('branch_id'))
    if cl:
        reasons.append(f"Branch closed ({cl['name']})")
    lv = on_leave(ctx, eid, slot['date'])
    if lv:
        reasons.append(f"On approved {lv['kind'].lower()}")
    why = availability_problem(emp, slot)
    if why:
        reasons.append(why)
    lst = [s for s in by_emp.get(eid, []) if s['id'] != ignore_id and s['id'] != slot.get('id')]
    a, b = span(slot)
    for s in lst:
        if overlap(a, b, *span(s)) > 0:
            where = f" at {branch_name(ctx, s.get('branch_id'))}" if s.get('branch_id') != slot.get('branch_id') else ''
            reasons.append(f"Already working {s['start_time']}–{s['end_time']}{where}")
            break
    same_day = [s for s in lst if s['date'] == slot['date']]
    if one_per_day and same_day:
        reasons.append('Already rostered that day')
    h = paid_hours(slot)
    day_h = sum(paid_hours(s) for s in same_day) + h
    if day_h > st['max_daily_hours'] + 1e-9:
        reasons.append(f"Would work {fmt_hours(day_h)} that day")
    prev_end, next_start = rest_neighbours(lst, slot)
    min_rest = st['min_rest_hours'] * 60
    if prev_end is not None and a - prev_end < min_rest:
        reasons.append(f"Only {fmt_hours(max(0, a - prev_end) / 60)} rest after previous shift")
    if next_start is not None and next_start - b < min_rest:
        reasons.append(f"Only {fmt_hours(max(0, next_start - b) / 60)} rest before next shift")
    ws = monday_of(date.fromisoformat(slot['date'])).isoformat()
    wdays = set(week_dates(ws))
    week_list = [s for s in lst if s['date'] in wdays]
    week_h = sum(paid_hours(s) for s in week_list)
    if week_h + h > emp['max_hours'] + 1e-9:
        reasons.append(f"Would reach {fmt_hours(week_h + h)} (max {fmt_hours(emp['max_hours'])})")
    days_worked = {s['date'] for s in week_list} | {slot['date']}
    if len(days_worked) == 7:
        reasons.append('No rest day left this week')
    worked_all = {s['date'] for s in lst}
    run = consecutive_run(worked_all, slot['date'])
    if run > st['max_consecutive_days']:
        reasons.append(f'{run} days in a row')
    prev_day = (date.fromisoformat(slot['date']) - timedelta(days=1)).isoformat()
    info = {
        'week_hours': round(week_h, 2), 'week_hours_after': round(week_h + h, 2),
        'overtime_after': round(max(0.0, week_h + h - st['weekly_ot_threshold']), 2),
        'weekend': sum(1 for s in week_list if is_weekend(s['date'])),
        'closing': sum(1 for s in week_list if is_closing(s)),
        'primary': slot.get('position_id') == emp.get('position_id'),
        'other_branch': other_branch,
        'away': bool(slot.get('branch_id') and emp.get('branch_id') and slot['branch_id'] != emp['branch_id']),
        'pattern': any(s['date'] == prev_day and s['start_time'] == slot['start_time'] for s in lst),
        'notes': notes,
    }
    return reasons, info


def score(emp, slot, info):
    """Lower is better. Fill people up to their usual hours first, keep staff at their home
    branch (people who can float are used when the home team runs out), repeat yesterday's
    start time where possible (steady patterns), share out weekends and closes, then prefer
    the person's main position and lower cost."""
    h = paid_hours(slot)
    target = emp.get('target_hours') or 0
    s = 0.0
    if info['week_hours'] + h <= target + 1e-9:
        s -= 20 + (target - info['week_hours'])
    else:
        s += (info['week_hours'] + h - target) * 4
    if info['primary']:
        s -= 6
    if info.get('other_branch'):
        s += 8
    if info.get('away'):
        s += 25
    if info.get('pattern'):
        s -= 3
    if is_weekend(slot['date']):
        s += 3 * info['weekend']
    if is_closing(slot):
        s += 2 * info['closing']
    s += (emp.get('hourly_rate') or 0) * 0.2
    return (round(s, 4), emp['id'])


def rank_candidates(ctx, slot, ignore_id=None):
    """Everyone, best first; people who can't take the shift go last with their reasons.
    Staff from other branches are included (borrowing is allowed) but ranked lower."""
    by_emp = group_by_employee(ctx['shifts'])
    rows = []
    for emp in ctx['employees'].values():
        if not emp.get('active'):
            continue
        reasons, info = check_candidate(ctx, emp, slot, by_emp, ignore_id=ignore_id)
        rows.append({'employee_id': emp['id'], 'name': emp['name'], 'ok': not reasons, 'reasons': reasons,
                     'score': score(emp, slot, info), **info})
    rows.sort(key=lambda r: (not r['ok'], r['score']))
    for r in rows:
        r['score'] = r['score'][0]
    return rows


def autofill(ctx, week_start, from_date=None, branch_id=None):
    """Create the shifts that staffing needs call for and assign open shifts.

    Works on one branch, or every active branch when branch_id is None. Closed days are
    skipped. Returns (new_shifts, assignments): new_shifts have negative temporary ids
    (employee_id may be None if nobody fits); assignments maps existing open shift ids to
    the person chosen. Existing assigned shifts are never moved.
    """
    days = [d for d in week_dates(week_start) if not from_date or d >= from_date]
    branch_ids = scope_branches(ctx, branch_id)
    shifts = [dict(s) for s in ctx['shifts']]
    new, tmp = [], -1
    for b in branch_ids:
        for ds in days:
            if closure(ctx, ds, b):
                continue
            todays = [s for s in shifts if s['date'] == ds and s.get('branch_id') == b]
            day_needs = needs_for(ctx, ds, b)
            need_by_id = {n['id']: n for n in day_needs}
            for m in match_needs(day_needs, todays, ds):
                n = need_by_id[m['need_id']]
                for _ in range(m['headcount'] - m['matched']):
                    s = {'id': tmp, 'date': ds, 'start_time': n['start_time'], 'end_time': n['end_time'],
                         'break_minutes': n.get('break_minutes') or 0, 'position_id': n['position_id'], 'branch_id': b,
                         'employee_id': None, 'status': 'draft', 'notes': ''}
                    tmp -= 1
                    shifts.append(s)
                    new.append(s)
    work = {s['id']: s for s in shifts}
    ctx2 = dict(ctx, shifts=shifts)
    by_emp = group_by_employee(shifts)
    active = [e for e in ctx['employees'].values() if e.get('active')]
    slots = [s for s in shifts if not s.get('employee_id') and s['date'] in days and s.get('branch_id') in branch_ids
             and not closure(ctx, s['date'], s.get('branch_id'))]

    def eligible(slot):
        return sum(1 for e in active if not check_candidate(ctx2, e, slot, by_emp, one_per_day=True, strict_branch=True)[0])

    slots.sort(key=lambda s: (eligible(s), s['date'], mins(s['start_time']), s['id']))
    for slot in slots:
        best = None
        for emp in active:
            reasons, info = check_candidate(ctx2, emp, slot, by_emp, one_per_day=True, strict_branch=True)
            if reasons:
                continue
            sc = score(emp, slot, info)
            if best is None or sc < best[0]:
                best = (sc, emp['id'])
        if best:
            slot['employee_id'] = best[1]
            by_emp[best[1]].append(slot)
            by_emp[best[1]].sort(key=span)
    _repair(ctx2, slots, active, by_emp)
    assignments = {s['id']: s['employee_id'] for s in slots if s['id'] > 0 and s.get('employee_id')}
    return [work[s['id']] for s in new], assignments


def _repair(ctx, slots, active, by_emp):
    """Second pass for shifts the greedy pass left open.

    For an open shift S, find someone X who could take S if one of the shifts this run gave
    them (Y, within a day of S) went to somebody else Z instead. Only shifts assigned in
    this run are moved, never ones a manager placed.
    """
    movable = {id(s) for s in slots}
    for slot in [s for s in slots if not s.get('employee_id')]:
        sd = date.fromisoformat(slot['date'])
        for x in active:
            if slot.get('employee_id'):
                break
            if check_candidate(ctx, x, slot, {}, one_per_day=True, strict_branch=True)[0]:
                continue  # skills, branch, leave, availability or hours rule them out on their own
            for y in list(by_emp.get(x['id'], [])):
                if id(y) not in movable or abs((date.fromisoformat(y['date']) - sd).days) > 1:
                    continue
                by_emp[x['id']].remove(y)
                if check_candidate(ctx, x, slot, by_emp, one_per_day=True, strict_branch=True)[0]:
                    by_emp[x['id']].append(y)
                    by_emp[x['id']].sort(key=span)
                    continue
                by_emp[x['id']].append(slot)
                y['employee_id'] = None
                best = None
                for z in active:
                    if z['id'] == x['id']:
                        continue
                    reasons, info = check_candidate(ctx, z, y, by_emp, one_per_day=True, strict_branch=True)
                    if not reasons:
                        sc = score(z, y, info)
                        if best is None or sc < best[0]:
                            best = (sc, z['id'])
                if best:
                    slot['employee_id'] = x['id']
                    y['employee_id'] = best[1]
                    by_emp[best[1]].append(y)
                    by_emp[best[1]].sort(key=span)
                    by_emp[x['id']].sort(key=span)
                    break
                by_emp[x['id']].remove(slot)
                y['employee_id'] = x['id']
                by_emp[x['id']].append(y)
                by_emp[x['id']].sort(key=span)


# ---------------------------------------------------------------- reports

def summarize_range(ctx, start, end, branch_id=None, employee_ids=None):
    """Hours, cost, overtime and fairness per person plus totals per day for [start, end].

    ctx['shifts'] must cover whole weeks around the range (for overtime) at every branch.
    """
    st = ctx['settings']
    emps = ctx['employees']
    by_emp_all = group_by_employee(ctx['shifts'])
    min_rest = st['min_rest_hours'] * 60
    per_day = defaultdict(lambda: {'hours': 0.0, 'cost': 0.0})
    rows = []

    def in_scope(s):
        return start <= s['date'] <= end and (branch_id is None or s.get('branch_id') == branch_id)

    for eid, emp in emps.items():
        if employee_ids and eid not in employee_ids:
            continue
        lst = by_emp_all.get(eid, [])
        r = {'employee_id': eid, 'name': emp['name'], 'shifts': 0, 'hours': 0.0, 'ot_hours': 0.0, 'cost': 0.0,
             'weekend': 0, 'closing': 0, 'opening': 0, 'clopens': 0, 'holiday_shifts': 0, 'target': emp.get('target_hours') or 0}
        for s, (c, ot) in zip(lst, item_costs(ctx, emp, shift_items(lst))):
            if not in_scope(s):
                continue
            h = paid_hours(s)
            r['shifts'] += 1
            r['hours'] += h
            r['ot_hours'] += ot
            r['cost'] += c
            r['weekend'] += is_weekend(s['date'])
            r['closing'] += is_closing(s)
            r['opening'] += is_opening(s)
            r['holiday_shifts'] += bool(holiday(ctx, s['date'], s.get('branch_id')))
            prev_end, _ = rest_neighbours(lst, s)
            if prev_end is not None and span(s)[0] - prev_end < min_rest:
                r['clopens'] += 1
            per_day[s['date']]['hours'] += h
            per_day[s['date']]['cost'] += c
        belongs = branch_id is None or branch_id in branches_of(emp)
        if r['shifts'] or employee_ids or (emp.get('active') and belongs):
            rows.append(r)
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    n_weeks = max(1, ((d1 - d0).days + 1) / 7)
    for r in rows:
        r['avg_week_hours'] = round(r['hours'] / n_weeks, 1)
        for k in ('hours', 'ot_hours', 'cost'):
            r[k] = round(r[k], 2)
    rows.sort(key=lambda r: r['name'])
    branches = scope_branches(ctx, branch_id)
    day_rows = []
    d = d0
    while d <= d1:
        ds = d.isoformat()
        sales = sum(ctx['forecasts'].get((b, ds)) or 0 for b in branches)
        c = round(per_day[ds]['cost'], 2)
        day_rows.append({'date': ds, 'hours': round(per_day[ds]['hours'], 2), 'cost': c, 'sales': sales,
                         'labour_pct': round(c / sales * 100, 1) if sales else None})
        d += timedelta(days=1)
    cost = sum(r['cost'] for r in day_rows)
    sales = sum(r['sales'] for r in day_rows)
    totals = {'hours': round(sum(r['hours'] for r in day_rows), 2), 'cost': round(cost, 2), 'sales': sales,
              'labour_pct': round(cost / sales * 100, 1) if sales and not employee_ids else None,
              'ot_hours': round(sum(r['ot_hours'] for r in rows), 2),
              'shifts': sum(r['shifts'] for r in rows), 'clopens': sum(r['clopens'] for r in rows)}
    return {'employees': rows, 'days': day_rows, 'totals': totals}


def compare_actual(ctx, start, end, now_min, branch_id=None, employee_ids=None):
    """Rostered shifts against clock records for [start, end].

    Each record is matched to at most one shift of the same person (largest overlap, or a
    clock-in within 3 hours of the start). Unmatched past shifts are no-shows; unmatched
    records are unscheduled work. Lateness and leaving early use the grace period setting.
    """
    st = ctx['settings']
    grace = st.get('late_grace_minutes', 5)
    emps = ctx['employees']
    by_emp = group_by_employee(ctx['shifts'])
    recs_by = defaultdict(list)
    for r in ctx.get('attendance', []):
        recs_by[r['employee_id']].append(r)
    rows, details = [], []
    per_day = defaultdict(lambda: {'sched': 0.0, 'actual': 0.0})

    def branch_ok(bid):
        return branch_id is None or bid == branch_id

    for eid, emp in emps.items():
        if employee_ids and eid not in employee_ids:
            continue
        shifts = by_emp.get(eid, [])
        recs = sorted(recs_by.get(eid, []), key=lambda r: r['clock_in'])
        pairs = []
        for s in shifts:
            a, b = span(s)
            for r in recs:
                ri = abs_dt(r['clock_in'])
                ro = abs_dt(r['clock_out']) if r.get('clock_out') else ri + 1
                ov = overlap(a, b, ri, ro)
                if ov > 0 or abs(ri - a) <= 180:
                    pairs.append((ov, -abs(ri - a), s['id'], r['id'], s, r))
        pairs.sort(key=lambda p: (p[0], p[1]), reverse=True)
        used_s, used_r, match = set(), set(), {}
        for ov, _d, sid, rid, s, r in pairs:
            if sid in used_s or rid in used_r:
                continue
            used_s.add(sid)
            used_r.add(rid)
            match[sid] = r
        row = {'employee_id': eid, 'name': emp['name'], 'sched_shifts': 0, 'sched_hours': 0.0, 'actual_hours': 0.0,
               'sched_cost': 0.0, 'actual_cost': 0.0, 'late': 0, 'late_minutes': 0, 'early': 0, 'no_show': 0,
               'unscheduled': 0, 'unscheduled_hours': 0.0, 'missing_out': 0, 'attended': 0}
        for s, (c, _ot) in zip(shifts, item_costs(ctx, emp, shift_items(shifts))):
            if not (start <= s['date'] <= end and branch_ok(s.get('branch_id'))):
                continue
            a, b = span(s)
            sh = paid_hours(s)
            r = match.get(s['id'])
            det = {'date': s['date'], 'employee_id': eid, 'name': emp['name'], 'branch_id': s.get('branch_id'), 'shift_id': s['id'],
                   'position_id': s.get('position_id'), 'sched_start': s['start_time'], 'sched_end': s['end_time'], 'sched_hours': round(sh, 2),
                   'clock_in': None, 'clock_out': None, 'actual_hours': None, 'late_min': 0, 'early_min': 0, 'over_min': 0, 'status': []}
            if not r and b > now_min:
                continue  # still to come: not part of the comparison yet
            row['sched_shifts'] += 1
            row['sched_hours'] += sh
            row['sched_cost'] += c
            per_day[s['date']]['sched'] += sh
            if not r:
                row['no_show'] += 1
                det['status'].append('no_show')
                details.append(det)
                continue
            row['attended'] += 1
            ri = abs_dt(r['clock_in'])
            det['clock_in'] = r['clock_in']
            det['late_min'] = max(0, ri - a)
            if det['late_min'] > grace:
                row['late'] += 1
                row['late_minutes'] += det['late_min']
                det['status'].append('late')
            if r.get('clock_out'):
                ro = abs_dt(r['clock_out'])
                det['clock_out'] = r['clock_out']
                ah = max(0, ro - ri - int(r.get('break_minutes') or 0)) / 60
                det['actual_hours'] = round(ah, 2)
                det['early_min'] = max(0, b - ro)
                det['over_min'] = max(0, ro - b)
                if det['early_min'] > grace:
                    row['early'] += 1
                    det['status'].append('early')
                row['actual_hours'] += ah
                per_day[s['date']]['actual'] += ah
            elif b > now_min:
                det['status'].append('in_progress')
            else:
                row['missing_out'] += 1
                det['status'].append('missing_out')
            if not det['status']:
                det['status'].append('ok')
            details.append(det)
        for r in recs:
            if r['id'] in used_r or not (start <= r['date'] <= end):
                continue
            if not branch_ok(r.get('branch_id') or emp.get('branch_id')):
                continue
            ri = abs_dt(r['clock_in'])
            ah = max(0, abs_dt(r['clock_out']) - ri - int(r.get('break_minutes') or 0)) / 60 if r.get('clock_out') else 0
            row['unscheduled'] += 1
            row['unscheduled_hours'] += ah
            row['actual_hours'] += ah
            per_day[r['date']]['actual'] += ah
            details.append({'date': r['date'], 'employee_id': eid, 'name': emp['name'], 'branch_id': r.get('branch_id') or emp.get('branch_id'),
                            'shift_id': None, 'position_id': None, 'sched_start': None, 'sched_end': None, 'sched_hours': 0,
                            'clock_in': r['clock_in'], 'clock_out': r.get('clock_out'), 'actual_hours': round(ah, 2),
                            'late_min': 0, 'early_min': 0, 'over_min': 0, 'status': ['unscheduled']})
        actual_items = [{'date': d['date'], 'hours': d['actual_hours'] or 0, 'branch_id': d['branch_id']}
                        for d in details if d['employee_id'] == eid and d['actual_hours']]
        actual_items.sort(key=lambda it: it['date'])
        row['actual_cost'] = sum(c for c, _ in item_costs(ctx, emp, actual_items))
        if row['sched_shifts'] or row['unscheduled']:
            row['attendance_pct'] = round(row['attended'] / row['sched_shifts'] * 100) if row['sched_shifts'] else None
            row['variance_hours'] = round(row['actual_hours'] - row['sched_hours'], 2)
            for k in ('sched_hours', 'actual_hours', 'sched_cost', 'actual_cost', 'unscheduled_hours'):
                row[k] = round(row[k], 2)
            rows.append(row)
    rows.sort(key=lambda r: r['name'])
    details.sort(key=lambda d: (d['date'], d['sched_start'] or d['clock_in'][11:16], d['name']))
    day_rows = []
    d, d1 = date.fromisoformat(start), date.fromisoformat(end)
    while d <= d1:
        ds = d.isoformat()
        day_rows.append({'date': ds, 'sched_hours': round(per_day[ds]['sched'], 2), 'actual_hours': round(per_day[ds]['actual'], 2)})
        d += timedelta(days=1)
    sched = sum(r['sched_shifts'] for r in rows)
    totals = {k: round(sum(r[k] for r in rows), 2) for k in ('sched_hours', 'actual_hours', 'sched_cost', 'actual_cost', 'unscheduled_hours')}
    totals.update({k: sum(r[k] for r in rows) for k in ('sched_shifts', 'late', 'late_minutes', 'early', 'no_show', 'unscheduled', 'missing_out', 'attended')})
    totals['attendance_pct'] = round(totals['attended'] / sched * 100) if sched else None
    totals['variance_hours'] = round(totals['actual_hours'] - totals['sched_hours'], 2)
    return {'employees': rows, 'days': day_rows, 'details': details, 'totals': totals}
