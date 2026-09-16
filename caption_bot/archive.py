"""Local SQLite-backed archive of completed episodes and /transfer links.

This is what makes /transfer possible: every time a batch finishes
processing successfully, callbacks.py records (season, episode, caption,
file_id, ...) here. A Telegram file_id can be resent to *any* chat this bot
can reach as long as the bot has seen the file once before — the original
chat/message does not need to still exist, and no media is re-downloaded or
re-uploaded to build a transfer.

Only Python's built-in sqlite3 module is used — no new dependency, and the
whole archive lives in a single file next to the bot (see DB_PATH in
config.py), which fits a bot that only ever runs locally.

Performance notes
-----------------

* `synchronous=NORMAL` under WAL removes an fsync per commit. On an SD card
  or a cheap VPS disk that fsync was the slowest part of finishing a batch,
  and WAL still keeps the database safe against a process crash.
* `record_episodes()` writes a whole batch in one transaction instead of one
  commit per episode.
* `season_counts()` replaces the old "list seasons, then COUNT(*) once per
  season" pattern, which re-ran a query per button every time the /transfer
  picker was redrawn.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from .config import DB_CACHE_KIB, DB_PATH

_lock = threading.Lock()
_connection: Optional[sqlite3.Connection] = None


def _get_connection() -> sqlite3.Connection:
    global _connection

    if _connection is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        _connection = sqlite3.connect(
            str(DB_PATH),
            check_same_thread=False,
            isolation_level=None,
            cached_statements=32,
        )

        _connection.execute("PRAGMA journal_mode=WAL;")
        _connection.execute("PRAGMA synchronous=NORMAL;")
        _connection.execute(f"PRAGMA cache_size=-{DB_CACHE_KIB};")
        _connection.execute("PRAGMA temp_store=MEMORY;")
        _connection.execute("PRAGMA mmap_size=0;")

        _init_schema(_connection)

    return _connection


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            owner_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            caption TEXT NOT NULL,
            media_type TEXT NOT NULL,
            file_id TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            duration INTEGER,
            supports_streaming INTEGER NOT NULL DEFAULT 0,
            has_spoiler INTEGER NOT NULL DEFAULT 0,
            cover_file_id TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, season, episode)
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transfers (
            token TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            seasons TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )


_INSERT_EPISODE = """
    INSERT INTO episodes (
        owner_id, season, episode, caption, media_type, file_id,
        width, height, duration, supports_streaming, has_spoiler,
        cover_file_id, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(owner_id, season, episode) DO UPDATE SET
        caption=excluded.caption,
        media_type=excluded.media_type,
        file_id=excluded.file_id,
        width=excluded.width,
        height=excluded.height,
        duration=excluded.duration,
        supports_streaming=excluded.supports_streaming,
        has_spoiler=excluded.has_spoiler,
        cover_file_id=excluded.cover_file_id,
        created_at=excluded.created_at;
"""


@dataclass(slots=True)
class EpisodeRecord:
    season: int
    episode: int
    caption: str
    media_type: str
    file_id: str
    width: Optional[int]
    height: Optional[int]
    duration: Optional[int]
    supports_streaming: bool
    has_spoiler: bool
    cover_file_id: Optional[str]


@dataclass(slots=True)
class TransferRecord:
    token: str
    owner_id: int
    seasons: list[int]


def _episode_row(
    owner_id: int,
    season: int,
    episode: int,
    caption: str,
    media_type: str,
    file_id: str,
    width: Optional[int],
    height: Optional[int],
    duration: Optional[int],
    supports_streaming: bool,
    has_spoiler: bool,
    cover_file_id: Optional[str],
    created_at: str,
) -> tuple:
    return (
        owner_id,
        season,
        episode,
        caption,
        media_type,
        file_id,
        width,
        height,
        duration,
        int(supports_streaming),
        int(has_spoiler),
        cover_file_id,
        created_at,
    )


def record_episode(
    owner_id: int,
    season: int,
    episode: int,
    caption: str,
    media_type: str,
    file_id: str,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    duration: Optional[int] = None,
    supports_streaming: bool = False,
    has_spoiler: bool = False,
    cover_file_id: Optional[str] = None,
) -> None:
    """Record (or overwrite) one successfully processed episode.

    Keyed on (owner_id, season, episode): reprocessing the same episode
    later (e.g. a redo) simply replaces the earlier record.
    """
    row = _episode_row(
        owner_id,
        season,
        episode,
        caption,
        media_type,
        file_id,
        width,
        height,
        duration,
        supports_streaming,
        has_spoiler,
        cover_file_id,
        datetime.now(timezone.utc).isoformat(),
    )

    with _lock:
        conn = _get_connection()
        conn.execute("BEGIN;")
        try:
            conn.execute(_INSERT_EPISODE, row)
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise


def record_episodes(rows: Sequence[dict]) -> int:
    """Record a whole finished batch in a single transaction.

    Each dict carries the same keys `record_episode()` takes. Returns the
    number of rows written.
    """
    if not rows:
        return 0

    created_at = datetime.now(timezone.utc).isoformat()

    prepared = [
        _episode_row(
            row["owner_id"],
            row["season"],
            row["episode"],
            row["caption"],
            row["media_type"],
            row["file_id"],
            row.get("width"),
            row.get("height"),
            row.get("duration"),
            row.get("supports_streaming", False),
            row.get("has_spoiler", False),
            row.get("cover_file_id"),
            created_at,
        )
        for row in rows
    ]

    with _lock:
        conn = _get_connection()
        conn.execute("BEGIN;")
        try:
            conn.executemany(_INSERT_EPISODE, prepared)
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    return len(prepared)


def season_counts(owner_id: int) -> list[tuple[int, int]]:
    """(season, episode_count) pairs, lowest season first.

    One query for the whole /transfer picker.
    """
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            """
            SELECT season, COUNT(*) FROM episodes
            WHERE owner_id = ?
            GROUP BY season
            ORDER BY season ASC;
            """,
            (owner_id,),
        ).fetchall()

    return [(row[0], row[1]) for row in rows]


def list_seasons(owner_id: int) -> list[int]:
    """Seasons with at least one archived episode, lowest first."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            """
            SELECT season FROM episodes
            WHERE owner_id = ?
            GROUP BY season
            ORDER BY season ASC;
            """,
            (owner_id,),
        ).fetchall()

    return [row[0] for row in rows]


def has_any_season(owner_id: int) -> bool:
    """Cheaper than list_seasons() when only existence matters."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT 1 FROM episodes WHERE owner_id = ? LIMIT 1;",
            (owner_id,),
        ).fetchone()

    return row is not None


