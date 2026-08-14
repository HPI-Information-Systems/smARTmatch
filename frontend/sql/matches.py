"""SQL query builders for match list and review pages.

The production schema stores one row per lost/auction pair in ``match_score``.
The frontend derives a stable route identifier from that composite key using
``<lost_uuid>:<auction_uuid>`` and queries ``match_score`` directly.
"""

_MATCH_DATE_SQL = """
CASE
    WHEN ms.metadata_final_score IS NOT NULL
         AND (ms.image_final_score IS NOT NULL OR ms.image_matching_confidence IS NOT NULL)
        THEN GREATEST(ms.metadata_match_date, ms.image_match_date)
    WHEN ms.image_final_score IS NOT NULL OR ms.image_matching_confidence IS NOT NULL
        THEN ms.image_match_date
    ELSE ms.metadata_match_date
END
""".strip()

_MATCH_EXPIRATION_INTERVAL_SQL = (
    "CAST(:match_expiration_seconds AS double precision) * INTERVAL '1 second'"
)

_MATCH_GROUPS_CTE_SQL = f"""
WITH match_groups AS NOT MATERIALIZED (
    SELECT
        ms.lost_id::text || ':' || ms.auction_id::text AS match_id,
        ms.lost_id AS lost_artwork_id,
        ms.auction_id AS auction_artwork_id,
        source_lost.institution_classification,
        CASE
            WHEN ms.image_final_score IS NULL AND ms.metadata_final_score IS NULL
                THEN NULL::double precision
            ELSE
                COALESCE(ms.image_final_score, 0.0)::double precision
                    * (CAST(:image_weight AS double precision) / 100.0)
                + COALESCE(ms.metadata_final_score, 0.0)::double precision
                    * ((100.0 - CAST(:image_weight AS double precision)) / 100.0)
        END AS final_score,
        ms.image_final_score::double precision AS image_similarity,
        CASE
            WHEN ms.metadata_final_score IS NULL THEN NULL
            WHEN ms.metadata_confidence_score IS NULL THEN NULL
            ELSE ((ms.metadata_final_score + ms.metadata_confidence_score) / 2.0)::double precision
        END AS metadata_similarity,
        COALESCE(ms.rating, 0)::smallint AS rating,
        COALESCE(ms.bookmarked, false) AS bookmarked,
        {_MATCH_DATE_SQL} AS match_date,
        COALESCE(ms.image_visualization, '{{}}'::jsonb) AS visualization,
        ms.best_image_file_id AS best_image_file_id,
        ms.lost_id::text || ':' || ms.auction_id::text AS metadata_match_id,
        ms.metadata_confidence_score::double precision AS metadata_confidence_score,
        ms.metadata_match_date AS metadata_event_date,
        ms.metadata_matching_program AS metadata_matching_program,
        metadata_mp.name AS metadata_matching_program_name,
        metadata_mp.version AS metadata_matching_program_version,
        ms.title_sim::double precision AS title_sim,
        ms.artist_sim::double precision AS artist_sim,
        ms.dating_sim::double precision AS dating_sim,
        ms.dimensions_sim::double precision AS dimensions_sim,
        ms.material_sim::double precision AS material_sim,
        ms.technique_sim::double precision AS technique_sim
    FROM match_score ms
    JOIN lost_artwork source_lost
      ON source_lost.lost_artwork_id = ms.lost_id
    LEFT JOIN matching_program metadata_mp
      ON metadata_mp.matching_program_id = ms.metadata_matching_program
)
"""

DIRECTORY_COUNTS_SQL = f"""
SELECT
    count(*) AS all_count,
    count(*) FILTER (
        WHERE COALESCE(ms.rating, 0) = 0 AND COALESCE(ms.bookmarked, false) IS FALSE
    ) AS new_count,
    count(*) FILTER (WHERE COALESCE(ms.bookmarked, false) IS TRUE) AS bookmarked_count,
    count(*) FILTER (WHERE COALESCE(ms.rating, 0) > 0) AS accepted_count,
    count(*) FILTER (
        WHERE ({_MATCH_DATE_SQL})
            < statement_timestamp() - {_MATCH_EXPIRATION_INTERVAL_SQL}
    ) AS expired_count,
    count(*) FILTER (WHERE COALESCE(ms.rating, 0) < 0) AS discarded_count
FROM match_score ms
"""

LOST_ARTWORK_INSTITUTION_CLASSIFICATIONS_SQL = """
SELECT institution_classification
FROM lost_artwork
GROUP BY institution_classification
ORDER BY institution_classification NULLS FIRST
"""

