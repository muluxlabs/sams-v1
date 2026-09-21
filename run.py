#!/usr/bin/env python3
"""
የስርዓቱ ማስጀመሪያ — SAMS entry point.

    python run.py                 start the service
    python run.py --init          create the database and the first admin
    python run.py --seed          load demo data for training or testing
    python run.py --backup PATH   run a verified backup and exit
    python run.py --check         health check and exit

Binds to 127.0.0.1 by default. The machine is offline and both clients —
the kiosk browser and the admin browser — run on it, so there is no reason
to listen on the network. Change the bind address only alongside a proper
auth review.
"""

from __future__ import annotations

import argparse
import logging
import sys
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DATA_DIR = Path.home() / ".sams"
DB_PATH = DATA_DIR / "sams.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("sams")


def cmd_init(db_path: Path) -> None:
    from sams.api import auth
    from sams.core.workcalendar import DEFAULT_HOLIDAYS
    from sams.db.connection import connect, init_schema
    from sams.db.repository import add_holiday, set_setting

    conn = connect(db_path)
    init_schema(conn)

    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM admin_user"
    ).fetchone()["n"]
    if existing:
        print("የመረጃ ቋቱ ቀድሞ ተዘጋጅቷል። (database already initialised)")
        return

    conn.execute(
        "INSERT OR IGNORE INTO department (id, name_am, name_latin) "
        "VALUES (1, 'የወረዳው ጽህፈት ቤት', 'Woreda Office')"
    )
    conn.commit()

    for h in DEFAULT_HOLIDAYS:
        data = {"name_am": h.name_am, "day_type": h.day_type.value}
        if h.ethiopian_month_day:
            data["eth_month"], data["eth_day"] = h.ethiopian_month_day
        else:
            data["greg_month"], data["greg_day"] = h.gregorian_month_day
        add_holiday(conn, data, None)

    defaults = {
        "shift": (
            '{"morning_start":"08:30","morning_end":"12:30",'
            '"afternoon_start":"14:00","afternoon_end":"17:30"}'
        ),
        "weekly_pattern": (
            '{"0":"full","1":"full","2":"full","3":"full",'
            '"4":"full","5":"off","6":"off"}'
        ),
        "duplicate_window": "60",
        "match_threshold": "0.42",
        "min_margin": "0.05",
        "max_credited_days": "10",
        "max_delegations": "3",
        "leave_reset_month": "11",
    }
    for k, v in defaults.items():
        set_setting(conn, k, v)

    print("\nየመጀመሪያ አስተዳዳሪ ይፍጠሩ — create the first administrator\n")
    username = input("የተጠቃሚ ስም (username): ").strip()
    display = input("ሙሉ ስም በአማርኛ (display name): ").strip()
    while True:
        pw = getpass("የይለፍ ቃል (password, min 8 chars): ")
        if len(pw) < 8:
            print("  ቢያንስ 8 ፊደል ያስፈልጋል")
            continue
        if pw != getpass("እንደገና ያስገቡ (repeat): "):
            print("  አልተመሳሰለም")
            continue
        break

    auth.create_user(conn, username, pw, "admin", display)
    conn.close()
    print(f"\n✓ ተዘጋጅቷል። የመረጃ ቋቱ: {db_path}")
    print("  አሁን `python run.py` ያስኪዱ፣ ከዚያም http://127.0.0.1:8700/admin ይክፈቱ።")


def cmd_seed(db_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
    from tests.seed import build_seed

    from sams.api import auth
    from sams.db.connection import connect

    facts = build_seed(db_path, eth_year=2018, n_employees=30)
    conn = connect(db_path)
    # The seeded admin (id 1) is referenced by audit and holiday rows, so it
    # is updated in place rather than deleted — a DELETE trips the foreign
    # key and would leave the database half-seeded.
    auth.change_password(conn, 1, "sams2026admin")
    conn.execute(
        "UPDATE admin_user SET username = 'admin', display_name_am = 'አስተዳዳሪ', "
        "must_change_pw = 0 WHERE id = 1"
    )
    conn.commit()
    auth.create_user(conn, "hr", "sams2026hr", "hr", "የሰው ሃይል ባለሙያ")
    auth.create_user(conn, "viewer", "sams2026view", "viewer", "ተመልካች")
    conn.close()
    print(f"✓ {len(facts['employees'])} ሰራተኞች ተጭነዋል ({facts['eth_year']} ዓ.ም.)")
    print("  admin / sams2026admin")
    print("  hr / sams2026hr")
    print("  viewer / sams2026view")
    print("\n  ⚠ የሙከራ መረጃ ብቻ ነው። በእውነተኛ ስራ ላይ አይጠቀሙ።")


def cmd_backup(db_path: Path, destination: str) -> None:
    from sams.db.connection import backup_to, connect, log_backup

    conn = connect(db_path)
    result = backup_to(conn, destination)
    log_backup(conn, result)
    conn.close()
    print(f"✓ ምትኬ ተረጋግጧል: {result['destination']} "
          f"({result['bytes'] / 1024:.0f} KB)")
    print(f"  {result['row_counts']}")


def cmd_check(db_path: Path) -> None:
    from sams.db.connection import connect, integrity_check, verify_hash_chain

    conn = connect(db_path)
    ok = integrity_check(conn)
    broken = verify_hash_chain(conn)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM attendance_event"
    ).fetchone()["n"]
    conn.close()
    print(f"database integrity : {'ok' if ok else 'FAILED'}")
    print(f"attendance events  : {n}")
    print(f"hash chain         : {'ok' if not broken else f'BROKEN at {broken[:10]}'}")
    sys.exit(0 if (ok and not broken) else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="SAMS — የተገኝነት መቆጣጠሪያ ስርዓት")
    ap.add_argument("--init", action="store_true", help="initialise the database")
    ap.add_argument("--seed", action="store_true", help="load demo data")
    ap.add_argument("--backup", metavar="PATH", help="run a verified backup")
    ap.add_argument("--check", action="store_true", help="health check")
    ap.add_argument("--db", default=str(DB_PATH), help="database path")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--no-recognition", action="store_true",
                    help="start without the face model (admin UI only)")
    args = ap.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.init:
        return cmd_init(db_path)
    if args.seed:
        return cmd_seed(db_path)
    if args.backup:
        return cmd_backup(db_path, args.backup)
    if args.check:
        return cmd_check(db_path)

    if not db_path.exists():
        print("የመረጃ ቋት አልተገኘም። መጀመሪያ `python run.py --init` ያስኪዱ።")
        sys.exit(1)

    import uvicorn

    from sams.api import app as app_module

    application = app_module.create_app(db_path)

    if not args.no_recognition:
        try:
            from sams.recognition.engine import FaceEngine

            engine = FaceEngine()
            engine.load()
            app_module.state.engine = engine
            log.info("face recognition ready")
        except Exception as e:
            # The admin UI, reports and leave management all work without
            # the camera. Refusing to start would take the whole office
            # down over one missing model.
            log.warning("face recognition unavailable (%s) — kiosk will "
                        "offer PIN entry only", e)

    print(f"\n  የተገኝነት ስርዓት እየሰራ ነው")
    print(f"  መመዝገቢያ (kiosk) : http://{args.host}:{args.port}/")
    print(f"  አስተዳደር (admin) : http://{args.host}:{args.port}/admin\n")
    uvicorn.run(application, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
