"""
የአማርኛ ቃላት — the Amharic string table.

Amharic is this system's native language, not a translation of an English
original. Strings are authored here first; an English column can be added
later without touching any other module. Building in English and translating
afterwards produces stilted Amharic and doubles the work.

Terminology is fixed here deliberately. Inconsistent wording across screens
and reports reads as carelessness to an official audience, so every module
takes its words from this file rather than inventing its own.
"""

from __future__ import annotations

S: dict[str, str] = {
    # -- system --------------------------------------------------------
    "app.title": "የሰራተኞች ተገኝነት መቆጣጠሪያ ስርዓት",
    "app.short": "የተገኝነት ስርዓት",
    "app.office": "የወልቃይት ወረዳ አስተዳደር",

    # -- kiosk ---------------------------------------------------------
    "kiosk.ready": "እባክዎ ወደ ካሜራው ይመልከቱ",
    "kiosk.scanning": "በመለየት ላይ...",
    "kiosk.welcome_in": "እንኳን ደህና መጡ",
    "kiosk.welcome_out": "መልካም ቀን",
    "kiosk.recorded_in": "የመግቢያ ሰዓትዎ ተመዝግቧል",
    "kiosk.recorded_out": "የመውጫ ሰዓትዎ ተመዝግቧል",
    "kiosk.not_recognized": "ሊለዩ አልቻሉም። እንደገና ይሞክሩ።",
    "kiosk.try_pin": "የፒን ኮድዎን ይጠቀሙ",
    "kiosk.enter_pin": "የፒን ኮድ ያስገቡ",
    "kiosk.pin_wrong": "የተሳሳተ የፒን ኮድ",
    "kiosk.already_punched": "ቀደም ብለው ምልክት አድርገዋል",
    "kiosk.no_camera": "ካሜራው አልተገኘም። ለአስተዳዳሪ ያሳውቁ።",
    "kiosk.camera_blocked": "ካሜራው ተሸፍኗል ወይም ብርሃን የለም",
    "kiosk.closed_today": "ዛሬ የስራ ቀን አይደለም",
    "kiosk.inactive": "መዝገብዎ ንቁ አይደለም። ለአስተዳዳሪ ያሳውቁ።",
    "kiosk.on_leave": "ዛሬ በፈቃድ ላይ ነዎት",
    "kiosk.multiple_faces": "ከአንድ በላይ ፊት ታይቷል። አንድ በአንድ ይቅረቡ።",
    "kiosk.no_face": "ፊት አልተገኘም። በቀጥታ ወደ ካሜራው ይመልከቱ።",
    "kiosk.system_error": "የስርዓት ችግር። ለአስተዳዳሪ ያሳውቁ።",

    # -- navigation ----------------------------------------------------
    "nav.dashboard": "ዳሽቦርድ",
    "nav.attendance": "ተገኝነት",
    "nav.employees": "ሰራተኞች",
    "nav.leave": "ፈቃድ",
    "nav.workload": "የስራ ክፍፍል",
    "nav.calendar": "የቀን መቁጠሪያ",
    "nav.reports": "ሪፖርቶች",
    "nav.settings": "ቅንብሮች",
    "nav.users": "ተጠቃሚዎች",
    "nav.logout": "ውጣ",

    # -- dashboard -----------------------------------------------------
    "dash.today": "የዛሬ ተገኝነት",
    "dash.present": "የተገኙ",
    "dash.absent": "ያልተገኙ",
    "dash.on_leave": "በፈቃድ ላይ",
    "dash.incomplete": "ያልተሟሉ መዝገቦች",
    "dash.total_staff": "ጠቅላላ ሰራተኞች",
    "dash.upcoming_holidays": "ቀጣይ በዓላትና መርሃ ግብሮች",
    "dash.pending_leave": "በመጠባበቅ ላይ ያሉ የፈቃድ ጥያቄዎች",
    "dash.open_tasks": "ክፍት ተግባራት",
    "dash.no_backup_warning": "ማስጠንቀቂያ፦ ምትኬ ከ48 ሰዓት በላይ አልተሰራም",
    "dash.backup_ok": "የመጨረሻ ምትኬ",
    "dash.clock_warning": "ማስጠንቀቂያ፦ የስርዓቱ ሰዓት ተቀይሯል",

    # -- employee ------------------------------------------------------
    "emp.name": "ሙሉ ስም",
    "emp.name_latin": "ስም (በላቲን)",
    "emp.code": "የሰራተኛ መለያ ቁጥር",
    "emp.position": "የስራ መደብ",
    "emp.phone": "ስልክ ቁጥር",
    "emp.department": "ዘርፍ",
    "emp.hired_on": "የተቀጠረበት ቀን",
    "emp.status": "ሁኔታ",
    "emp.active": "ንቁ",
    "emp.inactive": "ንቁ ያልሆነ",
    "emp.pin": "የፒን ኮድ",
    "emp.photos": "የተመዘገቡ ፎቶዎች",
    "emp.enroll": "ፎቶ መዝግብ",
    "emp.add": "አዲስ ሰራተኛ",
    "emp.edit": "አስተካክል",
    "emp.deactivate": "አቦዝን",

    # -- attendance ----------------------------------------------------
    "att.date": "ቀን",
    "att.weekday": "ዕለት",
    "att.in": "የመግቢያ ሰዓት",
    "att.out": "የመውጫ ሰዓት",
    "att.worked": "የሰራው ሰዓት",
    "att.expected": "የሚጠበቅ ሰዓት",
    "att.difference": "ልዩነት",
    "att.status": "ሁኔታ",
    "att.method": "የተመዘገበበት መንገድ",
    "att.correct": "መዝገብ አስተካክል",
    "att.reason": "ምክንያት",
    "att.bulk_entry": "በጅምላ መዝግብ",
    "att.outage_entry": "የኤሌክትሪክ መቋረጥ መዝገብ",

    # -- leave ---------------------------------------------------------
    "leave.request": "የፈቃድ ጥያቄ",
    "leave.new": "አዲስ የፈቃድ ጥያቄ",
    "leave.type": "የፈቃድ ዓይነት",
    "leave.from": "ከቀን",
    "leave.to": "እስከ ቀን",
    "leave.days": "የቀናት ብዛት",
    "leave.reason": "ምክንያት",
    "leave.balance": "የፈቃድ ቀሪ",
    "leave.entitled": "የተፈቀደ",
    "leave.used": "የተጠቀመ",
    "leave.remaining": "ቀሪ",
    "leave.approve": "አጽድቅ",
    "leave.reject": "ውድቅ አድርግ",
    "leave.cancel": "ሰርዝ",
    "leave.approved_by": "ያጸደቀው",

    # -- workload / delegation -----------------------------------------
    "task.delegate": "ተግባር አስተላልፍ",
    "task.optional": "ይህ አማራጭ ነው",
    "task.explain": (
        "በፈቃድ ላይ እያሉ ተግባርዎን ለሌላ ሰራተኛ ካስተላለፉና ተግባሩ ከተጠናቀቀ፣ "
        "ቀኑ እንደሰሩበት ይቆጠራል።"
    ),
    "task.description": "የተግባሩ መግለጫ",
    "task.assign_to": "ለማን",
    "task.available": "ዛሬ ያሉ ሰራተኞች",
    "task.unavailable": "ዛሬ የሌሉ",
    "task.open_count": "ክፍት ተግባራት",
    "task.call": "ደውል",
    "task.confirm_done": "ተጠናቋል ብለህ አረጋግጥ",
    "task.confirm_not_done": "አልተሰራም",
    "task.confirmed_by": "ያረጋገጠው",
    "task.distribution": "የተግባር ክፍፍል",
    "task.limit_reached": "የተመረጠው ሰራተኛ በቂ ተግባራት ተሰጥቶታል",

    # -- calendar ------------------------------------------------------
    "cal.day_type": "የቀን ዓይነት",
    "cal.set_day": "የቀን ዓይነት ቀይር",
    "cal.holidays": "የህዝብ በዓላት",
    "cal.add_holiday": "በዓል ጨምር",
    "cal.add_event": "መርሃ ግብር ጨምር",
    "cal.note": "ማስታወሻ",
    "cal.weekly_pattern": "ሳምንታዊ የስራ ቀናት",
    "cal.saturday": "ቅዳሜ",
    "cal.movable_note": (
        "ፋሲካ፣ ዒድ አል ፈጥር፣ ዒድ አል አድሃ እና መውሊድ በየዓመቱ ስለሚቀያየሩ "
        "በአስተዳዳሪ መግባት አለባቸው።"
    ),

    # -- reports -------------------------------------------------------
    "rep.title": "ሪፖርቶች",
    "rep.daily": "የዕለት ተገኝነት መዝገብ",
    "rep.monthly": "ወርሃዊ ማጠቃለያ",
    "rep.individual": "የግለሰብ መዝገብ",
    "rep.absence": "የቀሪዎች ሪፖርት",
    "rep.payroll": "የደመወዝ ስሌት ሪፖርት",
    "rep.exception": "የልዩ ሁኔታዎች መዝገብ",
    "rep.leave_summary": "የፈቃድ ማጠቃለያ",
    "rep.period": "የሪፖርት ጊዜ",
    "rep.month": "ወር",
    "rep.year": "ዓመት",
    "rep.generate": "ሪፖርት አውጣ",
    "rep.export_excel": "ወደ ኤክሴል",
    "rep.export_pdf": "ወደ ፒዲኤፍ",
    "rep.prepared_by": "ያዘጋጀው",
    "rep.approved_by": "ያጸደቀው",
    "rep.signature": "ፊርማ",
    "rep.stamp": "ማህተም",
    "rep.printed_on": "የታተመበት ቀን",
    "rep.no_data": "በዚህ ጊዜ ውስጥ መዝገብ የለም",

    # -- payroll -------------------------------------------------------
    "pay.expected_hours": "የሚጠበቅ ሰዓት",
    "pay.worked_hours": "የሰራው ሰዓት",
    "pay.credited_hours": "የሚከፈልበት ሰዓት",
    "pay.ratio": "የማሟላት መጠን",
    "pay.shortfall": "የጎደለ ሰዓት",

    # -- auth ----------------------------------------------------------
    "auth.login": "ግባ",
    "auth.username": "የተጠቃሚ ስም",
    "auth.password": "የይለፍ ቃል",
    "auth.wrong": "የተሳሳተ የተጠቃሚ ስም ወይም የይለፍ ቃል",
    "auth.locked": "መዝገብዎ ለጊዜው ተቆልፏል። ቆይተው ይሞክሩ።",
    "auth.change_password": "የይለፍ ቃል ቀይር",
    "auth.must_change": "በመጀመሪያ የይለፍ ቃልዎን መቀየር አለብዎት",
    "auth.no_permission": "ይህን ለማድረግ ፈቃድ የለዎትም",
    "auth.session_expired": "ጊዜዎ አልፏል። እንደገና ይግቡ።",

    # -- roles ---------------------------------------------------------
    "role.viewer": "ተመልካች",
    "role.hr": "የሰው ሃይል ባለሙያ",
    "role.admin": "አስተዳዳሪ",

    # -- common --------------------------------------------------------
    "ok": "እሺ",
    "save": "አስቀምጥ",
    "cancel": "ተወው",
    "delete": "አጥፋ",
    "search": "ፈልግ",
    "filter": "አጣራ",
    "yes": "አዎ",
    "no": "አይደለም",
    "all": "ሁሉም",
    "none": "የለም",
    "total": "ድምር",
    "hours": "ሰዓት",
    "days": "ቀናት",
    "today": "ዛሬ",
    "loading": "በመጫን ላይ...",
    "saved": "ተቀምጧል",
    "error": "ስህተት ተከስቷል",
    "confirm": "እርግጠኛ ነዎት?",
    "required": "ይህ መስክ ያስፈልጋል",
    "back": "ተመለስ",
    "print": "አትም",
}


def t(key: str, **kwargs: object) -> str:
    """Look up an Amharic string. Unknown keys return the key itself, which
    makes a missing translation visible on screen rather than silently
    rendering an empty label."""
    text = S.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def all_strings() -> dict[str, str]:
    """The whole table, for injecting into the front end in one go."""
    return dict(S)
