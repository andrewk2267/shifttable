# ShiftTable

Multi-branch shift rostering for teams of 5 to 30 people: restaurants, clinics and
spas, factories, or any shift business. The whole app is a single HTML page, a small
Python server and a SQLite database file, all in this folder. Nothing to install and no
internet needed.

The research behind the design is in [RESEARCH-AND-PLAN.md](RESEARCH-AND-PLAN.md).

## Start it

```bash
cd ShiftTable
python3 server.py
```

Open <http://127.0.0.1:8080>. On first start the app creates `roster.db` and loads the
F&B demo (Harbour Lane Group: two branches, 30 staff, six weeks of rosters with clock
records).

| Option | What it does |
|---|---|
| `--port 8081` | Use another port if 8080 is busy |
| `--host 0.0.0.0` | Let phones on the same Wi-Fi open it (the terminal prints the address). Plain HTTP, so only on a network you trust |
| `--db path/to/file.db` | Use a different database file. Its snapshots go in a `backups/` folder next to it |

Stop it with Ctrl+C. Your data stays in `roster.db`.

**Upgrading from the first version:** stop the old server and start it again. The
database upgrades itself on start, after saving a safety copy in
`backups/…-pre-upgrade-v1.db`. Existing data becomes one branch called "Main branch";
rename it or add more branches in **Settings → Branches**.

## Live demo on Vercel

**<https://shifttable-red.vercel.app>**: sign in with any of the demo logins below.

The repository deploys to Vercel as a **live demo**:

- `api/index.py` runs the same server as a Vercel Python function, and `vercel.json`
  sends every path to it.
- Vercel functions have no permanent disk. The database lives in `/tmp` and is
  re-created with the F&B demo whenever Vercel starts a fresh copy, so **changes don't
  last**. It shows the product, it doesn't store real rosters.
- When it's busy, Vercel runs several copies at once, each with its own demo database.
  An edit then only shows while your requests reach the copy that made it.
- For real use, run it on your own computer or a server with a disk (the instructions
  below), or move the data to a hosted database.
- Sign-ins are signed cookies, so they keep working when Vercel moves you between
  copies of the app. In the demo, a restore or password change doesn't sign anyone out.
- Environment variables:
  - `SHIFTTABLE_SECRET` (a long random string that signs sign-in cookies)
  - `SHIFTTABLE_UTC_OFFSET` (`8` for Singapore time; Vercel servers run on UTC)
- The function runs in Vercel's Singapore region (`sin1`).

## Demo logins

| Role | Username | Password | Can do |
|---|---|---|---|
| Owner | `admin` | `Admin#2026` | Everything, including branches, users, labour rules and data tools |
| Manager | `manager` | `Manager#2026` | Build and publish rosters, approve requests, manage the team, import clock data |
| Staff | `marcus` | `Staff#2026` | See the roster and their shifts, ask for cover, request leave |
| Staff | `priya` | `Staff#2026` | Same as above (part-time, with restricted availability) |

The same four logins work with every sample business. The sign-in page shows buttons
for them while **Settings → Business & rules → Show demo logins** is on. That switch
turns itself off the first time anyone changes a password. **Before real staff use the
app:** change every password, create real logins in **Settings → Users & logins**, and
delete the demo ones.

## Branches

- The **branch switcher** at the top of every page chooses one branch or **All
  branches**. Dashboard, roster, team and reports follow it.
- Everyone has a **home branch** and can be set up to **cover other branches**
  (Team → a person → Branches).
- Labour rules count a person's hours at **every** branch: daily and weekly limits,
  rest between shifts, double-booking.
- Coverage, labour cost and forecasts are per branch.
- On a single-branch roster, a person's shifts at other branches show as dimmed,
  dotted chips, so you can see they're busy.
- Auto-fill keeps people at their home branch. It only sends someone to another branch
  they're set up for when the home team runs short. **Find cover** also lists people
  from other branches, with a note.
- The dashboard's **Branches this week** table compares manpower, cost, labour % and
  coverage across branches.
- Staffing needs are set per branch, and can be copied from one branch to another.

## Holidays and events

- **Public holiday**: shifts that day cost the holiday pay multiplier (2× by
  default, per the Employment Act's extra day's pay). Singapore's 2026 and 2027
  holidays come built in; add them from **Settings → Holidays & events**.
- **Event**: information for everyone, such as promotions, private bookings, audits
  and training days.
- **Closed**: the branch is shut. Staffing needs pause, auto-fill and copy-week skip
  the day, and any shift on it is flagged.

Events can apply to all branches or one. Add or edit them from the roster (**+ Event**
in any day header, or on the **Month** calendar) or from Settings.

## Filtering by people

On the **Roster** and in **Reports**, the **Everyone** button opens a people picker.
Tick one or more people and press Apply to focus on them. Choose **Everyone** to clear
it.

## Clock-in data: actual vs rostered

In **Reports → Actual vs rostered → Import clock data** (managers and admins), or
**Settings → Data & backup → Import from CSV**, bring in a CSV from your time clock,
fingerprint scanner or payroll system:

`employee_code` or `employee` (name), `date`, `clock_in`, `clock_out`, optional
`break_minutes` and `branch`. Column names are matched loosely ("Staff ID",
"Time In", "Outlet" all work). Times like `07:58` or `7:58 am`; dates as `YYYY-MM-DD` or
`DD/MM/YYYY`; a clock-out earlier than the clock-in counts as the next day. You get a
preview first, records already imported are skipped, and a template is available.

The report matches each clock record to a rostered shift and shows:
- actual vs rostered hours and cost
- attendance rate and no-shows
- late arrivals and early finishes (over the grace period, 5 minutes by default)
- missing clock-outs
- unrostered work

