"""SQLite persistence layer for AASE.

The optimizer consumes the same normalized structures whether data came from
an ERP API, CSV/XLSX, or the PDF/OCR ingestion pipeline.
"""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3

from aase import calendar_day_penalties

DB_PATH = Path(__file__).resolve().parent / "aase.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (id TEXT PRIMARY KEY, size INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, qualified_courses TEXT DEFAULT '', available_slots TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS rooms (name TEXT PRIMARY KEY, capacity INTEGER DEFAULT 0, type TEXT DEFAULT 'Classroom');
CREATE TABLE IF NOT EXISTS classes (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, slot TEXT NOT NULL, course TEXT NOT NULL, teacher TEXT NOT NULL, room TEXT NOT NULL, group_id TEXT NOT NULL, source TEXT DEFAULT 'manual', is_locked INTEGER DEFAULT 0, allocation_status TEXT DEFAULT 'assigned', rejection_reason TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS absences (id INTEGER PRIMARY KEY AUTOINCREMENT, teacher TEXT NOT NULL, date TEXT, status TEXT DEFAULT 'absent', reason TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS calendar_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_date TEXT, event TEXT, event_type TEXT, scheduling_rule TEXT);
CREATE TABLE IF NOT EXISTS optimization_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, operation TEXT, status TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS allocation_rejections (id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL, teacher TEXT NOT NULL, reason TEXT DEFAULT '', approval_status TEXT DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(class_id));
"""


@contextmanager
def connect():
    """Open a row-factory connection and always close it after use."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(classes)")}
        if "allocation_status" not in columns:
            conn.execute("ALTER TABLE classes ADD COLUMN allocation_status TEXT DEFAULT 'assigned'")
        if "rejection_reason" not in columns:
            conn.execute("ALTER TABLE classes ADD COLUMN rejection_reason TEXT DEFAULT ''")
        rejection_columns = {row["name"] for row in conn.execute("PRAGMA table_info(allocation_rejections)")}
        if "approval_status" not in rejection_columns:
            conn.execute("ALTER TABLE allocation_rejections ADD COLUMN approval_status TEXT DEFAULT 'pending'")
        conn.commit()


def has_core_data():
    """Return True when a real timetable dataset already exists.

    This prevents Streamlit session startup from reseeding demo classes over
    another user's imported timetable.
    """
    init_db()
    with connect() as conn:
        return conn.execute("SELECT EXISTS(SELECT 1 FROM classes LIMIT 1)").fetchone()[0] == 1


def seed_from_data(data, source="demo", replace_classes=True):
    init_db()
    # Demo data is a fallback only. Never overwrite an existing imported or
    # manually-created timetable just because a new browser session started.
    if source == "demo" and has_core_data():
        return

    with connect() as conn:
        if replace_classes:
            conn.execute("DELETE FROM classes")
            # A replacement import must not leave stale teachers/rooms/students
            # from the previous dataset behind.
            if source != "demo":
                conn.execute("DELETE FROM teachers")
                conn.execute("DELETE FROM rooms")
                conn.execute("DELETE FROM students")

        for student in data.get("students", []):
            conn.execute(
                "INSERT INTO students(id,size) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET size=excluded.size",
                (student.get("id", "UNKNOWN"), student.get("size", 0)),
            )
        for teacher in data.get("teachers", []):
            conn.execute(
                """INSERT INTO teachers(name,qualified_courses,available_slots) VALUES(?,?,?)
                   ON CONFLICT(name) DO UPDATE SET qualified_courses=excluded.qualified_courses,
                   available_slots=excluded.available_slots""",
                (
                    teacher.get("name", "UNKNOWN"),
                    "|".join(teacher.get("qualified_courses", [])),
                    json.dumps(teacher.get("available_slots", {})),
                ),
            )
        for room in data.get("rooms", []):
            conn.execute(
                "INSERT INTO rooms(name,capacity,type) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET capacity=excluded.capacity,type=excluded.type",
                (room.get("name", "UNKNOWN"), max(0, int(room.get("capacity", 0) or 0)), room.get("type", "Classroom")),
            )
        if replace_classes:
            for row in data.get("classes", []):
                conn.execute(
                    "INSERT INTO classes(day,slot,course,teacher,room,group_id,source) VALUES(?,?,?,?,?,?,?)",
                    (
                        row.get("day", "UNKNOWN"), row.get("slot", "UNKNOWN"),
                        row.get("course", "UNKNOWN"), row.get("teacher", "UNKNOWN"),
                        row.get("room", "UNKNOWN"), row.get("group", "UNKNOWN"), source,
                    ),
                )
        conn.commit()


