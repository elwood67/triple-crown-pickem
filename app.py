"""
🏇 Triple Crown Pick'em — A friends-only horse race picking app
================================================================
Pick 3 horses per race, score points based on their finishing positions,
climb the leaderboard. No money, just bragging rights.

Scoring:  1st=20, 2nd=12, 3rd=8, 4th=4, 5th=2, anything else=0
Sum your 3 horses' points = your score for that race.

Run with: streamlit run app.py
"""

import sqlite3
import re
import streamlit as st
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
DB_PATH = Path(__file__).parent / "horse_picks.db"
SCORING = {1: 20, 2: 12, 3: 8, 4: 4, 5: 2}  # Position -> base points


def get_admin_password() -> str:
    """Read admin password from Streamlit secrets, falling back to a local
    dev password if no secrets file exists. NEVER hard-code real passwords."""
    try:
        pw = st.secrets.get("admin_password", "")
        if pw:
            return pw
    except (FileNotFoundError, KeyError):
        pass
    # Local-dev fallback (only used when there's no secrets.toml file).
    return "local-dev-password"

# Longshot multiplier tiers (applied to base points based on ML odds)
# Threshold = decimal odds threshold (e.g., favorite is anything where horse pays
# back less than 3.5x stake, mid-tier 3.5-10x, longshot 10-20x, bomb 20x+).
ODDS_TIERS = [
    # (decimal_odds_max_exclusive, multiplier, label, emoji)
    (3.5,    1.0, "Favorite",    "⭐"),   # ML 5/2 or shorter (e.g., 6/5, 8/5, 2-1)
    (10.5,   1.3, "Mid-tier",    ""),     # 3-1 to 9-1
    (20.5,   1.7, "Longshot",    "🎯"),   # 10-1 to 19-1
    (9999.0, 2.5, "Bomb",        "💣"),   # 20-1 and up
]


def ml_to_decimal(ml: str | None) -> float | None:
    """Convert morning-line odds string to decimal (total return per $1 stake).
    Examples:
        '4-1'   -> 5.0   (win $4 + your $1 back)
        '5/2'   -> 3.5
        '6/5'   -> 2.2
        'EVEN'  -> 2.0
        None    -> None
    """
    if not ml:
        return None
    s = ml.strip().upper().replace(" ", "")
    if s in ("EVEN", "EVS", "EVENS", "1-1", "1/1"):
        return 2.0
    m = re.match(r"^(\d+)\s*[-/]\s*(\d+)$", s)
    if not m:
        return None
    num, den = int(m.group(1)), int(m.group(2))
    if den == 0:
        return None
    return (num / den) + 1.0


def odds_tier(ml: str | None):
    """Return (multiplier, label, emoji) for a given ML odds string.
    Returns the mid-tier as default if odds can't be parsed."""
    decimal = ml_to_decimal(ml)
    if decimal is None:
        return (1.3, "Mid-tier", "")
    for max_dec, mult, label, emoji in ODDS_TIERS:
        if decimal < max_dec:
            return (mult, label, emoji)
    return ODDS_TIERS[-1][1:]


def score_for_position_with_odds(pos: int | None, ml: str | None) -> tuple[int, float, int]:
    """Compute the points a horse earns given its finish position and ML odds.

    Returns (base_points, multiplier, final_points).
    final_points is rounded to nearest int.
    """
    base = SCORING.get(pos, 0) if pos else 0
    if base == 0:
        return (0, 1.0, 0)
    mult, _, _ = odds_tier(ml)
    return (base, mult, round(base * mult))

st.set_page_config(
    page_title="🏇 Triple Crown Pick'em",
    page_icon="🏇",
    layout="wide",
)

# ============================================================
# SMART ENTRY PARSER
# ============================================================
# Recognizes morning-line odds: "4-1", "12-1", "5/2", "9/2", "7/2", "EVEN", "EVS"
# Denominator must be 1, 2, or 5 (the only realistic ML odds denominators)
ML_PATTERN = re.compile(
    r"\b(\d{1,3}\s*[-/]\s*[125]|EVEN|EVS|EVENS)\b",
    re.IGNORECASE,
)

# Lines that are clearly headers, not horses
HEADER_HINTS = re.compile(
    r"\b(post|pp|horse|jockey|trainer|owner|weight|wgt|m/l|ml|odds|"
    r"morning\s*line|name|silks|program|race\s*\d+|entries?|field|"
    r"scratched?|also\s*eligible)\b",
    re.IGNORECASE,
)

# Words that are NOT horse names — used to clean up parsed names
STOP_WORDS = re.compile(
    r"^(post|pp|horse|jockey|jky|trainer|trn|tr|owner|wgt|weight|"
    r"m/l|ml|odds|silks?)$",
    re.IGNORECASE,
)


def _looks_like_jockey_or_trainer(token: str) -> bool:
    """Detect a jockey/trainer attribution token like 'Ortiz, Jr., I.' or 'I. Ortiz' or 'Trained by Pletcher'."""
    if re.search(r",\s*(Jr\.?|Sr\.?|II|III|[A-Z]\.)", token):
        return True
    if re.match(r"^[A-Z]\.\s*[A-Z]", token):
        return True
    # "Trained by X" or "X trainee" — these are attribution phrases, not names
    if re.search(r"\b(trained\s+by|trainee)\b", token, re.IGNORECASE):
        return True
    return False


