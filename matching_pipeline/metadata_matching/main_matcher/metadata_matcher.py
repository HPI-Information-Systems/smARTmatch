"""" 
Main Matcher for metadata matching pipeline. 

This module implements the main matching logic for metadata fields, 
including title, artist, dating, dimensions, material, and technique. 
It calculates similarity scores and confidence scores for each pair of 
lost artwork and auction artwork, and stores the results in the database.
"""

# pyright: reportMissingImports=false
import os
import time
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from matching_pipeline.shared.db import connect as db_connect
from matching_pipeline.metadata_matching.similarity_functions.artist.count_confidence import (
    confidence_function as artist_confidence_function,
)
from matching_pipeline.metadata_matching.similarity_functions.artist.matching_function import (
    similarity_function as artist_similarity_function,
)
from matching_pipeline.metadata_matching.similarity_functions.dating.count_confidence import (
    confidence_function as dating_confidence_function,
)
from matching_pipeline.metadata_matching.similarity_functions.dating.matching_function import (
    similarity_function as dating_similarity_function,
)
from matching_pipeline.metadata_matching.similarity_functions.dimensions.count_confidence import (
    confidence_function as dimensions_confidence_function,
    _score_dimensions,
)
from matching_pipeline.metadata_matching.similarity_functions.dimensions.matching_function import (
    similarity_function as dimensions_similarity_function,
)
from matching_pipeline.metadata_matching.similarity_functions.technique_and_material.count_confidence import (
    confidence_function as technique_material_confidence_function,
)
from matching_pipeline.metadata_matching.similarity_functions.technique_and_material.load_hierarchy import load_hierarchies
from matching_pipeline.metadata_matching.similarity_functions.technique_and_material.matching_function import (
    similarity_function as technique_material_similarity_function,
)
from matching_pipeline.metadata_matching.similarity_functions.title.count_confidence import (
    confidence_function as title_confidence_function,
)
from matching_pipeline.metadata_matching.similarity_functions.title.matching_function import (
    similarity_function as title_similarity_function,
)

logger = logging.getLogger(__name__)

MATCHING_PROGRAM_ID = "1e3297fc-ee74-4498-a4d3-ac98ddb8c70d"
THRESHOLD = 0.7
CONF_THRESHOLD = 0.5

WEIGHTS = {
    "title": 0.35,
    "artist": 0.25,
    "dating": 0.15,
    "dimensions": 0.15,
    "material": 0.05,
    "technique": 0.05,
}

_MAX_WORKERS = 8
_BATCH_SIZE = _MAX_WORKERS * 2

MAX_SIM_STRING_LEN = int(os.environ.get("MAX_SIM_STRING_LEN", 100))

def safe_text(value):
    if value is None:
        return None

    value = str(value)

    if len(value) > MAX_SIM_STRING_LEN:
        return value[:MAX_SIM_STRING_LEN]

    return value

# bulk insert match_score rows into the database, using ON CONFLICT to update existing rows
def bulk_insert(cur, conn, rows):
    if not rows:
        return

    cur.executemany(
        """
        INSERT INTO match_score (
            lost_id,
            auction_id,
            title_sim,
            artist_sim,
            dating_sim,
            dimensions_sim,
            material_sim,
            technique_sim,
            metadata_final_score,
            metadata_confidence_score,
            metadata_matching_program
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (lost_id, auction_id) DO UPDATE SET
            title_sim = EXCLUDED.title_sim,
            artist_sim = EXCLUDED.artist_sim,
            dating_sim = EXCLUDED.dating_sim,
            dimensions_sim = EXCLUDED.dimensions_sim,
            material_sim = EXCLUDED.material_sim,
            technique_sim = EXCLUDED.technique_sim,
            metadata_final_score = EXCLUDED.metadata_final_score,
            metadata_confidence_score = EXCLUDED.metadata_confidence_score,
            metadata_matching_program = EXCLUDED.metadata_matching_program
        """,
        rows,
    )
    conn.commit()

# mark auction_artwork rows as processed to avoid reprocessing them in future runs
def bulk_mark_auction_processed(cur, conn, auction_ids):
    if not auction_ids:
        return

    cur.execute(
        """
        UPDATE auction_artwork
        SET is_metadata_matching_processed = true
        WHERE auction_artwork_id = ANY(%s)
          AND is_metadata_matching_processed = false
        """,
        (auction_ids,),
    )
    conn.commit()

