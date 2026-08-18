CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS country (
    country_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    country_name    varchar(255) NOT NULL,
    country_name_en varchar(255)
);

CREATE TABLE IF NOT EXISTS location (
    location_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    location_name    varchar(255) NOT NULL,
    location_name_en varchar(255),
    country          varchar(255),
    country_en       varchar(255),
    raw_data         text,
    lat              real,
    lon              real,
    historical_info  text,
    display_name     text,
    country_ids      uuid[] NOT NULL DEFAULT '{}'::uuid[]
);

CREATE TABLE IF NOT EXISTS location_name_variants (
    name_variant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    location_id     uuid NOT NULL REFERENCES location(location_id) ON DELETE CASCADE,
    name_variant    varchar(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS institution (
    institution_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name           varchar(255) NOT NULL,
    address        varchar(255),
    phone          varchar(50),
    fax            varchar(50),
    website        varchar(255),
    email          varchar(255),
    contact_ids    uuid[] DEFAULT '{}'::uuid[],
    raw_data       text
);

CREATE TABLE IF NOT EXISTS contact (
    contact_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    first_name     varchar(255) NOT NULL,
    last_name      varchar(255) NOT NULL,
    role           varchar(255),
    phone          varchar(50),
    email          varchar(255),
    institution_id uuid REFERENCES institution(institution_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artist (
    artist_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complete_name          varchar(255) NOT NULL,
    date_of_birth          date,
    date_of_death          date,
    place_of_birth         uuid REFERENCES location(location_id) ON DELETE SET NULL,
    place_of_death         uuid REFERENCES location(location_id) ON DELETE SET NULL,
    raw_data               text,
    gender                 varchar(20),
    period_of_activity     varchar(255),
    preferred_name         varchar(255),
    surname                varchar(255),
    title_of_nobility      varchar(255),
    profession             text,
    biographical_info      text,
    associated_country_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
    activity_place_ids     uuid[] NOT NULL DEFAULT '{}'::uuid[]
);

CREATE TABLE IF NOT EXISTS artist_name_variants (
    name_variant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    artist_id       uuid REFERENCES artist(artist_id) ON DELETE CASCADE,
    name_variant    varchar(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS literature_source (
    literature_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title                  text NOT NULL,
    author                 text,
    publishing_date        date,
    publishing_location    text,
    raw_data               text,
    publishing_location_id uuid REFERENCES location(location_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS image_matching_input_state (
    singleton boolean PRIMARY KEY DEFAULT true,
    lost_content_revision bigint NOT NULL DEFAULT 1,
    CONSTRAINT ck_image_matching_input_state_singleton CHECK (singleton),
    CONSTRAINT ck_image_matching_input_state_revision
        CHECK (lost_content_revision > 0)
);

INSERT INTO image_matching_input_state (singleton, lost_content_revision)
VALUES (true, 1)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS image_file (
    image_file_id serial PRIMARY KEY,
    file_path text UNIQUE,
    source_url text,
    content_sha256 varchar(64),
    content_version bigint NOT NULL DEFAULT 1,
    is_embedded boolean NOT NULL DEFAULT false,
    cleaned_up_at timestamptz,
    CONSTRAINT ck_image_file_content_sha256 CHECK (
        content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_image_file_content_version CHECK (content_version > 0),
    CONSTRAINT ck_image_file_cleanup_state CHECK (
        (cleaned_up_at IS NULL AND file_path IS NOT NULL)
        OR (cleaned_up_at IS NOT NULL AND file_path IS NULL)
    )
);

CREATE OR REPLACE FUNCTION version_image_file_content_change()
RETURNS trigger AS $$
BEGIN
    IF NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256 THEN
        NEW.content_version = OLD.content_version + 1;
        NEW.is_embedded = false;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_version_image_file_content_change ON image_file;

CREATE TRIGGER trg_version_image_file_content_change
BEFORE UPDATE OF content_sha256 ON image_file
FOR EACH ROW
EXECUTE FUNCTION version_image_file_content_change();

CREATE TABLE IF NOT EXISTS lost_artwork (
    lost_artwork_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title                  text,
    artist_ids             uuid[] DEFAULT '{}'::uuid[],
    depicted_person_ids    uuid[] NOT NULL DEFAULT '{}'::uuid[],
    width                  real,
    height                 real,
    width_frame            real,
    height_frame           real,
    depth                  text,
    diameter               text,
    dating                 varchar(255),
    dating_start           smallint,
    dating_end             smallint,
    description            text,
    inventory_number       text,
    material               varchar(255),
    dict_material_name     varchar(50)[] DEFAULT '{}'::varchar(50)[],
    technique              varchar(255),
    dict_technique_name    varchar(50)[] DEFAULT '{}'::varchar(50)[],
    provenance             text,
    institution_id         uuid REFERENCES institution(institution_id) ON DELETE SET NULL,
    institution_classification text,
    circumstances_of_loss  text,
    lost_art_id            varchar(255),
    lost_art_url           varchar(255),
    keywords               text[] NOT NULL DEFAULT '{}'::text[],
    literature_source_id   uuid REFERENCES literature_source(literature_id) ON DELETE SET NULL,
    literature_source_page smallint,
    type_of_loss           varchar(255),
    restituted             boolean NOT NULL DEFAULT false,
    raw_data               jsonb,
    location_history       jsonb
);

CREATE TABLE IF NOT EXISTS auction_platform (
    auction_platform_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                varchar(255) NOT NULL,
    address             varchar(255),
    phone               varchar(50),
    email               varchar(255),
    raw_data            text
);

CREATE TABLE IF NOT EXISTS auctioneer (
    auctioneer_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          varchar(255) NOT NULL,
    address       varchar(255),
    phone         varchar(50),
    email         varchar(255),
    raw_data      text
);

CREATE TABLE IF NOT EXISTS expert (
    expert_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    first_name   varchar(255) NOT NULL,
    last_name    varchar(255) NOT NULL,
    organization varchar(255),
    phone        varchar(50),
    email        varchar(255),
    raw_data     text
);

CREATE TABLE IF NOT EXISTS auction_artwork (
    auction_artwork_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at              timestamptz NOT NULL DEFAULT now(),
    normalised_version      integer,
    title                   text,
    artist_id               uuid REFERENCES artist(artist_id) ON DELETE SET NULL,
    artist_full_name        varchar(255),
    artist_birth_date       date,
    artist_death_date       date,
    artist_birth_place      uuid REFERENCES location(location_id) ON DELETE SET NULL,
    artist_death_place      uuid REFERENCES location(location_id) ON DELETE SET NULL,
    width                   real,
    height                  real,
    width_frame             real,
    height_frame            real,
    dating                  varchar(255),
    dating_start            smallint,
    dating_end              smallint,
    description             text,
    auction_details         jsonb,
    material                varchar(255),
    dict_material_name     varchar(50)[] DEFAULT '{}'::varchar(50)[],
    technique               varchar(255),
    dict_technique_name    varchar(50)[] DEFAULT '{}'::varchar(50)[],
    provenance              text,
    auction_date            date,
    lot_id                  varchar(255),
    lot_url                 text,
    auction_platform_id     uuid REFERENCES auction_platform(auction_platform_id) ON DELETE SET NULL,
    auctioneer_id           uuid REFERENCES auctioneer(auctioneer_id) ON DELETE SET NULL,
    expert_id               uuid REFERENCES expert(expert_id) ON DELETE SET NULL,
    condition               text,
    signature               text,
    literature              text,
    raw_data                jsonb,
    artist_raw_data         text,
    date_of_birth_raw_data  text,
    date_of_death_raw_data  text,
    place_of_birth_raw_data text,
    place_of_death_raw_data text,
    dimensions_raw_data     text,
    is_metadata_matching_processed boolean NOT NULL DEFAULT false,
    is_metadata_matching_processed_at timestamptz,
    is_metadata_extraction_processed boolean NOT NULL DEFAULT false,
    is_metadata_extraction_processed_at timestamptz,
    is_image_matching_processed boolean NOT NULL DEFAULT false,
    is_image_matching_processed_at timestamptz
);

CREATE OR REPLACE FUNCTION set_auction_artwork_processed_timestamps()
RETURNS trigger AS $$
BEGIN
    -- Any persisted matcher input change invalidates the previous metadata pass.
    -- The row comparison also catches explicit transitions to NULL made outside
    -- the shared scraper upsert helper.
    IF ROW(
        NEW.title,
        NEW.artist_full_name,
        NEW.width,
        NEW.height,
        NEW.dating_start,
        NEW.dating_end,
        NEW.dict_material_name,
        NEW.dict_technique_name,
        NEW.description
    ) IS DISTINCT FROM ROW(
        OLD.title,
        OLD.artist_full_name,
        OLD.width,
        OLD.height,
        OLD.dating_start,
        OLD.dating_end,
        OLD.dict_material_name,
        OLD.dict_technique_name,
        OLD.description
    ) THEN
        NEW.is_metadata_matching_processed = false;
        NEW.is_metadata_matching_processed_at = NULL;
    END IF;

    IF NEW.description IS DISTINCT FROM OLD.description THEN
        NEW.is_metadata_extraction_processed = false;
        NEW.is_metadata_extraction_processed_at = NULL;
    END IF;

    IF NEW.is_metadata_matching_processed = true
       AND OLD.is_metadata_matching_processed = false THEN
        NEW.is_metadata_matching_processed_at = now();
    END IF;

    IF NEW.is_metadata_extraction_processed = true
       AND OLD.is_metadata_extraction_processed = false THEN
        NEW.is_metadata_extraction_processed_at = now();
    END IF;

    IF NEW.is_image_matching_processed = true
       AND OLD.is_image_matching_processed = false THEN
        NEW.is_image_matching_processed_at = now();
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_auction_artwork_processed_timestamps ON auction_artwork;

CREATE TRIGGER trg_auction_artwork_processed_timestamps
BEFORE UPDATE ON auction_artwork
FOR EACH ROW
EXECUTE FUNCTION set_auction_artwork_processed_timestamps();

CREATE TABLE IF NOT EXISTS auction_artwork_image_file (
    auction_artwork_id uuid NOT NULL REFERENCES auction_artwork(auction_artwork_id) ON DELETE CASCADE,
    image_file_id integer NOT NULL REFERENCES image_file(image_file_id) ON DELETE CASCADE,
    is_image_matching_processed boolean NOT NULL DEFAULT false,
    is_image_matching_completed_without_error boolean NOT NULL DEFAULT false,
    PRIMARY KEY (auction_artwork_id, image_file_id)
);

CREATE OR REPLACE FUNCTION reset_auction_artwork_image_matching_for_pending_link()
RETURNS trigger AS $$
BEGIN
    UPDATE auction_artwork
    SET is_image_matching_processed = false,
        is_image_matching_processed_at = NULL
    WHERE auction_artwork_id = NEW.auction_artwork_id
      AND (
          is_image_matching_processed = true
          OR is_image_matching_processed_at IS NOT NULL
      );

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reset_auction_artwork_image_matching_for_pending_link
ON auction_artwork_image_file;

CREATE TRIGGER trg_reset_auction_artwork_image_matching_for_pending_link
AFTER INSERT ON auction_artwork_image_file
FOR EACH ROW
WHEN (
    NEW.is_image_matching_processed = false
    OR NEW.is_image_matching_completed_without_error = false
)
EXECUTE FUNCTION reset_auction_artwork_image_matching_for_pending_link();

CREATE TABLE IF NOT EXISTS lost_artwork_image_file (
    lost_artwork_id uuid NOT NULL REFERENCES lost_artwork(lost_artwork_id) ON DELETE CASCADE,
    image_file_id integer NOT NULL REFERENCES image_file(image_file_id) ON DELETE CASCADE,
    PRIMARY KEY (lost_artwork_id, image_file_id)
);

CREATE TABLE IF NOT EXISTS matching_program (
    matching_program_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                varchar(50) NOT NULL,
    version             varchar(50) NOT NULL,
    description         text
);

CREATE TABLE IF NOT EXISTS scraper_run (
    run_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scraper_name    varchar(100) NOT NULL,
    status          varchar(20)  NOT NULL DEFAULT 'running',
    started_at      timestamptz  NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    entries_scraped    integer      NOT NULL DEFAULT 0,
    entries_skipped    integer      NOT NULL DEFAULT 0,
    total_entries      integer      NOT NULL DEFAULT 0,
    queue_total        integer      NOT NULL DEFAULT 0,
    queue_processed    integer      NOT NULL DEFAULT 0,
    progress_updated_at timestamptz,
    error_message      text
);


CREATE TABLE IF NOT EXISTS match_score (
    lost_id          uuid NOT NULL REFERENCES lost_artwork(lost_artwork_id) ON DELETE CASCADE,
    auction_id       uuid NOT NULL REFERENCES auction_artwork(auction_artwork_id) ON DELETE CASCADE,
    title_sim        real CHECK (title_sim BETWEEN 0 AND 1),
    artist_sim       real CHECK (artist_sim BETWEEN 0 AND 1),
    dating_sim       real CHECK (dating_sim BETWEEN 0 AND 1),
    dimensions_sim   real CHECK (dimensions_sim BETWEEN 0 AND 1),
    material_sim     real CHECK (material_sim BETWEEN 0 AND 1),
    technique_sim    real CHECK (technique_sim BETWEEN 0 AND 1),
    metadata_final_score      real CHECK (metadata_final_score BETWEEN 0 AND 1),
    metadata_confidence_score   real CHECK (metadata_confidence_score BETWEEN 0 AND 1),
    metadata_match_date       timestamptz DEFAULT now(),
    metadata_matching_program uuid CONSTRAINT fk_match_score_metadata_matching_program REFERENCES matching_program(matching_program_id) ON DELETE CASCADE,
    image_matching_confidence real CHECK (image_matching_confidence BETWEEN 0 AND 1),
    image_final_score        real CHECK (image_final_score BETWEEN -1 AND 1),
    image_blocking_similarity real CHECK (image_blocking_similarity BETWEEN -1 AND 1),
    image_match_date         timestamptz DEFAULT now(),
    image_matching_program   uuid CONSTRAINT fk_match_score_image_matching_program REFERENCES matching_program(matching_program_id) ON DELETE CASCADE,
    image_visualization      jsonb NOT NULL DEFAULT '{}'::jsonb,
    best_image_file_id       integer CONSTRAINT fk_match_score_best_image_file_id REFERENCES image_file(image_file_id) ON DELETE SET NULL,
    rating             smallint,
    bookmarked         boolean,
    PRIMARY KEY (lost_id, auction_id)
);

CREATE OR REPLACE FUNCTION invalidate_all_image_matching_for_lost_change()
RETURNS void AS $$
BEGIN
    -- One global invalidation is sufficient for every lost-image mutation in the
    -- current transaction and avoids repeatedly scanning the score/link tables.
    IF current_setting('smartmatch.lost_invalidation_done', true) = '1' THEN
        RETURN;
    END IF;
    PERFORM set_config('smartmatch.lost_invalidation_done', '1', true);

    UPDATE image_matching_input_state
    SET lost_content_revision = lost_content_revision + 1
    WHERE singleton = true;

    UPDATE match_score
    SET image_matching_confidence = NULL,
        image_final_score = NULL,
        image_blocking_similarity = NULL,
        image_match_date = NULL,
        image_matching_program = NULL,
        image_visualization = '{}'::jsonb,
        best_image_file_id = NULL
    WHERE metadata_final_score IS NOT NULL
      AND (
          image_matching_confidence IS NOT NULL
          OR image_final_score IS NOT NULL
          OR image_blocking_similarity IS NOT NULL
          OR image_match_date IS NOT NULL
          OR image_matching_program IS NOT NULL
          OR image_visualization <> '{}'::jsonb
          OR best_image_file_id IS NOT NULL
      );

    DELETE FROM match_score
    WHERE metadata_final_score IS NULL;

    UPDATE auction_artwork_image_file link
    SET is_image_matching_processed = false,
        is_image_matching_completed_without_error = false
    FROM image_file image
    WHERE image.image_file_id = link.image_file_id
      AND image.cleaned_up_at IS NULL
      AND image.file_path IS NOT NULL
      AND (
          link.is_image_matching_processed = true
          OR link.is_image_matching_completed_without_error = true
      );

    UPDATE auction_artwork artwork
    SET is_image_matching_processed = false,
        is_image_matching_processed_at = NULL
    WHERE EXISTS (
            SELECT 1
            FROM auction_artwork_image_file link
            JOIN image_file image USING (image_file_id)
            WHERE link.auction_artwork_id = artwork.auction_artwork_id
              AND image.cleaned_up_at IS NULL
              AND image.file_path IS NOT NULL
        )
      AND (
          artwork.is_image_matching_processed = true
          OR artwork.is_image_matching_processed_at IS NOT NULL
      );
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION invalidate_image_matching_after_lost_link_change()
RETURNS trigger AS $$
BEGIN
    PERFORM invalidate_all_image_matching_for_lost_change();
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_invalidate_image_matching_after_lost_link_change
ON lost_artwork_image_file;

CREATE TRIGGER trg_invalidate_image_matching_after_lost_link_change
AFTER INSERT OR DELETE OR UPDATE ON lost_artwork_image_file
FOR EACH ROW
EXECUTE FUNCTION invalidate_image_matching_after_lost_link_change();

CREATE OR REPLACE FUNCTION invalidate_image_matching_after_content_change()
RETURNS trigger AS $$
DECLARE
    lost_image_changed boolean;
    affected_auction_ids uuid[];
BEGIN
    IF NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256 THEN
        RETURN NEW;
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM lost_artwork_image_file link
        WHERE link.image_file_id = OLD.image_file_id
    )
    INTO lost_image_changed;

    SELECT COALESCE(
        array_agg(DISTINCT link.auction_artwork_id),
        ARRAY[]::uuid[]
    )
    INTO affected_auction_ids
    FROM auction_artwork_image_file link
    WHERE link.image_file_id = OLD.image_file_id;

    IF lost_image_changed THEN
        PERFORM invalidate_all_image_matching_for_lost_change();
    ELSIF cardinality(affected_auction_ids) > 0 THEN
        -- Scores are artwork-level aggregates. Recompute every live sibling image
        -- for each artwork that linked to the replaced auction image.
        UPDATE match_score
        SET image_matching_confidence = NULL,
            image_final_score = NULL,
            image_blocking_similarity = NULL,
            image_match_date = NULL,
            image_matching_program = NULL,
            image_visualization = '{}'::jsonb,
            best_image_file_id = NULL
        WHERE metadata_final_score IS NOT NULL
          AND auction_id = ANY(affected_auction_ids)
          AND (
              image_matching_confidence IS NOT NULL
              OR image_final_score IS NOT NULL
              OR image_blocking_similarity IS NOT NULL
              OR image_match_date IS NOT NULL
              OR image_matching_program IS NOT NULL
              OR image_visualization <> '{}'::jsonb
              OR best_image_file_id IS NOT NULL
          );

        DELETE FROM match_score
        WHERE metadata_final_score IS NULL
          AND auction_id = ANY(affected_auction_ids);

        UPDATE auction_artwork_image_file link
        SET is_image_matching_processed = false,
            is_image_matching_completed_without_error = false
        FROM image_file image
        WHERE image.image_file_id = link.image_file_id
          AND image.cleaned_up_at IS NULL
          AND image.file_path IS NOT NULL
          AND link.auction_artwork_id = ANY(affected_auction_ids)
          AND (
              link.is_image_matching_processed = true
              OR link.is_image_matching_completed_without_error = true
          );

        UPDATE auction_artwork artwork
        SET is_image_matching_processed = false,
            is_image_matching_processed_at = NULL
        WHERE artwork.auction_artwork_id = ANY(affected_auction_ids)
          AND EXISTS (
                SELECT 1
                FROM auction_artwork_image_file link
                JOIN image_file image USING (image_file_id)
                WHERE link.auction_artwork_id = artwork.auction_artwork_id
                  AND image.cleaned_up_at IS NULL
                  AND image.file_path IS NOT NULL
            )
          AND (
              artwork.is_image_matching_processed = true
              OR artwork.is_image_matching_processed_at IS NOT NULL
          );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_invalidate_image_matching_after_content_change
ON image_file;

CREATE TRIGGER trg_invalidate_image_matching_after_content_change
AFTER UPDATE OF content_sha256 ON image_file
FOR EACH ROW
WHEN (NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256)
EXECUTE FUNCTION invalidate_image_matching_after_content_change();

CREATE TABLE IF NOT EXISTS dict_technique (
    technique_name       varchar(50) PRIMARY KEY,
    technique_parent     varchar(50) REFERENCES dict_technique(technique_name) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS dict_material (
    material_name       varchar(50) PRIMARY KEY,
    material_parent     varchar(50) REFERENCES dict_material(material_name) ON DELETE SET NULL
);


CREATE TABLE IF NOT EXISTS material_variant (
    material_variant_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dict_material_name          varchar(50) NOT NULL REFERENCES dict_material(material_name) ON DELETE CASCADE,
    material_raw_data           varchar(50) NOT NULL,
    match_type                  varchar(50) NOT NULL DEFAULT 'exact'

);

CREATE TABLE IF NOT EXISTS technique_variant (
    technique_variant_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dict_technique_name     varchar(50) NOT NULL REFERENCES dict_technique(technique_name) ON DELETE CASCADE,
    technique_raw_data      varchar(50) NOT NULL,
    match_type              varchar(50) NOT NULL DEFAULT 'exact'
);