Totals are per person, with a shift-by-shift exceptions list you can export. To
re-import corrected data, use **Delete clock records in this range** first.

## Sample businesses

**Settings → Data & backup → Sample businesses** loads a complete demo:

| Sample | Branches | People | Shifts |
|---|---|---|---|
| **F&B**: Harbour Lane Group | Robertson Quay bistro-bar, Tampines Mall café | 30 | Opening, mid, closing, lunch and dinner peaks |
| **Health & Wellness**: Vitality Wellness | Novena, Jurong East | 22 | Physio, spa, gym, front desk; weekday late and weekend shifts |
| **Manufacturing**: Apex Precision Components | Tuas (3 shifts incl. nights), Woodlands (2 shifts) | 30 | Morning, afternoon, night, office and Saturday half-day |

Each has six weeks of rosters, staffing needs, public holidays, events and a closure,
leave and shift requests, and three weeks of clock records with realistic lateness
and no-shows. Loading one replaces all business data. Logins and labour rules are kept.
The business name, forecast label and labour target change to suit the industry.

## What each role sees

**Managers**
- **Dashboard**: labour cost vs target by day, coverage, roster health, the branch
  comparison, requests waiting, whether next week is published in time, who's on shift
  today, and upcoming holidays and events.
- **Roster**
  - Week grid, day timeline or month calendar.
  - Click an empty cell to add a shift, click a shift to edit it, or drag to move it
    (hold Alt/Option to copy).
  - **Auto-fill**, **Copy last week** and **Publish** work on the branch you're viewing.
  - The **Issues** panel lists every rule breach.
- **Requests**: approve or decline leave, cover, release and pick-up requests. Each
  change is checked against the rules first.
- **Team**
  - People, staff codes, home and cover branches, and positions they're trained for.
  - Pay rate, usual and max hours, and weekly availability.
- **Reports**
  - Hours, cost, overtime, holiday shifts and fairness (weekends, closes, clopens).
  - The actual-vs-rostered comparison.
- **Settings**: labour rules, branches, positions and shift times, staffing needs,
  holidays and events, users, data tools and the activity log.

**Staff**
- **My shifts**
  - Next shift with its branch, and the next four weeks.
  - Hours this week, and open shifts they can pick up.
  - Upcoming holidays and events.
- **Team roster**: the published roster (week, day or month), with their own row at the
  top.
- **Requests**: cover, release, pick-up and leave requests.
- **My availability**.

## Labour rules (Singapore defaults, all editable)

- 12 h max per day.
- Overtime after 44 h a week, paid at 1.5×. Public holidays are paid at 2×.
- 72 h overtime cap per month.
- A rest day every week, and no more than 6 days in a row.
- A 45-minute break on shifts over 6 h.
- Part-time is under 35 h a week.
- 11 h rest between shifts (no clopens).
- Publish rosters 7 days ahead.
- 5-minute grace period for lateness.

## Data

In **Settings → Data & backup** (admin only):

- **Download .db** (complete SQLite copy) or **.json** (readable, portable).
- **Save snapshot now** keeps a copy in `backups/`. A snapshot is also taken
  automatically before every restore, import, sample load, reset, clock-record delete
  or upgrade (the last 20 automatic ones are kept).
- **Restore** from a `.db` or `.json` file, or from a snapshot. The file is checked
  first; a file without an active admin login is refused. Backups from the first version
  are upgraded automatically. Everyone signs in again afterwards.
- **Import CSV** with a preview step. Templates are available for each type.
  - **Team** columns: `name, code, position, skills, type, hourly_rate, target_hours,
    max_hours, branch, branches, phone, email, notes`.
    - `skills` and `branches` are separated by `;`.
    - `type` is full-time, part-time or casual.
    - Unknown positions and branches are created.
    - Existing names are updated.
  - **Shifts** columns: `date, start, end, break, employee, position, branch, notes`.
    - A blank employee makes an open shift.
    - A blank branch means the person's home branch.
    - Shifts are imported as drafts.
  - **Clock records**: see above.
- **Clear all business data**: logins and settings are kept.

To start completely fresh, stop the server and delete `roster.db`. It is recreated
with the F&B demo on the next start.

## Tests

```bash
cd ShiftTable
python3 -m unittest discover -s tests -v
```

The 54 tests cover:
- **The rule engine**: every labour rule, cross-branch limits, branch setup, holiday pay,
  closures, coverage, auto-fill fairness and home-branch preference, clock-record
  matching, and a 30-person two-branch week.
- **The API**: sign-in and lockout, roles, CSRF, branches, events and the month
  calendar, shifts, publishing per branch, requests, users, reports with branch and
  people filters, and the actual-vs-rostered report.
- **Data handling**: backup, restore and CSV/clock imports including bad files, all three
  sample businesses, and upgrading a first-version database and backup.
- **Hosting**: signed sign-in cookies (tampering, expiry, sharing across demo copies),
  the Vercel entry point and the business clock offset.

They use temporary databases and never touch `roster.db`.

## Troubleshooting

- **"Address already in use"**: another program uses port 8080. Run
  `python3 server.py --port 8081`.
- **The page looks broken right after an update**: an old server is still running.
  Stop it (Ctrl+C) and start it again.
- **Server starts but the page never loads, on an external drive (macOS)**: macOS may
  be waiting for you to allow Python to access files on a removable volume. Look for
  the permission prompt, or allow it under System Settings → Privacy & Security →
  Files and Folders.
- **Locked out after 5 wrong passwords**: wait 15 minutes, or restart the server.
  Lockouts are kept in memory only.
