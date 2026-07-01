#!/usr/bin/env python3
"""
Build or update a Bob's Burgers SQLite episode database using TV-API.com.

The script uses only the SeasonEpisodes endpoint because it already returns:

    id
    seasonNumber
    episodeNumber
    title
    image
    year
    released
    plot
    imDbRating
    imDbRatingCount

Examples
--------
Create or update the database:

    python update_tv_api_ratings.py

Delete the existing database and rebuild it:

    python update_tv_api_ratings.py --rebuild

Test only seasons 1 and 2:

    python update_tv_api_ratings.py --rebuild --max-season 2

Use a different database:

    python update_tv_api_ratings.py --database my_database.db --rebuild
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

API_KEY = "k_efuep56w"

BASE_URL = "https://tv-api.com/en/API"

SERIES_IMDB_ID = "tt1561755"
SERIES_TITLE = "Bob's Burgers"

DEFAULT_DATABASE = Path(__file__).resolve().with_name(
    "bobs_burgers_tv_api.db"
)

DEFAULT_MAX_SEASON = 30
DEFAULT_DELAY_SECONDS = 0.35
DEFAULT_TIMEOUT_SECONDS = 30.0

# Stop after this many consecutive seasons return no episodes.
EMPTY_SEASON_STOP_COUNT = 2


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def clean_text(value: Any) -> str | None:
    """Convert a value to clean text and decode HTML entities."""
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    return html.unescape(text)


def parse_integer(value: Any) -> int | None:
    """Convert values such as '2,534' into integers."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    text = str(value).strip()

    if not text:
        return None

    digits = re.sub(r"[^0-9-]", "", text)

    if not digits or digits == "-":
        return None

    try:
        return int(digits)
    except ValueError:
        return None


def parse_float(value: Any) -> float | None:
    """Convert values such as '7.7' into floats."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "")

    if not text:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def json_text(value: Any) -> str:
    """Serialize a value to JSON."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


# ============================================================
# HTTP
# ============================================================

def build_session() -> requests.Session:
    """Create a requests session with retry handling."""
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": "bobs-burgers-season-episodes-builder/2.0",
            "Accept": "application/json",
        }
    )

    session.mount(
        "https://",
        HTTPAdapter(max_retries=retry),
    )

    return session


def build_season_url(season_number: int) -> str:
    """Build the API URL for one season."""
    return (
        f"{BASE_URL}/SeasonEpisodes/"
        f"{API_KEY}/"
        f"{SERIES_IMDB_ID}/"
        f"{season_number}"
    )


def fetch_season(
    session: requests.Session,
    season_number: int,
    timeout: float,
) -> tuple[int, dict[str, Any] | None, str]:
    """Download one SeasonEpisodes response."""
    response = session.get(
        build_season_url(season_number),
        timeout=timeout,
    )

    response_text = response.text

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if payload is not None and not isinstance(payload, dict):
        payload = None

    return response.status_code, payload, response_text


def get_api_error(
    payload: dict[str, Any] | None,
) -> str | None:
    """Extract an API error message."""
    if not isinstance(payload, dict):
        return None

    error = (
        payload.get("errorMessage")
        or payload.get("error")
        or payload.get("message")
    )

    if error is None:
        return None

    text = str(error).strip()

    return text or None


# ============================================================
# DATABASE SCHEMA HELPERS
# ============================================================