def parse_entries_text(text: str) -> list:
    """
    Parse pasted entries text into a list of {post, name, ml} dicts.

    Handles common formats:
        "1  Renegade  Ortiz Jr  Pletcher  4-1"
        "Post 1: Renegade (4-1)"
        "1. Renegade — 4-1"
        "Renegade  4-1"
        "1   Renegade"  (no ML)
    Returns [] if nothing parseable.
    """
    rows = []
    seen_names = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip lines that are just numbers or symbols
        if re.fullmatch(r"[\d\s\-_=*]+", line):
            continue

        # Pull out ML odds
        ml_match = ML_PATTERN.search(line)
        ml = None
        if ml_match:
            ml = ml_match.group(0).strip().upper().replace(" ", "")
            if ml in ("EVS", "EVENS"):
                ml = "EVEN"
            line = (line[:ml_match.start()] + line[ml_match.end():]).strip()

        # Pull out leading post number — handles "1", "1.", "1)", "Post 1:", "1 -"
        post = None
        post_match = re.match(
            r"^\s*(?:post\s*)?(\d{1,2})\s*[\.\):\-–—]?\s*",
            line, re.IGNORECASE,
        )
        if post_match:
            post = int(post_match.group(1))
            line = line[post_match.end():].strip()

        # NOW check for header content (after stripping post + ml).
        # Skip only if the row is OVERWHELMINGLY header words (e.g.,
        # "Post Horse Jockey Trainer ML"), not just one header word in
        # a real horse name (e.g., "Test Horse" or "Lookin At Lucky").
        remaining_clean = re.sub(r"[^\w\s]", " ", line).strip()
        if remaining_clean:
            words = remaining_clean.split()
            if words:
                header_word_count = sum(
                    1 for w in words if HEADER_HINTS.fullmatch(w)
                )
                # Skip only if 75%+ of words are header-only AND at least 2 words
                if len(words) >= 2 and header_word_count / len(words) >= 0.75:
                    continue

        # Split into parts on big whitespace gaps / tabs / parens / em-dashes
        parts = re.split(r"\s{2,}|\t+|\s*[\|\(\)\—–\-]\s+", line)
        parts = [p.strip(" -—–.,()[]") for p in parts if p.strip()]
        if not parts:
            continue

        # First non-stopword, non-jockey-looking part is the horse name
        name = None
        for part in parts:
            if not part:
                continue
            if STOP_WORDS.match(part):
                continue
            if _looks_like_jockey_or_trainer(part):
                continue
            name = part
            break

        if not name:
            continue
        if len(name) < 2 or name.isdigit():
            continue
        # Reject names that look like leftover header rows
        if HEADER_HINTS.match(name):
            continue
        # Real horse names don't contain colons or "Miles"/"Furlongs"/"Purse"
        if ":" in name:
            continue
        if re.search(r"\b(miles?|furlongs?|purse|distance|grade|stakes\s+race)\b", name, re.IGNORECASE):
            continue

        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        rows.append({"post": post, "name": name, "ml": ml})

    return rows



# ============================================================
# DATABASE — supports both SQLite (local dev) and Postgres (production)
# ============================================================
# The connection string is read from Streamlit secrets `database_url`.
# If the secret is missing or empty, we fall back to a local SQLite file
# (so you can run/test locally without internet).

def _get_database_url() -> str | None:
    """Get DB URL from secrets, else None (= use local SQLite)."""
    try:
        url = st.secrets.get("database_url", "").strip()
        return url or None
    except (FileNotFoundError, KeyError):
        return None


def _is_postgres() -> bool:
    url = _get_database_url()
    return bool(url and url.startswith(("postgres://", "postgresql://")))


@contextmanager
def get_db():
    """Yield a DB connection. Auto-selects SQLite or Postgres based on secrets."""
    if _is_postgres():
        import psycopg  # lazy import so SQLite-only deploys don't need it
        from psycopg.rows import dict_row
        url = _get_database_url()
        conn = psycopg.connect(url, row_factory=dict_row, autocommit=False)
        try:
            yield _PgWrapper(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


class _PgWrapper:
    """Thin wrapper that lets us call .execute(sql, params) on a psycopg connection
    while auto-translating SQLite `?` placeholders to `%s`. Returns objects that
    behave like SQLite cursors (.fetchone(), .fetchall(), .lastrowid)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()):
        translated = _translate_sql(sql)
        cur = self._conn.cursor()
        cur.execute(translated, params)
        return _PgCursor(cur, translated)

    def executescript(self, script: str):
        # For schema setup: split on ';' and execute each non-empty statement.
        cur = self._conn.cursor()
        for stmt in script.split(";"):
            s = stmt.strip()
            if not s:
                continue
            cur.execute(_translate_sql(s))
        cur.close()


class _PgCursor:
    """Wraps psycopg cursor to emulate SQLite cursor semantics for our app."""

    def __init__(self, cur, original_sql: str):
        self._cur = cur
        self._original_sql = original_sql.lstrip().lower()

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur.fetchall())

    @property
    def lastrowid(self):
        # For RETURNING-based inserts, fetch the returned id.
        if "returning" in self._original_sql:
            row = self._cur.fetchone()
            if row:
                return list(row.values())[0]
        return None


def _translate_sql(sql: str) -> str:
    """Translate SQLite-flavored SQL to Postgres-flavored SQL.
    Handles the small set of differences this app actually uses."""
    s = sql

    # Schema-time translations (only matter inside CREATE TABLE)
    if "CREATE TABLE" in s.upper():
        s = re.sub(
            r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
            "SERIAL PRIMARY KEY",
            s,
            flags=re.IGNORECASE,
        )

    # INSERT OR IGNORE → ON CONFLICT DO NOTHING
    # Need to add ON CONFLICT clause based on table's unique constraint.
    # Simpler & general approach: rewrite to "ON CONFLICT DO NOTHING".
    s = re.sub(
        r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
        "INSERT INTO",
        s,
        flags=re.IGNORECASE,
    )
    if "INSERT INTO" in s.upper() and "OR IGNORE" not in sql.upper():
        # Only add ON CONFLICT if the original used OR IGNORE
        pass
    if re.search(r"\bINSERT\s+OR\s+IGNORE\b", sql, re.IGNORECASE):
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # If this is an INSERT INTO races / horses / etc and original wanted
    # lastrowid behavior, append RETURNING for primary keys.
    # We detect the table and infer the PK column name.
    pk_map = {
        "races": "race_id",
        "horses": "horse_id",
        "picks": "pick_id",
    }
    m = re.match(
        r"\s*INSERT\s+INTO\s+(\w+)",
        s,
        flags=re.IGNORECASE,
    )
    if m:
        table = m.group(1).lower()
        if table in pk_map and "RETURNING" not in s.upper() and "ON CONFLICT" not in s.upper():
            s = s.rstrip().rstrip(";") + f" RETURNING {pk_map[table]}"

    # COLLATE NOCASE → LOWER() (used in ORDER BY)
    s = re.sub(
        r"ORDER\s+BY\s+(\w+)\s+COLLATE\s+NOCASE",
        r"ORDER BY LOWER(\1)",
        s,
        flags=re.IGNORECASE,
    )

    # SQLite ? placeholders → Postgres %s
    s = s.replace("?", "%s")

    return s


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS races (
                race_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                race_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                race_url TEXT,
                picks_lock_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS horses (
                horse_id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id INTEGER NOT NULL,
                horse_name TEXT NOT NULL,
                post_position INTEGER,
                morning_line TEXT,
                final_position INTEGER,
                FOREIGN KEY (race_id) REFERENCES races(race_id) ON DELETE CASCADE,
                UNIQUE (race_id, horse_name)
            );

            CREATE TABLE IF NOT EXISTS picks (
                pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                race_id INTEGER NOT NULL,
                horse_id INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE,
                FOREIGN KEY (race_id) REFERENCES races(race_id) ON DELETE CASCADE,
                FOREIGN KEY (horse_id) REFERENCES horses(horse_id) ON DELETE CASCADE,
                UNIQUE (username, race_id, slot)
            );
        """)

        # --- Lightweight migrations for older databases ---
        _migrate_add_column(conn, "races", "race_url", "TEXT")
        _migrate_add_column(conn, "races", "picks_lock_at", "TEXT")