# fetch existing pairs from match_score table to process all image matches, 
# even if metadata_final_score is NULL, as long as image_final_score is not NULL
def fetch_existing_pairs(cur, auction_ids):
    if not auction_ids:
        return set()

    cur.execute(
        """
        SELECT lost_id, auction_id
        FROM match_score
        WHERE metadata_final_score IS NULL
          AND image_final_score IS NOT NULL
          AND auction_id = ANY(%s)
        """,
        (auction_ids,),
    )
    return set(cur.fetchall())

def prepare_lost_row(lost_row):
    (
        lost_id,
        lost_title_raw,
        lost_dict_material_name,
        lost_dict_technique_name,
        lost_width_raw,
        lost_height_raw,
        lost_dating_start_raw,
        lost_dating_end_raw,
        lost_artist_complete_names_raw,
    ) = lost_row

    lost_width = float(lost_width_raw) if lost_width_raw is not None else None
    lost_height = float(lost_height_raw) if lost_height_raw is not None else None

    # kde calculation for lost dimensions confidence
    lost_dim_conf = 0.0
    if lost_width is not None and lost_height is not None:
        lost_dim_conf = _score_dimensions(lost_width, lost_height)

    return {
        "lost_id": lost_id,
        "lost_title": str(lost_title_raw) if lost_title_raw is not None else None,
        "lost_dict_material_name": [str(name) for name in (lost_dict_material_name or [])],
        "lost_dict_technique_name": [str(name) for name in (lost_dict_technique_name or [])],
        "lost_width": lost_width,
        "lost_height": lost_height,
        "lost_dim_conf": lost_dim_conf,
        "lost_dating_start": int(lost_dating_start_raw) if lost_dating_start_raw is not None else None,
        "lost_dating_end": int(lost_dating_end_raw) if lost_dating_end_raw is not None else None,
        "lost_artist_complete_names": [
            str(name) for name in (lost_artist_complete_names_raw or [])
        ],
    }

def prepare_auction_row(auc_row):
    (
        auction_id,
        auction_artist_raw,
        auction_title_raw,
        auction_dict_material_name,
        auction_dict_technique_name,
        auction_width_raw,
        auction_height_raw,
        auction_dating_start_raw,
        auction_dating_end_raw,
    ) = auc_row

    auction_width = float(auction_width_raw) if auction_width_raw is not None else None
    auction_height = float(auction_height_raw) if auction_height_raw is not None else None

    # kde calculation for auction dimensions confidence
    auc_dim_conf = 0.0
    if auction_width is not None and auction_height is not None:
        auc_dim_conf = _score_dimensions(auction_width, auction_height)

    return {
        "auction_id": auction_id,
        "auction_artist": str(auction_artist_raw) if auction_artist_raw is not None else None,
        "auction_title": str(auction_title_raw) if auction_title_raw is not None else None,
        "auction_dict_material_name": [str(name) for name in (auction_dict_material_name or [])][:10],
        "auction_dict_technique_name": [str(name) for name in (auction_dict_technique_name or [])][:10],
        "auction_width": auction_width,
        "auction_height": auction_height,
        "auc_dim_conf": auc_dim_conf,
        "auction_dating_start": int(auction_dating_start_raw) if auction_dating_start_raw is not None else None,
        "auction_dating_end": int(auction_dating_end_raw) if auction_dating_end_raw is not None else None,
    }


