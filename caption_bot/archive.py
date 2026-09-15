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
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import DB_PATH

_lock = threading.Lock()
_connection: Optional[sqlite3.Connection] = None


def _get_connection() -> sqlite3.Connection:
    global _connection

    if _connection is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        _connection = sqlite3.connect(
            str(DB_PATH),
            check_same_thread=False,
        )
        _connection.execute("PRAGMA journal_mode=WAL;")
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

    conn.commit()


@dataclass
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


@dataclass
class TransferRecord:
    token: str
    owner_id: int
    seasons: list[int]


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
    with _lock:
        conn = _get_connection()
        conn.execute(
            """
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
            """,
            (
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
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


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


def create_transfer(owner_id: int, seasons: list[int]) -> str:
    """Create a new transfer token for the given seasons.

    The token is URL-safe (matches Telegram's deep-link payload charset)
    and is not tied to a single use — opening the link again just resends
    everything again, which is convenient if a send partially fails.
    """
    token = secrets.token_urlsafe(8)

    with _lock:
        conn = _get_connection()
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
        conn.commit()

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
