# ShiftTable: research and plan

A roster app for a food and beverage business with 5 to 30 staff. It is a single-page
HTML app backed by a SQLite file in the same folder, served by a Python
standard-library server.

---

## 1. Research: what goes wrong with F&B rosters

### 1.1 What shift workers and managers report

| # | Problem | Evidence |
|---|---|---|
| P1 | **Schedules arrive late and change at short notice.** Workers cannot plan childcare, study or second jobs. | About a third of hourly food-service workers get their schedule with less than a week's notice ([NWLC 2025 fact sheet](https://nwlc.org/wp-content/uploads/2025/10/Schedules-That-Work-Act-2025-Factsheet.pdf)). Fair Workweek laws now require 7 to 14 days' notice ([Rippling](https://www.rippling.com/blog/predictive-scheduling-laws), [7shifts](https://www.7shifts.com/blog/predictive-scheduling-for-restaurants/)). |
| P2 | **"Clopening"**: closing late, then opening the next morning. Causes fatigue and burnout. | More than one in three food-service workers have worked a close followed by an open ([Factorial](https://factorialhr.com/blog/restaurant-shift-scheduling/)). Recommended minimum rest is 10 to 11 hours between shifts ([Deputy](https://www.deputy.com/blog/clopening-shifts-how-to-eliminate-them-without-gaps)). |
| P3 | **Unfair distribution.** The same people always get weekends, closes or too few hours. | Unfair shift distribution is one of the top five restaurant scheduling problems ([Scheduler Systems](https://scheduler-systems.com/blog/restaurant-scheduling-problems)). 26% of workers cite a lack of available shifts as a source of financial instability ([Toast](https://pos.toasttab.com/blog/data/restaurant-employee-insights)). |
| P4 | **Understaffed peaks, overstaffed lulls.** | 72% of hourly workers say understaffing regularly puts them under serious strain, and 93% report more burnout when short-staffed ([NetSuite](https://www.netsuite.com/portal/resource/articles/human-resources/hospitality-staff-scheduling.shtml)). Understaffing can cost around 30% of a day's revenue; overstaffing pushes labour above 42% of sales ([Plantime](https://www.plantime.io/en/articles/restaurant-scheduling-guide)). |
| P5 | **Call-outs and no-shows**, and a scramble to find cover. | No-show rates run at 5 to 15% ([Beeline](https://www.beeline.com/resources/whitepaper/hospitality-labor-demands-smarter-not-optional-shift-management)). |
| P6 | **Chaotic shift swaps** done by text message, with nothing recorded. | Swap and call-out chaos is the most-cited pain point that scheduling tools address ([7shifts](https://www.7shifts.com/restaurant-employee-scheduling-software/)). |
| P7 | **Availability and leave ignored.** People get rostered on days they said they couldn't work. | More than 60% of workers need flexible hours ([Plantime](https://www.plantime.io/en/articles/restaurant-scheduling-guide)); flexible scheduling is the second-most valued job factor, after pay (35% vs 37%) ([Toast](https://pos.toasttab.com/blog/data/restaurant-employee-insights)). |
| P8 | **Labour cost is invisible until payroll.** | Healthy labour cost is 25 to 35% of sales depending on format; track it weekly, not monthly ([7shifts](https://www.7shifts.com/blog/how-to-manage-your-restaurant-labor-cost-percentage/)). |
| P9 | **Managers lose hours every week building rosters by hand.** | Managers average 2.6 to 8.4 hours a week on scheduling; 67% of operators say it contributes directly to manager burnout ([Plantime](https://www.plantime.io/en/articles/restaurant-scheduling-guide), [US Tech Automations](https://ustechautomations.com/resources/blog/restaurants-staff-scheduling-pain-solution-2026)). |
| P10 | **Accidental labour-law breaches**: too many hours, no rest day, no break. | See the Singapore rules in 1.2. |

The cost of getting this wrong is turnover. Turnover runs at 75 to 130% a year in
restaurants, and each replacement costs between $2,400 and $18,100
([Plantime](https://www.plantime.io/en/articles/restaurant-scheduling-guide)).

### 1.2 Singapore Employment Act, Part IV (the defaults the app enforces)

Source: [MOM, hours of work, overtime and rest days](https://www.mom.gov.sg/employment-practices/hours-of-work-overtime-and-rest-days).

| Rule | Limit | App setting |
|---|---|---|
| Max hours per day, including overtime | 12 h | `max_daily_hours = 12` |
| Normal hours per week (overtime above this) | 44 h | `weekly_ot_threshold = 44` |
| Overtime pay | at least 1.5x the basic hourly rate | `ot_multiplier = 1.5` |
| Overtime cap | 72 h per month | `max_monthly_ot = 72` |
| Rest day | 1 per week (30 continuous hours for shift workers) | always checked, 7 days worked = error |
| Continuous work without a break | at most 6 h; 45 min meal break for an 8 h stretch | `break_after_hours = 6`, `min_break_minutes = 45` |
| Part-time definition | under 35 h per week | `part_time_max_hours = 35` |

Rest between shifts is not in the Act. The app uses the industry best practice of
**11 hours** (`min_rest_hours`), which catches every clopen. All of these values are
editable in Settings, so a business outside Singapore can use its own rules.

---

## 2. Roster planning method the app is built around

The research points to one workflow. Every screen in the app is a step in it.

1. **Forecast demand.** Enter projected sales per day on the roster.
2. **Define staffing needs.** For each weekday, set how many people of each position
   are needed and when, for example "Server, 17:30 to 22:30, 2 people". This is the
   demand curve.
3. **Keep templates.** Opening, closing and peak shifts are one click away.
4. **Auto-fill fairly.** The engine creates the shifts the needs call for and assigns
   people by skill, availability, approved leave, rest rules and hours limits. It
   prefers people who are below their contracted hours and rotates weekends and
   closes.
5. **Check before publishing.** The roster health panel lists every breach: overlap,
   clopen, over 12 h, missing break, no rest day, overtime cap, unavailable, not
   trained, on leave, coverage gap, labour % over target.
6. **Publish early.** Staff only see published shifts. The app shows how many days'
   notice the team is getting and warns when that is under the target (7 days by
   default).
7. **Handle changes through requests, not texts.** Staff request leave, ask a named
   colleague to cover, release a shift to the open pool or pick up an open shift. A
   manager approves with one click, after the engine has checked the change for
   breaches. Every change to a published shift is flagged to staff and logged.
8. **Review cost and fairness.** Reports show hours, overtime, cost, labour % and how
   weekends, closes and clopens were spread across the team.

## 3. Problem to feature map

| Problem | Feature |
|---|---|
| P1 Late or changing schedules | Draft/published states, publish-notice indicator, a "changed" badge on edited shifts, an audit log |
| P2 Clopening | Rest-gap check (11 h), also across week boundaries; auto-fill never creates one |
| P3 Unfair distribution | Hours vs target meter on every row; auto-fill fairness scoring; fairness report (weekends, closes, clopens per person) |
| P4 Under/overstaffing | Staffing needs per weekday; coverage badge per day; day timeline with a coverage strip; labour % per day |
| P5 Call-outs | "Find cover" on any shift: ranked replacements with the reason each person does or doesn't fit |
| P6 Swap chaos | Cover, release and pickup requests with a manager approval step and a conflict preview |
| P7 Availability ignored | Weekly availability per person (editable by staff themselves) plus approved leave; both block auto-fill and flag manual shifts |
| P8 Invisible labour cost | Cost per shift, day and week including the overtime premium; labour % against forecast sales; target threshold |
| P9 Manager time | Auto-fill, copy last week, templates, drag and drop, print view, copy-as-text for a WhatsApp group |
| P10 Legal breaches | The Singapore EA rules in 1.2, checked on every change |

## 4. Architecture

```
ShiftTable/
  index.html          single-page app (all CSS and JS inline, no CDN, works offline)
  server.py           HTTP server and JSON API (Python standard library only)
  rules.py            roster rule engine: checks, coverage, candidate ranking, auto-fill
  sample_data.py      demo business, relative to today's date
  roster.db           SQLite database, created on first run
  backups/            server-side snapshots
  tests/test_app.py   API and rule-engine tests (unittest)
```

- **Why Python stdlib**: `python3 server.py` runs with nothing to install. `sqlite3` and
  `http.server` both ship with Python.
- **One source of truth for rules**: the server evaluates the roster and returns issues
  with the data. The browser never re-implements the labour rules.
- **Security**: PBKDF2-SHA256 password hashing (200k iterations), HttpOnly SameSite=Strict
  session cookie, a custom header required on every write (CSRF), login throttling
  (5 failures locks the account for 15 minutes), role checks on every endpoint,
  parameterised SQL throughout, all rendered text escaped by default, and a strict
  Content-Security-Policy. The server binds to 127.0.0.1 unless told otherwise.

### Roles

| Role | Can do |
|---|---|
| Admin (owner) | Everything, plus users, rule settings and data (backup, restore, import, reset) |
| Manager | Build and publish rosters, manage team, approve requests, see reports |
| Staff | See the published roster and their own shifts, set availability, request leave, cover, release or pickup |

### Data model

`settings`, `users`, `sessions`, `positions`, `employees`, `employee_skills`,
`availability`, `shift_templates`, `staffing_needs`, `shifts`, `forecasts`,
`leave_requests`, `shift_requests`, `audit_log`.

## 5. Data settings (backup, restore, import)

- **Backup**: download the full database as `.db` (SQLite online-backup API, so it is
  consistent while the server runs) or as portable `.json`. Snapshots can also be
  kept on the server in `backups/`.
- **Automatic safety snapshot** before every restore, import, sample load or reset.
- **Restore** from a `.db` or `.json` file, or from a server snapshot. The file is
  validated first: SQLite header, `integrity_check`, required tables and columns, and
  at least one active admin, so a bad file cannot lock you out. After a restore everyone
  signs in again.
- **Import** employees or shifts from CSV (Excel-friendly; accepts `DD/MM/YYYY` dates).
  There is a preview step showing new, updated and rejected rows before anything is
  written. CSV templates are downloadable.
- **Sample data**: reload the demo business at any time. User accounts are kept.
- **Reset**: clear all business data and keep user accounts.

## 6. Verification plan

1. **Unit tests for the rule engine**: every rule fires on a crafted case and stays
   silent on a compliant one; auto-fill never produces a breach it is meant to avoid.
2. **API tests** against a temporary database: login, lockout, role enforcement, CSRF
   header, CRUD validation, publish, request approval flows, backup/restore
   round-trips (`.db` and `.json`), rejection of corrupt and admin-less backups, CSV
   import preview and commit, and SQL-injection-shaped input.
3. **Browser run-through** of every screen as admin, manager and staff: create, edit,
   drag and delete shifts, auto-fill, publish, requests, team, reports, settings, data
   tools, console errors, dark mode and phone width.
4. **Scale check**: 30 employees on one week renders and evaluates quickly.

---

## 7. Version 2: multi-branch, industries, calendar, attendance

### 7.1 What changed and why

| Request | Design |
|---|---|
| Manage several branches | A `branches` table. Every shift, staffing need and forecast belongs to a branch. People have a **home branch** and can be set up to **cover** others. Labour-law checks look at a person's hours at every branch (a 6 h shift at each of two branches is still a 12 h day); coverage, cost and labour % are per branch. A branch switcher scopes every page, and the dashboard compares branches side by side. |
| Sample data for F&B, Health & Wellness, Manufacturing | Three generated businesses with two branches each, built by the same auto-fill a manager uses, so they respect every rule. They include industry shift patterns (F&B peaks; clinic weekday-late and weekend shifts; factory 3-shift rotations with nights crossing midnight) and industry labour targets (30%, 40%, 18%). The demo logins link to people in every dataset. |
| Holidays and events on the roster calendar | A `calendar_events` table with three kinds. **Public holiday** is costed at the holiday pay multiplier (the Employment Act's extra day's pay, 2×), and Singapore's gazetted 2026 and 2027 holidays are built in ([MOM 2026](https://www.mom.gov.sg/newsroom/press-releases/2025/0616-public-holidays-for-2026), [MOM 2027](https://www.mom.gov.sg/newsroom/press-releases/2026/0618-public-holidays-for-2027)). **Event** is informational. **Closed** pauses staffing needs; auto-fill and copy-week skip the day, and any shift on it is flagged. Events show in the week headers, the day view and a new **month calendar**, and can be added or edited from any of them. |
| Filter roster and reports by employees | A people picker (multi-select with search) on the roster (client-side) and reports (server-side `employees=` filter). Labour % is hidden when a subset of people is selected, because comparing part of the team with the whole forecast would mislead. |
| Import clock-in/out data for comparison | An `attendance` table and a CSV importer that accepts time-clock and payroll exports: loose column names, staff codes or names, `DD/MM/YYYY`, am/pm, overnight clock-outs, duplicate skipping, and a preview. The **Actual vs rostered** report matches each record to a shift (largest overlap, or a clock-in within 3 hours of the start). It shows hours and cost variance, attendance rate, no-shows, lateness and early finishes beyond a grace period, missing clock-outs and unrostered work, per person and shift by shift. |

### 7.2 Auto-fill across branches

Scoring now prefers each branch's **home team**. People who can float are only sent
when their home branch no longer needs them, and are placed where no local person
fits. Auto-fill also prefers repeating yesterday's start time, which gives steadier
patterns (and keeps factory night crews together), and never schedules anyone at a
branch they aren't set up for. **Find cover** still lists staff from other branches,
with a note, because borrowing is how multi-site operators cover call-outs.

### 7.3 Upgrading existing data

The server upgrades a version 1 database in place on start, after saving a snapshot:
- It adds the new tables.
- It gives employees, shifts, needs and forecasts a branch: a new "Main branch".
- It turns positions' FOH/BOH areas into free-text departments, so other industries can
  use "Clinical", "Production" and so on.

Version 1 `.db` and `.json` backups restore into version 2 the same way. The upgrade
was tested on a copy of the live database: every row was kept.