def season_episode_count(owner_id: int, season: int) -> int:
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            """
            SELECT COUNT(*) FROM episodes
            WHERE owner_id = ? AND season = ?;
            """,
            (owner_id, season),
        ).fetchone()

    return row[0] if row else 0


def get_season_episodes(
    owner_id: int,
    season: int,
) -> list[EpisodeRecord]:
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            """
            SELECT season, episode, caption, media_type, file_id,
                   width, height, duration, supports_streaming,
                   has_spoiler, cover_file_id
            FROM episodes
            WHERE owner_id = ? AND season = ?
            ORDER BY episode ASC;
            """,
            (owner_id, season),
        ).fetchall()

    return [
        EpisodeRecord(
            season=row[0],
            episode=row[1],
            caption=row[2],
            media_type=row[3],
            file_id=row[4],
            width=row[5],
            height=row[6],
            duration=row[7],
            supports_streaming=bool(row[8]),
            has_spoiler=bool(row[9]),
            cover_file_id=row[10],
        )
        for row in rows
    ]


def fetch_season_page(
    owner_id: int,
    season: int,
    after_episode: int = 0,
    limit: int = 16,
) -> list[EpisodeRecord]:
    """One page of a season, ordered by episode.

    /transfer sends one episode at a time with a delay between sends, so
    there is no reason to hold a whole season in memory. Paging (rather
    than a long-lived cursor) also means the archive lock is never held
    across an await.
    """
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            """
            SELECT season, episode, caption, media_type, file_id,
                   width, height, duration, supports_streaming,
                   has_spoiler, cover_file_id
            FROM episodes
            WHERE owner_id = ? AND season = ? AND episode > ?
            ORDER BY episode ASC
            LIMIT ?;
            """,
            (owner_id, season, after_episode, limit),
        ).fetchall()

    return [
        EpisodeRecord(
            season=row[0],
            episode=row[1],
            caption=row[2],
            media_type=row[3],
            file_id=row[4],
            width=row[5],
            height=row[6],
            duration=row[7],
            supports_streaming=bool(row[8]),
            has_spoiler=bool(row[9]),
            cover_file_id=row[10],
        )
        for row in rows
    ]


def create_transfer(owner_id: int, seasons: list[int]) -> str:
    """Create a new transfer token for the given seasons.

    The token is URL-safe (matches Telegram's deep-link payload charset)
    and is not tied to a single use — opening the link again just resends
    everything again, which is convenient if a send partially fails.
    """
    token = secrets.token_urlsafe(8)

    with _lock:
        conn = _get_connection()
        conn.execute("BEGIN;")
        try:
            conn.execute(
                """
                INSERT INTO transfers (token, owner_id, seasons, created_at)
                VALUES (?, ?, ?, ?);
                """,
                (
                    token,
                    owner_id,
                    ",".join(str(s) for s in seasons),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    return token


def get_transfer(token: str) -> Optional[TransferRecord]:
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            """
            SELECT token, owner_id, seasons FROM transfers
            WHERE token = ?;
            """,
            (token,),
        ).fetchone()

    if row is None:
        return None

    return TransferRecord(
        token=row[0],
        owner_id=row[1],
        seasons=[int(s) for s in row[2].split(",") if s],
    )


def close() -> None:
    """Close the archive connection (called on shutdown)."""
    global _connection

    with _lock:
        if _connection is not None:
            try:
                _connection.execute("PRAGMA optimize;")
                _connection.close()
            except Exception:
                pass

            _connection = None