def _migrate_add_column(conn, table: str, column: str, col_type: str):
    """Add a column to a table if it doesn't already exist. Works for both backends."""
    if _is_postgres():
        # Postgres supports IF NOT EXISTS on ADD COLUMN
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}")
    else:
        # SQLite: check pragma first, then add if missing
        cur = conn.execute(f"PRAGMA table_info({table})")
        cols = [row["name"] for row in cur.fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def safe_race_url(url: str | None) -> str | None:
    """Return the URL only if it's a sane http(s) link, else None.
    Prevents javascript: and data: URLs sneaking into clickable links."""
    if not url:
        return None
    u = url.strip()
    if not u:
        return None
    if not (u.startswith("http://") or u.startswith("https://")):
        return None
    return u


# ============================================================
# DEADLINE / TIME HELPERS
# ============================================================
# All deadlines are stored in Eastern Time (US racing convention) as
# naive ISO strings like "2026-05-16T18:00:00". We compare in ET, display in ET.

from datetime import timezone, timedelta

# US Eastern Time. Note: this doesn't auto-handle DST cleanly but
# for picks-lock comparison it's close enough — the lock window is
# wide and missing by an hour around DST transitions doesn't matter.
# For perfect DST handling, install zoneinfo (Python 3.9+).
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    # Fallback: fixed UTC-5 offset (won't handle DST)
    ET = timezone(timedelta(hours=-5))


def now_et() -> datetime:
    """Current time in Eastern Time (timezone-aware)."""
    return datetime.now(ET)


def parse_lock_time(iso_str: str | None) -> datetime | None:
    """Parse a stored ISO datetime string as Eastern Time (timezone-aware).
    Returns None if invalid or empty."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt
    except (ValueError, TypeError):
        return None


def is_locked(lock_at_iso: str | None) -> bool:
    """True if the deadline has passed. Picks should be rejected."""
    dt = parse_lock_time(lock_at_iso)
    if dt is None:
        return False  # no deadline set → never locked (legacy races)
    return now_et() >= dt


def format_lock_time(iso_str: str | None) -> str:
    """Format a deadline for human display, e.g. 'Sat May 16, 6:00 PM ET'."""
    dt = parse_lock_time(iso_str)
    if dt is None:
        return ""
    return dt.strftime("%a %b %-d, %-I:%M %p ET") if hasattr(dt, "strftime") else str(dt)


def time_until_lock(lock_at_iso: str | None) -> str:
    """Human-readable time-until-lock, e.g. '2 hours 14 minutes' or 'just a moment'.
    Returns '' if no deadline. Returns 'closed' if past."""
    dt = parse_lock_time(lock_at_iso)
    if dt is None:
        return ""
    delta = dt - now_et()
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "closed"
    days = secs // 86400
    hours = (secs % 86400) // 3600
    minutes = (secs % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and not days:  # don't show minutes if we're showing days
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        return "less than a minute"
    return " ".join(parts)


# ============================================================
# DATA ACCESS
# ============================================================
def upsert_user(username: str):
    username = username.strip()
    if not username:
        return
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, created_at) VALUES (?, ?)",
            (username, now_iso()),
        )


def list_users():
    with get_db() as conn:
        return [r["username"] for r in conn.execute(
            "SELECT username FROM users ORDER BY username COLLATE NOCASE"
        )]


def list_races(status_filter=None):
    with get_db() as conn:
        if status_filter:
            placeholders = ",".join("?" * len(status_filter))
            rows = conn.execute(
                f"SELECT * FROM races WHERE status IN ({placeholders}) "
                "ORDER BY race_date DESC, race_id DESC",
                tuple(status_filter),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM races ORDER BY race_date DESC, race_id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_race(race_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM races WHERE race_id = ?", (race_id,)
        ).fetchone()
        return dict(row) if row else None


def list_horses(race_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM horses WHERE race_id = ? "
            "ORDER BY COALESCE(post_position, 999), horse_name",
            (race_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_race(name, race_date, race_url=None, picks_lock_at=None):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO races (name, race_date, status, race_url, picks_lock_at, created_at) "
            "VALUES (?, ?, 'open', ?, ?, ?)",
            (name, race_date, race_url, picks_lock_at, now_iso()),
        )
        return cur.lastrowid


def update_race_url(race_id, race_url):
    """Set or clear the race link. Pass None or '' to clear."""
    url = (race_url or "").strip() or None
    with get_db() as conn:
        conn.execute(
            "UPDATE races SET race_url = ? WHERE race_id = ?",
            (url, race_id),
        )


def update_picks_lock_at(race_id, picks_lock_at: str | None):
    """Set or clear the picks deadline. Pass None to clear."""
    with get_db() as conn:
        conn.execute(
            "UPDATE races SET picks_lock_at = ? WHERE race_id = ?",
            (picks_lock_at, race_id),
        )


def auto_close_expired_races():
    """Flip any race from 'open' to 'closed' if its picks deadline has passed.
    Called on every page load; cheap and idempotent."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT race_id, picks_lock_at FROM races WHERE status = 'open' AND picks_lock_at IS NOT NULL"
        ).fetchall()
        for r in rows:
            row = dict(r)
            if is_locked(row["picks_lock_at"]):
                conn.execute(
                    "UPDATE races SET status = 'closed' WHERE race_id = ?",
                    (row["race_id"],),
                )


def add_horse(race_id, horse_name, post_position=None, morning_line=None):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO horses (race_id, horse_name, post_position, morning_line) "
            "VALUES (?, ?, ?, ?)",
            (race_id, horse_name.strip(), post_position, morning_line),
        )


def remove_horse(horse_id):
    with get_db() as conn:
        conn.execute("DELETE FROM horses WHERE horse_id = ?", (horse_id,))


def update_race_status(race_id, status):
    with get_db() as conn:
        conn.execute(
            "UPDATE races SET status = ? WHERE race_id = ?", (status, race_id)
        )


