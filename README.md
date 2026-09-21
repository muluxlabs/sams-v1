# SAMS — የሰራተኞች ተገኝነት መቆጣጠሪያ ስርዓት

Smart Employee Attendance Management System for the **Woreda Welkait Administration**.
Offline, Amharic-only, Ethiopian calendar throughout.

Built by MBW Next Digital Solutions Initiative.

---

## What it does

- **Face recognition attendance** at a single station, with a PIN fallback that always works
- **Hours worked against hours expected** — the system never labels anyone "late", it accounts for time
- **Ethiopian calendar everywhere**, including report month boundaries and ጳጉሜ
- **Leave management** with balances, plus task delegation that credits a covered leave day as worked
- **Workload balancing** — who is available today, and who is already carrying the most
- **Reports** in Amharic, exportable to Excel and PDF with signature and stamp blocks
- **Survives power loss** — the database opens clean after being killed mid-write

Everything runs on one machine with no internet connection.

---

## Requirements

| | |
|---|---|
| Machine | Laptop (battery carries the daily outage), 16GB RAM, SSD |
| OS | Windows 10/11, or Linux |
| Python | 3.11 or newer |
| Camera | External USB webcam, 1080p, good low-light performance |
| Storage | 20GB free (capture images are ~4GB/year at 30 staff) |

A **laptop is strongly preferred over a desktop**. The office loses power for one to
two hours most days; a laptop's own battery covers that with nothing for anyone to do,
while a desktop plus a typical UPS gives only 20–30 minutes.

---

## Installation

```bash
pip install -r requirements.txt

# Face recognition (optional at first — everything else works without it)
pip install insightface onnxruntime opencv-python

python run.py --init        # creates the database and the first administrator
python run.py               # starts the service
```

Then open:

- **Attendance station** — <http://127.0.0.1:8700/>
- **Administration** — <http://127.0.0.1:8700/admin>

### Try it with demo data

```bash
python run.py --seed --db demo.db     # 30 staff, a full Ethiopian year
python run.py --db demo.db
```

Sign in as `admin` / `sams2026admin`. **Demo data only — never use on a live install.**

---

## Daily operation

| Task | How |
|---|---|
| Staff check in and out | Walk up to the station; two punches a day |
| Recognition fails | The screen offers PIN entry after two attempts |
| Power is out | Keep the paper sheet, then **ተገኝነት → በጅምላ መዝግብ** afterwards |
| Set a half day or event | **የቀን መቁጠሪያ** → pick the date → choose the type |
| Approve leave | **ፈቃድ** → አጽድቅ / ውድቅ አድርግ |
| Confirm a delegated task | **ፈቃድ** → ተጠናቋል (this is what credits the leave day) |
| Monthly report | **ሪፖርቶች** → ወርሃዊ ማጠቃለያ → ወደ ፒዲኤፍ |
| Backup | **ቅንብሮች** → አሁን ምትኬ ስራ, or `python run.py --backup D:\backup\sams.db` |

---

## Setting it up for the office

### 1. Enrollment

Five photos per person: neutral front, slight left, slight right, with and without
the glasses or headscarf they normally wear. The quality gate refuses blurred, dark
or badly angled captures and says why in Amharic.

**Enrollment quality determines everything downstream.** A rushed session produces a
system that never works properly and nobody can explain why. Budget a careful morning
for thirty people, and use it to collect written consent at the same time.

### 2. Calibration

After everyone is enrolled: **ቅንብሮች → የፊት መለያ ማስተካከያ**.

This measures the threshold on the Woreda's own staff rather than using a published
default. If the false-reject rate comes back above 2%, **re-enroll — never lower the
threshold.** A false accept means one employee marked another present, which is fraud
and discredits every report the system produces.

### 3. Lighting

Survey where the station will stand at the actual arrival hour. A camera facing a
bright doorway silhouettes everyone who walks in and no model recovers from that.
Plan for a small, constant light aimed at the face.

### 4. Holidays

Fixed holidays ship pre-loaded. **ፋሲካ, ዒድ አል ፈጥር, ዒድ አል አድሃ and መውሊድ move every year
and must be entered by an administrator** — otherwise all thirty staff show as absent
on the day.

### 5. Backups

Set a nightly scheduled task:

```
python run.py --backup D:\backup\sams-%date%.db
```

Every backup is verified by reopening the copy and counting rows. The dashboard shows
a red banner if none has succeeded in 48 hours. **Rehearse a restore during training,
with the HR officer's own hands on the keyboard** — a backup nobody has restored is
not a backup.

---

## How hours are calculated

Two punches a day. Worked hours are the span between them, less the **overlap** with
the lunch window:

