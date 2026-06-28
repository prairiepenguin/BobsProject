from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DATA_PATH = APP_DIR / "data" / "tvdb-194031-bobs-burgers-episodes.json"
DB_PATH = APP_DIR / "bobs_burgers.db"
APP_SCHEMA_VERSION = "2026-06-28.2"
ROLE_ORDER = ("Director", "Writer", "Actor", "Guest Star")
CREATIVE_ROLES = ("Director", "Writer")


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
    episodes = payload.get("episodes", [])
    source_mtime = DATA_PATH.stat().st_mtime
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(
            """
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
                INSERT INTO episodes (episode_id, series_id, season_number, episode_number, absolute_number, title, aired, runtime, year, finale_type, overview, image, production_code, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode.get("id"), episode.get("seriesId"), episode.get("seasonNumber"), episode.get("number"), episode.get("absoluteNumber"),
                    episode.get("name") or "Untitled", episode.get("aired"), episode.get("runtime"),
                    int(episode["year"]) if str(episode.get("year") or "").isdigit() else None,
                    episode.get("finaleType"), episode.get("overview"), episode.get("image"), episode.get("productionCode"), episode.get("lastUpdated"),
                ),
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
        conn.execute("INSERT INTO metadata (key, value) VALUES ('source_mtime', ?)", (str(source_mtime),))
        conn.execute("INSERT INTO metadata (key, value) VALUES ('episode_count', ?)", (str(len(episodes)),))
        conn.execute("INSERT INTO metadata (key, value) VALUES ('app_schema_version', ?)", (APP_SCHEMA_VERSION,))
        conn.executescript(
            """
            CREATE INDEX idx_episodes_season_number ON episodes(season_number, episode_number);
            CREATE INDEX idx_episodes_year_air_title ON episodes(year, aired, title);
            CREATE INDEX idx_credits_role_person_episode ON credits(role, person_id, episode_id);
            CREATE INDEX idx_credits_episode_role_person ON credits(episode_id, role, person_id);
            CREATE INDEX idx_people_name ON people(name COLLATE NOCASE);
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
        schema_row = conn.execute("SELECT value FROM metadata WHERE key = 'app_schema_version'").fetchone()
        conn.close()
    except sqlite3.Error:
        return True
    return (
        not row
        or row[0] != str(DATA_PATH.stat().st_mtime)
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
    nav_options = ["Home", "Episodes", "Person Search", "Trends", "Teams", "Data Quality", "SQL"]
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
        cols[0].button(label, key=f"{key_prefix}-{idx}-{row.episode_id}", on_click=open_episode, args=(int(row.episode_id),), use_container_width=True)
        cols[1].markdown(f'<div class="bb-muted">{row.aired or "Unknown"}</div>', unsafe_allow_html=True)
        cols[2].markdown(f'<div class="bb-muted">{getattr(row, "creatives", None) or "No creative credits"}</div>', unsafe_allow_html=True)


def person_button_rows(frame: pd.DataFrame, key_prefix: str, metric_col: str = "credits") -> None:
    if frame.empty:
        st.caption("No people found.")
        return
    list_header("Person", metric_col.replace("_", " ").title())
    for idx, row in enumerate(frame.itertuples(index=False)):
        cols = st.columns([5, 1])
        cols[0].button(row.name, key=f"{key_prefix}-{idx}-{row.person_id}", on_click=open_person, args=(int(row.person_id),), use_container_width=True)
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
        if st.button("Browse episodes", key="home-go-episodes", use_container_width=True):
            st.session_state.pending_nav = "Episodes"
            st.rerun()
    with prompt_cols[1]:
        st.markdown("**Find people**")
        st.caption("Open writers, directors, actors, and guest stars.")
        if st.button("Search people", key="home-go-people", use_container_width=True):
            st.session_state.pending_nav = "Person Search"
            st.rerun()
    with prompt_cols[2]:
        st.markdown("**Compare teams**")
        st.caption("See director/writer and creative/cast partnerships.")
        if st.button("Explore teams", key="home-go-teams", use_container_width=True):
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
        st.dataframe(episodes, use_container_width=True, hide_index=True)


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
        cols[idx % 3].button(f"{person.name}{role_note}{character_note}", key=f"{key_prefix}-{label}-{idx}-{person.person_id}-{person.role}", on_click=open_person, args=(int(person.person_id),), use_container_width=True)


def related_episode_rows(frame: pd.DataFrame, key_prefix: str) -> None:
    if frame.empty:
        st.caption("No related creative-team episodes found.")
        return
    header = st.columns([3, 1, 3, 2])
    for col, label in zip(header, ["Episode", "Aired", "Shared People", "Connection"]):
        col.markdown(f'<div class="bb-list-header">{label}</div>', unsafe_allow_html=True)
    for idx, row in enumerate(frame.itertuples(index=False)):
        cols = st.columns([3, 1, 3, 2])
        cols[0].button(f"{episode_code(row.season_number, row.episode_number)} - {row.title}", key=f"{key_prefix}-{idx}-{row.episode_id}", on_click=open_episode, args=(int(row.episode_id),), use_container_width=True)
        cols[1].markdown(f'<div class="bb-muted">{row.aired or "Unknown"}</div>', unsafe_allow_html=True)
        cols[2].markdown(f'<div class="bb-muted">{row.shared_people or ""}</div>', unsafe_allow_html=True)
        cols[3].markdown(f'<div class="bb-muted">{row.connection_types or ""}</div>', unsafe_allow_html=True)


def episode_detail_page(episode_id: int) -> None:
    episode = load_frame("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,))
    if episode.empty:
        st.error("Episode not found.")
        return
    row = episode.iloc[0]
    st.button("Back to explorer", on_click=back_to_explorer)
    page_header(row.title, "Explorer / Episode", "Writers, directors, cast, guest stars, and episodes connected by the same creative team.")
    cols = st.columns(5)
    cols[0].metric("Episode", episode_code(row.season_number, row.episode_number))
    cols[1].metric("Air Date", row.aired or "Unknown")
    cols[2].metric("Runtime", "Unknown" if pd.isna(row.runtime) else f"{int(row.runtime)} min")
    cols[3].metric("Year", "Unknown" if pd.isna(row.year) else f"{int(row.year)}")
    cols[4].metric("TVDB ID", f"{int(row.episode_id)}")
    if row.overview:
        st.write(row.overview)
    if row.image:
        st.image(row.image, use_container_width=True)
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
    tab_overview, tab_timeline, tab_collabs, tab_episodes = st.tabs(["Overview", "Career Timeline", "Network", "Episodes"])
    with tab_overview:
        left, right = st.columns([1, 2])
        with left:
            st.subheader("Role Mix")
            st.dataframe(role_summary, hide_index=True, use_container_width=True)
        with right:
            st.subheader("Top Collaborators")
            person_button_rows(get_person_collaborators(person_id).head(25), f"collabs-{person_id}", "episodes_together")
    with tab_timeline:
        st.subheader("Credit Timeline")
        if timeline.empty:
            st.caption("No dated credits found.")
        else:
            st.bar_chart(timeline, x="year", y="credits", color="role", use_container_width=True)
            st.dataframe(timeline, hide_index=True, use_container_width=True)
    with tab_collabs:
        st.subheader("Collaboration Network")
        person_button_rows(get_person_collaborators(person_id), f"network-{person_id}", "episodes_together")
    with tab_episodes:
        st.subheader("Episode Credits")
        display = credits.copy()
        display.insert(0, "episode", display.apply(lambda item: episode_code(item["season_number"], item["episode_number"]), axis=1))
        st.dataframe(display, hide_index=True, use_container_width=True)


def person_page(person_id: int) -> None:
    st.button("Back to explorer", on_click=back_to_explorer)
    person_profile(person_id)


def trends_view(where_clause: str, params: tuple) -> None:
    page_header("Trends", "Analysis", "Explore patterns by season, year, director, writer, and recurring creative teams.")
    by_season = load_frame(f"SELECT e.season_number AS season, COUNT(*) AS episodes FROM episodes e WHERE {where_clause} GROUP BY e.season_number ORDER BY e.season_number", params)
    top_directors = load_frame(f"SELECT p.person_id, p.name, COUNT(DISTINCT e.episode_id) AS episodes FROM episodes e JOIN credits c ON c.episode_id = e.episode_id AND c.role = 'Director' JOIN people p ON p.person_id = c.person_id WHERE {where_clause} GROUP BY p.person_id, p.name ORDER BY episodes DESC, p.name COLLATE NOCASE LIMIT 50", params)
    top_writers = load_frame(f"SELECT p.person_id, p.name, COUNT(DISTINCT e.episode_id) AS episodes FROM episodes e JOIN credits c ON c.episode_id = e.episode_id AND c.role = 'Writer' JOIN people p ON p.person_id = c.person_id WHERE {where_clause} GROUP BY p.person_id, p.name ORDER BY episodes DESC, p.name COLLATE NOCASE LIMIT 50", params)
    overview_tab, directors_tab, writers_tab = st.tabs(["Overview", "Directors", "Writers"])
    with overview_tab:
        st.subheader("Episodes by Season")
        st.bar_chart(by_season, x="season", y="episodes", use_container_width=True)
    with directors_tab:
        st.subheader("Episodes by Director")
        st.bar_chart(top_directors, x="name", y="episodes", use_container_width=True)
        person_button_rows(top_directors, "trend-director", "episodes")
    with writers_tab:
        st.subheader("Episodes by Writer")
        st.bar_chart(top_writers, x="name", y="episodes", use_container_width=True)
        person_button_rows(top_writers, "trend-writer", "episodes")


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
        cols[0].button(row.first_name, key=f"team-first-{team_type}-{row.first_id}-{row.second_id}", on_click=open_person, args=(int(row.first_id),), use_container_width=True)
        cols[1].button(row.second_name, key=f"team-second-{team_type}-{row.first_id}-{row.second_id}", on_click=open_person, args=(int(row.second_id),), use_container_width=True)
        cols[2].metric("Together", f"{row.episodes_together:,.0f}")
        cols[3].metric("Latest", "Unknown" if pd.isna(row.latest_year) else f"{int(row.latest_year)}")


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
            st.dataframe(missing, hide_index=True, use_container_width=True)
    with tab_duplicates:
        duplicates = load_frame("SELECT LOWER(TRIM(name)) AS normalized_name, COUNT(*) AS rows, GROUP_CONCAT(person_id) AS person_ids FROM people GROUP BY LOWER(TRIM(name)) HAVING COUNT(*) > 1 ORDER BY rows DESC, normalized_name")
        st.dataframe(duplicates, hide_index=True, use_container_width=True)


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
            st.dataframe(load_frame(query), hide_index=True, use_container_width=True)
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