def delete_race(race_id):
    """Delete a race and all its horses + picks (FK CASCADE handles the children)."""
    with get_db() as conn:
        conn.execute("DELETE FROM races WHERE race_id = ?", (race_id,))


def set_finishing_positions(race_id, position_map):
    """position_map: {horse_id: final_position or None}"""
    with get_db() as conn:
        # Clear first so removed positions don't linger
        conn.execute(
            "UPDATE horses SET final_position = NULL WHERE race_id = ?",
            (race_id,),
        )
        for hid, pos in position_map.items():
            if pos is not None:
                conn.execute(
                    "UPDATE horses SET final_position = ? WHERE horse_id = ?",
                    (pos, hid),
                )


def save_picks(username, race_id, horse_ids):
    """horse_ids is a list of 3 horse_ids in slot order. Replaces any existing picks.
    Raises if race is locked or already closed."""
    if len(horse_ids) != 3:
        raise ValueError("Must pick exactly 3 horses")
    if len(set(horse_ids)) != 3:
        raise ValueError("All 3 horses must be different")

    # Server-side deadline enforcement — defense in depth.
    race = get_race(race_id)
    if race is None:
        raise ValueError("Race not found")
    if race["status"] != "open":
        raise ValueError(f"Picks are closed for this race (status: {race['status']})")
    if is_locked(race.get("picks_lock_at")):
        raise ValueError("The picks deadline for this race has passed")

    with get_db() as conn:
        conn.execute(
            "DELETE FROM picks WHERE username = ? AND race_id = ?",
            (username, race_id),
        )
        for slot, hid in enumerate(horse_ids, start=1):
            conn.execute(
                "INSERT INTO picks (username, race_id, horse_id, slot, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, race_id, hid, slot, now_iso()),
            )


def get_user_picks(username, race_id):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.slot, h.horse_id, h.horse_name, h.post_position, h.morning_line, h.final_position
            FROM picks p
            JOIN horses h ON h.horse_id = p.horse_id
            WHERE p.username = ? AND p.race_id = ?
            ORDER BY p.slot
        """, (username, race_id)).fetchall()
        return [dict(r) for r in rows]


def get_all_picks_for_race(race_id):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.username, p.slot, h.horse_id, h.horse_name, h.morning_line, h.final_position
            FROM picks p
            JOIN horses h ON h.horse_id = p.horse_id
            WHERE p.race_id = ?
            ORDER BY p.username, p.slot
        """, (race_id,)).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# SCORING
# ============================================================
def score_for_position(pos):
    """Legacy alias — base score only. Prefer score_for_position_with_odds."""
    if pos is None:
        return 0
    return SCORING.get(pos, 0)


def race_leaderboard(race_id):
    """Returns list of {username, score, picks: [{horse_name, final_position, base_points, multiplier, points, ml}]}"""
    picks = get_all_picks_for_race(race_id)
    by_user = {}
    for p in picks:
        by_user.setdefault(p["username"], []).append(p)

    rows = []
    for username, user_picks in by_user.items():
        user_picks.sort(key=lambda x: x["slot"])
        pick_details = []
        score = 0
        for p in user_picks:
            base, mult, final = score_for_position_with_odds(
                p["final_position"], p.get("morning_line")
            )
            score += final
            pick_details.append({
                "horse_name": p["horse_name"],
                "final_position": p["final_position"],
                "ml": p.get("morning_line"),
                "base_points": base,
                "multiplier": mult,
                "points": final,
            })
        rows.append({
            "username": username,
            "score": score,
            "picks": pick_details,
        })
    rows.sort(key=lambda r: (-r["score"], r["username"].lower()))
    return rows


def cumulative_leaderboard():
    """Aggregate scores across all settled races."""
    settled = [r for r in list_races(status_filter=["settled"])]
    totals = {}
    for race in settled:
        race_rows = race_leaderboard(race["race_id"])
        for row in race_rows:
            u = row["username"]
            if u not in totals:
                totals[u] = {"username": u, "total_score": 0, "races_played": 0, "breakdown": []}
            totals[u]["total_score"] += row["score"]
            totals[u]["races_played"] += 1
            totals[u]["breakdown"].append({
                "race_name": race["name"],
                "score": row["score"],
            })
    result = list(totals.values())
    result.sort(key=lambda r: (-r["total_score"], r["username"].lower()))
    return result


# ============================================================
# UI HELPERS
# ============================================================
def position_emoji(pos):
    if pos == 1:
        return "🥇"
    if pos == 2:
        return "🥈"
    if pos == 3:
        return "🥉"
    if pos is None:
        return "—"
    return f"#{pos}"


def medal_for_rank(rank):
    if rank == 1:
        return "🥇"
    if rank == 2:
        return "🥈"
    if rank == 3:
        return "🥉"
    return f"#{rank}"


# ============================================================
# PAGES
# ============================================================
def page_home():
    st.title("🏇 Triple Crown Pick'em")
    st.caption("Pick 3 horses. Score points. Bragging rights only — no money, just glory.")

    st.markdown("---")

    races = list_races()
    open_races = [r for r in races if r["status"] == "open"]
    closed_races = [r for r in races if r["status"] == "closed"]
    settled_races = [r for r in races if r["status"] == "settled"]

    col1, col2, col3 = st.columns(3)
    col1.metric("🟢 Open Races", len(open_races))
    col2.metric("🔒 Closed (waiting on results)", len(closed_races))
    col3.metric("🏁 Settled", len(settled_races))

    st.markdown("### 📋 How it works")
    st.markdown("""
    1. **Pick a username** in the sidebar (just a name — no password)
    2. **Find an open race** below and pick your **3 horses**
    3. **Wait for the race** — admin enters official results
    4. **Score points** based on where your horses finished AND their morning-line odds:

    **Base points by finish:**
    🥇 1st = **20** | 🥈 2nd = **12** | 🥉 3rd = **8** | 4th = **4** | 5th = **2** | 6th+ = 0

    **Odds multiplier — picking longshots that hit pays BIG:**
    ⭐ **Favorite** (5/2 or shorter) → ×1.0
    *Mid-tier* (3/1 to 9/1) → ×1.3
    🎯 **Longshot** (10/1 to 19/1) → ×1.7
    💣 **Bomb** (20/1+) → ×2.5

    5. **Highest total wins the round.** Cumulative leaderboard tracks the season.
    """)

    with st.expander("🧮 Quick example"):
        st.markdown("""
        Two players each pick the winner of a race:
        - **Player A** picks the **6/5 favorite** that wins → 20 × 1.0 = **20 pts**
        - **Player B** picks the **30/1 bomb** that wins → 20 × 2.5 = **50 pts**

        Picking favorites is safe but doesn't pay big. Calling a longshot that hits is the move.
        """)

    if open_races:
        st.markdown("### 🟢 Races Open for Picks")
        for race in open_races:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                link_str = ""
                if race.get("race_url"):
                    link_str = f"  \n🔗 [View entries / past performances]({race['race_url']})"
                lock_str = ""
                if race.get("picks_lock_at"):
                    remaining = time_until_lock(race["picks_lock_at"])
                    lock_str = f"  \n⏰ Picks lock in **{remaining}** ({format_lock_time(race['picks_lock_at'])})"
                c1.markdown(f"**{race['name']}**  \n*{race['race_date']}*{link_str}{lock_str}")
                horse_count = len(list_horses(race["race_id"]))
                c2.markdown(f"🐎 {horse_count} horses entered")

    if settled_races:
        st.markdown("### 🏁 Recent Results")
        for race in settled_races[:3]:
            lb = race_leaderboard(race["race_id"])
            with st.container(border=True):
                link_str = ""
                if race.get("race_url"):
                    link_str = f" • 🔗 [Race page]({race['race_url']})"
                st.markdown(f"**{race['name']}** — *{race['race_date']}*{link_str}")
                if lb:
                    winner = lb[0]
                    st.markdown(f"🏆 Round winner: **{winner['username']}** with {winner['score']} pts")
                else:
                    st.caption("No picks were submitted for this race.")