_MATCH_SEARCH_FILTER_CTE_SQL = """
search_filtered_groups AS (
    SELECT mg.*
    FROM match_groups mg
    WHERE EXISTS (
        SELECT 1
        FROM lost_artwork l
        JOIN auction_artwork aa ON aa.auction_artwork_id = mg.auction_artwork_id
        LEFT JOIN artist auction_artist ON auction_artist.artist_id = aa.artist_id
        LEFT JOIN LATERAL (
            SELECT array_agg(a.complete_name::text ORDER BY a.complete_name) AS artist_names
            FROM artist a
            WHERE a.artist_id = ANY(l.artist_ids)
        ) lost_artists ON true
        WHERE l.lost_artwork_id = mg.lost_artwork_id
          AND concat_ws(
            ' ',
            l.title,
            l.dating,
            l.description,
            l.inventory_number,
            l.material,
            l.technique,
            l.provenance,
            l.circumstances_of_loss,
            l.lost_art_id,
            l.lost_art_url,
            l.keywords::text,
            lost_artists.artist_names::text,
            aa.title,
            aa.artist_full_name,
            auction_artist.complete_name,
            aa.artist_raw_data,
            aa.dating,
            aa.description,
            aa.auction_details::text,
            aa.material,
            aa.technique,
            aa.provenance,
            aa.lot_id,
            aa.lot_url,
            aa.condition,
            aa.signature,
            aa.literature,
            aa.raw_data::text,
            aa.dimensions_raw_data
        ) ILIKE CAST(:search_like AS text)
    )
)
"""

_MATCH_GROUP_FILTER_SQL = f"""
(
    CAST(:rating AS text) IS NULL
    OR CAST(:rating AS text) NOT IN ('unrated', 'accepted', 'discarded', 'expired')
    OR (CAST(:rating AS text) = 'unrated' AND rating = 0)
    OR (CAST(:rating AS text) = 'accepted' AND rating > 0)
    OR (CAST(:rating AS text) = 'discarded' AND rating < 0)
    OR (
        CAST(:rating AS text) = 'expired'
        AND match_date < statement_timestamp() - {_MATCH_EXPIRATION_INTERVAL_SQL}
    )
)
AND (
    CAST(:rating AS text) = 'expired'
    OR CAST(:bookmarked AS text) IS NULL
    OR CAST(:bookmarked AS text) NOT IN ('true', 'false')
    OR (CAST(:bookmarked AS text) = 'true' AND bookmarked IS TRUE)
    OR (CAST(:bookmarked AS text) = 'false' AND bookmarked IS FALSE)
)
AND (
    CAST(:source_filter AS text) IS NULL
    OR CAST(:source_filter AS text) = 'all'
    OR institution_classification = CAST(:source_filter AS text)
)
"""

_MATCH_DETAILS_SELECT_SQL = """
SELECT
    pg.neighbor_position,
    pg.match_id,
    pg.lost_artwork_id,
    pg.auction_artwork_id,
    pg.final_score,
    pg.image_similarity,
    pg.metadata_similarity,
    pg.rating,
    pg.bookmarked,
    pg.match_date,
    pg.visualization,
    pg.best_image_file_id,
    pg.metadata_match_id,
    pg.metadata_confidence_score,
    pg.metadata_event_date,
    pg.metadata_matching_program,
    pg.metadata_matching_program_name,
    pg.metadata_matching_program_version,
    pg.title_sim,
    pg.artist_sim,
    pg.dating_sim,
    pg.dimensions_sim,
    pg.material_sim,
    pg.technique_sim,
    l.title AS lost_title,
    l.dating AS lost_dating,
    l.width AS lost_width,
    l.height AS lost_height,
    l.width_frame AS lost_width_frame,
    l.height_frame AS lost_height_frame,
    l.depth AS lost_depth,
    l.diameter AS lost_diameter,
    l.material AS lost_material,
    l.technique AS lost_technique,
    l.description AS lost_description,
    l.provenance AS lost_provenance,
    l.lost_art_url AS lost_art_url,
    l.institution_classification AS lost_institution_classification,
    COALESCE(lost_artists.artist_names, ARRAY[]::text[]) AS lost_artist_names,
    COALESCE(lost_images.image_file_ids, ARRAY[]::integer[]) AS lost_image_file_ids,
    COALESCE(lost_images.image_paths, ARRAY[]::text[]) AS lost_image_paths,
    aa.title AS auction_title,
    aa.artist_raw_data AS auction_artist_raw_data,
    aa.artist_full_name AS auction_artist_full_name,
    auction_artist.complete_name AS auction_artist_name,
    aa.dating AS auction_dating,
    aa.width AS auction_width,
    aa.height AS auction_height,
    aa.width_frame AS auction_width_frame,
    aa.height_frame AS auction_height_frame,
    aa.dimensions_raw_data AS auction_dimensions_raw_data,
    aa.material AS auction_material,
    aa.technique AS auction_technique,
    aa.description AS auction_description,
    aa.provenance AS auction_provenance,
    aa.lot_url AS auction_lot_url,
    COALESCE(auction_images.image_file_ids, ARRAY[]::integer[]) AS auction_image_file_ids,
    COALESCE(auction_images.image_paths, ARRAY[]::text[]) AS auction_image_paths
FROM paged_groups pg
JOIN lost_artwork l ON l.lost_artwork_id = pg.lost_artwork_id
JOIN auction_artwork aa ON aa.auction_artwork_id = pg.auction_artwork_id
LEFT JOIN artist auction_artist ON auction_artist.artist_id = aa.artist_id
LEFT JOIN LATERAL (
    SELECT array_agg(a.complete_name::text ORDER BY a.complete_name) AS artist_names
    FROM artist a
    WHERE a.artist_id = ANY(l.artist_ids)
) lost_artists ON true
LEFT JOIN LATERAL (
    SELECT
        array_agg(i.image_file_id ORDER BY i.file_path) AS image_file_ids,
        array_agg(i.file_path ORDER BY i.file_path) AS image_paths
    FROM lost_artwork_image_file lif
    JOIN image_file i ON i.image_file_id = lif.image_file_id
    WHERE lif.lost_artwork_id = l.lost_artwork_id
) lost_images ON true
LEFT JOIN LATERAL (
    SELECT
        array_agg(i.image_file_id ORDER BY i.file_path) AS image_file_ids,
        array_agg(i.file_path ORDER BY i.file_path) AS image_paths
    FROM auction_artwork_image_file aif
    JOIN image_file i ON i.image_file_id = aif.image_file_id
    WHERE aif.auction_artwork_id = aa.auction_artwork_id
) auction_images ON true
"""