def load_data():
    init_db()
    with connect() as conn:
        classes = [dict(r) for r in conn.execute("SELECT id,day,slot,course,teacher,room,group_id AS 'group',source,allocation_status,rejection_reason FROM classes ORDER BY id")]
        teachers = [
            {"name": r["name"], "qualified_courses": [x for x in r["qualified_courses"].split("|") if x], "available_slots": json.loads(r["available_slots"] or "{}")}
            for r in conn.execute("SELECT name,qualified_courses,available_slots FROM teachers ORDER BY name")
        ]
        rooms = [dict(r) for r in conn.execute("SELECT name,capacity,type FROM rooms ORDER BY name")]
        students = [dict(r) for r in conn.execute("SELECT id,size FROM students ORDER BY id")]
        calendar_events = [
            {"date": r["event_date"], "name": r["event"], "type": r["event_type"], "scheduling_rule": r["scheduling_rule"]}
            for r in conn.execute("SELECT event_date,event,event_type,scheduling_rule FROM calendar_events ORDER BY event_date,id")
        ]
        absences = [dict(r) for r in conn.execute("SELECT teacher,date,status,reason FROM absences WHERE status='absent' ORDER BY date,id")]
        allocation_rejections = [dict(r) for r in conn.execute("SELECT class_id,teacher,reason,approval_status,created_at FROM allocation_rejections ORDER BY id DESC")]

    data = {"classes": classes, "teachers": teachers, "rooms": rooms, "students": students, "calendar_events": calendar_events, "absences": absences, "allocation_rejections": allocation_rejections}
    if calendar_events:
        restricted = any(e.get("type") == "restricted_holiday" and e.get("scheduling_rule") == "blocked" for e in calendar_events)
        data["_calendar_day_penalties"] = calendar_day_penalties(calendar_events, restricted_as_holiday=restricted)
    return data



def set_teacher_availability(teacher, day, slots, available=True):
    """Add or remove exact timetable slots from a teacher's availability."""
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT available_slots FROM teachers WHERE name=?", (teacher,)).fetchone()
        if not row:
            raise ValueError(f"Teacher not found: {teacher}")
        availability = json.loads(row["available_slots"] or "{}")
        current = list(availability.get(day, []))
        if available:
            current = list(dict.fromkeys(current + list(slots)))
        else:
            blocked = set(slots)
            current = [slot for slot in current if slot not in blocked]
        availability[day] = current
        conn.execute("UPDATE teachers SET available_slots=? WHERE name=?", (json.dumps(availability), teacher))
        conn.commit()


def reject_allocation(class_id, teacher, reason=""):
    """Mark an automatic allocation as rejected by the assigned teacher."""
    init_db()
    with connect() as conn:
        conn.execute("UPDATE classes SET allocation_status='rejected', rejection_reason=? WHERE id=? AND teacher=?", (reason, class_id, teacher))
        conn.execute("INSERT INTO allocation_rejections(class_id,teacher,reason) VALUES(?,?,?) ON CONFLICT(class_id) DO UPDATE SET teacher=excluded.teacher, reason=excluded.reason, created_at=CURRENT_TIMESTAMP", (class_id, teacher, reason))
        conn.commit()


def approve_allocation_rejection(class_id):
    """Approve a teacher rejection; the optimizer will create a replacement record."""
    init_db()
    with connect() as conn:
        conn.execute("UPDATE allocation_rejections SET approval_status='approved' WHERE class_id=?", (class_id,))
        conn.commit()


def mark_reallocation_complete(class_id, new_class_id):
    init_db()
    with connect() as conn:
        conn.execute("UPDATE allocation_rejections SET approval_status='reallocated', reason=reason || ? WHERE class_id=?",
                     (f" | Replacement class ID: {new_class_id}", class_id))
        conn.commit()


def accept_allocation(class_id, teacher):
    """Restore a previously rejected allocation."""
    init_db()
    with connect() as conn:
        conn.execute("UPDATE classes SET allocation_status='assigned', rejection_reason='' WHERE id=? AND teacher=?", (class_id, teacher))
        conn.execute("DELETE FROM allocation_rejections WHERE class_id=? AND teacher=?", (class_id, teacher))
        conn.commit()

def record_absence(teacher, absence_date, reason=""):
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO absences(teacher,date,status,reason) VALUES(?,?,?,?)", (teacher, absence_date, "absent", reason))
        conn.commit()


def absence_count():
    init_db()
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM absences WHERE status='absent'").fetchone()[0]


def save_calendar(events, restricted_as_holiday=False):
    """Persist normalized academic-calendar parser output."""
    init_db()
    with connect() as conn:
        conn.execute("DELETE FROM calendar_events")
        for e in events:
            event_type = e.get("type", "academic_event")
            if event_type == "holiday":
                rule = "blocked"
            elif event_type == "restricted_holiday":
                rule = "blocked" if restricted_as_holiday else "optional"
            else:
                rule = "event"
            conn.execute(
                "INSERT INTO calendar_events(event_date,event,event_type,scheduling_rule) VALUES(?,?,?,?)",
                (str(e.get("date", "")), e.get("name", e.get("event", "")), event_type, e.get("scheduling_rule", rule)),
            )
        conn.commit()


def record_run(operation, status, summary):
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO optimization_runs(operation,status,summary) VALUES(?,?,?)", (operation, status, summary))
        conn.commit()


def recent_runs(limit=10):
    init_db()
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM optimization_runs ORDER BY id DESC LIMIT ?", (limit,))]