def page_make_picks():
    st.title("📝 Make Your Picks")

    username = st.session_state.get("username", "").strip()
    if not username:
        st.warning("👈 Set your username in the sidebar first!")
        return

    open_races = [r for r in list_races(status_filter=["open"])]
    if not open_races:
        st.info("No races are currently open for picks. Check back later! 🏇")
        return

    race_options = {f"{r['name']} ({r['race_date']})": r["race_id"] for r in open_races}
    race_label = st.selectbox("Choose a race:", list(race_options.keys()))
    race_id = race_options[race_label]
    race = get_race(race_id)
    horses = list_horses(race_id)

    if len(horses) < 3:
        st.warning(f"This race only has {len(horses)} horses entered — need at least 3 to pick. Admin needs to add more.")
        return

    # Show existing picks if any
    existing = get_user_picks(username, race_id)
    if existing:
        st.success(f"✅ You've already picked for this race. Update below to change.")

    st.markdown(f"### Pick your 3 horses for **{race['name']}**")
    if race.get("race_url"):
        st.markdown(
            f"🔗 [**Open entries / past performances on Equibase**]({race['race_url']})"
        )

    # Countdown / lock banner
    if race.get("picks_lock_at"):
        if is_locked(race["picks_lock_at"]):
            st.error(f"🔒 Picks closed at {format_lock_time(race['picks_lock_at'])}. No further changes.")
        else:
            remaining = time_until_lock(race["picks_lock_at"])
            st.info(
                f"⏰ Picks lock at **{format_lock_time(race['picks_lock_at'])}** — "
                f"that's **{remaining}** from now."
            )

    st.caption(
        "All 3 must be different. Order doesn't affect scoring. "
        "**⭐ Favorite | 🎯 Longshot | 💣 Bomb** — picking longshots that hit pays bigger."
    )

    def horse_label(h):
        post = f"#{h['post_position']} " if h['post_position'] else ""
        ml = h.get('morning_line')
        ml_str = f" ({ml})" if ml else ""
        _, _, emoji = odds_tier(ml)
        emoji_str = f" {emoji}" if emoji else ""
        return f"{post}{h['horse_name']}{ml_str}{emoji_str}"

    horse_choices = {horse_label(h): h["horse_id"] for h in horses}
    none_label = "— select a horse —"
    options = [none_label] + list(horse_choices.keys())

    # Pre-fill defaults from existing picks
    default_indices = [0, 0, 0]
    if existing:
        for idx, p in enumerate(existing):
            for i, h in enumerate(horses):
                if h["horse_id"] == p["horse_id"]:
                    label = horse_label(h)
                    if label in options:
                        default_indices[idx] = options.index(label)

    col1, col2, col3 = st.columns(3)
    pick1 = col1.selectbox("🐎 Pick #1", options, index=default_indices[0], key="p1")
    pick2 = col2.selectbox("🐎 Pick #2", options, index=default_indices[1], key="p2")
    pick3 = col3.selectbox("🐎 Pick #3", options, index=default_indices[2], key="p3")

    locked = is_locked(race.get("picks_lock_at"))
    if st.button(
        "🔒 Lock In My Picks",
        type="primary",
        use_container_width=True,
        disabled=locked,
    ):
        picks_raw = [pick1, pick2, pick3]
        if none_label in picks_raw:
            st.error("Please select all 3 horses.")
            return
        horse_ids = [horse_choices[p] for p in picks_raw]
        if len(set(horse_ids)) != 3:
            st.error("All 3 horses must be different!")
            return
        try:
            save_picks(username, race_id, horse_ids)
            st.success("🏇 Picks locked in! Good luck.")
            st.balloons()
        except Exception as e:
            st.error(f"Error saving picks: {e}")


