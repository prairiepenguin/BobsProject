from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DATA_PATH = APP_DIR / "data" / "tvdb-194031-bobs-burgers-episodes.json"
RATINGS_PATH = APP_DIR / "data" / "omdb-bobs-burgers-ratings.json"
TV_API_DB_PATH = APP_DIR / "bobs_burgers_tv_api.db"
DB_PATH = APP_DIR / "bobs_burgers.db"
APP_SCHEMA_VERSION = "2026-07-01.3"
ROLE_ORDER = ("Director", "Writer", "Actor", "Guest Star")
CREATIVE_ROLES = ("Director", "Writer")
DISPLAY_RATING_SOURCE = "IMDb"


st.set_page_config(
    page_title="Bob's Burgers Credits",
    page_icon=":material/live_tv:",
    layout="wide",
    initial_sidebar_state="expanded",
)


def apply_theme() -> None:
    st.markdown(
        """
        <style>
            .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1320px; }
            [data-testid="stSidebar"] { border-right: 1px solid #e5e1dc; }
            [data-testid="stMetric"] { background: #fbfaf8; border: 1px solid #ebe4dc; border-radius: 8px; padding: 0.85rem 1rem; }
            [data-testid="stMetric"] * { color: #2f2924 !important; }
            [data-testid="stMetricLabel"], [data-testid="stMetricValue"], [data-testid="stMetricDelta"] { color: #2f2924 !important; }
            div.stButton > button { border-radius: 8px; border-color: #d9d2ca; min-height: 2.65rem; text-align: left; justify-content: flex-start; }
            div.stButton > button:hover { border-color: #d94f30; color: #b83c24; }
            .bb-eyebrow { color: #756b62; font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.15rem; }
            .bb-page-title { font-size: 2rem; font-weight: 720; line-height: 1.15; margin-bottom: 0.2rem; }
            .bb-page-copy { color: #5f5851; font-size: 1rem; margin-bottom: 1.15rem; }
            .bb-list-header { color: #756b62; font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid #ebe4dc; padding-bottom: 0.35rem; margin-bottom: 0.35rem; }
            .bb-muted { color: #746a61; font-size: 0.92rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def page_header(title: str, eyebrow: str = "", copy: str = "") -> None:
    if eyebrow:
        st.markdown(f'<div class="bb-eyebrow">{eyebrow}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="bb-page-title">{title}</div>', unsafe_allow_html=True)
    if copy:
        st.markdown(f'<div class="bb-page-copy">{copy}</div>', unsafe_allow_html=True)


def placeholders(items: tuple | list) -> str:
    return ", ".join("?" for _ in items)


def role_sort_sql(alias: str = "c") -> str:
    return f"""
        CASE {alias}.role
            WHEN 'Director' THEN 0
            WHEN 'Writer' THEN 1
            WHEN 'Actor' THEN 2
            WHEN 'Guest Star' THEN 3
            ELSE 4
        END
    """


def episode_code(season: int | None, number: int | None) -> str:
    if season == 0:
        return f"Special {number}"
    if season is None or number is None:
        return "Unknown"
    return f"S{season:02d}E{number:02d}"


def list_header(*labels: str) -> None:
    widths = [4, 1, 2, 2][: len(labels)]
    cols = st.columns(widths)
    for col, label in zip(cols, labels):
        col.markdown(f'<div class="bb-list-header">{label}</div>', unsafe_allow_html=True)


def load_json_export() -> dict:
    if not DATA_PATH.exists():
        st.error(f"Could not find the TVDB export at {DATA_PATH}.")
        st.stop()
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def load_ratings_export() -> dict:
    if not RATINGS_PATH.exists():
        return {"episodes": [], "fetchedAt": ""}
    return json.loads(RATINGS_PATH.read_text(encoding="utf-8-sig"))


def merge_tv_api_ratings(conn: sqlite3.Connection) -> None:
    if not TV_API_DB_PATH.exists():
        return

    conn.execute("ATTACH DATABASE ? AS tvapi", (str(TV_API_DB_PATH),))
    source_rows = conn.execute(
        """
        SELECT imdb_id, season_number, episode_number, imdb_rating,
               imdb_rating_count, rating_normalized, fetched_at
        FROM tvapi.episodes
        WHERE imdb_rating IS NOT NULL
        """
    ).fetchall()

    target_by_imdb = {
        row["external_id"]: row["episode_id"]
        for row in conn.execute(
            "SELECT episode_id, external_id FROM external_ids WHERE UPPER(source) = 'IMDB'"
        )
    }
    target_by_season_episode = {
        (row["season_number"], row["episode_number"]): row["episode_id"]
        for row in conn.execute(
            "SELECT episode_id, season_number, episode_number FROM episodes"
        )
    }

    for row in source_rows:
        episode_ids = [
            match["episode_id"]
            for match in conn.execute(
                "SELECT episode_id FROM external_ids WHERE UPPER(source) = 'IMDB' AND external_id = ?",
                (row["imdb_id"],),
            )
        ]
        if not episode_ids:
            fallback_id = target_by_season_episode.get(
                (row["season_number"], row["episode_number"])
            )
            if not fallback_id:
                continue
            episode_ids = [fallback_id]
            target_by_imdb[row["imdb_id"]] = fallback_id
            conn.execute(
                "INSERT OR IGNORE INTO external_ids (episode_id, source, external_id) VALUES (?, 'IMDB', ?)",
                (fallback_id, row["imdb_id"]),
            )

        rating = float(row["imdb_rating"])
        normalized = row["rating_normalized"] if row["rating_normalized"] is not None else round(rating * 10, 2)
        for episode_id in episode_ids:
            conn.execute(
                """
                INSERT INTO episode_ratings (episode_id, source, rating_label, rating_normalized, votes, fetched_at)
                VALUES (?, 'IMDb', ?, ?, ?, ?)
                ON CONFLICT(episode_id, source) DO UPDATE SET
                    rating_label = excluded.rating_label,
                    rating_normalized = excluded.rating_normalized,
                    votes = excluded.votes,
                    fetched_at = excluded.fetched_at
                """,
                (
                    episode_id,
                    f"{rating:.1f}/10",
                    normalized,
                    row["imdb_rating_count"],
                    row["fetched_at"],
                ),
            )


def remote_id_map(episode: dict) -> dict[str, str]:
    ids: dict[str, str] = {}
    for remote in episode.get("remoteIds") or []:
        if isinstance(remote, str) and ":" in remote:
            source, value = remote.split(":", 1)
            ids[source] = value
        elif isinstance(remote, dict):
            source = remote.get("sourceName") or remote.get("source") or remote.get("type") or remote.get("name")
            value = remote.get("id") or remote.get("identifier") or remote.get("url")
            if source and value:
                ids[str(source)] = str(value)
    return ids


def normalized_rating(source: str, value: str | None) -> float | None:
    if not value or value == "N/A":
        return None
    text = str(value).strip()
    try:
        if text.endswith("%"):
            return float(text[:-1])
        if "/" in text:
            left, right = text.split("/", 1)
            return float(left.replace(",", "")) / float(right.replace(",", "")) * 100
        numeric = float(text.replace(",", ""))
        return numeric if numeric > 10 else numeric * 10
    except ValueError:
        return None


def clean_vote_count(value: str | None) -> int | None:
    if not value or value == "N/A":
        return None
    digits = "".join(char for char in str(value) if char.isdigit())
    return int(digits) if digits else None


def rating_source_name(source: str) -> str:
    return "IMDb" if source == "Internet Movie Database" else source


def tvdb_content_rating(episode: dict) -> str | None:
    ratings = episode.get("contentRatings") or []
    if isinstance(ratings, str):
        return ratings or None
    if not isinstance(ratings, list):
        return None
    usa_rating = next((item for item in ratings if isinstance(item, dict) and item.get("country") == "usa"), None)
    rating = usa_rating or next((item for item in ratings if isinstance(item, dict)), None)
    if rating:
        return rating.get("name")
    return None


def names_for_role(episode: dict, role: str) -> list[str]:
    source = episode.get("writers") or [] if role == "Writer" else episode.get("directors") or [] if role == "Director" else []
    names: list[str] = []
    seen: set[str] = set()
    for name in source:
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    for credit in episode.get("characters") or []:
        if credit.get("peopleType") != role:
            continue
        name = credit.get("personName") or credit.get("name")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def build_database() -> None:
    payload = load_json_export()
    ratings_payload = load_ratings_export()
    episodes = payload.get("episodes", [])
    ratings_by_episode = {item.get("tvdbEpisodeId"): item for item in ratings_payload.get("episodes", [])}
    source_mtime = DATA_PATH.stat().st_mtime
    ratings_mtime = RATINGS_PATH.stat().st_mtime if RATINGS_PATH.exists() else 0
    tv_api_mtime = TV_API_DB_PATH.stat().st_mtime if TV_API_DB_PATH.exists() else 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS episode_ratings;
            DROP TABLE IF EXISTS external_ids;
            DROP TABLE IF EXISTS credits;
            DROP TABLE IF EXISTS people;
            DROP TABLE IF EXISTS episodes;
            DROP TABLE IF EXISTS metadata;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE episodes (
                episode_id INTEGER PRIMARY KEY,
                series_id INTEGER,
                season_number INTEGER,
                episode_number INTEGER,
                absolute_number INTEGER,
                title TEXT NOT NULL,
                aired TEXT,
                runtime INTEGER,
                year INTEGER,
                finale_type TEXT,
                overview TEXT,
                image TEXT,
                production_code TEXT,
                content_rating TEXT,
                last_updated TEXT
            );
            CREATE TABLE people (
                person_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE,
                tvdb_url TEXT,
                image TEXT
            );
            CREATE TABLE credits (
                credit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                character_name TEXT,
                source TEXT NOT NULL DEFAULT 'TVDB',
                FOREIGN KEY (episode_id) REFERENCES episodes (episode_id),
                FOREIGN KEY (person_id) REFERENCES people (person_id),
                UNIQUE (episode_id, person_id, role, character_name)
            );
            CREATE TABLE external_ids (
                episode_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                FOREIGN KEY (episode_id) REFERENCES episodes (episode_id),
                UNIQUE (episode_id, source)
            );
            CREATE TABLE episode_ratings (
                rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                rating_label TEXT NOT NULL,
                rating_normalized REAL,
                votes INTEGER,
                fetched_at TEXT,
                FOREIGN KEY (episode_id) REFERENCES episodes (episode_id),
                UNIQUE (episode_id, source)
            );
            """
        )
        people_by_name: dict[str, int] = {}
        next_person_id = -1

        def ensure_person(name: str, tvdb_id: int | None = None, url: str | None = None, image: str | None = None) -> int:
            nonlocal next_person_id
            key = name.strip().lower()
            if key in people_by_name:
                person_id = people_by_name[key]
            elif tvdb_id:
                person_id = int(tvdb_id)
            else:
                person_id = next_person_id
                next_person_id -= 1
            people_by_name[key] = person_id
            conn.execute(
                """
                INSERT INTO people (person_id, name, tvdb_url, image)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET
                    name = excluded.name,
                    tvdb_url = COALESCE(excluded.tvdb_url, people.tvdb_url),
                    image = COALESCE(excluded.image, people.image)
                """,
                (person_id, name.strip(), url, image),
            )
            return person_id

        for episode in episodes:
            conn.execute(
                """
                INSERT INTO episodes (episode_id, series_id, season_number, episode_number, absolute_number, title, aired, runtime, year, finale_type, overview, image, production_code, content_rating, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode.get("id"), episode.get("seriesId"), episode.get("seasonNumber"), episode.get("number"), episode.get("absoluteNumber"),
                    episode.get("name") or "Untitled", episode.get("aired"), episode.get("runtime"),
                    int(episode["year"]) if str(episode.get("year") or "").isdigit() else None,
                    episode.get("finaleType"), episode.get("overview"), episode.get("image"), episode.get("productionCode"), tvdb_content_rating(episode), episode.get("lastUpdated"),
                ),
            )
            for source, external_id in remote_id_map(episode).items():
                conn.execute(
                    "INSERT OR IGNORE INTO external_ids (episode_id, source, external_id) VALUES (?, ?, ?)",
                    (episode.get("id"), source, external_id),
                )
            ratings_record = ratings_by_episode.get(episode.get("id"))
            if ratings_record and ratings_record.get("response") == "True":
                seen_rating_sources: set[str] = set()
                for rating in ratings_record.get("ratings") or []:
                    source = rating_source_name(rating.get("source") or "")
                    value = rating.get("value")
                    if not source or not value or source in seen_rating_sources:
                        continue
                    seen_rating_sources.add(source)
                    conn.execute(
                        "INSERT OR IGNORE INTO episode_ratings (episode_id, source, rating_label, rating_normalized, votes, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            episode.get("id"), source, value, normalized_rating(source, value),
                            clean_vote_count(ratings_record.get("imdbVotes")) if source == "IMDb" else None,
                            ratings_payload.get("fetchedAt"),
                        ),
                    )
                if ratings_record.get("metascore") and ratings_record.get("metascore") != "N/A" and "Metacritic" not in seen_rating_sources:
                    value = f'{ratings_record.get("metascore")}/100'
                    conn.execute(
                        "INSERT OR IGNORE INTO episode_ratings (episode_id, source, rating_label, rating_normalized, votes, fetched_at) VALUES (?, 'Metacritic', ?, ?, NULL, ?)",
                        (episode.get("id"), value, normalized_rating("Metacritic", value), ratings_payload.get("fetchedAt")),
                    )
            inserted_credit_keys: set[tuple[int, str, str | None]] = set()
            for credit in episode.get("characters") or []:
                role = credit.get("peopleType")
                name = credit.get("personName") or credit.get("name")
                if not role or not name:
                    continue
                person_id = ensure_person(name, credit.get("peopleId"), credit.get("url"), credit.get("personImgURL"))
                character_name = credit.get("name") if role in ("Actor", "Guest Star") else None
                key = (person_id, role, character_name)
                if key in inserted_credit_keys:
                    continue
                inserted_credit_keys.add(key)
                conn.execute(
                    "INSERT OR IGNORE INTO credits (episode_id, person_id, role, character_name, source) VALUES (?, ?, ?, ?, 'TVDB')",
                    (episode.get("id"), person_id, role, character_name),
                )
            for role in CREATIVE_ROLES:
                for name in names_for_role(episode, role):
                    person_id = ensure_person(name)
                    key = (person_id, role, None)
                    if key in inserted_credit_keys:
                        continue
                    inserted_credit_keys.add(key)
                    conn.execute(
                        "INSERT OR IGNORE INTO credits (episode_id, person_id, role, character_name, source) VALUES (?, ?, ?, NULL, 'TVDB')",
                        (episode.get("id"), person_id, role),
                    )
        merge_tv_api_ratings(conn)
        conn.execute("INSERT INTO metadata (key, value) VALUES ('source_mtime', ?)", (str(source_mtime),))
        conn.execute("INSERT INTO metadata (key, value) VALUES ('ratings_mtime', ?)", (str(ratings_mtime),))
        conn.execute("INSERT INTO metadata (key, value) VALUES ('tv_api_mtime', ?)", (str(tv_api_mtime),))
        conn.execute("INSERT INTO metadata (key, value) VALUES ('episode_count', ?)", (str(len(episodes)),))
        conn.execute("INSERT INTO metadata (key, value) VALUES ('app_schema_version', ?)", (APP_SCHEMA_VERSION,))
        conn.executescript(
            """
            CREATE INDEX idx_episodes_season_number ON episodes(season_number, episode_number);
            CREATE INDEX idx_episodes_year_air_title ON episodes(year, aired, title);
            CREATE INDEX idx_credits_role_person_episode ON credits(role, person_id, episode_id);
            CREATE INDEX idx_credits_episode_role_person ON credits(episode_id, role, person_id);
            CREATE INDEX idx_people_name ON people(name COLLATE NOCASE);
            CREATE INDEX idx_external_ids_episode_source ON external_ids(episode_id, source);
            CREATE INDEX idx_episode_ratings_episode_source ON episode_ratings(episode_id, source);
            """
        )
        conn.commit()
    finally:
        conn.close()


def database_needs_rebuild() -> bool:
    if not DB_PATH.exists():
        return True
    if not DATA_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM metadata WHERE key = 'source_mtime'").fetchone()
        ratings_row = conn.execute("SELECT value FROM metadata WHERE key = 'ratings_mtime'").fetchone()
        tv_api_row = conn.execute("SELECT value FROM metadata WHERE key = 'tv_api_mtime'").fetchone()
        schema_row = conn.execute("SELECT value FROM metadata WHERE key = 'app_schema_version'").fetchone()
        conn.close()
    except sqlite3.Error:
        return True
    return (
        not row
        or row[0] != str(DATA_PATH.stat().st_mtime)
        or (RATINGS_PATH.exists() and (not ratings_row or ratings_row[0] != str(RATINGS_PATH.stat().st_mtime)))
        or (TV_API_DB_PATH.exists() and (not tv_api_row or tv_api_row[0] != str(TV_API_DB_PATH.stat().st_mtime)))
        or not schema_row
        or schema_row[0] != APP_SCHEMA_VERSION
    )


def ensure_database() -> None:
    if database_needs_rebuild():
        st.cache_resource.clear()
        st.cache_data.clear()
        build_database()


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    ensure_database()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -64000")
    return conn


@st.cache_data(show_spinner=False)
def load_frame(query: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(query, get_connection(), params=params)


@st.cache_data(show_spinner=False)
def table_exists(table_name: str) -> bool:
    row = get_connection().execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


@st.cache_data(show_spinner=False)
def get_episode_storefronts(episode_id: int) -> pd.DataFrame:
    if not table_exists("store_next_door"):
        return pd.DataFrame(columns=["store_name", "notes"])
    return load_frame(
        """
        SELECT store_name, notes
        FROM store_next_door
        WHERE episode_id = ?
        ORDER BY store_next_door_id
        """,
        (episode_id,),
    )


def render_episode_storefronts(episode_id: int) -> None:
    storefronts = get_episode_storefronts(episode_id)
    st.subheader("Store Next Door")
    if storefronts.empty:
        if table_exists("store_next_door"):
            st.caption("No Store Next Door listing is recorded for this episode.")
        else:
            st.caption("Store Next Door data is not installed in this database.")
        return

    for storefront in storefronts.itertuples(index=False):
        st.markdown(f"**{storefront.store_name}**")
        if getattr(storefront, "notes", None):
            st.caption(storefront.notes)


@st.cache_data(show_spinner=False)
def get_roles() -> list[str]:
    rows = get_connection().execute("SELECT DISTINCT role FROM credits ORDER BY role COLLATE NOCASE").fetchall()
    return [row["role"] for row in rows]


@st.cache_data(show_spinner=False)
def get_seasons() -> list[int]:
    rows = get_connection().execute("SELECT DISTINCT season_number FROM episodes ORDER BY season_number").fetchall()
    return [row["season_number"] for row in rows]


@st.cache_data(show_spinner=False)
def get_year_bounds() -> tuple[int, int]:
    row = get_connection().execute("SELECT MIN(year), MAX(year) FROM episodes WHERE year IS NOT NULL").fetchone()
    return int(row[0] or 2011), int(row[1] or 2011)


def open_episode(episode_id: int) -> None:
    st.session_state.view = "episode"
    st.session_state.selected_episode_id = int(episode_id)


def open_person(person_id: int) -> None:
    st.session_state.view = "person"
    st.session_state.selected_person_id = int(person_id)


def back_to_explorer() -> None:
    st.session_state.view = "explorer"


def render_sidebar_nav() -> str:
    st.sidebar.title("Bob's Burgers Project")
    st.sidebar.caption(f"App build {APP_SCHEMA_VERSION}")
    nav_options = ["Home", "Episodes", "Ratings", "Person Search", "Trends", "Teams", "Data Quality", "SQL"]
    pending_nav = st.session_state.pop("pending_nav", None)
    if pending_nav in nav_options:
        st.session_state.main_nav = pending_nav
    page = st.sidebar.radio("Navigate", nav_options, key="main_nav")
    st.sidebar.divider()
    return page


def sidebar_filters(alias: str = "e") -> tuple[str, tuple]:
    min_year, max_year = get_year_bounds()
    seasons = get_seasons()
    st.sidebar.header("Filters")
    search = st.sidebar.text_input("Episode title contains")
    selected_seasons = st.sidebar.multiselect("Season", seasons, format_func=lambda value: "Specials" if value == 0 else f"Season {value}")
    year_range = st.sidebar.slider("Year", min_year, max_year, (min_year, max_year))
    roles = st.sidebar.multiselect("Credit role", get_roles())
    where = [f"({alias}.year IS NULL OR {alias}.year BETWEEN ? AND ?)"]
    params: list = [year_range[0], year_range[1]]
    if search.strip():
        where.append(f"LOWER({alias}.title) LIKE ?")
        params.append(f"%{search.strip().lower()}%")
    if selected_seasons:
        where.append(f"{alias}.season_number IN ({placeholders(selected_seasons)})")
        params.extend(selected_seasons)
    if roles:
        where.append(f"EXISTS (SELECT 1 FROM credits fc WHERE fc.episode_id = {alias}.episode_id AND fc.role IN ({placeholders(roles)}))")
        params.extend(roles)
    return " AND ".join(where), tuple(params)


def metric_row() -> None:
    summary = load_frame(
        """
        SELECT COUNT(*) AS episodes,
               COUNT(DISTINCT season_number) AS seasons,
               (SELECT COUNT(*) FROM people) AS people,
               (SELECT COUNT(*) FROM credits) AS credits,
               SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM credits c WHERE c.episode_id = e.episode_id AND c.role = 'Writer') THEN 1 ELSE 0 END) AS missing_writers,
               SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM credits c WHERE c.episode_id = e.episode_id AND c.role = 'Director') THEN 1 ELSE 0 END) AS missing_directors
        FROM episodes e
        """
    ).iloc[0]
    cols = st.columns(6)
    labels = ["Episodes", "Seasons", "People", "Credits", "Missing Writers", "Missing Directors"]
    keys = ["episodes", "seasons", "people", "credits", "missing_writers", "missing_directors"]
    for col, label, key in zip(cols, labels, keys):
        col.metric(label, f"{summary[key]:,.0f}")


def episode_button_rows(frame: pd.DataFrame, key_prefix: str) -> None:
    if frame.empty:
        st.caption("No episodes found.")
        return
    list_header("Episode", "Aired", "Writers / Directors")
    for idx, row in enumerate(frame.itertuples(index=False)):
        cols = st.columns([4, 1, 2])
        label = f"{episode_code(row.season_number, row.episode_number)} - {row.title}"
        cols[0].button(label, key=f"{key_prefix}-{idx}-{row.episode_id}", on_click=open_episode, args=(int(row.episode_id),), width="stretch")
        cols[1].markdown(f'<div class="bb-muted">{row.aired or "Unknown"}</div>', unsafe_allow_html=True)
        cols[2].markdown(f'<div class="bb-muted">{getattr(row, "creatives", None) or "No creative credits"}</div>', unsafe_allow_html=True)


def person_button_rows(frame: pd.DataFrame, key_prefix: str, metric_col: str = "credits") -> None:
    if frame.empty:
        st.caption("No people found.")
        return
    list_header("Person", metric_col.replace("_", " ").title())
    for idx, row in enumerate(frame.itertuples(index=False)):
        cols = st.columns([5, 1])
        cols[0].button(row.name, key=f"{key_prefix}-{idx}-{row.person_id}", on_click=open_person, args=(int(row.person_id),), width="stretch")
        cols[1].markdown(f'<div class="bb-muted">{getattr(row, metric_col):,.0f}</div>', unsafe_allow_html=True)


def search_episodes(search: str = "", limit: int = 75) -> pd.DataFrame:
    term = search.strip().lower()
    if not term:
        return load_frame(
            f"""
            SELECT e.*, GROUP_CONCAT(DISTINCT c.role || ': ' || p.name) AS creatives
            FROM episodes e
            LEFT JOIN credits c ON c.episode_id = e.episode_id AND c.role IN ({placeholders(CREATIVE_ROLES)})
            LEFT JOIN people p ON p.person_id = c.person_id
            GROUP BY e.episode_id
            ORDER BY e.season_number DESC, e.episode_number DESC
            LIMIT ?
            """,
            (*CREATIVE_ROLES, limit),
        )
    return load_frame(
        f"""
        SELECT e.*, GROUP_CONCAT(DISTINCT c.role || ': ' || p.name) AS creatives
        FROM episodes e
        LEFT JOIN credits c ON c.episode_id = e.episode_id AND c.role IN ({placeholders(CREATIVE_ROLES)})
        LEFT JOIN people p ON p.person_id = c.person_id
        WHERE LOWER(e.title) LIKE ? OR LOWER(COALESCE(e.overview, '')) LIKE ?
        GROUP BY e.episode_id
        ORDER BY CASE WHEN LOWER(e.title) = ? THEN 0 WHEN LOWER(e.title) LIKE ? THEN 1 ELSE 2 END, e.season_number, e.episode_number
        LIMIT ?
        """,
        (*CREATIVE_ROLES, f"%{term}%", f"%{term}%", term, f"{term}%", limit),
    )


def search_people(search: str, role_group: str = "Directors") -> pd.DataFrame:
    term = search.strip().lower()
    role_map = {"Directors": ("Director",), "Writers": ("Writer",), "Actors": ("Actor",), "Guest Stars": ("Guest Star",), "All People": tuple()}
    roles = role_map.get(role_group, ("Director",))
    joins = "JOIN credits c ON c.person_id = p.person_id" if roles else "LEFT JOIN credits c ON c.person_id = p.person_id"
    where = []
    params: list = []
    if roles:
        where.append(f"c.role IN ({placeholders(roles)})")
        params.extend(roles)
    if term:
        where.append("LOWER(p.name) LIKE ?")
        params.append(f"%{term}%")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    ranking_sql = ""
    if term:
        ranking_sql = "CASE WHEN LOWER(p.name) = ? THEN 0 WHEN LOWER(p.name) LIKE ? THEN 1 ELSE 2 END,"
        params.extend([term, f"{term}%"])
    return load_frame(
        f"""
        SELECT p.person_id, p.name, COUNT(DISTINCT c.episode_id) AS credits
        FROM people p
        {joins}
        {where_sql}
        GROUP BY p.person_id, p.name
        HAVING credits > 0
        ORDER BY {ranking_sql} credits DESC, p.name COLLATE NOCASE
        LIMIT 75
        """,
        tuple(params),
    )


def home_dashboard() -> None:
    page_header("Welcome to Bob's Burgers Credits", "Discover", "Look up episodes, follow writers and directors, and explore recurring creative teams across the series.")
    metric_row()
    prompt_cols = st.columns(3)
    with prompt_cols[0]:
        st.markdown("**Browse episodes**")
        st.caption("Search by title, season, air date, and creative credits.")
        if st.button("Browse episodes", key="home-go-episodes", width="stretch"):
            st.session_state.pending_nav = "Episodes"
            st.rerun()
    with prompt_cols[1]:
        st.markdown("**Find people**")
        st.caption("Open writers, directors, actors, and guest stars.")
        if st.button("Search people", key="home-go-people", width="stretch"):
            st.session_state.pending_nav = "Person Search"
            st.rerun()
    with prompt_cols[2]:
        st.markdown("**Compare teams**")
        st.caption("See director/writer and creative/cast partnerships.")
        if st.button("Explore teams", key="home-go-teams", width="stretch"):
            st.session_state.pending_nav = "Teams"
            st.rerun()
    quick_episode, quick_person = st.columns(2)
    with quick_episode:
        st.subheader("Quick Episode Search")
        episode_term = st.text_input("Episode title", key="home_episode_search")
        episode_button_rows(search_episodes(episode_term).head(6), "home-episode")
    with quick_person:
        st.subheader("Directors You Might Recognize")
        person_term = st.text_input("Person name", key="home_person_search")
        if person_term.strip():
            person_button_rows(search_people(person_term, "All People").head(6), "home-person")
        else:
            featured = load_frame("""
                SELECT p.person_id, p.name, COUNT(DISTINCT c.episode_id) AS credits
                FROM people p JOIN credits c ON c.person_id = p.person_id
                WHERE c.role = 'Director'
                GROUP BY p.person_id, p.name
                ORDER BY credits DESC, p.name COLLATE NOCASE
                LIMIT 6
            """)
            person_button_rows(featured, "home-featured-person")


def episodes_view(where_clause: str, params: tuple) -> None:
    page_header("Episodes", "Browse", "Search and filter Bob's Burgers episodes, then open a full episode page for credits and related episodes.")
    quick_search = st.text_input("Search episode titles", key="episodes_page_search")
    local_where = where_clause
    local_params = list(params)
    if quick_search.strip():
        local_where = f"({local_where}) AND LOWER(e.title) LIKE ?"
        local_params.append(f"%{quick_search.strip().lower()}%")
    episodes = load_frame(
        f"""
        SELECT e.*, GROUP_CONCAT(DISTINCT c.role || ': ' || p.name) AS creatives
        FROM episodes e
        LEFT JOIN credits c ON c.episode_id = e.episode_id AND c.role IN ({placeholders(CREATIVE_ROLES)})
        LEFT JOIN people p ON p.person_id = c.person_id
        WHERE {local_where}
        GROUP BY e.episode_id
        ORDER BY e.season_number, e.episode_number
        LIMIT 500
        """,
        (*CREATIVE_ROLES, *local_params),
    )
    episode_button_rows(episodes, "episodes-list")
    with st.expander("Table view"):
        st.dataframe(episodes, width="stretch", hide_index=True)


def person_search_view() -> None:
    page_header("Find a Person", "Search", "Start with directors and writers, then switch roles when you want actors and guest stars too.")
    filter_col, search_col = st.columns([1, 2])
    with filter_col:
        role_group = st.selectbox("Person type", ["Directors", "Writers", "Actors", "Guest Stars", "All People"])
    with search_col:
        search = st.text_input("Search by name")
    person_button_rows(search_people(search, role_group), "person-search")


def credit_button_grid(credits: pd.DataFrame, roles: tuple[str, ...], label: str, key_prefix: str) -> None:
    group = credits[credits["role"].isin(roles)]
    st.markdown(f"**{label}**")
    if group.empty:
        st.caption(f"No {label.lower()} credits found in this database.")
        return
    cols = st.columns(3)
    for idx, person in enumerate(group.itertuples(index=False)):
        role_note = "" if len(roles) == 1 else f" ({person.role})"
        character_note = f" as {person.character_name}" if getattr(person, "character_name", None) else ""
        cols[idx % 3].button(f"{person.name}{role_note}{character_note}", key=f"{key_prefix}-{label}-{idx}-{person.person_id}-{person.role}", on_click=open_person, args=(int(person.person_id),), width="stretch")


def related_episode_rows(frame: pd.DataFrame, key_prefix: str) -> None:
    if frame.empty:
        st.caption("No related creative-team episodes found.")
        return
    header = st.columns([3, 1, 3, 2])
    for col, label in zip(header, ["Episode", "Aired", "Shared People", "Connection"]):
        col.markdown(f'<div class="bb-list-header">{label}</div>', unsafe_allow_html=True)
    for idx, row in enumerate(frame.itertuples(index=False)):
        cols = st.columns([3, 1, 3, 2])
        cols[0].button(f"{episode_code(row.season_number, row.episode_number)} - {row.title}", key=f"{key_prefix}-{idx}-{row.episode_id}", on_click=open_episode, args=(int(row.episode_id),), width="stretch")
        cols[1].markdown(f'<div class="bb-muted">{row.aired or "Unknown"}</div>', unsafe_allow_html=True)
        cols[2].markdown(f'<div class="bb-muted">{row.shared_people or ""}</div>', unsafe_allow_html=True)
        cols[3].markdown(f'<div class="bb-muted">{row.connection_types or ""}</div>', unsafe_allow_html=True)


def rated_episode_rows(frame: pd.DataFrame, key_prefix: str) -> None:
    if frame.empty:
        st.caption("No rated episodes found.")
        return
    header = st.columns([3, 1, 1, 1, 1])
    for col, label in zip(header, ["Episode", "Role", "Rating", "Votes", "TVDB Content Rating"]):
        col.markdown(f'<div class="bb-list-header">{label}</div>', unsafe_allow_html=True)
    for idx, row in enumerate(frame.itertuples(index=False)):
        cols = st.columns([3, 1, 1, 1, 1])
        cols[0].button(f"{episode_code(row.season_number, row.episode_number)} - {row.title}", key=f"{key_prefix}-{idx}-{row.episode_id}", on_click=open_episode, args=(int(row.episode_id),), width="stretch")
        cols[1].markdown(f'<div class="bb-muted">{row.role}</div>', unsafe_allow_html=True)
        normalized = "" if pd.isna(row.rating_normalized) else f" ({row.rating_normalized:.1f}/100)"
        cols[2].markdown(f'<div class="bb-muted">{row.rating_label}{normalized}</div>', unsafe_allow_html=True)
        votes = "" if pd.isna(row.votes) else f"{int(row.votes):,}"
        cols[3].markdown(f'<div class="bb-muted">{votes}</div>', unsafe_allow_html=True)
        content_rating = getattr(row, "content_rating", None) or ""
        cols[4].markdown(f'<div class="bb-muted">{content_rating}</div>', unsafe_allow_html=True)


def episode_detail_page(episode_id: int) -> None:
    episode = load_frame("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,))
    if episode.empty:
        st.error("Episode not found.")
        return
    row = episode.iloc[0]
    st.button("Back to explorer", on_click=back_to_explorer)
    page_header(row.title, "Explorer / Episode", "Writers, directors, cast, guest stars, and episodes connected by the same creative team.")
    cols = st.columns(6)
    cols[0].metric("Episode", episode_code(row.season_number, row.episode_number))
    cols[1].metric("Air Date", row.aired or "Unknown")
    cols[2].metric("Runtime", "Unknown" if pd.isna(row.runtime) else f"{int(row.runtime)} min")
    cols[3].metric("Year", "Unknown" if pd.isna(row.year) else f"{int(row.year)}")
    cols[4].metric("TVDB Content Rating", row.content_rating or "Unknown")
    cols[5].metric("TVDB ID", f"{int(row.episode_id)}")
    ratings = load_frame("SELECT source, rating_label, rating_normalized, votes FROM episode_ratings WHERE episode_id = ? AND source = ?", (episode_id, DISPLAY_RATING_SOURCE))
    if not ratings.empty:
        rating_cols = st.columns(min(len(ratings), 4))
        for idx, rating in enumerate(ratings.itertuples(index=False)):
            help_text = f"Normalized: {rating.rating_normalized:.1f}/100" if not pd.isna(rating.rating_normalized) else None
            vote_text = f" ({int(rating.votes):,} votes)" if not pd.isna(rating.votes) else ""
            rating_cols[idx % len(rating_cols)].metric(rating.source, f"{rating.rating_label}{vote_text}", help=help_text)
    render_episode_storefronts(episode_id)
    if row.overview:
        st.write(row.overview)
    if row.image:
        st.image(row.image, width="stretch")
    credits = load_frame(
        f"""
        SELECT p.person_id, p.name, c.role, c.character_name, GROUP_CONCAT(DISTINCT c.source) AS sources
        FROM credits c JOIN people p ON p.person_id = c.person_id
        WHERE c.episode_id = ?
        GROUP BY p.person_id, p.name, c.role, c.character_name
        ORDER BY {role_sort_sql('c')}, p.name COLLATE NOCASE
        """,
        (episode_id,),
    )
    st.subheader("Credits")
    credit_button_grid(credits, ("Director",), "Directors", f"episode-{episode_id}")
    credit_button_grid(credits, ("Writer",), "Writing", f"episode-{episode_id}")
    credit_button_grid(credits, ("Actor",), "Cast", f"episode-{episode_id}")
    credit_button_grid(credits, ("Guest Star",), "Guest Stars", f"episode-{episode_id}")
    related = load_frame(
        f"""
        WITH selected_creatives AS (SELECT DISTINCT person_id FROM credits WHERE episode_id = ? AND role IN ({placeholders(CREATIVE_ROLES)})),
        related_connections AS (
            SELECT e.episode_id, e.title, e.season_number, e.episode_number, e.aired, p.name, c.role AS connection_type
            FROM episodes e
            JOIN credits c ON c.episode_id = e.episode_id
            JOIN selected_creatives sc ON sc.person_id = c.person_id
            JOIN people p ON p.person_id = c.person_id
            WHERE e.episode_id <> ? AND c.role IN ({placeholders(CREATIVE_ROLES)})
        )
        SELECT episode_id, title, season_number, episode_number, aired,
               GROUP_CONCAT(DISTINCT name) AS shared_people,
               GROUP_CONCAT(DISTINCT connection_type) AS connection_types,
               COUNT(DISTINCT name) AS shared_count
        FROM related_connections
        GROUP BY episode_id, title, season_number, episode_number, aired
        ORDER BY shared_count DESC, season_number, episode_number
        LIMIT 30
        """,
        (episode_id, *CREATIVE_ROLES, episode_id, *CREATIVE_ROLES),
    )
    st.subheader("Related Creative-Team Episodes")
    related_episode_rows(related, f"related-{episode_id}")


def get_person_collaborators(person_id: int) -> pd.DataFrame:
    return load_frame(
        """
        SELECT other.person_id, other.name, COUNT(DISTINCT c1.episode_id) AS episodes_together, GROUP_CONCAT(DISTINCT c2.role) AS roles
        FROM credits c1
        JOIN credits c2 ON c2.episode_id = c1.episode_id AND c2.person_id <> c1.person_id
        JOIN people other ON other.person_id = c2.person_id
        WHERE c1.person_id = ?
        GROUP BY other.person_id, other.name
        ORDER BY episodes_together DESC, other.name COLLATE NOCASE
        LIMIT 75
        """,
        (person_id,),
    )


def get_person_rated_episodes(person_id: int, roles: tuple[str, ...]) -> pd.DataFrame:
    params: list = [person_id, *roles, DISPLAY_RATING_SOURCE]
    return load_frame(
        f"""
        SELECT e.episode_id, e.title, e.season_number, e.episode_number, e.aired, e.content_rating,
               c.role, er.rating_label, er.rating_normalized, er.votes
        FROM credits c
        JOIN episodes e ON e.episode_id = c.episode_id
        JOIN episode_ratings er ON er.episode_id = e.episode_id
        WHERE c.person_id = ?
          AND c.role IN ({placeholders(roles)})
          AND er.source = ?
        GROUP BY e.episode_id, e.title, e.season_number, e.episode_number, e.aired, e.content_rating,
                 c.role, er.rating_label, er.rating_normalized, er.votes
        ORDER BY er.rating_normalized DESC, er.votes DESC, e.season_number, e.episode_number
        LIMIT 75
        """,
        tuple(params),
    )


def person_profile(person_id: int) -> None:
    person = load_frame("SELECT * FROM people WHERE person_id = ?", (person_id,))
    if person.empty:
        st.error("Person not found.")
        return
    row = person.iloc[0]
    page_header(row["name"], "Explorer / Person", "Episode credits, role mix, timeline, and recurring collaborators.")
    credits = load_frame(
        f"""
        SELECT e.episode_id, e.title, e.season_number, e.episode_number, e.aired, e.year, c.role, c.character_name
        FROM credits c JOIN episodes e ON e.episode_id = c.episode_id
        WHERE c.person_id = ?
        ORDER BY e.season_number, e.episode_number, {role_sort_sql('c')}
        """,
        (person_id,),
    )
    role_summary = load_frame("SELECT role, COUNT(DISTINCT episode_id) AS episodes FROM credits WHERE person_id = ? GROUP BY role ORDER BY episodes DESC, role COLLATE NOCASE", (person_id,))
    cols = st.columns(4)
    for idx, summary in enumerate(role_summary.itertuples(index=False)):
        cols[idx % 4].metric(summary.role, f"{summary.episodes:,.0f}")
    timeline = credits.dropna(subset=["year"]).groupby(["year", "role"]).size().reset_index(name="credits")
    tab_overview, tab_ratings, tab_timeline, tab_collabs, tab_episodes = st.tabs(["Overview", "Best Ratings", "Career Timeline", "Network", "Episodes"])
    with tab_overview:
        left, right = st.columns([1, 2])
        with left:
            st.subheader("Role Mix")
            st.dataframe(role_summary, hide_index=True, width="stretch")
        with right:
            st.subheader("Top Collaborators")
            person_button_rows(get_person_collaborators(person_id).head(25), f"collabs-{person_id}", "episodes_together")
    with tab_ratings:
        creative_roles = tuple(role for role in CREATIVE_ROLES if role in set(role_summary["role"].tolist()))
        if not creative_roles:
            st.caption("No director or writer credits found for this person.")
        else:
            selected_roles = st.multiselect("Creative role", list(creative_roles), default=list(creative_roles), key=f"person-ratings-roles-{person_id}")
            if selected_roles:
                rated = get_person_rated_episodes(person_id, tuple(selected_roles))
                rated_episode_rows(rated, f"person-rated-{person_id}")
            else:
                st.caption("Select at least one role.")
    with tab_timeline:
        st.subheader("Credit Timeline")
        if timeline.empty:
            st.caption("No dated credits found.")
        else:
            st.bar_chart(timeline, x="year", y="credits", color="role", width="stretch")
            st.dataframe(timeline, hide_index=True, width="stretch")
    with tab_collabs:
        st.subheader("Collaboration Network")
        person_button_rows(get_person_collaborators(person_id), f"network-{person_id}", "episodes_together")
    with tab_episodes:
        st.subheader("Episode Credits")
        display = credits.copy()
        display.insert(0, "episode", display.apply(lambda item: episode_code(item["season_number"], item["episode_number"]), axis=1))
        st.dataframe(display, hide_index=True, width="stretch")


def person_page(person_id: int) -> None:
    st.button("Back to explorer", on_click=back_to_explorer)
    person_profile(person_id)


def top_rated_role_episodes(role: str, where_clause: str, params: tuple) -> pd.DataFrame:
    return load_frame(
        f"""
        SELECT p.person_id, p.name, e.episode_id, e.season_number, e.episode_number, e.title, e.content_rating,
               er.rating_label, er.rating_normalized, er.votes
        FROM episodes e
        JOIN credits c ON c.episode_id = e.episode_id AND c.role = ?
        JOIN people p ON p.person_id = c.person_id
        JOIN episode_ratings er ON er.episode_id = e.episode_id
        WHERE {where_clause} AND er.source = ?
        ORDER BY er.rating_normalized DESC, er.votes DESC, e.season_number, e.episode_number, p.name COLLATE NOCASE
        LIMIT 75
        """,
        (role, *params, DISPLAY_RATING_SOURCE),
    )


def trend_filters(base_where: str, base_params: tuple) -> tuple[str, tuple, int]:
    st.subheader("Trend Filters")
    content_rows = load_frame(
        "SELECT DISTINCT content_rating FROM episodes WHERE content_rating IS NOT NULL AND TRIM(content_rating) <> '' ORDER BY content_rating"
    )
    content_ratings = content_rows["content_rating"].tolist() if not content_rows.empty else []
    rating_bounds = load_frame(
        """
        SELECT COALESCE(MIN(rating_normalized), 0) AS min_rating,
               COALESCE(MAX(rating_normalized), 100) AS max_rating,
               COALESCE(MAX(votes), 0) AS max_votes
        FROM episode_ratings
        WHERE source = ?
        """,
        (DISPLAY_RATING_SOURCE,),
    ).iloc[0]
    min_rating_bound = int(rating_bounds["min_rating"])
    max_rating_bound = int(rating_bounds["max_rating"])
    max_votes_bound = int(rating_bounds["max_votes"])

    filter_cols = st.columns([2, 1, 1, 1, 1])
    selected_content = filter_cols[0].multiselect("Content rating", content_ratings)
    min_rating = filter_cols[1].slider(
        "Min IMDb",
        min_rating_bound,
        max_rating_bound,
        min_rating_bound,
    )
    min_votes = filter_cols[2].number_input(
        "Min votes",
        min_value=0,
        max_value=max_votes_bound,
        value=0,
        step=100,
    )
    include_specials = filter_cols[3].checkbox("Specials", value=True)
    min_credits = filter_cols[4].number_input("Min credits", min_value=1, max_value=25, value=1, step=1)

    clauses = [base_where]
    params = list(base_params)
    if selected_content:
        clauses.append(f"e.content_rating IN ({placeholders(selected_content)})")
        params.extend(selected_content)
    if min_rating > min_rating_bound:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM episode_ratings tr
                WHERE tr.episode_id = e.episode_id
                  AND tr.source = ?
                  AND tr.rating_normalized >= ?
            )
            """
        )
        params.extend([DISPLAY_RATING_SOURCE, min_rating])
    if min_votes > 0:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM episode_ratings tr
                WHERE tr.episode_id = e.episode_id
                  AND tr.source = ?
                  AND tr.votes >= ?
            )
            """
        )
        params.extend([DISPLAY_RATING_SOURCE, int(min_votes)])
    if not include_specials:
        clauses.append("e.season_number <> 0")

    return " AND ".join(f"({clause})" for clause in clauses), tuple(params), int(min_credits)


def role_rating_summary(role: str, where_clause: str, params: tuple, min_credits: int) -> pd.DataFrame:
    return load_frame(
        f"""
        SELECT p.person_id,
               p.name,
               COUNT(DISTINCT e.episode_id) AS rated_episodes,
               ROUND(AVG(er.rating_normalized), 1) AS avg_imdb,
               ROUND(SUM(er.rating_normalized * COALESCE(er.votes, 0)) / NULLIF(SUM(COALESCE(er.votes, 0)), 0), 1) AS vote_weighted_imdb,
               MIN(er.rating_normalized) AS lowest_imdb,
               MAX(er.rating_normalized) AS highest_imdb,
               SUM(er.votes) AS total_votes
        FROM episodes e
        JOIN credits c ON c.episode_id = e.episode_id AND c.role = ?
        JOIN people p ON p.person_id = c.person_id
        JOIN episode_ratings er ON er.episode_id = e.episode_id AND er.source = ?
        WHERE {where_clause}
        GROUP BY p.person_id, p.name
        HAVING rated_episodes >= ?
        ORDER BY avg_imdb DESC, rated_episodes DESC, total_votes DESC, p.name COLLATE NOCASE
        LIMIT 50
        """,
        (role, DISPLAY_RATING_SOURCE, *params, min_credits),
    )


def trends_view(where_clause: str, params: tuple) -> None:
    page_header("Trends", "Analysis", "Explore patterns by season, year, director, writer, and recurring creative teams.")
    trend_where, trend_params, min_credits = trend_filters(where_clause, params)
    by_season = load_frame(f"SELECT e.season_number AS season, COUNT(*) AS episodes FROM episodes e WHERE {trend_where} GROUP BY e.season_number ORDER BY e.season_number", trend_params)
    season_ratings = load_frame(
        f"""
        SELECT e.season_number AS season,
               COUNT(DISTINCT e.episode_id) AS rated_episodes,
               ROUND(AVG(er.rating_normalized), 1) AS avg_imdb,
               ROUND(SUM(er.rating_normalized * COALESCE(er.votes, 0)) / NULLIF(SUM(COALESCE(er.votes, 0)), 0), 1) AS vote_weighted_imdb,
               MIN(er.rating_normalized) AS lowest_imdb,
               MAX(er.rating_normalized) AS highest_imdb,
               SUM(er.votes) AS total_votes
        FROM episodes e
        JOIN episode_ratings er ON er.episode_id = e.episode_id AND er.source = ?
        WHERE {trend_where}
        GROUP BY e.season_number
        ORDER BY e.season_number
        """,
        (DISPLAY_RATING_SOURCE, *trend_params),
    )
    top_directors = load_frame(f"SELECT p.person_id, p.name, COUNT(DISTINCT e.episode_id) AS episodes FROM episodes e JOIN credits c ON c.episode_id = e.episode_id AND c.role = 'Director' JOIN people p ON p.person_id = c.person_id WHERE {trend_where} GROUP BY p.person_id, p.name HAVING episodes >= ? ORDER BY episodes DESC, p.name COLLATE NOCASE LIMIT 50", (*trend_params, min_credits))
    top_writers = load_frame(f"SELECT p.person_id, p.name, COUNT(DISTINCT e.episode_id) AS episodes FROM episodes e JOIN credits c ON c.episode_id = e.episode_id AND c.role = 'Writer' JOIN people p ON p.person_id = c.person_id WHERE {trend_where} GROUP BY p.person_id, p.name HAVING episodes >= ? ORDER BY episodes DESC, p.name COLLATE NOCASE LIMIT 50", (*trend_params, min_credits))
    director_ratings = role_rating_summary("Director", trend_where, trend_params, min_credits)
    writer_ratings = role_rating_summary("Writer", trend_where, trend_params, min_credits)
    overview_tab, directors_tab, writers_tab = st.tabs(["Overview", "Directors", "Writers"])
    with overview_tab:
        st.subheader("Average IMDb by Season")
        if season_ratings.empty:
            st.caption("No IMDb ratings found for the current filters.")
        else:
            st.line_chart(season_ratings, x="season", y="avg_imdb", width="stretch")
            st.dataframe(season_ratings, hide_index=True, width="stretch")
        st.subheader("Episodes by Season")
        st.bar_chart(by_season, x="season", y="episodes", width="stretch")
    with directors_tab:
        st.subheader("Average IMDb by Director")
        if director_ratings.empty:
            st.caption("No rated directed episodes found for the current filters.")
        else:
            st.bar_chart(director_ratings.head(25), x="name", y="avg_imdb", width="stretch")
            st.dataframe(director_ratings[["name", "rated_episodes", "avg_imdb", "vote_weighted_imdb", "lowest_imdb", "highest_imdb", "total_votes"]], hide_index=True, width="stretch")
        st.subheader("Episodes by Director")
        st.bar_chart(top_directors, x="name", y="episodes", width="stretch")
        person_button_rows(top_directors, "trend-director", "episodes")
        st.subheader("Highest-Rated Directed Episodes")
        directed = top_rated_role_episodes("Director", trend_where, trend_params)
        if directed.empty:
            st.caption("No rated directed episodes found for the current filters.")
        else:
            directed_display = directed.copy()
            directed_display.insert(0, "episode", directed_display.apply(lambda item: episode_code(item["season_number"], item["episode_number"]), axis=1))
            st.dataframe(directed_display[["name", "episode", "title", "rating_label", "rating_normalized", "votes", "content_rating"]], hide_index=True, width="stretch")
    with writers_tab:
        st.subheader("Average IMDb by Writer")
        if writer_ratings.empty:
            st.caption("No rated written episodes found for the current filters.")
        else:
            st.bar_chart(writer_ratings.head(25), x="name", y="avg_imdb", width="stretch")
            st.dataframe(writer_ratings[["name", "rated_episodes", "avg_imdb", "vote_weighted_imdb", "lowest_imdb", "highest_imdb", "total_votes"]], hide_index=True, width="stretch")
        st.subheader("Episodes by Writer")
        st.bar_chart(top_writers, x="name", y="episodes", width="stretch")
        person_button_rows(top_writers, "trend-writer", "episodes")
        st.subheader("Highest-Rated Written Episodes")
        written = top_rated_role_episodes("Writer", trend_where, trend_params)
        if written.empty:
            st.caption("No rated written episodes found for the current filters.")
        else:
            written_display = written.copy()
            written_display.insert(0, "episode", written_display.apply(lambda item: episode_code(item["season_number"], item["episode_number"]), axis=1))
            st.dataframe(written_display[["name", "episode", "title", "rating_label", "rating_normalized", "votes", "content_rating"]], hide_index=True, width="stretch")


def teams_dashboard() -> None:
    page_header("Frequent Creative Teams", "Teams", "Compare recurring partnerships across directors, writers, actors, and guest stars.")
    team_type = st.radio("Team type", ["Director + Writer", "Director + Actor", "Writer + Actor", "Actor + Guest Star"], horizontal=True)
    result_limit = int(st.slider("Results", 10, 75, 25, 5))
    role_pairs = {"Director + Writer": ("Director", "Writer"), "Director + Actor": ("Director", "Actor"), "Writer + Actor": ("Writer", "Actor"), "Actor + Guest Star": ("Actor", "Guest Star")}
    first_role, second_role = role_pairs[team_type]
    teams = load_frame(
        """
        SELECT first.person_id AS first_id, first.name AS first_name, second.person_id AS second_id, second.name AS second_name,
               COUNT(DISTINCT c1.episode_id) AS episodes_together, MAX(e.year) AS latest_year
        FROM credits c1
        JOIN credits c2 ON c2.episode_id = c1.episode_id AND c2.person_id <> c1.person_id
        JOIN episodes e ON e.episode_id = c1.episode_id
        JOIN people first ON first.person_id = c1.person_id
        JOIN people second ON second.person_id = c2.person_id
        WHERE c1.role = ? AND c2.role = ?
        GROUP BY first.person_id, first.name, second.person_id, second.name
        ORDER BY episodes_together DESC, latest_year DESC, first.name COLLATE NOCASE, second.name COLLATE NOCASE
        LIMIT ?
        """,
        (first_role, second_role, result_limit),
    )
    if teams.empty:
        st.caption("No frequent teams found for this selection.")
        return
    for row in teams.itertuples(index=False):
        cols = st.columns([3, 3, 1, 1])
        cols[0].button(row.first_name, key=f"team-first-{team_type}-{row.first_id}-{row.second_id}", on_click=open_person, args=(int(row.first_id),), width="stretch")
        cols[1].button(row.second_name, key=f"team-second-{team_type}-{row.first_id}-{row.second_id}", on_click=open_person, args=(int(row.second_id),), width="stretch")
        cols[2].metric("Together", f"{row.episodes_together:,.0f}")
        cols[3].metric("Latest", "Unknown" if pd.isna(row.latest_year) else f"{int(row.latest_year)}")


def ratings_view(where_clause: str, params: tuple) -> None:
    page_header("Ratings", "Compare", "Find the highest-rated episodes in the IMDb data.")
    coverage = load_frame(
        f"""
        SELECT COUNT(DISTINCT e.episode_id) AS episodes
        FROM episodes e
        WHERE {where_clause}
        """,
        params,
    ).iloc[0]
    st.metric("Episodes", f"{coverage.episodes:,.0f}")
    local_where = where_clause
    local_params = [*params, DISPLAY_RATING_SOURCE]
    ratings = load_frame(
        f"""
        SELECT e.episode_id, e.season_number, e.episode_number, e.title, e.aired,
               er.rating_label, er.rating_normalized, er.votes
        FROM episodes e
        JOIN episode_ratings er ON er.episode_id = e.episode_id
        WHERE {local_where} AND er.source = ?
        ORDER BY er.rating_normalized DESC, e.season_number, e.episode_number
        """,
        tuple(local_params),
    )
    if ratings.empty:
        st.caption("No ratings found for the current filters.")
        return
    comparison = ratings[["episode_id", "season_number", "episode_number", "title", "aired", "rating_normalized"]].copy()
    comparison = comparison.rename(columns={"rating_normalized": "IMDb"})
    comparison.insert(0, "episode", comparison.apply(lambda item: episode_code(item["season_number"], item["episode_number"]), axis=1))
    tabs = st.tabs(["Comparison", "Top Episodes", "Raw Ratings"])
    with tabs[0]:
        st.dataframe(comparison.drop(columns=["episode_id", "season_number", "episode_number"]), hide_index=True, width="stretch")
    with tabs[1]:
        top = ratings.dropna(subset=["rating_normalized"]).copy()
        top.insert(0, "episode", top.apply(lambda item: episode_code(item["season_number"], item["episode_number"]), axis=1))
        st.dataframe(top[["episode", "title", "rating_label", "rating_normalized", "votes"]].head(50), hide_index=True, width="stretch")
    with tabs[2]:
        display = ratings.copy()
        display.insert(0, "episode", display.apply(lambda item: episode_code(item["season_number"], item["episode_number"]), axis=1))
        st.dataframe(display, hide_index=True, width="stretch")


def data_quality_view() -> None:
    page_header("Data Quality", "Maintenance", "Find missing writer/director credits, undated episodes, and duplicate people names.")
    summary = load_frame(
        """
        SELECT COUNT(*) AS episodes,
               SUM(CASE WHEN aired IS NULL THEN 1 ELSE 0 END) AS missing_air_date,
               SUM(CASE WHEN overview IS NULL OR TRIM(overview) = '' THEN 1 ELSE 0 END) AS missing_overview,
               SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM credits c WHERE c.episode_id = e.episode_id AND c.role = 'Writer') THEN 1 ELSE 0 END) AS missing_writers,
               SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM credits c WHERE c.episode_id = e.episode_id AND c.role = 'Director') THEN 1 ELSE 0 END) AS missing_directors
        FROM episodes e
        """
    ).iloc[0]
    cols = st.columns(len(summary.index))
    for idx, key in enumerate(summary.index):
        cols[idx].metric(key.replace("_", " ").title(), f"{summary[key]:,.0f}")
    tab_missing, tab_duplicates = st.tabs(["Missing Credits", "Possible Duplicate People"])
    with tab_missing:
        missing = load_frame(
            """
            SELECT e.episode_id, e.season_number, e.episode_number, e.title, e.aired,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM credits c WHERE c.episode_id = e.episode_id AND c.role = 'Writer') THEN 1 ELSE 0 END AS missing_writer,
                   CASE WHEN NOT EXISTS (SELECT 1 FROM credits c WHERE c.episode_id = e.episode_id AND c.role = 'Director') THEN 1 ELSE 0 END AS missing_director,
                   'Missing writer/director' AS creatives
            FROM episodes e
            WHERE NOT EXISTS (SELECT 1 FROM credits c WHERE c.episode_id = e.episode_id AND c.role = 'Writer')
               OR NOT EXISTS (SELECT 1 FROM credits c WHERE c.episode_id = e.episode_id AND c.role = 'Director')
            ORDER BY e.season_number, e.episode_number
            """
        )
        episode_button_rows(missing, "quality-missing")
        with st.expander("Table view"):
            st.dataframe(missing, hide_index=True, width="stretch")
    with tab_duplicates:
        duplicates = load_frame("SELECT LOWER(TRIM(name)) AS normalized_name, COUNT(*) AS rows, GROUP_CONCAT(person_id) AS person_ids FROM people GROUP BY LOWER(TRIM(name)) HAVING COUNT(*) > 1 ORDER BY rows DESC, normalized_name")
        st.dataframe(duplicates, hide_index=True, width="stretch")


def raw_sql_view() -> None:
    st.subheader("Read-only SQL")
    st.caption("Use SELECT queries only. The app blocks writes before running anything.")
    query = st.text_area(
        "Query",
        value=("SELECT e.season_number, e.episode_number, e.title, c.role, p.name\n"
               "FROM episodes e\n"
               "JOIN credits c ON c.episode_id = e.episode_id\n"
               "JOIN people p ON p.person_id = c.person_id\n"
               "WHERE c.role IN ('Writer', 'Director')\n"
               "ORDER BY e.season_number, e.episode_number\n"
               "LIMIT 50"),
        height=180,
    )
    if st.button("Run query", type="primary"):
        normalized = query.strip().lower()
        blocked = ["insert", "update", "delete", "drop", "alter", "create", "replace", "pragma", "attach"]
        if not normalized.startswith("select") or any(f" {word} " in f" {normalized} " for word in blocked):
            st.error("Only read-only SELECT queries are allowed.")
            return
        try:
            st.dataframe(load_frame(query), hide_index=True, width="stretch")
        except Exception as exc:
            st.error(f"Query failed: {exc}")


def main() -> None:
    apply_theme()
    ensure_database()
    st.title("Bob's Burgers Credits")
    st.caption("Explore episodes, people, credits, and creative collaborations from the TVDB export.")
    view = st.session_state.get("view", "explorer")
    if view == "episode" and st.session_state.get("selected_episode_id"):
        episode_detail_page(int(st.session_state.selected_episode_id))
        return
    if view == "person" and st.session_state.get("selected_person_id"):
        person_page(int(st.session_state.selected_person_id))
        return
    page = render_sidebar_nav()
    if page == "Home":
        home_dashboard()
    elif page == "Episodes":
        where_clause, params = sidebar_filters("e")
        episodes_view(where_clause, params)
    elif page == "Ratings":
        where_clause, params = sidebar_filters("e")
        ratings_view(where_clause, params)
    elif page == "Person Search":
        person_search_view()
    elif page == "Trends":
        where_clause, params = sidebar_filters("e")
        trends_view(where_clause, params)
    elif page == "Teams":
        teams_dashboard()
    elif page == "Data Quality":
        data_quality_view()
    elif page == "SQL":
        raw_sql_view()


if __name__ == "__main__":
    main()