def calculate_pair_score(lost, auc):
    lost_id = lost["lost_id"]
    lost_title = safe_text(lost["lost_title"])
    lost_dict_material_name = lost["lost_dict_material_name"]
    lost_dict_technique_name = lost["lost_dict_technique_name"]
    lost_width = lost["lost_width"]
    lost_height = lost["lost_height"]
    lost_dating_start = lost["lost_dating_start"]
    lost_dating_end = lost["lost_dating_end"]
    lost_artist_complete_names = [
        safe_text(name)
        for name in lost["lost_artist_complete_names"]
        if name is not None
    ]

    auction_id = auc["auction_id"]
    auction_artist = safe_text(auc["auction_artist"])
    auction_title = safe_text(auc["auction_title"])
    auction_dict_material_name = auc["auction_dict_material_name"]
    auction_dict_technique_name = auc["auction_dict_technique_name"]
    auction_width = auc["auction_width"]
    auction_height = auc["auction_height"]
    auction_dating_start = auc["auction_dating_start"]
    auction_dating_end = auc["auction_dating_end"]

        # for empty artist and title, we skip the matching process and return a default score of 0.0
    if (lost_title is None and not lost_artist_complete_names) or (auction_artist is None and auction_title is None):
        return defaultdict(lambda: {"sim": 0.0, "conf": 0.0}), 0.0, 0.0, 0

    # store similarity and confidence scores for each component in a dictionary
    scores = defaultdict(lambda: {"sim": 0.0, "conf": 0.0})

    scores["artist"]["sim"] = None
    scores["artist"]["conf"] = artist_confidence_function(lost_artist_complete_names, auction_artist,)

    for artist_name in lost_artist_complete_names:
        artist_score = artist_similarity_function(
            artist_name,
            auction_artist,
        )
        if artist_score is not None or scores["artist"]["sim"] is None:
            if scores["artist"]["sim"] is None or artist_score > scores["artist"]["sim"]:
                scores["artist"]["sim"] = artist_score

    scores["title"]["sim"] = title_similarity_function(
        lost_title,
        auction_title,
    )
    scores["title"]["conf"] = title_confidence_function(
        lost_title,
        auction_title,
    )

    scores["material"]["sim"], scores["technique"]["sim"] = (
        technique_material_similarity_function(
            lost_dict_material_name,
            lost_dict_technique_name,
            auction_dict_material_name,
            auction_dict_technique_name,
        )
    )

    scores["material"]["conf"], scores["technique"]["conf"] = (
        technique_material_confidence_function(
            lost_dict_material_name,
            auction_dict_material_name,
            lost_dict_technique_name,
            auction_dict_technique_name,
        )
    )

    scores["dating"]["sim"] = dating_similarity_function(
        lost_dating_start,
        lost_dating_end,
        auction_dating_start,
        auction_dating_end,
    )
    scores["dating"]["conf"] = dating_confidence_function(
        lost_dating_start,
        lost_dating_end,
        auction_dating_start,
        auction_dating_end,
    )

    scores["dimensions"]["sim"] = dimensions_similarity_function(
        lost_width,
        lost_height,
        auction_width,
        auction_height,
    )
    scores["dimensions"]["conf"] = dimensions_confidence_function(
        lost["lost_dim_conf"], 
        auc["auc_dim_conf"]
    )

    components = [
        "title",
        "artist",
        "dating",
        "dimensions",
        "material",
        "technique",
    ]

    final_score = 0.0
    confidence_score = 0.0
    weights_sum = 0.0

    for key in components:
        partial_sim_score = scores[key]["sim"]
        partial_conf_score = scores[key]["conf"]

        w = WEIGHTS.get(key, 0.0)
        confidence_score += w * partial_conf_score

        if partial_sim_score is not None:
            weights_sum += w
            final_score += w * partial_sim_score

    # Normalize final_score by weights_sum to handle cases where some components may be missing (None)
    final_score = (final_score / weights_sum) if weights_sum > 0 else 0.0

    valid_match = 1

    # filter matches that are worthless based on missing artist/title and low scores
    # only valid matches will be inserted into the database
    if (lost_title is None and lost_artist_complete_names == []) or (auction_artist is None and auction_title is None):
        valid_match = 0

    if final_score < THRESHOLD:
        valid_match = 0

    if confidence_score < CONF_THRESHOLD:
        valid_match = 0

    return scores, final_score, confidence_score, valid_match