def page_leaderboard():
    st.title("🏆 Leaderboard")

    tab_cumulative, tab_per_race = st.tabs(["📊 Season Standings", "🏁 By Race"])

    with tab_cumulative:
        st.markdown("### Cumulative Standings (across all settled races)")
        cumulative = cumulative_leaderboard()
        if not cumulative:
            st.info("No settled races yet — leaderboard will populate once results are entered.")
        else:
            for rank, row in enumerate(cumulative, start=1):
                with st.container(border=True):
                    c1, c2, c3 = st.columns([1, 3, 2])
                    c1.markdown(f"### {medal_for_rank(rank)}")
                    c2.markdown(f"**{row['username']}**  \n*{row['races_played']} race(s) played*")
                    c3.metric("Total Points", row["total_score"])
                    with st.expander("Race-by-race breakdown"):
                        for b in row["breakdown"]:
                            st.markdown(f"- *{b['race_name']}*: **{b['score']}** pts")

    with tab_per_race:
        races = list_races(status_filter=["settled", "closed"])
        if not races:
            st.info("No races have been closed or settled yet.")
            return
        race_options = {f"{r['name']} ({r['race_date']}) — {r['status']}": r["race_id"] for r in races}
        race_label = st.selectbox("Race:", list(race_options.keys()))
        race_id = race_options[race_label]
        race = get_race(race_id)
        lb = race_leaderboard(race_id)

        if race.get("race_url"):
            st.markdown(f"🔗 [View race entries / results on Equibase]({race['race_url']})")

        if race["status"] == "closed":
            st.info("🔒 This race is closed for picks but results haven't been entered yet.")
        if not lb:
            st.warning("No picks submitted for this race.")
            return

        for rank, row in enumerate(lb, start=1):
            with st.container(border=True):
                c1, c2 = st.columns([1, 5])
                c1.markdown(f"### {medal_for_rank(rank)}")
                c2.markdown(f"**{row['username']}** — {row['score']} pts")
                pick_lines = []
                for p in row["picks"]:
                    _, tier_label, tier_emoji = odds_tier(p.get("ml"))
                    ml_str = f" ({p['ml']})" if p.get("ml") else ""
                    emoji_str = f" {tier_emoji}" if tier_emoji else ""
                    if p.get("multiplier", 1.0) != 1.0 and p.get("base_points", 0) > 0:
                        breakdown = f"{p['base_points']} × {p['multiplier']:.1f} = "
                    else:
                        breakdown = ""
                    pick_lines.append(
                        f"- {p['horse_name']}{ml_str}{emoji_str} → "
                        f"{position_emoji(p['final_position'])} "
                        f"({breakdown}**{p['points']}** pts)"
                    )
                c2.markdown("\n".join(pick_lines))


def page_my_picks():
    st.title("📋 My Picks")
    username = st.session_state.get("username", "").strip()
    if not username:
        st.warning("👈 Set your username in the sidebar first!")
        return

    races = list_races()
    user_history = []
    for race in races:
        picks = get_user_picks(username, race["race_id"])
        if picks:
            pick_details = []
            score = 0
            for p in picks:
                base, mult, final = score_for_position_with_odds(
                    p["final_position"], p.get("morning_line")
                )
                score += final
                pick_details.append({
                    **p,
                    "base_points": base,
                    "multiplier": mult,
                    "points": final,
                })
            user_history.append({"race": race, "picks": pick_details, "score": score})

    if not user_history:
        st.info("You haven't made any picks yet. Head to **Make Picks** to get started.")
        return

    total = sum(h["score"] for h in user_history if h["race"]["status"] == "settled")
    settled_count = sum(1 for h in user_history if h["race"]["status"] == "settled")

    c1, c2, c3 = st.columns(3)
    c1.metric("Races Played", len(user_history))
    c2.metric("Settled Races", settled_count)
    c3.metric("Total Points", total)

    st.markdown("---")
    for item in user_history:
        race = item["race"]
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"### {race['name']}")
            link_str = f" • 🔗 [Race page]({race['race_url']})" if race.get("race_url") else ""
            c1.caption(f"{race['race_date']} • Status: **{race['status']}**{link_str}")
            if race["status"] == "settled":
                c2.metric("My Score", item["score"])
            else:
                c2.markdown("*Awaiting results*")

            for p in item["picks"]:
                _, tier_label, tier_emoji = odds_tier(p.get("morning_line"))
                ml_str = f" ({p['morning_line']})" if p.get("morning_line") else ""
                emoji_str = f" {tier_emoji}" if tier_emoji else ""
                tier_str = f" *[{tier_label}]*" if tier_label and race["status"] != "settled" else ""
                if race["status"] == "settled":
                    if p["multiplier"] != 1.0 and p["base_points"] > 0:
                        score_str = f"{p['base_points']} × {p['multiplier']:.1f} = **{p['points']} pts**"
                    else:
                        score_str = f"**{p['points']} pts**"
                    st.markdown(
                        f"- **{p['horse_name']}**{ml_str}{emoji_str} "
                        f"→ {position_emoji(p['final_position'])} ({score_str})"
                    )
                else:
                    st.markdown(
                        f"- **{p['horse_name']}**{ml_str}{emoji_str}{tier_str}"
                    )


