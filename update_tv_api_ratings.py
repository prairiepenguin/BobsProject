#!/usr/bin/env python3
"""
Create or update a Bob's Burgers episode database using TV-API.com.

The SeasonEpisodes endpoint already returns:

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

Examples:

    Rebuild the database from scratch:
        python build_bobs_burgers_db.py --rebuild

    Update the existing database:
        python build_bobs_burgers_db.py

    Test only seasons 1 and 2:
        python build_bobs_burgers_db.py --rebuild --max-season 2

    Use a different database filename:
        python build_bobs_burgers_db.py --database my_bobs_database.db
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
DEFAULT_DELAY = 0.35
DEFAULT_TIMEOUT = 30.0

# Stop after this many consecutive seasons return no episodes.
EMPTY_SEASON_STOP_COUNT = 2


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now() -> str:
    """Return the current UTC timestamp in ISO format."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def clean_text(value: Any) -> str | None:
    """Convert API text to a clean string and decode HTML entities."""
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
    """Serialize a Python value as compact JSON."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


# ============================================================
# HTTP
# ============================================================

def build_session() -> requests.Session:
    """Create a requests session with automatic retries."""
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
            "User-Agent": "bobs-burgers-season-episodes-builder/1.0",
            "Accept": "application/json",
        }
    )

    session.mount(
        "https://",
        HTTPAdapter(max_retries=retry),
    )

    return session


def season_url(season_number: int) -> str:
    """Build the SeasonEpisodes API URL."""
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
    """
    Request one season.

    Returns:
        HTTP status code
        parsed JSON dictionary, if available
        original response text
    """
    url = season_url(season_number)

    response = session.get(
        url,
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


def get_api_error(payload: dict[str, Any] | None) -> str | None:
    """Extract an error message from a TV-API response."""
    if not isinstance(payload, dict):
        return None

    error = (
        payload.get("errorMessage")
        or payload.get("error")
        or payload.get("message")
    )

    if error:
        text = str(error).strip()

        if text:
            return text

    return None


# ============================================================
# DATABASE
# ============================================================

def open_database(
    database_path: Path,
    rebuild: bool,
) -> sqlite3.Connection:
    """Open the database and optionally delete the old copy."""
    if rebuild and database_path.exists():
        database_path.unlink()

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
    """Create all database tables and indexes."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS series (
            series_id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            full_title TEXT,
            content_type TEXT,
            start_year INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS episodes (
            episode_id INTEGER PRIMARY KEY AUTOINCREMENT,

            series_id INTEGER NOT NULL,

            imdb_id TEXT NOT NULL UNIQUE,

            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,

            title TEXT NOT NULL,
            image_url TEXT,

            year INTEGER,
            released TEXT,
            plot TEXT,

            imdb_rating REAL,
            imdb_rating_count INTEGER,

            rating_normalized REAL,

            fetched_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            FOREIGN KEY (series_id)
                REFERENCES series(series_id)
                ON DELETE CASCADE,

            UNIQUE (
                series_id,
                season_number,
                episode_number
            )
        );

        CREATE INDEX IF NOT EXISTS
            idx_episodes_season_episode
        ON episodes (
            season_number,
            episode_number
        );

        CREATE INDEX IF NOT EXISTS
            idx_episodes_rating
        ON episodes (
            imdb_rating DESC
        );

        CREATE INDEX IF NOT EXISTS
            idx_episodes_rating_count
        ON episodes (
            imdb_rating_count DESC
        );

        CREATE INDEX IF NOT EXISTS
            idx_episodes_released
        ON episodes (
            released
        );

        CREATE TABLE IF NOT EXISTS season_responses (
            response_id INTEGER PRIMARY KEY AUTOINCREMENT,

            series_id INTEGER NOT NULL,
            season_number INTEGER NOT NULL,

            http_status INTEGER,
            succeeded INTEGER NOT NULL DEFAULT 0,

            episode_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,

            response_json TEXT,
            response_text TEXT,

            fetched_at TEXT NOT NULL,

            FOREIGN KEY (series_id)
                REFERENCES series(series_id)
                ON DELETE CASCADE,

            UNIQUE (
                series_id,
                season_number
            )
        );

        CREATE TABLE IF NOT EXISTS collection_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,

            started_at TEXT NOT NULL,
            completed_at TEXT,

            database_path TEXT NOT NULL,

            seasons_requested INTEGER NOT NULL DEFAULT 0,
            seasons_succeeded INTEGER NOT NULL DEFAULT 0,
            seasons_failed INTEGER NOT NULL DEFAULT 0,

            episodes_received INTEGER NOT NULL DEFAULT 0,
            episodes_inserted INTEGER NOT NULL DEFAULT 0,
            episodes_updated INTEGER NOT NULL DEFAULT 0
        );

        CREATE VIEW IF NOT EXISTS episode_ratings AS
        SELECT
            e.episode_id,
            e.imdb_id,
            e.season_number,
            e.episode_number,
            e.title,
            e.imdb_rating,
            e.imdb_rating_count,
            e.rating_normalized,
            e.released,
            e.image_url
        FROM episodes e;

        CREATE VIEW IF NOT EXISTS top_rated_episodes AS
        SELECT
            e.imdb_id,
            e.season_number,
            e.episode_number,
            e.title,
            e.imdb_rating,
            e.imdb_rating_count,
            e.released
        FROM episodes e
        WHERE e.imdb_rating IS NOT NULL
        ORDER BY
            e.imdb_rating DESC,
            e.imdb_rating_count DESC;
        """
    )

    conn.commit()


# ============================================================
# SERIES
# ============================================================

def upsert_series(
    conn: sqlite3.Connection,
    payload: dict[str, Any] | None = None,
) -> int:
    """Create or update the Bob's Burgers series record."""
    now = utc_now()

    imdb_id = SERIES_IMDB_ID
    title = SERIES_TITLE
    full_title = None
    content_type = "TVSeries"
    start_year = 2011

    if isinstance(payload, dict):
        imdb_id = clean_text(payload.get("imDbId")) or imdb_id
        title = clean_text(payload.get("title")) or title
        full_title = clean_text(payload.get("fullTitle"))
        content_type = clean_text(payload.get("type")) or content_type
        start_year = parse_integer(payload.get("year")) or start_year

    conn.execute(
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

        ON CONFLICT(imdb_id) DO UPDATE SET
            title = excluded.title,
            full_title = COALESCE(
                excluded.full_title,
                series.full_title
            ),
            content_type = COALESCE(
                excluded.content_type,
                series.content_type
            ),
            start_year = COALESCE(
                excluded.start_year,
                series.start_year
            ),
            updated_at = excluded.updated_at
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

    row = conn.execute(
        """
        SELECT series_id
        FROM series
        WHERE imdb_id = ?
        """,
        (imdb_id,),
    ).fetchone()

    if row is None:
        raise RuntimeError("Could not create the series record")

    return int(row[0])


# ============================================================
# EPISODES
# ============================================================

def extract_episode_items(
    payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return the episode list from a SeasonEpisodes response."""
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


def upsert_episode(
    conn: sqlite3.Connection,
    series_id: int,
    item: dict[str, Any],
) -> str:
    """
    Insert or update one episode.

    Returns:
        "inserted" or "updated"
    """
    imdb_id = clean_text(item.get("id"))

    if not imdb_id:
        raise ValueError("Episode does not contain an IMDb ID")

    season_number = parse_integer(item.get("seasonNumber"))
    episode_number = parse_integer(item.get("episodeNumber"))

    if season_number is None:
        raise ValueError(
            f"Episode {imdb_id} has no valid season number"
        )

    if episode_number is None:
        raise ValueError(
            f"Episode {imdb_id} has no valid episode number"
        )

    title = clean_text(item.get("title"))

    if not title:
        title = imdb_id

    image_url = clean_text(item.get("image"))
    year = parse_integer(item.get("year"))
    released = clean_text(item.get("released"))
    plot = clean_text(item.get("plot"))

    imdb_rating = parse_float(item.get("imDbRating"))

    imdb_rating_count = parse_integer(
        item.get("imDbRatingCount")
    )

    rating_normalized = (
        round(imdb_rating * 10, 2)
        if imdb_rating is not None
        else None
    )

    now = utc_now()

    existing = conn.execute(
        """
        SELECT episode_id
        FROM episodes
        WHERE imdb_id = ?
        """,
        (imdb_id,),
    ).fetchone()

    action = "updated" if existing else "inserted"

    conn.execute(
        """
        INSERT INTO episodes (
            series_id,
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
            fetched_at,
            created_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?
        )

        ON CONFLICT(imdb_id) DO UPDATE SET
            series_id = excluded.series_id,
            season_number = excluded.season_number,
            episode_number = excluded.episode_number,
            title = excluded.title,
            image_url = excluded.image_url,
            year = excluded.year,
            released = excluded.released,
            plot = excluded.plot,
            imdb_rating = excluded.imdb_rating,
            imdb_rating_count = excluded.imdb_rating_count,
            rating_normalized = excluded.rating_normalized,
            fetched_at = excluded.fetched_at,
            updated_at = excluded.updated_at
        """,
        (
            series_id,
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
            now,
            now,
            now,
        ),
    )

    return action


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
    """Save the full SeasonEpisodes response."""
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

        ON CONFLICT(
            series_id,
            season_number
        )
        DO UPDATE SET
            http_status = excluded.http_status,
            succeeded = excluded.succeeded,
            episode_count = excluded.episode_count,
            error_message = excluded.error_message,
            response_json = excluded.response_json,
            response_text = excluded.response_text,
            fetched_at = excluded.fetched_at
        """,
        (
            series_id,
            season_number,
            http_status,
            1 if succeeded else 0,
            episode_count,
            error_message,
            json_text(payload) if payload is not None else None,
            response_text,
            utc_now(),
        ),
    )


# ============================================================
# COMMAND-LINE ARGUMENTS
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a Bob's Burgers SQLite database from "
            "TV-API SeasonEpisodes responses."
        )
    )

    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=(
            "Output SQLite database. Defaults to "
            "bobs_burgers_tv_api.db beside this script."
        ),
    )

    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete the existing database and rebuild it.",
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
        default=DEFAULT_DELAY,
        help=(
            "Seconds to wait between requests. "
            f"Default: {DEFAULT_DELAY}."
        ),
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=(
            "Request timeout in seconds. "
            f"Default: {DEFAULT_TIMEOUT}."
        ),
    )

    parser.add_argument(
        "--do-not-stop-on-empty",
        action="store_true",
        help=(
            "Continue through max-season even after consecutive "
            "empty seasons."
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
    conn.commit()

    series_id = upsert_series(conn)
    conn.commit()

    seasons_requested = 0
    seasons_succeeded = 0
    seasons_failed = 0

    episodes_received = 0
    episodes_inserted = 0
    episodes_updated = 0

    consecutive_empty_seasons = 0

    print(f"Database: {database_path}")
    print(f"Series: {SERIES_TITLE}")
    print(f"IMDb ID: {SERIES_IMDB_ID}")
    print()

    try:
        for season_number in range(
            1,
            args.max_season + 1,
        ):
            seasons_requested += 1

            print(f"Season {season_number}:")

            try:
                status, payload, response_text = fetch_season(
                    session=session,
                    season_number=season_number,
                    timeout=args.timeout,
                )

            except requests.RequestException as exc:
                seasons_failed += 1

                error_message = str(exc)

                print(f"  Request failed: {error_message}")

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

                consecutive_empty_seasons += 1

                if (
                    not args.do_not_stop_on_empty
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

                continue

            error_message = get_api_error(payload)

            if status < 200 or status >= 300:
                seasons_failed += 1

                message = error_message or f"HTTP {status}"

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

                consecutive_empty_seasons += 1

            elif error_message:
                seasons_failed += 1

                print(f"  API error: {error_message}")

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

                consecutive_empty_seasons += 1

            else:
                items = extract_episode_items(payload)
                episode_count = len(items)

                if episode_count == 0:
                    print("  No episodes returned")

                    consecutive_empty_seasons += 1
                else:
                    print(f"  Episodes returned: {episode_count}")

                    consecutive_empty_seasons = 0
                    seasons_succeeded += 1
                    episodes_received += episode_count

                    with conn:
                        series_id = upsert_series(
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

                                if action == "inserted":
                                    episodes_inserted += 1
                                else:
                                    episodes_updated += 1

                                season_value = parse_integer(
                                    item.get("seasonNumber")
                                )

                                episode_value = parse_integer(
                                    item.get("episodeNumber")
                                )

                                title_value = (
                                    clean_text(item.get("title"))
                                    or clean_text(item.get("id"))
                                    or "Unknown"
                                )

                                rating_value = parse_float(
                                    item.get("imDbRating")
                                )

                                count_value = parse_integer(
                                    item.get("imDbRatingCount")
                                )

                                print(
                                    f"    S{season_value}"
                                    f"E{episode_value} "
                                    f"{title_value} | "
                                    f"IMDb: "
                                    f"{rating_value if rating_value is not None else 'N/A'} "
                                    f"| Votes: "
                                    f"{count_value if count_value is not None else 'N/A'}"
                                )

                            except (
                                ValueError,
                                sqlite3.Error,
                            ) as exc:
                                print(
                                    f"    Episode skipped: {exc}",
                                    file=sys.stderr,
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

                if episode_count == 0:
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

            if (
                not args.do_not_stop_on_empty
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
            "\nStopped by user. Saved episodes were preserved.",
            file=sys.stderr,
        )

        return_code = 130

    else:
        return_code = 0

    finally:
        try:
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
                        episodes_updated = ?
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
                        run_id,
                    ),
                )

            total_episodes_row = conn.execute(
                """
                SELECT COUNT(*)
                FROM episodes
                """
            ).fetchone()

            total_episodes = (
                int(total_episodes_row[0])
                if total_episodes_row
                else 0
            )

        finally:
            session.close()
            conn.close()

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
    print(f"Total episodes in database: {total_episodes}")
    print("=" * 60)

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())