def process_one_auction(auc_row, prepared_lost_rows, existing_pairs, auction_number: int, total_auctions: int | None = None,):
    auc = prepare_auction_row(auc_row)
    auction_id = auc["auction_id"]
    auction_progress = (
        f"{auction_number}/{total_auctions}"
        if total_auctions is not None
        else str(auction_number)
    )

    logger.debug(
        f"START auction {auction_progress}, "
        f"auction_id={auction_id}, "
        f"lost_total={len(prepared_lost_rows)}"
    )
    rows_to_insert = []
    skipped_rows = 0
    processed_pairs = 0

    for lost in prepared_lost_rows:
        lost_id = lost["lost_id"]
        processed_pairs += 1
        scores, final_score, confidence_score, valid_match = calculate_pair_score(lost, auc)
        pair_exists = (lost_id, auction_id) in existing_pairs

        if valid_match == 1 or pair_exists:
            rows_to_insert.append(
                (
                    lost_id,
                    auction_id,
                    scores["title"]["sim"],
                    scores["artist"]["sim"],
                    scores["dating"]["sim"],
                    scores["dimensions"]["sim"],
                    scores["material"]["sim"],
                    scores["technique"]["sim"],
                    final_score,
                    confidence_score,
                    MATCHING_PROGRAM_ID,
                )
            )
        else:
            skipped_rows += 1

    logger.debug(
        f"END auction {auction_progress}, "
        f"auction_id={auction_id}, "
        f"processed_pairs={processed_pairs}, "
        f"matches_to_insert={len(rows_to_insert)}, "
        f"skipped={skipped_rows}"
    )
    return auction_id, rows_to_insert, skipped_rows, processed_pairs