def page_admin():
    st.title("⚙️ Admin")

    if not st.session_state.get("admin_authed"):
        pw = st.text_input("Admin password:", type="password")
        if st.button("Login"):
            if pw == get_admin_password():
                st.session_state["admin_authed"] = True
                st.rerun()
            else:
                st.error("Wrong password.")
        return

    st.success("✅ Admin mode")
    if st.button("Log out"):
        st.session_state["admin_authed"] = False
        st.rerun()

    tab_create, tab_manage, tab_results = st.tabs(
        ["➕ Create Race", "🐎 Manage Horses", "🏁 Enter Results"]
    )

    # --- CREATE RACE -------------------------------------------------
    with tab_create:
        st.markdown("### Create a new race")
        with st.form("new_race"):
            name = st.text_input("Race name (e.g. 'Preakness Stakes 2026')")
            from datetime import time as _time, date as _date
            race_date_obj = st.date_input("Race date")
            race_date = race_date_obj.isoformat()

            st.markdown("**Picks lock at (Eastern Time)** — required")
            st.caption("After this time, no new picks or changes can be saved. Friends see a countdown.")
            lc1, lc2 = st.columns(2)
            lock_date_obj = lc1.date_input("Lock date", value=race_date_obj, key="lock_date_new")
            lock_time_obj = lc2.time_input("Lock time (ET)", value=_time(18, 0), key="lock_time_new")

            race_url_input = st.text_input(
                "Equibase / DRF link (optional)",
                placeholder="https://www.equibase.com/static/entry/...",
                help="Paste the entries or PPs page URL — friends will get a clickable button to open the page."
            )
            submitted = st.form_submit_button("Create Race")
            if submitted:
                if not name.strip():
                    st.error("Name required.")
                else:
                    # Combine lock date + time into a naive ISO string (treated as ET)
                    lock_dt = datetime.combine(lock_date_obj, lock_time_obj)
                    lock_iso = lock_dt.isoformat(timespec="seconds")
                    if datetime.now(ET).replace(tzinfo=None) >= lock_dt:
                        st.error("Lock time is already in the past — pick a future time.")
                    else:
                        url = safe_race_url(race_url_input)
                        if race_url_input.strip() and not url:
                            st.warning("URL must start with http:// or https:// — saving race without link.")
                        rid = create_race(name.strip(), race_date, url, lock_iso)
                        st.success(
                            f"Created race #{rid}: {name} — picks lock {format_lock_time(lock_iso)}"
                        )
                        st.rerun()

        st.markdown("---")
        st.markdown("### Existing races")
        for race in list_races():
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])
                horse_count = len(list_horses(race["race_id"]))
                url_str = ""
                if race.get("race_url"):
                    url_str = f"  \n🔗 [Race link]({race['race_url']})"
                lock_str = ""
                if race.get("picks_lock_at"):
                    lock_fmt = format_lock_time(race["picks_lock_at"])
                    if is_locked(race["picks_lock_at"]):
                        lock_str = f"  \n⏰ Picks were due **{lock_fmt}**"
                    else:
                        lock_str = f"  \n⏰ Picks lock **{lock_fmt}** (in {time_until_lock(race['picks_lock_at'])})"
                else:
                    lock_str = "  \n⚠️ *No lock time set — please set one below*"
                c1.markdown(
                    f"**{race['name']}** — *{race['race_date']}*  \n"
                    f"Status: `{race['status']}` • 🐎 {horse_count} horses"
                    f"{url_str}{lock_str}"
                )
                if race["status"] == "open":
                    if c2.button("🔒 Close picks", key=f"close_{race['race_id']}"):
                        update_race_status(race["race_id"], "closed")
                        st.rerun()
                if race["status"] == "closed":
                    if c2.button("🔓 Reopen", key=f"reopen_{race['race_id']}"):
                        update_race_status(race["race_id"], "open")
                        st.rerun()
                if race["status"] == "settled":
                    if c2.button("↩️ Unsettle", key=f"unsettle_{race['race_id']}"):
                        update_race_status(race["race_id"], "closed")
                        st.rerun()

                # --- Delete with 2-step confirmation ---
                confirm_key = f"confirm_delete_{race['race_id']}"
                if not st.session_state.get(confirm_key):
                    if c3.button("🗑️ Delete", key=f"delete_{race['race_id']}"):
                        st.session_state[confirm_key] = True
                        st.rerun()
                else:
                    c3.warning("⚠️ Sure?")
                    cc1, cc2 = c3.columns(2)
                    if cc1.button("✅ Yes", key=f"yes_{race['race_id']}"):
                        delete_race(race["race_id"])
                        st.session_state.pop(confirm_key, None)
                        st.success(f"Deleted '{race['name']}'.")
                        st.rerun()
                    if cc2.button("❌ No", key=f"no_{race['race_id']}"):
                        st.session_state.pop(confirm_key, None)
                        st.rerun()

                # --- Edit URL (expander to keep things tidy) ---
                with st.expander("🔗 Edit race link"):
                    new_url = st.text_input(
                        "Equibase / DRF URL",
                        value=race.get("race_url") or "",
                        placeholder="https://www.equibase.com/static/entry/...",
                        key=f"url_input_{race['race_id']}",
                    )
                    bc1, bc2 = st.columns(2)
                    if bc1.button("💾 Save link", key=f"url_save_{race['race_id']}"):
                        cleaned = safe_race_url(new_url)
                        if new_url.strip() and not cleaned:
                            st.warning("URL must start with http:// or https://")
                        else:
                            update_race_url(race["race_id"], cleaned)
                            st.success("Link updated.")
                            st.rerun()
                    if race.get("race_url"):
                        if bc2.button("🗑️ Remove link", key=f"url_clear_{race['race_id']}"):
                            update_race_url(race["race_id"], None)
                            st.rerun()

                # --- Edit picks deadline ---
                with st.expander("⏰ Edit picks deadline"):
                    from datetime import time as _time
                    existing_lock = parse_lock_time(race.get("picks_lock_at"))
                    default_date = existing_lock.date() if existing_lock else datetime.fromisoformat(race["race_date"]).date()
                    default_time = existing_lock.time().replace(tzinfo=None) if existing_lock else _time(18, 0)
                    if hasattr(default_time, "tzinfo"):
                        # Strip tzinfo for time_input compatibility
                        default_time = _time(default_time.hour, default_time.minute)
                    edit_date = st.date_input(
                        "Lock date",
                        value=default_date,
                        key=f"lock_date_{race['race_id']}",
                    )
                    edit_time = st.time_input(
                        "Lock time (Eastern Time)",
                        value=default_time,
                        key=f"lock_time_{race['race_id']}",
                    )
                    if st.button("💾 Save deadline", key=f"lock_save_{race['race_id']}"):
                        new_lock_dt = datetime.combine(edit_date, edit_time)
                        new_lock_iso = new_lock_dt.isoformat(timespec="seconds")
                        update_picks_lock_at(race["race_id"], new_lock_iso)
                        st.success(f"Deadline updated to {format_lock_time(new_lock_iso)}.")
                        st.rerun()

    # --- MANAGE HORSES ------------------------------------------------
    with tab_manage:
        st.markdown("### Add / remove horses for a race")
        races = list_races()
        if not races:
            st.info("Create a race first.")
        else:
            race_opts = {f"{r['name']} ({r['race_date']})": r["race_id"] for r in races}
            picked = st.selectbox("Race:", list(race_opts.keys()), key="manage_race")
            rid = race_opts[picked]

            with st.form("add_horse"):
                c1, c2, c3 = st.columns([3, 1, 1])
                hname = c1.text_input("Horse name")
                hpost = c2.number_input("Post #", min_value=0, max_value=30, value=0, step=1)
                hml = c3.text_input("ML odds (e.g. '4-1')")
                if st.form_submit_button("Add horse"):
                    if hname.strip():
                        add_horse(
                            rid,
                            hname,
                            int(hpost) if hpost > 0 else None,
                            hml.strip() or None,
                        )
                        st.rerun()

            st.markdown("---")
            st.markdown("### 📋 Smart Paste — auto-fill the field")
            st.caption(
                "Copy entries from Equibase, DRF, the track's website, or anywhere else and paste below. "
                "The parser tries to figure out post number, horse name, and morning-line odds automatically. "
                "You'll see a preview before anything is saved."
            )

            paste_input = st.text_area(
                "Paste entries text here:",
                placeholder=(
                    "Examples of what works:\n"
                    "  1  Renegade  Ortiz Jr  Pletcher  4-1\n"
                    "  Post 1: Renegade (4-1)\n"
                    "  1. Renegade — 4-1\n"
                    "  Renegade 4-1\n"
                ),
                height=180,
                key="smart_paste_input",
            )

            c_parse, c_clear = st.columns([1, 1])
            do_parse = c_parse.button("🔍 Parse Preview", use_container_width=True)
            do_clear = c_clear.button("🗑️ Clear preview", use_container_width=True)

            if do_clear:
                st.session_state.pop("parsed_horses", None)
                st.rerun()

            if do_parse and paste_input.strip():
                parsed = parse_entries_text(paste_input)
                st.session_state["parsed_horses"] = parsed

            parsed = st.session_state.get("parsed_horses")
            if parsed is not None:
                if not parsed:
                    st.warning(
                        "Couldn't parse any horses from that text. Check the format and try again, "
                        "or use the single-horse form above to enter manually."
                    )
                else:
                    st.success(f"✅ Parsed **{len(parsed)} horses**. Review and edit below — nothing is saved yet.")
                    st.caption("You can edit any field. Uncheck the box to skip a row. Then click 'Save All'.")

                    edited_rows = []
                    with st.form("preview_form"):
                        # Header
                        h1, h2, h3, h4 = st.columns([0.5, 1, 4, 1.5])
                        h1.markdown("**Use**")
                        h2.markdown("**Post**")
                        h3.markdown("**Horse**")
                        h4.markdown("**ML odds**")

                        for i, row in enumerate(parsed):
                            c1, c2, c3, c4 = st.columns([0.5, 1, 4, 1.5])
                            keep = c1.checkbox("", value=True, key=f"keep_{i}", label_visibility="collapsed")
                            post = c2.number_input(
                                "post", min_value=0, max_value=30,
                                value=int(row["post"]) if row["post"] else 0, step=1,
                                key=f"post_{i}", label_visibility="collapsed",
                            )
                            name = c3.text_input(
                                "name", value=row["name"], key=f"name_{i}",
                                label_visibility="collapsed",
                            )
                            ml = c4.text_input(
                                "ml", value=row["ml"] or "", key=f"ml_{i}",
                                label_visibility="collapsed",
                            )
                            edited_rows.append({"keep": keep, "post": post, "name": name, "ml": ml})

                        if st.form_submit_button("💾 Save All Horses", type="primary"):
                            saved = 0
                            for r in edited_rows:
                                if not r["keep"] or not r["name"].strip():
                                    continue
                                add_horse(
                                    rid,
                                    r["name"].strip(),
                                    int(r["post"]) if r["post"] > 0 else None,
                                    r["ml"].strip() or None,
                                )
                                saved += 1
                            st.session_state.pop("parsed_horses", None)
                            st.success(f"🐎 Saved {saved} horses.")
                            st.rerun()

            st.markdown("---")
            st.markdown("### Current field")
            horses = list_horses(rid)
            if not horses:
                st.info("No horses yet.")
            else:
                for h in horses:
                    c1, c2 = st.columns([5, 1])
                    label = f"#{h['post_position']} {h['horse_name']}" if h["post_position"] else h["horse_name"]
                    if h["morning_line"]:
                        label += f" — {h['morning_line']}"
                    c1.markdown(label)
                    if c2.button("🗑️", key=f"del_{h['horse_id']}"):
                        remove_horse(h["horse_id"])
                        st.rerun()

    # --- ENTER RESULTS ------------------------------------------------
    with tab_results:
        st.markdown("### Enter finishing positions")
        races = [r for r in list_races() if r["status"] in ("closed", "settled", "open")]
        if not races:
            st.info("No races to settle.")
        else:
            race_opts = {f"{r['name']} ({r['race_date']}) — {r['status']}": r["race_id"] for r in races}
            picked = st.selectbox("Race:", list(race_opts.keys()), key="results_race")
            rid = race_opts[picked]
            horses = list_horses(rid)
            if not horses:
                st.warning("No horses entered.")
            else:
                st.caption("Enter the finishing position for each horse (1, 2, 3, ...). Leave blank for horses outside the top finish you care about.")
                with st.form("results_form"):
                    pos_map = {}
                    for h in horses:
                        c1, c2 = st.columns([4, 1])
                        label = f"#{h['post_position']} {h['horse_name']}" if h["post_position"] else h["horse_name"]
                        c1.markdown(label)
                        current = h["final_position"] if h["final_position"] is not None else 0
                        val = c2.number_input(
                            "Final pos",
                            min_value=0, max_value=30,
                            value=int(current),
                            step=1,
                            key=f"pos_{h['horse_id']}",
                            label_visibility="collapsed",
                        )
                        pos_map[h["horse_id"]] = int(val) if val > 0 else None
                    do_settle = st.checkbox("Mark race as settled (locks scoring into cumulative leaderboard)", value=True)
                    if st.form_submit_button("💾 Save Results", type="primary"):
                        set_finishing_positions(rid, pos_map)
                        if do_settle:
                            update_race_status(rid, "settled")
                        st.success("Results saved!")
                        st.rerun()


