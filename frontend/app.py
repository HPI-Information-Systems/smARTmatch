import json
import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, abort
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DataError
from sqlalchemy.orm import joinedload, sessionmaker

from scrapers.models_production import (
    AuctionArtwork,
    AuctionArtworkImageFile,
    ImageFile,
    LostArtwork,
    LostArtworkImageFile,
    MatchScore,
)

from .search import clean_search_value as _clean_search_value
from .sql import matches as match_sql
from .stats import get_dashboard_stats  # noqa: F401

app = Flask(__name__)


def _env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": _env_int("POSTGRES_PORT", 5433),
    "db_name": os.getenv("POSTGRES_DB", "smartmatch"),
    "user": os.getenv("POSTGRES_USER", "smartmatch"),
    "password": os.getenv("POSTGRES_PASSWORD", "smartmatch"),
}


def _database_url():
    return URL.create(
        "postgresql+psycopg",
        username=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        database=DB_CONFIG["db_name"],
    )


DB_URL = _database_url()

engine = create_engine(DB_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
session = Session()

DEFAULT_IMAGE_WEIGHT = 50

SOURCE_FILTER_ALL = "all"


@dataclass
class CombinedMatchView:
    match_id: object
    lost_artwork_id: object
    auction_artwork_id: object
    lost_artwork: object
    auction_artwork: object
    final_score: float | None
    image_similarity: float | None
    metadata_similarity: float | None
    rating: int | None
    bookmarked: bool | None
    match_date: object
    visualization: dict
    best_image_file_id: int | None = None
    match_scores: object | None = None


@dataclass(frozen=True)
class MatchPage:
    matches: list
    total_matches: int
    page: int
    per_page: int
    max_page: int


def _score_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score_percent(value):
    score = _score_float(value)
    if score is None:
        return "--"
    return f"{score * 100:.2f}%".replace(".", ",")


def score_percent_short(value):
    score = _score_float(value)
    if score is None:
        return "--"
    return f"{score * 100:.0f}%".replace(".", ",")


def score_width(value):
    score = _score_float(value)
    if score is None:
        return "0"
    return f"{max(0.0, min(score * 100, 100.0)):.2f}"


def _weighted_score_width(score, weight_share):
    score_value = _score_float(score)
    if score_value is None:
        return "0"
    clamped_score = max(0.0, min(score_value, 1.0))
    clamped_share = max(0.0, min(weight_share, 1.0))
    return f"{clamped_score * clamped_share * 100:.2f}"


def combined_score_segments(image_similarity, metadata_similarity, image_weight):
    image_share = normalize_image_weight(image_weight) / 100
    return {
        "image": _weighted_score_width(image_similarity, image_share),
        "metadata": _weighted_score_width(metadata_similarity, 1 - image_share),
    }


def _format_dimension_number(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _clean_dimension_text(value):
    text = str(value or "").strip()
    return text or None


def dimensions_label(artwork):
    if artwork is None:
        return None

    width = _format_dimension_number(getattr(artwork, "width", None))
    height = _format_dimension_number(getattr(artwork, "height", None))
    if width is not None and height is not None:
        return f"{width} x {height} cm"

    raw_dimensions = _clean_dimension_text(
        getattr(artwork, "dimensions_raw_data", None)
    )
    if raw_dimensions:
        return raw_dimensions

    frame_width = _format_dimension_number(getattr(artwork, "width_frame", None))
    frame_height = _format_dimension_number(getattr(artwork, "height_frame", None))
    if frame_width is not None and frame_height is not None:
        return f"Rahmen: {frame_width} x {frame_height} cm"

    diameter = _clean_dimension_text(getattr(artwork, "diameter", None))
    if diameter:
        return f"Ø {diameter}"

    depth = _clean_dimension_text(getattr(artwork, "depth", None))
    if depth:
        return f"Tiefe: {depth}"

    return None


def auction_artist_label(auction_artwork):
    if auction_artwork is None:
        return None

    artist = getattr(auction_artwork, "artist", None)
    candidates = (
        getattr(auction_artwork, "artist_full_name", None),
        getattr(artist, "complete_name", None),
        getattr(auction_artwork, "artist_raw_data", None),
    )
    for value in candidates:
        label = str(value or "").strip()
        if label:
            return label
    return None


def normalize_image_weight(value):
    try:
        weight = int(value)
    except (TypeError, ValueError):
        weight = DEFAULT_IMAGE_WEIGHT
    return max(0, min(weight, 100))


def metadata_weight(image_weight):
    return 100 - normalize_image_weight(image_weight)


def normalize_source_filter(value):
    normalized = str(value or SOURCE_FILTER_ALL).strip()
    return normalized[:255] or SOURCE_FILTER_ALL


app.jinja_env.globals.update(
    score_percent=score_percent,
    score_percent_short=score_percent_short,
    score_width=score_width,
    combined_score_segments=combined_score_segments,
    dimensions_label=dimensions_label,
    auction_artist_label=auction_artist_label,
    metadata_weight=metadata_weight,
)


def _match_query_params(
    rating="all",
    bookmarked="all",
    search=None,
    source_filter=SOURCE_FILTER_ALL,
    image_weight=DEFAULT_IMAGE_WEIGHT,
    page=1,
    per_page=20,
):
    clean_search = _clean_search_value(search)
    page_number = _positive_int(page, 1)
    page_size = _positive_int(per_page, 20)
    return {
        "rating": rating,
        "bookmarked": bookmarked,
        "search_like": f"%{clean_search}%",
        "apply_filters": not bool(clean_search),
        "source_filter": normalize_source_filter(source_filter),
        "image_weight": normalize_image_weight(image_weight),
        "limit": page_size,
        "offset": (page_number - 1) * page_size,
    }


def _positive_int(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _max_page(total_matches, per_page):
    if total_matches <= 0:
        return 0
    return total_matches // per_page + (total_matches % per_page > 0)


def _json_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _image_file_items(image_file_ids, image_paths):
    items = []
    seen = set()
    for image_file_id, file_path in zip(image_file_ids or [], image_paths or []):
        path = str(file_path or "").strip()
        if image_file_id is None or image_file_id in seen or not path:
            continue
        seen.add(image_file_id)
        items.append(SimpleNamespace(image_file_id=int(image_file_id), file_path=path))
    return items


def _prioritized_image_files(image_files, primary_image_file_id):
    if primary_image_file_id is None:
        return list(image_files or [])
    try:
        primary_id = int(primary_image_file_id)
    except (TypeError, ValueError):
        return list(image_files or [])
    return sorted(
        list(image_files or []),
        key=lambda item: (getattr(item, "image_file_id", None) != primary_id),
    )


def _page_row_artwork(row, prefix):
    image_paths = list(getattr(row, f"{prefix}_image_paths", None) or [])
    image_file_ids = list(getattr(row, f"{prefix}_image_file_ids", None) or [])
    image_files = _image_file_items(image_file_ids, image_paths)

    if prefix == "lost":
        artwork = SimpleNamespace(
            lost_artwork_id=row.lost_artwork_id,
            title=row.lost_title,
            artists=[
                SimpleNamespace(complete_name=name)
                for name in (row.lost_artist_names or [])
            ],
            dating=row.lost_dating,
            width=row.lost_width,
            height=row.lost_height,
            width_frame=row.lost_width_frame,
            height_frame=row.lost_height_frame,
            depth=row.lost_depth,
            diameter=row.lost_diameter,
            material=row.lost_material,
            technique=row.lost_technique,
            description=getattr(row, "lost_description", None),
            provenance=getattr(row, "lost_provenance", None),
            lost_art_url=getattr(row, "lost_art_url", None),
            institution_classification=getattr(
                row, "lost_institution_classification", None
            ),
            image_paths=image_paths,
            image_files=image_files,
        )
    else:
        auction_artist_name = getattr(row, "auction_artist_name", None)
        artwork = SimpleNamespace(
            auction_artwork_id=row.auction_artwork_id,
            title=row.auction_title,
            artist_raw_data=row.auction_artist_raw_data,
            artist_full_name=row.auction_artist_full_name,
            artist=(
                SimpleNamespace(complete_name=auction_artist_name)
                if auction_artist_name
                else None
            ),
            dating=row.auction_dating,
            width=row.auction_width,
            height=row.auction_height,
            width_frame=row.auction_width_frame,
            height_frame=row.auction_height_frame,
            dimensions_raw_data=row.auction_dimensions_raw_data,
            material=row.auction_material,
            technique=row.auction_technique,
            description=getattr(row, "auction_description", None),
            provenance=getattr(row, "auction_provenance", None),
            lot_url=getattr(row, "auction_lot_url", None),
            image_paths=image_paths,
            image_files=image_files,
        )

    artwork.material_labels = _display_labels(artwork.material)
    artwork.technique_labels = _display_labels(artwork.technique)
    return artwork


def _page_row_match_scores(row, lost_artwork, auction_artwork):
    metadata_match_id = getattr(row, "metadata_match_id", None)
    if metadata_match_id is None:
        return None
    return SimpleNamespace(
        match_id=metadata_match_id,
        lost_id=row.lost_artwork_id,
        auction_id=row.auction_artwork_id,
        lost=lost_artwork,
        auction=auction_artwork,
        final_score=row.metadata_similarity,
        confidence_score=row.metadata_confidence_score,
        created_at=row.metadata_event_date,
        matching_program=row.metadata_matching_program,
        matching_program_obj=SimpleNamespace(
            name=row.metadata_matching_program_name,
            version=row.metadata_matching_program_version,
        ),
        title_sim=row.title_sim,
        artist_sim=row.artist_sim,
        dating_sim=row.dating_sim,
        dimensions_sim=row.dimensions_sim,
        material_sim=row.material_sim,
        technique_sim=row.technique_sim,
    )


def _page_row_combined_match(row):
    lost_artwork = _page_row_artwork(row, "lost")
    auction_artwork = _page_row_artwork(row, "auction")
    best_image_file_id = getattr(row, "best_image_file_id", None)
    auction_artwork.image_files = _prioritized_image_files(
        auction_artwork.image_files,
        best_image_file_id,
    )
    if auction_artwork.image_files:
        auction_artwork.image_paths = [
            item.file_path for item in auction_artwork.image_files
        ]
    return CombinedMatchView(
        match_id=row.match_id,
        lost_artwork_id=row.lost_artwork_id,
        auction_artwork_id=row.auction_artwork_id,
        lost_artwork=lost_artwork,
        auction_artwork=auction_artwork,
        final_score=row.final_score,
        image_similarity=row.image_similarity,
        metadata_similarity=row.metadata_similarity,
        rating=row.rating,
        bookmarked=row.bookmarked,
        match_date=row.match_date,
        visualization=_json_dict(row.visualization),
        best_image_file_id=best_image_file_id,
        match_scores=_page_row_match_scores(row, lost_artwork, auction_artwork),
    )


def _image_paths_from_links(image_links):
    paths = []
    seen = set()
    for image_link in image_links or []:
        image_file = getattr(image_link, "image_file", None)
        path = str(getattr(image_file, "file_path", "") or "").strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _display_labels(*values):
    labels = []
    seen = set()
    for value in values:
        label = str(value or "").strip()
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def get_matches_page(
    rating="all",
    sort="similarity",
    bookmarked="all",
    search=None,
    source_filter=SOURCE_FILTER_ALL,
    image_weight=DEFAULT_IMAGE_WEIGHT,
    page=1,
    per_page=20,
):
    clean_search = _clean_search_value(search)
    search_active = bool(clean_search)
    page_number = _positive_int(page, 1)
    page_size = _positive_int(per_page, 20)
    params = _match_query_params(
        rating=rating,
        bookmarked=bookmarked,
        search=clean_search,
        source_filter=source_filter,
        image_weight=image_weight,
        page=page_number,
        per_page=page_size,
    )
    total_matches = int(
        session.execute(
            text(match_sql.match_count_sql(search_active)), params
        ).scalar_one()
    )
    max_page = _max_page(total_matches, page_size)
    if total_matches == 0 or params["offset"] >= total_matches:
        return MatchPage([], total_matches, page_number, page_size, max_page)

    rows = session.execute(
        text(match_sql.match_page_sql(sort, search_active)), params
    ).all()
    matches = [_page_row_combined_match(row) for row in rows]
    return MatchPage(matches, total_matches, page_number, page_size, max_page)


def get_match_count(
    rating="all",
    bookmarked="all",
    search=None,
    source_filter=SOURCE_FILTER_ALL,
    image_weight=DEFAULT_IMAGE_WEIGHT,
):
    clean_search = _clean_search_value(search)
    params = _match_query_params(
        rating=rating,
        bookmarked=bookmarked,
        search=clean_search,
        source_filter=source_filter,
        image_weight=image_weight,
    )
    return int(
        session.execute(
            text(match_sql.match_count_sql(bool(clean_search))), params
        ).scalar_one()
    )


def get_new_match_source_filter_options():
    rows = session.execute(
        text(match_sql.LOST_ARTWORK_INSTITUTION_CLASSIFICATIONS_SQL)
    ).all()
    if len(rows) <= 1:
        return []

    return [
        {
            "value": row.institution_classification,
            "label": row.institution_classification,
        }
        for row in rows
        if row.institution_classification is not None
    ]


def get_top_unlabeled_auction_previews(limit=21):
    try:
        preview_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if preview_limit < 1:
        return []

    preview_sql = """
    WITH scored AS (
        SELECT
            ms.lost_id::text || ':' || ms.auction_id::text AS match_id,
            ms.auction_id AS artwork_id,
            aa.title,
            CASE
                WHEN ms.image_final_score IS NULL AND ms.metadata_final_score IS NULL
                    THEN NULL::double precision
                ELSE
                    COALESCE(ms.image_final_score, 0.0)::double precision
                        * (CAST(:image_weight AS double precision) / 100.0)
                    + COALESCE(ms.metadata_final_score, 0.0)::double precision
                        * ((100.0 - CAST(:image_weight AS double precision)) / 100.0)
            END AS score,
            CASE
                WHEN ms.metadata_final_score IS NOT NULL
                     AND (ms.image_final_score IS NOT NULL OR ms.image_matching_confidence IS NOT NULL)
                    THEN GREATEST(ms.metadata_match_date, ms.image_match_date)
                WHEN ms.image_final_score IS NOT NULL OR ms.image_matching_confidence IS NOT NULL
                    THEN ms.image_match_date
                ELSE ms.metadata_match_date
            END AS match_date
        FROM match_score ms
        JOIN auction_artwork aa ON aa.auction_artwork_id = ms.auction_id
        WHERE COALESCE(ms.rating, 0) = 0
          AND COALESCE(ms.bookmarked, false) IS FALSE
          AND EXISTS (
              SELECT 1
              FROM auction_artwork_image_file aif
              JOIN image_file i ON i.image_file_id = aif.image_file_id
              WHERE aif.auction_artwork_id = ms.auction_id
                AND i.file_path IS NOT NULL
          )
    ), ranked AS (
        SELECT
            scored.*,
            row_number() OVER (
                PARTITION BY artwork_id
                ORDER BY
                    (score IS NOT NULL) DESC,
                    score DESC NULLS LAST,
                    match_date DESC NULLS LAST,
                    match_id
            ) AS auction_rank
        FROM scored
    )
    SELECT match_id, artwork_id, title, score
    FROM ranked
    WHERE auction_rank = 1
    ORDER BY
        (score IS NOT NULL) DESC,
        score DESC NULLS LAST,
        match_date DESC NULLS LAST,
        match_id
    LIMIT :limit
    """
    rows = session.execute(
        text(preview_sql),
        {"image_weight": DEFAULT_IMAGE_WEIGHT, "limit": preview_limit},
    ).mappings()
    return [dict(row) for row in rows]


def get_directory_counts():
    row = session.execute(text(match_sql.DIRECTORY_COUNTS_SQL)).one()
    return {
        "all": int(row.all_count or 0),
        "new": int(row.new_count or 0),
        "bookmarked": int(row.bookmarked_count or 0),
        "accepted": int(row.accepted_count or 0),
        "discarded": int(row.discarded_count or 0),
    }


def _uuid_value(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def match_pair_from_values(lost_artwork_id, auction_artwork_id):
    lost_uuid = _uuid_value(lost_artwork_id)
    auction_uuid = _uuid_value(auction_artwork_id)
    if lost_uuid is None or auction_uuid is None:
        return None
    return lost_uuid, auction_uuid


def match_pair_from_id(match_id):
    text_id = str(match_id or "").strip()
    if ":" not in text_id:
        return None
    lost_id, auction_id = text_id.split(":", 1)
    return match_pair_from_values(lost_id, auction_id)


def _match_identifier(value):
    return str(value or "").strip()


def _match_contains_id(match, match_id):
    current_match_id = _match_identifier(match_id)
    return bool(current_match_id) and current_match_id == _match_identifier(
        match.match_id
    )


def get_match_by_id(match_id, image_weight=DEFAULT_IMAGE_WEIGHT):
    pair = match_pair_from_id(match_id)
    if pair is None:
        return None

    lost_artwork_id, auction_artwork_id = pair
    params = _match_query_params(image_weight=image_weight)
    params["current_lost_artwork_id"] = lost_artwork_id
    params["current_auction_artwork_id"] = auction_artwork_id
    rows = session.execute(text(match_sql.match_detail_sql()), params).all()
    if not rows:
        return None
    return _page_row_combined_match(rows[0])


def get_previous_and_next_match(
    match_id,
    rating="all",
    sort="similarity",
    bookmarked="all",
    search=None,
    source_filter=SOURCE_FILTER_ALL,
    image_weight=DEFAULT_IMAGE_WEIGHT,
):
    current_match_id = _match_identifier(match_id)
    if not current_match_id:
        return None, None

    clean_search = _clean_search_value(search)
    params = _match_query_params(
        rating=rating,
        bookmarked=bookmarked,
        search=clean_search,
        source_filter=source_filter,
        image_weight=image_weight,
    )
    params["current_match_id"] = current_match_id
    rows = session.execute(
        text(match_sql.match_neighbors_sql(sort, bool(clean_search))), params
    ).all()

    previous_match, next_match = None, None
    for row in rows:
        match = _page_row_combined_match(row)
        if row.neighbor_position < 0:
            previous_match = match
        else:
            next_match = match
    return previous_match, next_match


def get_next_match_to_label(
    match_id,
    rating="all",
    sort="similarity",
    bookmarked="all",
    search=None,
    source_filter=SOURCE_FILTER_ALL,
    image_weight=DEFAULT_IMAGE_WEIGHT,
):
    previous_match, next_match = get_previous_and_next_match(
        match_id,
        rating=rating,
        sort=sort,
        bookmarked=bookmarked,
        search=search,
        source_filter=source_filter,
        image_weight=image_weight,
    )
    if next_match is not None:
        return next_match

    first_page = get_matches_page(
        rating=rating,
        sort=sort,
        bookmarked=bookmarked,
        search=search,
        source_filter=source_filter,
        image_weight=image_weight,
        page=1,
        per_page=2,
    )
    return next(
        (
            match
            for match in first_page.matches
            if not _match_contains_id(match, match_id)
        ),
        previous_match,
    )


def _existing_file_path(path):
    raw_path = str(path or "").strip()
    if not raw_path:
        abort(404)

    path_obj = Path(raw_path).expanduser()
    image_root = Path(os.getenv("SMARTMATCH_IMAGES_DIR", "db/images")).expanduser()
    if not image_root.is_absolute():
        image_root = Path.cwd() / image_root

    candidates = (
        [path_obj]
        if path_obj.is_absolute()
        else [Path.cwd() / path_obj, image_root / path_obj]
    )
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return str(resolved)
    abort(404)


def get_image_file_path(image_file_id):
    try:
        image_file = (
            session.query(ImageFile)
            .filter(ImageFile.image_file_id == int(image_file_id))
            .one_or_none()
        )
        if image_file is None:
            abort(404)
        return _existing_file_path(image_file.file_path)
    except (DataError, TypeError, ValueError, AttributeError):
        abort(404)


def get_primary_image_path(model, id_column, artwork_id):
    try:
        if model is AuctionArtwork:
            artwork = (
                session.query(AuctionArtwork)
                .options(
                    joinedload(AuctionArtwork.auction_artwork_image_files).joinedload(
                        AuctionArtworkImageFile.image_file
                    )
                )
                .filter(id_column == artwork_id)
                .one_or_none()
            )
            image_links = (
                artwork.auction_artwork_image_files if artwork is not None else []
            )
        elif model is LostArtwork:
            artwork = (
                session.query(LostArtwork)
                .options(
                    joinedload(LostArtwork.lost_artwork_image_files).joinedload(
                        LostArtworkImageFile.image_file
                    )
                )
                .filter(id_column == artwork_id)
                .one_or_none()
            )
            image_links = (
                artwork.lost_artwork_image_files if artwork is not None else []
            )
        else:
            abort(404)

        image_paths = _image_paths_from_links(image_links)
        return _existing_file_path(image_paths[0])
    except (DataError, TypeError, IndexError, AttributeError):
        abort(404)


def get_keypoint_plot_path(match_id):
    try:
        pair = match_pair_from_id(match_id)
        if pair is None:
            abort(404)
        lost_artwork_id, auction_artwork_id = pair
        visualization = (
            session.query(MatchScore.image_visualization)
            .filter(
                MatchScore.lost_id == lost_artwork_id,
                MatchScore.auction_id == auction_artwork_id,
            )
            .scalar()
        )

        primary_plot = None
        if isinstance(visualization, dict):
            primary_plot = visualization.get("keypoint_plot")

        return _existing_file_path(primary_plot)
    except (DataError, TypeError, IndexError, AttributeError):
        abort(404)


def main():
    host = os.getenv("FRONTEND_HOST", "0.0.0.0")
    port = _env_int("FRONTEND_PORT", 80)
    debug = os.getenv("FRONTEND_DEBUG", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    app.run(host=host, port=port, debug=debug)


def _register_routes():
    from .routes import register_routes

    register_routes()


_register_routes()