def match_metadata():
    conn_lost = None
    conn_auc_read = None
    conn_auc_write = None

    cur_insert_score = None
    cur_lost = None
    cur_auc = None

    lost_counter = 0
    total_pair_counter = 0
    global_start = time.perf_counter()

    try:
        load_hierarchies()

        logger.info("Loaded matching functions successfully.")
        logger.info(f"Using MATCHING_BATCH_SIZE={_BATCH_SIZE}, MATCHING_MAX_WORKERS={_MAX_WORKERS}.")

        logger.info("Connecting to database...")
        conn_lost = db_connect()
        conn_auc_read = db_connect()
        conn_auc_write = db_connect()
        logger.info("Connected to database successfully.")

        cur_program_check = conn_auc_write.cursor()
        logger.info("Checking matching_program_id existence...")
        check_start = time.perf_counter()
        cur_program_check.execute(
            """
            SELECT 1
            FROM matching_program
            WHERE matching_program_id = %s
            LIMIT 1
            """,
            (MATCHING_PROGRAM_ID,),
        )
        program_exists = cur_program_check.fetchone() is not None
        logger.info(
            f"matching_program_id check completed in {time.perf_counter() - check_start:.2f}s"
        )
        cur_program_check.close()

        if not program_exists:
            logger.error(f"matching_program_id '{MATCHING_PROGRAM_ID}' does not exist.")
            raise RuntimeError(
                f"matching_program_id '{MATCHING_PROGRAM_ID}' does not exist in matching_program table."
            )

        logger.info("Loading lost_artwork rows into RAM...")
        lost_query_start = time.perf_counter()
        cur_lost = conn_lost.cursor()
        cur_lost.execute(
            """
            SELECT
                l.lost_artwork_id,
                l.title,
                l.dict_material_name,
                l.dict_technique_name,
                l.width,
                l.height,
                l.dating_start,
                l.dating_end,
                (
                    SELECT array_agg(a.complete_name ORDER BY a.complete_name)
                    FROM artist a
                    WHERE a.artist_id = ANY(l.artist_ids)
                ) AS lost_artist_complete_names
            FROM lost_artwork l
            ORDER BY l.lost_artwork_id
            """
        )
        lost_rows = cur_lost.fetchall()
        prepared_lost_rows = [prepare_lost_row(row) for row in lost_rows]
        lost_ids = [row["lost_id"] for row in prepared_lost_rows]
        lost_counter = len(prepared_lost_rows)

        logger.info(
            f"Loaded and prepared {lost_counter} lost_artwork rows in "
            f"{time.perf_counter() - lost_query_start:.2f}s"
        )

        logger.info("Streaming auction_artwork rows in batches...")
        cur_auc = conn_auc_read.cursor("auction_artwork_stream")
        cur_auc.execute(
            """
            SELECT
                c.auction_artwork_id,
                c.artist_full_name,
                c.title,
                c.dict_material_name,
                c.dict_technique_name,
                c.width,
                c.height,
                c.dating_start,
                c.dating_end
            FROM auction_artwork c
            WHERE c.is_metadata_matching_processed = false
              AND c.is_metadata_extraction_processed = true
              AND c.is_image_matching_processed = true
            ORDER BY (
                    EXISTS (
                        SELECT 1 FROM match_score m
                        WHERE m.auction_id = c.auction_artwork_id
                          AND m.metadata_final_score IS NULL
                          AND m.image_final_score IS NOT NULL
                    )
                ) DESC
            """
        )

        cur_insert_score = conn_auc_write.cursor()

        # use ThreadPoolExecutor to process auction_artwork rows in parallel, with a maximum of _MAX_WORKERS threads.
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="matching-worker",) as executor:
            auction_counter = 0

            while True:
                # load a batch of auction_artwork rows from the database, 
                # and process them in parallel using the ThreadPoolExecutor.
                auc_query_start = time.perf_counter()
                auction_rows = cur_auc.fetchmany(_BATCH_SIZE)
                if not auction_rows:
                    logger.info("No more auction_artwork rows to process.")
                    break

                auction_ids_batch = [row[0] for row in auction_rows]
                logger.info(
                    f"Loaded {len(auction_rows)} auction_artwork rows in "
                    f"{time.perf_counter() - auc_query_start:.2f}s"
                )

                existing_pairs = fetch_existing_pairs(cur_insert_score, auction_ids_batch)

                batch_start = time.perf_counter()
                batch_skipped_rows = 0
                batch_processed_pairs = 0
                processed_auction_ids = []
                rows_buffer = []
                batch_start_number = auction_counter + 1

                futures = []
                for batch_index, auc_row in enumerate(auction_rows, start=batch_start_number):
                    futures.append(
                        executor.submit(
                            process_one_auction,
                            auc_row,
                            prepared_lost_rows,
                            existing_pairs,
                            batch_index,
                            None,
                        )
                    )

                auction_counter += len(auction_rows)

                for future in as_completed(futures):
                    auction_id, rows_to_insert, skipped_rows, processed_pairs = future.result()

                    processed_auction_ids.append(auction_id)
                    batch_skipped_rows += skipped_rows
                    batch_processed_pairs += processed_pairs
                    rows_buffer.extend(rows_to_insert)

                    # insert rows in batches
                    if len(rows_buffer) >= _BATCH_SIZE:
                        insert_rows_start = time.perf_counter()
                        batch_size = len(rows_buffer)
                        bulk_insert(cur_insert_score, conn_auc_write, rows_buffer)
                        logger.info(
                            f"Inserted {batch_size} scores in "
                            f"{time.perf_counter() - insert_rows_start:.2f}s"
                        )
                        rows_buffer.clear()

                if rows_buffer:
                    insert_rows_start = time.perf_counter()
                    batch_size = len(rows_buffer)
                    bulk_insert(cur_insert_score, conn_auc_write, rows_buffer)
                    logger.info(
                        f"Inserted {batch_size} scores in "
                        f"{time.perf_counter() - insert_rows_start:.2f}s"
                    )
                    rows_buffer.clear()

                total_pair_counter += batch_processed_pairs

                logger.info(
                    f"Processed {len(processed_auction_ids)} auction_artwork rows, "
                    f"{batch_processed_pairs} auction-lost pairs, "
                    f"skipped {batch_skipped_rows} rows in "
                    f"{time.perf_counter() - batch_start:.2f}s"
                )

                bulk_mark_auction_processed(
                    cur_insert_score,
                    conn_auc_write,
                    processed_auction_ids,
                )

    except Exception as e:
        logger.exception("An error occurred during metadata matching:")
        raise

    finally:
        cur_insert_score.close() if cur_insert_score is not None else None
        cur_lost.close() if cur_lost is not None else None
        cur_auc.close() if cur_auc is not None else None
        conn_lost.close() if conn_lost is not None else None
        conn_auc_read.close() if conn_auc_read is not None else None
        conn_auc_write.close() if conn_auc_write is not None else None

    logger.info("Finished processing.")
    logger.info(f"Loaded lost artworks: {lost_counter}")
    logger.info(f"Processed auction-lost pairs: {total_pair_counter}")
    elapsed = time.perf_counter() - global_start
    logger.info(f"Total elapsed: {elapsed:.2f}s")

    return {
        "lost_loaded": lost_counter,
        "auction_pairs_processed": total_pair_counter,
        "elapsed_seconds": elapsed,
    }


if __name__ == "__main__":
    match_metadata()