# ============================================================
# SIDEBAR
# ============================================================
def sidebar():
    st.sidebar.title("🏇 Pick'em")
    st.sidebar.markdown("---")

    # Username
    current = st.session_state.get("username", "")
    existing_users = list_users()
    st.sidebar.markdown("### 👤 Who are you?")
    if existing_users:
        choice = st.sidebar.selectbox(
            "Existing user:",
            ["— new user —"] + existing_users,
            index=(existing_users.index(current) + 1) if current in existing_users else 0,
        )
        if choice != "— new user —":
            st.session_state["username"] = choice
        else:
            new_name = st.sidebar.text_input("New username:", value="")
            if st.sidebar.button("Save"):
                if new_name.strip():
                    upsert_user(new_name)
                    st.session_state["username"] = new_name.strip()
                    st.rerun()
    else:
        new_name = st.sidebar.text_input("Username:", value=current)
        if st.sidebar.button("Save"):
            if new_name.strip():
                upsert_user(new_name)
                st.session_state["username"] = new_name.strip()
                st.rerun()

    if st.session_state.get("username"):
        st.sidebar.success(f"Hi, **{st.session_state['username']}** 👋")

    st.sidebar.markdown("---")
    page = st.sidebar.radio(
        "Navigate:",
        ["🏠 Home", "📝 Make Picks", "🏆 Leaderboard", "📋 My Picks", "⚙️ Admin"],
    )
    st.sidebar.markdown("---")
    st.sidebar.caption("🐴 No money. Just glory.")
    return page


# ============================================================
# MAIN
# ============================================================
def main():
    init_db()
    auto_close_expired_races()
    page = sidebar()
    if page == "🏠 Home":
        page_home()
    elif page == "📝 Make Picks":
        page_make_picks()
    elif page == "🏆 Leaderboard":
        page_leaderboard()
    elif page == "📋 My Picks":
        page_my_picks()
    elif page == "⚙️ Admin":
        page_admin()


if __name__ == "__main__":
    main()