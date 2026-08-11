-- Classify lost artwork provenance without relying on institution_id. Some
-- imported LostArt-only rows have an incorrect SPSG institution association,
-- so SPSG reporter evidence is intentionally restricted to reporter/contact
-- fields instead of searching descriptions, provenance, or literature.

CREATE OR REPLACE VIEW public.lost_artwork_source_classification AS
WITH source_data AS (
    SELECT
        la.lost_artwork_id,
        la.lost_art_id,
        la.lost_art_url,
        CASE
            WHEN NULLIF(btrim(la.raw_data::text), '') IS NULL THEN '{}'::jsonb
            ELSE la.raw_data::text::jsonb
        END AS raw_data
    FROM public.lost_artwork la
), evidence AS (
    SELECT
        source_data.*,
        concat_ws(
            E'\n',
            -- Shape used by the combined SPSG/LostArt import.
            raw_data -> 'lostart' -> 'scraped' ->> 'Kontakt',
            raw_data -> 'lostart' -> 'scraped' ->> 'Kontakt (DE)',
            raw_data -> 'lostart' -> 'scraped' ->> 'Contact',
            raw_data -> 'lostart' -> 'scraped' ->> 'Contact (EN)',
            raw_data -> 'lostart' -> 'scraped' ->> 'Suchauftrag, Institution',
            raw_data -> 'lostart' -> 'scraped' ->> 'Suchanfrage, Institution',
            raw_data -> 'lostart' -> 'scraped' ->> 'Search Request, Institution',
            raw_data -> 'lostart' -> 'scraped' ->> 'E-Mail',
            raw_data -> 'lostart' -> 'scraped' ->> 'Email',
            raw_data -> 'lostart' -> 'scraped' ->> 'Homepage',
            raw_data -> 'lostart' -> 'scraped' ->> 'Website',
            -- Shape written directly by the LostArt scraper.
            raw_data ->> 'Kontakt',
            raw_data ->> 'Kontakt (DE)',
            raw_data ->> 'Contact',
            raw_data ->> 'Contact (EN)',
            raw_data ->> 'Suchauftrag, Institution',
            raw_data ->> 'Suchanfrage, Institution',
            raw_data ->> 'Search Request, Institution',
            raw_data ->> 'E-Mail',
            raw_data ->> 'Email',
            raw_data ->> 'Homepage',
            raw_data ->> 'Website'
        ) AS reporter_text
    FROM source_data
), flags AS (
    SELECT
        lost_artwork_id,
        COALESCE(raw_data ? 'spsg', false) AS has_spsg_internal_source,
        COALESCE(raw_data ? 'lostart', false)
            OR NULLIF(btrim(lost_art_id), '') IS NOT NULL
            OR lower(COALESCE(lost_art_url, ''))
                ~ '^https?://([^/]+[.])?lostart[.](de|org)(/|$)'
            AS has_lostart_source,
        reporter_text
            ~* '(^|[^[:alnum:]])spsg([^[:alnum:]]|$)'
            OR reporter_text
            ~* 'stiftung[[:space:]]+preu(ß|ss)ische[[:space:]]+schl(ö|oe|o)sser[[:space:]]+und[[:space:]]+g(ä|ae|a)rten'
            AS has_spsg_reporter_evidence
    FROM evidence
)
SELECT
    lost_artwork_id,
    has_spsg_internal_source,
    has_lostart_source,
    has_spsg_reporter_evidence,
    CASE
        WHEN has_spsg_internal_source AND has_lostart_source
            THEN 'SPSG (internal and lostart)'
        WHEN has_spsg_internal_source
            THEN 'SPSG (internal)'
        WHEN has_lostart_source AND has_spsg_reporter_evidence
            THEN 'SPSG (lostart)'
        ELSE 'non-SPSG'
    END AS source_category,
    CASE
        WHEN has_spsg_internal_source AND has_lostart_source
            THEN 'raw_data.spsg + LostArt source marker'
        WHEN has_spsg_internal_source
            THEN 'raw_data.spsg'
        WHEN has_lostart_source AND has_spsg_reporter_evidence
            THEN 'LostArt reporter/contact identifies SPSG'
        ELSE 'no SPSG internal source or LostArt reporter/contact evidence'
    END AS classification_evidence
FROM flags;

COMMENT ON VIEW public.lost_artwork_source_classification IS
    'Auditable SPSG/LostArt provenance classification; intentionally does not infer ownership from institution_id.';
COMMENT ON COLUMN public.lost_artwork_source_classification.source_category IS
    'One of SPSG (internal), SPSG (internal and lostart), SPSG (lostart), or non-SPSG.';
COMMENT ON COLUMN public.lost_artwork_source_classification.classification_evidence IS
    'The evidence class used to assign source_category.';