def filtered_match_groups_cte_sql(search_active):
    source_table = "search_filtered_groups" if search_active else "match_groups"
    return f"""
filtered_groups AS (
    SELECT *
    FROM {source_table}
    WHERE (
        CAST(:apply_filters AS boolean) = false
        OR ({_MATCH_GROUP_FILTER_SQL})
    )
)
"""


def match_group_ctes_sql(search_active):
    ctes = [_MATCH_GROUPS_CTE_SQL]
    if search_active:
        ctes.append(_MATCH_SEARCH_FILTER_CTE_SQL)
    ctes.append(filtered_match_groups_cte_sql(search_active))
    return ",\n".join(cte.strip() for cte in ctes)


def match_order_sql(sort, prefix=""):
    def column(name):
        return f"{prefix}{name}"

    if sort == "similarity" or not sort:
        return (
            f"({column('final_score')} IS NOT NULL) DESC, "
            f"{column('final_score')} DESC NULLS LAST, "
            f"{column('match_date')} DESC NULLS LAST, "
            f"{column('match_id')}"
        )
    if sort == "oldest":
        return f"{column('match_date')} ASC NULLS FIRST, {column('match_id')}"
    return f"{column('match_date')} DESC NULLS LAST, {column('match_id')}"


def match_count_sql(search_active):
    return f"""
{match_group_ctes_sql(search_active)}
SELECT count(*)
FROM filtered_groups
"""


def match_page_sql(sort, search_active):
    order_sql = match_order_sql(sort)
    detail_order_sql = match_order_sql(sort, prefix="pg.")
    return f"""
{match_group_ctes_sql(search_active)},
paged_groups AS (
    SELECT filtered_groups.*, NULL::integer AS neighbor_position
    FROM filtered_groups
    ORDER BY {order_sql}
    LIMIT :limit OFFSET :offset
)
{_MATCH_DETAILS_SELECT_SQL}
ORDER BY {detail_order_sql}
"""


def match_detail_sql():
    return f"""
{_MATCH_GROUPS_CTE_SQL},
paged_groups AS (
    SELECT match_groups.*, NULL::integer AS neighbor_position
    FROM match_groups
    WHERE match_groups.lost_artwork_id = CAST(:current_lost_artwork_id AS uuid)
      AND match_groups.auction_artwork_id = CAST(:current_auction_artwork_id AS uuid)
)
{_MATCH_DETAILS_SELECT_SQL}
"""


def match_neighbors_sql(sort, search_active):
    order_sql = match_order_sql(sort)
    detail_order_sql = "pg.neighbor_position, " + match_order_sql(sort, prefix="pg.")
    return f"""
{match_group_ctes_sql(search_active)},
ordered_groups AS (
    SELECT
        fg.*,
        lag(fg.match_id) OVER ordered_match_groups AS previous_match_id,
        lead(fg.match_id) OVER ordered_match_groups AS next_match_id
    FROM filtered_groups fg
    WINDOW ordered_match_groups AS (ORDER BY {order_sql})
),
current_neighbors AS (
    SELECT previous_match_id, next_match_id
    FROM ordered_groups og
    WHERE og.match_id = CAST(:current_match_id AS text)
    ORDER BY {order_sql}
    LIMIT 1
),
paged_groups AS (
    SELECT
        og.*,
        CASE
            WHEN og.match_id = cn.previous_match_id THEN -1
            ELSE 1
        END AS neighbor_position
    FROM ordered_groups og
    CROSS JOIN current_neighbors cn
    WHERE og.match_id IN (cn.previous_match_id, cn.next_match_id)
)
{_MATCH_DETAILS_SELECT_SQL}
ORDER BY {detail_order_sql}
"""