```
worked = (punch_out − punch_in) − overlap[(punch_in, punch_out), (12:30, 14:00)]
```

Deducting the overlap rather than a flat 1.5 hours matters: someone who works
08:30–12:30 never touched lunch and keeps all four hours. A flat deduction would
quietly steal 90 minutes from every morning-only day.

| Day type | Expected | Kiosk |
|---|---|---|
| ሙሉ የስራ ቀን | 7.5h | open |
| ግማሽ ቀን (ጥዋት) | 4.0h | open |
| ግማሽ ቀን (ከሰዓት) | 3.5h | open |
| የእረፍት ቀን / የህዝብ በዓል | 0 | closed |
| የመርሃ ግብር ቀን | 7.5h, credited to everyone | closed |

Approved leave and event days are **credited** — paid without punches. `የሚከፈልበት ሰዓት`
is the payroll figure; `የሰራው ሰዓት` is time actually at the desk. They differ, and both
are shown.

---

## The delegation rule

When requesting leave, staff may optionally hand their work to a colleague. If the
task is **confirmed complete by a supervisor**, that leave day is credited as worked.

Four guards, all configurable in settings:

| Guard | Default | Why |
|---|---|---|
| Annual credit cap | 10 days | Otherwise a whole year converts into paid absence |
| Concurrent delegations per person | 3 | Stops the same reliable people absorbing everything |
| Completion must be confirmed | yes | No credit for work nobody verified |
| Self-confirmation | not allowed | Otherwise you approve your own cover |

**የተግባር ክፍፍል ሪፖርት** shows who is receiving the work. Built in from the start rather
than added after the first complaint about favouritism.

---

## Security

- Three roles: **ተመልካች** (read only) · **የሰው ሃይል** (records, leave) · **አስተዳዳሪ** (everything)
- PBKDF2-SHA256 passwords, 600,000 iterations; lockout after 5 failed attempts
- `attendance_event` is **append-only** — corrections supersede, never overwrite
- Every event is **hash-chained**, so editing the database file directly is detectable
- Every event stores a **monotonic counter** alongside the wall clock, so clock tampering shows
- Full audit log of every correction, employee change and settings change

Run `python run.py --check` any time to verify database integrity and the hash chain.

### Lock down the kiosk

The kiosk page blocks the context menu and the usual escape keys, but that is only
half of it. Run the kiosk browser under a **restricted Windows account with no access
to the database directory**. A fullscreen browser on an unlocked account is an open
computer standing in a public corridor.

---

## Tests

```bash
python -m pytest tests/ -q
```

The suite is written to break the system, not to demonstrate it:

| Area | What it does |
|---|---|
| Ethiopian calendar | Round-trips **every day from 1900 to 2100**; pins ጳጉሜ 6 and year boundaries |
| Hours | Lunch-overlap edges, missing punches, out-before-in, implausible spans |
| Leave | Every way found to game the delegation rule |
| Power loss | Kills the writer mid-write with `os._exit` **20 times**, requires a clean database each time |
| Integrity | Edits the database behind the system's back and requires detection |
| Recognition | Strangers, look-alikes, threshold monotonicity, calibration |
| Reports | Validated against a seeded full Ethiopian year with known arithmetic |
| Amharic | Extracts text back out of the PDF and requires the **numbers** to be there |
| Security | Role escalation, brute force, injection, forged sessions |

That last one exists because of a real failure during the build: the first font was
Ethiopic-only, so it rendered every name perfectly and **every number as blank space**,
with no error raised anywhere. See `tools/build_font.py`.

---

## Layout

```
run.py                  entry point
sams/
  core/
    ethiopian.py        calendar conversion, Ethiopian month boundaries
    workcalendar.py     day types, holidays, expected hours
    hours.py            the worked-hours engine
    leave.py            leave, balances, delegation policy
  db/
    schema.sql          append-only events, hash chain, audit
    connection.py       WAL, crash safety, verified backup, clock integrity
    repository.py       all writes that affect pay
  recognition/engine.py InsightFace wrapper, gallery, calibration
  reports/              report builders, Excel and PDF export
  api/                  FastAPI service, auth, roles
  web/                  kiosk and admin interfaces
  i18n/am.py            the Amharic string table
assets/fonts/           merged Ethiopic + Latin font
tools/build_font.py     rebuilds that font
tests/                  249 tests
```

---

## Not in this version

Deferred deliberately, per §12 of the proposal: multiple stations, cloud sync, SMS
notifications, payroll system integration, mobile access, biometric hardware beyond
the webcam.

The architecture anticipates the first two — the service already speaks HTTP, so a
second station or admin access from another PC is a bind-address change and an auth
review rather than a rewrite.