def table_exists(
    conn: sqlite3.Connection,
    table_name: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()

    return row is not None


def get_table_columns(
    conn: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    if not table_exists(conn, table_name):
        return set()

    return {
        str(row[1])
        for row in conn.execute(
            f'PRAGMA table_info("{table_name}")'
        )
    }


def ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    """
    Add a missing column to an existing table.

    This is what prevents the previous:
        no such column: imdb_rating
    error.
    """
    columns = get_table_columns(
        conn,
        table_name,
    )

    if column_name in columns:
        return

    print(
        f"Adding missing column: "
        f"{table_name}.{column_name}"
    )

    conn.execute(
        f"""
        ALTER TABLE "{table_name}"
        ADD COLUMN "{column_name}" {column_definition}
        """
    )


def remove_database_files(database_path: Path) -> None:
    """Delete the database and its SQLite WAL support files."""
    paths = (
        database_path,
        Path(str(database_path) + "-wal"),
        Path(str(database_path) + "-shm"),
    )

    for path in paths:
        if path.exists():
            path.unlink()


# ============================================================
# DATABASE CREATION AND MIGRATION
# ============================================================

def open_database(
    database_path: Path,
    rebuild: bool,
) -> sqlite3.Connection:
    """Open, create, or rebuild the SQLite database."""
    if rebuild:
        remove_database_files(database_path)

    database_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = sqlite3.connect(database_path)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")

    create_schema(conn)

    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """
    Create base tables, migrate older tables, then create indexes
    and views only after all required columns exist.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS series (
            series_id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT,
            title TEXT,
            full_title TEXT,
            content_type TEXT,
            start_year INTEGER,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS episodes (
            episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER,
            series_imdb_id TEXT,
            imdb_id TEXT,
            season_number INTEGER,
            episode_number INTEGER,
            title TEXT,
            image_url TEXT,
            year INTEGER,
            released TEXT,
            plot TEXT,
            imdb_rating REAL,
            imdb_rating_count INTEGER,
            rating_normalized REAL,
            raw_json TEXT,
            fetched_at TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS season_responses (
            response_id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER,
            season_number INTEGER,
            http_status INTEGER,
            succeeded INTEGER DEFAULT 0,
            episode_count INTEGER DEFAULT 0,
            error_message TEXT,
            response_json TEXT,
            response_text TEXT,
            fetched_at TEXT
        );

        CREATE TABLE IF NOT EXISTS collection_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            completed_at TEXT,
            database_path TEXT,
            seasons_requested INTEGER DEFAULT 0,
            seasons_succeeded INTEGER DEFAULT 0,
            seasons_failed INTEGER DEFAULT 0,
            episodes_received INTEGER DEFAULT 0,
            episodes_inserted INTEGER DEFAULT 0,
            episodes_updated INTEGER DEFAULT 0,
            episodes_skipped INTEGER DEFAULT 0
        );
        """
    )

    # --------------------------------------------------------
    # Migrate older versions of the series table
    # --------------------------------------------------------

    series_columns = {
        "imdb_id": "TEXT",
        "title": "TEXT",
        "full_title": "TEXT",
        "content_type": "TEXT",
        "start_year": "INTEGER",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }

    for column_name, definition in series_columns.items():
        ensure_column(
            conn,
            "series",
            column_name,
            definition,
        )

    # --------------------------------------------------------
    # Migrate older versions of the episodes table
    # --------------------------------------------------------

    episode_columns = {
        "series_id": "INTEGER",
        "series_imdb_id": "TEXT",
        "imdb_id": "TEXT",
        "season_number": "INTEGER",
        "episode_number": "INTEGER",
        "title": "TEXT",
        "image_url": "TEXT",
        "year": "INTEGER",
        "released": "TEXT",
        "plot": "TEXT",
        "imdb_rating": "REAL",
        "imdb_rating_count": "INTEGER",
        "rating_normalized": "REAL",
        "raw_json": "TEXT",
        "fetched_at": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }

    for column_name, definition in episode_columns.items():
        ensure_column(
            conn,
            "episodes",
            column_name,
            definition,
        )

    # --------------------------------------------------------
    # Migrate older season_responses tables
    # --------------------------------------------------------

    response_columns = {
        "series_id": "INTEGER",
        "season_number": "INTEGER",
        "http_status": "INTEGER",
        "succeeded": "INTEGER DEFAULT 0",
        "episode_count": "INTEGER DEFAULT 0",
        "error_message": "TEXT",
        "response_json": "TEXT",
        "response_text": "TEXT",
        "fetched_at": "TEXT",
    }

    for column_name, definition in response_columns.items():
        ensure_column(
            conn,
            "season_responses",
            column_name,
            definition,
        )

    # --------------------------------------------------------
    # Migrate older collection_runs tables
    # --------------------------------------------------------

    run_columns = {
        "started_at": "TEXT",
        "completed_at": "TEXT",
        "database_path": "TEXT",
        "seasons_requested": "INTEGER DEFAULT 0",
        "seasons_succeeded": "INTEGER DEFAULT 0",
        "seasons_failed": "INTEGER DEFAULT 0",
        "episodes_received": "INTEGER DEFAULT 0",
        "episodes_inserted": "INTEGER DEFAULT 0",
        "episodes_updated": "INTEGER DEFAULT 0",
        "episodes_skipped": "INTEGER DEFAULT 0",
    }

    for column_name, definition in run_columns.items():
        ensure_column(
            conn,
            "collection_runs",
            column_name,
            definition,
        )

    # Remove old versions of the views before recreating them.
    conn.executescript(
        """
        DROP VIEW IF EXISTS episode_ratings;
        DROP VIEW IF EXISTS top_rated_episodes;
        DROP VIEW IF EXISTS season_summary;
        """
    )

    # Indexes are created only after migrations are finished.
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS
            idx_episodes_imdb_id
        ON episodes(imdb_id);

        CREATE INDEX IF NOT EXISTS
            idx_episodes_season_episode
        ON episodes(
            season_number,
            episode_number
        );

        CREATE INDEX IF NOT EXISTS
            idx_episodes_imdb_rating
        ON episodes(imdb_rating DESC);

        CREATE INDEX IF NOT EXISTS
            idx_episodes_rating_count
        ON episodes(imdb_rating_count DESC);

        CREATE INDEX IF NOT EXISTS
            idx_episodes_released
        ON episodes(released);

        CREATE INDEX IF NOT EXISTS
            idx_season_responses_season
        ON season_responses(season_number);

        CREATE VIEW episode_ratings AS
        SELECT
            episode_id,
            imdb_id,
            season_number,
            episode_number,
            title,
            imdb_rating,
            imdb_rating_count,
            rating_normalized,
            released,
            image_url
        FROM episodes;

        CREATE VIEW top_rated_episodes AS
        SELECT
            imdb_id,
            season_number,
            episode_number,
            title,
            imdb_rating,
            imdb_rating_count,
            released,
            image_url
        FROM episodes
        WHERE imdb_rating IS NOT NULL
        ORDER BY
            imdb_rating DESC,
            imdb_rating_count DESC,
            season_number,
            episode_number;

        CREATE VIEW season_summary AS
        SELECT
            season_number,
            COUNT(*) AS episode_count,
            ROUND(AVG(imdb_rating), 2) AS average_imdb_rating,
            MIN(imdb_rating) AS lowest_imdb_rating,
            MAX(imdb_rating) AS highest_imdb_rating,
            SUM(imdb_rating_count) AS total_rating_count
        FROM episodes
        GROUP BY season_number
        ORDER BY season_number;
        """
    )

    conn.commit()


# ============================================================
# SERIES DATA
# ============================================================

def get_or_create_series(
    conn: sqlite3.Connection,
    payload: dict[str, Any] | None = None,
) -> int:
    """Create or update the main Bob's Burgers series row."""
    imdb_id = SERIES_IMDB_ID
    title = SERIES_TITLE
    full_title = None
    content_type = "TVSeries"
    start_year = 2011

    if isinstance(payload, dict):
        imdb_id = (
            clean_text(payload.get("imDbId"))
            or SERIES_IMDB_ID
        )

        title = (
            clean_text(payload.get("title"))
            or SERIES_TITLE
        )

        full_title = clean_text(
            payload.get("fullTitle")
        )

        content_type = (
            clean_text(payload.get("type"))
            or "TVSeries"
        )

        start_year = (
            parse_integer(payload.get("year"))
            or 2011
        )

    existing = conn.execute(
        """
        SELECT series_id
        FROM series
        WHERE imdb_id = ?
        ORDER BY series_id
        LIMIT 1
        """,
        (imdb_id,),
    ).fetchone()

    now = utc_now()

    if existing:
        series_id = int(existing[0])

        conn.execute(
            """
            UPDATE series
            SET title = ?,
                full_title = COALESCE(?, full_title),
                content_type = COALESCE(?, content_type),
                start_year = COALESCE(?, start_year),
                updated_at = ?
            WHERE series_id = ?
            """,
            (
                title,
                full_title,
                content_type,
                start_year,
                now,
                series_id,
            ),
        )

        return series_id

    cursor = conn.execute(
        """
        INSERT INTO series (
            imdb_id,
            title,
            full_title,
            content_type,
            start_year,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            imdb_id,
            title,
            full_title,
            content_type,
            start_year,
            now,
            now,
        ),
    )

    return int(cursor.lastrowid)


# ============================================================
# EPISODE DATA
# ============================================================

def extract_episode_items(
    payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extract the episodes list from a response."""
    if not isinstance(payload, dict):
        return []

    episodes = payload.get("episodes")

    if not isinstance(episodes, list):
        return []

    return [
        item
        for item in episodes
        if isinstance(item, dict)
    ]


def find_existing_episode(
    conn: sqlite3.Connection,
    imdb_id: str,
    season_number: int,
    episode_number: int,
) -> tuple[int] | None:
    """Find an existing episode by IMDb ID or season/episode."""
    row = conn.execute(
        """
        SELECT episode_id
        FROM episodes
        WHERE imdb_id = ?
        ORDER BY episode_id
        LIMIT 1
        """,
        (imdb_id,),
    ).fetchone()

    if row:
        return row

    return conn.execute(
        """
        SELECT episode_id
        FROM episodes
        WHERE series_imdb_id = ?
          AND season_number = ?
          AND episode_number = ?
        ORDER BY episode_id
        LIMIT 1
        """,
        (
            SERIES_IMDB_ID,
            season_number,
            episode_number,
        ),
    ).fetchone()


def upsert_episode(
    conn: sqlite3.Connection,
    series_id: int,
    item: dict[str, Any],
) -> str:
    """
    Insert or update one episode.

    Returns:
        inserted
        updated
    """
    imdb_id = clean_text(item.get("id"))

    if not imdb_id:
        raise ValueError(
            "Episode does not contain a valid IMDb ID"
        )

    season_number = parse_integer(
        item.get("seasonNumber")
    )

    episode_number = parse_integer(
        item.get("episodeNumber")
    )

    if season_number is None:
        raise ValueError(
            f"{imdb_id} does not have a valid season number"
        )

    if episode_number is None:
        raise ValueError(
            f"{imdb_id} does not have a valid episode number"
        )

    title = (
        clean_text(item.get("title"))
        or imdb_id
    )

    image_url = clean_text(
        item.get("image")
    )

    year = parse_integer(
        item.get("year")
    )

    released = clean_text(
        item.get("released")
    )

    plot = clean_text(
        item.get("plot")
    )

    imdb_rating = parse_float(
        item.get("imDbRating")
    )

    imdb_rating_count = parse_integer(
        item.get("imDbRatingCount")
    )

    rating_normalized = (
        round(imdb_rating * 10, 2)
        if imdb_rating is not None
        else None
    )

    raw_json = json_text(item)
    now = utc_now()

    existing = find_existing_episode(
        conn=conn,
        imdb_id=imdb_id,
        season_number=season_number,
        episode_number=episode_number,
    )

    if existing:
        episode_id = int(existing[0])

        conn.execute(
            """
            UPDATE episodes
            SET series_id = ?,
                series_imdb_id = ?,
                imdb_id = ?,
                season_number = ?,
                episode_number = ?,
                title = ?,
                image_url = ?,
                year = ?,
                released = ?,
                plot = ?,
                imdb_rating = ?,
                imdb_rating_count = ?,
                rating_normalized = ?,
                raw_json = ?,
                fetched_at = ?,
                updated_at = ?
            WHERE episode_id = ?
            """,
            (
                series_id,
                SERIES_IMDB_ID,
                imdb_id,
                season_number,
                episode_number,
                title,
                image_url,
                year,
                released,
                plot,
                imdb_rating,
                imdb_rating_count,
                rating_normalized,
                raw_json,
                now,
                now,
                episode_id,
            ),
        )

        return "updated"

    conn.execute(
        """
        INSERT INTO episodes (
            series_id,
            series_imdb_id,
            imdb_id,
            season_number,
            episode_number,
            title,
            image_url,
            year,
            released,
            plot,
            imdb_rating,
            imdb_rating_count,
            rating_normalized,
            raw_json,
            fetched_at,
            created_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?
        )
        """,
        (
            series_id,
            SERIES_IMDB_ID,
            imdb_id,
            season_number,
            episode_number,
            title,
            image_url,
            year,
            released,
            plot,
            imdb_rating,
            imdb_rating_count,
            rating_normalized,
            raw_json,
            now,
            now,
            now,
        ),
    )

    return "inserted"


# ============================================================
# SEASON RESPONSE STORAGE
# ============================================================

def save_season_response(
    conn: sqlite3.Connection,
    series_id: int,
    season_number: int,
    http_status: int | None,
    succeeded: bool,
    episode_count: int,
    error_message: str | None,
    payload: dict[str, Any] | None,
    response_text: str | None,
) -> None:
    """Insert or update the stored response for one season."""
    existing = conn.execute(
        """
        SELECT response_id
        FROM season_responses
        WHERE series_id = ?
          AND season_number = ?
        ORDER BY response_id
        LIMIT 1
        """,
        (
            series_id,
            season_number,
        ),
    ).fetchone()

    values = (
        http_status,
        1 if succeeded else 0,
        episode_count,
        error_message,
        (
            json_text(payload)
            if payload is not None
            else None
        ),
        response_text,
        utc_now(),
    )

    if existing:
        conn.execute(
            """
            UPDATE season_responses
            SET http_status = ?,
                succeeded = ?,
                episode_count = ?,
                error_message = ?,
                response_json = ?,
                response_text = ?,
                fetched_at = ?
            WHERE response_id = ?
            """,
            (
                *values,
                int(existing[0]),
            ),
        )

        return

    conn.execute(
        """
        INSERT INTO season_responses (
            series_id,
            season_number,
            http_status,
            succeeded,
            episode_count,
            error_message,
            response_json,
            response_text,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            series_id,
            season_number,
            *values,
        ),
    )


# ============================================================
# COMMAND-LINE OPTIONS
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or update a Bob's Burgers SQLite "
            "database using TV-API SeasonEpisodes data."
        )
    )

    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=(
            "Output database path. Defaults to "
            "bobs_burgers_tv_api.db beside this script."
        ),
    )

    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Delete the database and recreate it from scratch."
        ),
    )

    parser.add_argument(
        "--max-season",
        type=int,
        default=DEFAULT_MAX_SEASON,
        help=(
            "Highest season number to request. "
            f"Default: {DEFAULT_MAX_SEASON}."
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=(
            "Seconds to wait between requests. "
            f"Default: {DEFAULT_DELAY_SECONDS}."
        ),
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            "Request timeout in seconds. "
            f"Default: {DEFAULT_TIMEOUT_SECONDS}."
        ),
    )

    parser.add_argument(
        "--continue-after-empty",
        action="store_true",
        help=(
            "Continue to max-season instead of stopping "
            "after consecutive empty seasons."
        ),
    )

    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    args = parse_args()

    if args.max_season < 1:
        print(
            "ERROR: --max-season must be at least 1.",
            file=sys.stderr,
        )
        return 2

    if args.delay < 0:
        print(
            "ERROR: --delay cannot be negative.",
            file=sys.stderr,
        )
        return 2

    if args.timeout <= 0:
        print(
            "ERROR: --timeout must be greater than zero.",
            file=sys.stderr,
        )
        return 2

    database_path = args.database.expanduser().resolve()

    conn: sqlite3.Connection | None = None
    session: requests.Session | None = None

    return_code = 0
    run_id: int | None = None

    seasons_requested = 0
    seasons_succeeded = 0
    seasons_failed = 0

    episodes_received = 0
    episodes_inserted = 0
    episodes_updated = 0
    episodes_skipped = 0

    consecutive_empty_seasons = 0
    total_episodes = 0

    try:
        conn = open_database(
            database_path=database_path,
            rebuild=args.rebuild,
        )

        session = build_session()

        run_cursor = conn.execute(
            """
            INSERT INTO collection_runs (
                started_at,
                database_path
            )
            VALUES (?, ?)
            """,
            (
                utc_now(),
                str(database_path),
            ),
        )

        run_id = int(run_cursor.lastrowid)

        series_id = get_or_create_series(conn)
        conn.commit()

        print()
        print(f"Database: {database_path}")
        print(f"Series: {SERIES_TITLE}")
        print(f"IMDb ID: {SERIES_IMDB_ID}")
        print()

        for season_number in range(
            1,
            args.max_season + 1,
        ):
            seasons_requested += 1

            print(f"Season {season_number}:")

            try:
                (
                    status,
                    payload,
                    response_text,
                ) = fetch_season(
                    session=session,
                    season_number=season_number,
                    timeout=args.timeout,
                )

            except requests.RequestException as exc:
                seasons_failed += 1
                consecutive_empty_seasons += 1

                error_message = str(exc)

                print(
                    f"  Request failed: {error_message}"
                )

                with conn:
                    save_season_response(
                        conn=conn,
                        series_id=series_id,
                        season_number=season_number,
                        http_status=None,
                        succeeded=False,
                        episode_count=0,
                        error_message=error_message,
                        payload=None,
                        response_text=None,
                    )

            else:
                error_message = get_api_error(payload)

                if not 200 <= status < 300:
                    seasons_failed += 1
                    consecutive_empty_seasons += 1

                    message = (
                        error_message
                        or f"HTTP {status}"
                    )

                    print(f"  Failed: {message}")

                    with conn:
                        save_season_response(
                            conn=conn,
                            series_id=series_id,
                            season_number=season_number,
                            http_status=status,
                            succeeded=False,
                            episode_count=0,
                            error_message=message,
                            payload=payload,
                            response_text=response_text,
                        )

                elif error_message:
                    seasons_failed += 1
                    consecutive_empty_seasons += 1

                    print(
                        f"  API error: {error_message}"
                    )

                    with conn:
                        save_season_response(
                            conn=conn,
                            series_id=series_id,
                            season_number=season_number,
                            http_status=status,
                            succeeded=False,
                            episode_count=0,
                            error_message=error_message,
                            payload=payload,
                            response_text=response_text,
                        )

                else:
                    items = extract_episode_items(
                        payload
                    )

                    episode_count = len(items)

                    if episode_count == 0:
                        consecutive_empty_seasons += 1

                        print(
                            "  No episodes returned"
                        )

                        with conn:
                            save_season_response(
                                conn=conn,
                                series_id=series_id,
                                season_number=season_number,
                                http_status=status,
                                succeeded=True,
                                episode_count=0,
                                error_message=None,
                                payload=payload,
                                response_text=response_text,
                            )

                    else:
                        consecutive_empty_seasons = 0
                        seasons_succeeded += 1
                        episodes_received += episode_count

                        print(
                            f"  Episodes returned: "
                            f"{episode_count}"
                        )

                        with conn:
                            series_id = get_or_create_series(
                                conn=conn,
                                payload=payload,
                            )

                            for item in items:
                                try:
                                    action = upsert_episode(
                                        conn=conn,
                                        series_id=series_id,
                                        item=item,
                                    )

                                except (
                                    ValueError,
                                    sqlite3.Error,
                                ) as exc:
                                    episodes_skipped += 1

                                    print(
                                        f"    Skipped: {exc}",
                                        file=sys.stderr,
                                    )

                                    continue

                                if action == "inserted":
                                    episodes_inserted += 1
                                else:
                                    episodes_updated += 1

                                season_value = (
                                    parse_integer(
                                        item.get(
                                            "seasonNumber"
                                        )
                                    )
                                )

                                episode_value = (
                                    parse_integer(
                                        item.get(
                                            "episodeNumber"
                                        )
                                    )
                                )

                                title_value = (
                                    clean_text(
                                        item.get("title")
                                    )
                                    or clean_text(
                                        item.get("id")
                                    )
                                    or "Unknown"
                                )

                                rating_value = (
                                    parse_float(
                                        item.get(
                                            "imDbRating"
                                        )
                                    )
                                )

                                vote_value = (
                                    parse_integer(
                                        item.get(
                                            "imDbRatingCount"
                                        )
                                    )
                                )

                                rating_text = (
                                    f"{rating_value:.1f}"
                                    if rating_value
                                    is not None
                                    else "N/A"
                                )

                                vote_text = (
                                    f"{vote_value:,}"
                                    if vote_value
                                    is not None
                                    else "N/A"
                                )

                                print(
                                    f"    "
                                    f"S{season_value}"
                                    f"E{episode_value} "
                                    f"{title_value} | "
                                    f"IMDb {rating_text} | "
                                    f"{vote_text} ratings"
                                )

                            save_season_response(
                                conn=conn,
                                series_id=series_id,
                                season_number=season_number,
                                http_status=status,
                                succeeded=True,
                                episode_count=episode_count,
                                error_message=None,
                                payload=payload,
                                response_text=response_text,
                            )

            if (
                not args.continue_after_empty
                and consecutive_empty_seasons
                >= EMPTY_SEASON_STOP_COUNT
            ):
                print()
                print(
                    "Stopping after "
                    f"{EMPTY_SEASON_STOP_COUNT} "
                    "consecutive empty or failed seasons."
                )
                break

            if args.delay:
                time.sleep(args.delay)

    except KeyboardInterrupt:
        print(
            "\nStopped by user. Completed data was preserved.",
            file=sys.stderr,
        )

        return_code = 130

    except Exception as exc:
        print(
            f"\nERROR: {exc}",
            file=sys.stderr,
        )

        return_code = 1

        raise

    finally:
        if conn is not None:
            try:
                if run_id is not None:
                    with conn:
                        conn.execute(
                            """
                            UPDATE collection_runs
                            SET completed_at = ?,
                                seasons_requested = ?,
                                seasons_succeeded = ?,
                                seasons_failed = ?,
                                episodes_received = ?,
                                episodes_inserted = ?,
                                episodes_updated = ?,
                                episodes_skipped = ?
                            WHERE run_id = ?
                            """,
                            (
                                utc_now(),
                                seasons_requested,
                                seasons_succeeded,
                                seasons_failed,
                                episodes_received,
                                episodes_inserted,
                                episodes_updated,
                                episodes_skipped,
                                run_id,
                            ),
                        )

                total_row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM episodes
                    """
                ).fetchone()

                total_episodes = (
                    int(total_row[0])
                    if total_row
                    else 0
                )

            finally:
                conn.close()

        if session is not None:
            session.close()

    print()
    print("=" * 60)
    print("Finished")
    print(f"Database: {database_path}")
    print(f"Seasons requested: {seasons_requested}")
    print(f"Seasons succeeded: {seasons_succeeded}")
    print(f"Seasons failed: {seasons_failed}")
    print(f"Episodes received: {episodes_received}")
    print(f"Episodes inserted: {episodes_inserted}")
    print(f"Episodes updated: {episodes_updated}")
    print(f"Episodes skipped: {episodes_skipped}")
    print(f"Total episodes in database: {total_episodes}")
    print("=" * 60)

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())