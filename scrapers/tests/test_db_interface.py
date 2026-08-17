from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError

from scrapers.db_interface import Database, DatabaseError, _normalize_content_sha256


class _ScalarResult:
    def scalar_one_or_none(self):
        return None


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _InsertResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _ExistingResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.flushed = False

    def execute(self, stmt):
        return _ScalarResult()

    def add(self, instance) -> None:
        self.added.append(instance)

    def flush(self) -> None:
        self.flushed = True


class DatabaseEnvironmentTests(unittest.TestCase):
    def test_postgres_settings_use_required_env_vars(self) -> None:
        env = {
            "POSTGRES_HOST": "db",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "smartmatch_production",
            "POSTGRES_USER": "smartmatch",
            "POSTGRES_PASSWORD": "smartmatch",
        }

        with patch.dict(os.environ, env, clear=True):
            settings = Database._postgres_settings_from_env()

        self.assertEqual(settings.host, "db")
        self.assertEqual(settings.port, 5432)
        self.assertEqual(settings.database, "smartmatch_production")
        self.assertEqual(settings.user, "smartmatch")
        self.assertEqual(settings.password, "smartmatch")

    def test_postgres_settings_reject_missing_required_env_vars(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(DatabaseError, "POSTGRES_HOST"):
                Database._postgres_settings_from_env()

    def test_engine_target_uses_psycopg_driver_without_db_url_envs(self) -> None:
        env = {
            "POSTGRES_HOST": "db",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "smartmatch_production",
            "POSTGRES_USER": "smartmatch",
            "POSTGRES_PASSWORD": "smartmatch",
        }

        with patch.dict(os.environ, env, clear=True):
            target = Database._engine_target(Database._postgres_settings_from_env())

        self.assertEqual(target.drivername, "postgresql+psycopg")
        self.assertEqual(target.host, "db")
        self.assertEqual(target.port, 5432)
        self.assertEqual(target.database, "smartmatch_production")


class DatabaseArtistTests(unittest.TestCase):
    def test_get_or_create_artist_does_not_pass_variant_name_to_artist_model(
        self,
    ) -> None:
        db = Database.__new__(Database)
        fake_session = _FakeSession()
        db._get_session = lambda: fake_session  # type: ignore[attr-defined]

        class FakeArtist:
            def __init__(self, *, complete_name: str, raw_data: str | None = None):
                self.complete_name = complete_name
                self.raw_data = raw_data
                self.artist_id = "artist-1"

        class _FakeSelect:
            def where(self, *args, **kwargs):
                return self

        class _FakeField:
            def __eq__(self, other):
                return True

        class _FakeArtistModel(FakeArtist):
            complete_name = _FakeField()

        with (
            patch("scrapers.db_interface.Artist", _FakeArtistModel),
            patch(
                "scrapers.db_interface.select", lambda *args, **kwargs: _FakeSelect()
            ),
        ):
            artist = Database.get_or_create_artist(
                db,
                complete_name="Jane Doe",
                variant_name=["J. Doe", "Jane D."],
                raw_data='{"source":"test"}',
            )

        self.assertEqual(artist.complete_name, "Jane Doe")
        self.assertTrue(fake_session.flushed)
        self.assertEqual(len(fake_session.added), 3)
        variant_rows = fake_session.added[1:]
        self.assertEqual(
            [row.name_variant for row in variant_rows], ["J. Doe", "Jane D."]
        )


class DatabaseCaseInsensitiveLookupTests(unittest.TestCase):
    def test_get_or_create_artist_uses_case_insensitive_lookup(self) -> None:
        db = Database.__new__(Database)
        existing_artist = object()

        class ExistingSession:
            def __init__(self) -> None:
                self.last_stmt = ""
                self.added = []

            def execute(self, stmt):
                self.last_stmt = str(stmt)
                return _ExistingResult(existing_artist)

            def add(self, instance) -> None:
                self.added.append(instance)

            def flush(self) -> None:
                raise AssertionError("flush should not run when artist already exists")

        session = ExistingSession()
        db._get_session = lambda: session  # type: ignore[attr-defined]

        artist = Database.get_or_create_artist(db, complete_name="  jAnE   DOE  ")

        self.assertIs(artist, existing_artist)
        self.assertIn("lower(artist.complete_name)", session.last_stmt.lower())
        self.assertEqual(session.added, [])

    def test_get_or_create_artist_recovers_from_unique_violation(self) -> None:
        db = Database.__new__(Database)
        existing_artist = object()

        class Session:
            def __init__(self) -> None:
                self.added = []
                self.lookup_calls = 0
                self.rollback_called = False

            def execute(self, stmt):
                self.lookup_calls += 1
                if self.lookup_calls == 1:
                    return _ExistingResult(None)
                return _ExistingResult(existing_artist)

            def add(self, instance) -> None:
                self.added.append(instance)

            def flush(self) -> None:
                raise IntegrityError("insert", {}, Exception("duplicate"))

            def rollback(self) -> None:
                self.rollback_called = True

        session = Session()
        db._get_session = lambda: session  # type: ignore[attr-defined]
        db._model_matches_live_table = lambda **kwargs: True  # type: ignore[attr-defined]

        artist = Database.get_or_create_artist(db, complete_name="Emilio Vedova")

        self.assertIs(artist, existing_artist)
        self.assertTrue(session.rollback_called)
        self.assertEqual(len(session.added), 1)

    def test_get_or_create_auction_platform_normalizes_whitespace(self) -> None:
        db = Database.__new__(Database)

        class Session:
            def __init__(self) -> None:
                self.last_stmt = ""
                self.added = []
                self.flushed = False

            def execute(self, stmt):
                self.last_stmt = str(stmt)
                return _ScalarResult()

            def add(self, instance) -> None:
                self.added.append(instance)

            def flush(self) -> None:
                self.flushed = True

        session = Session()
        db._get_session = lambda: session  # type: ignore[attr-defined]

        platform = Database.get_or_create_auction_platform(db, name="  Sotheby's   ")

        self.assertTrue(session.flushed)
        self.assertIn("lower(auction_platform.name)", session.last_stmt.lower())
        self.assertEqual(platform.name, "Sotheby's")

    def test_get_or_create_auctioneer_uses_case_insensitive_lookup(self) -> None:
        db = Database.__new__(Database)
        existing_auctioneer = object()

        class ExistingSession:
            def __init__(self) -> None:
                self.last_stmt = ""
                self.added = []

            def execute(self, stmt):
                self.last_stmt = str(stmt)
                return _ExistingResult(existing_auctioneer)

            def add(self, instance) -> None:
                self.added.append(instance)

            def flush(self) -> None:
                raise AssertionError(
                    "flush should not run when auctioneer already exists"
                )

        session = ExistingSession()
        db._get_session = lambda: session  # type: ignore[attr-defined]

        auctioneer = Database.get_or_create_auctioneer(db, name="  sOtHeBy'S ")

        self.assertIs(auctioneer, existing_auctioneer)
        self.assertIn("lower(auctioneer.name)", session.last_stmt.lower())
        self.assertEqual(session.added, [])


class DatabaseAuctionArtworkTests(unittest.TestCase):
    def test_content_digest_validation_and_existing_row_update(self) -> None:
        digest = "A" * 64
        self.assertEqual(_normalize_content_sha256(digest), digest.lower())
        with self.assertRaisesRegex(ValueError, "64-character hexadecimal"):
            _normalize_content_sha256("bad")

        db = Database.__new__(Database)
        executed: list[tuple[str, object]] = []

        class FakeSession:
            def execute(self, stmt, params=None):
                executed.append((str(stmt), params))
                return _ScalarResult()

        db._get_session = lambda: FakeSession()  # type: ignore[attr-defined]
        db._table_has_columns = lambda *_args, **_kwargs: True  # type: ignore[attr-defined]
        db._image_file_id_by_path = lambda **_kwargs: 17  # type: ignore[attr-defined]

        with patch(
            "scrapers.db_interface._stored_image_content_sha256",
            return_value="b" * 64,
        ):
            result = Database._ensure_image_file_id(
                db,
                file_path="image.jpg",
                source_url="https://example.org/image.jpg",
                content_sha256=digest,
            )

        self.assertEqual(result, 17)
        self.assertEqual(len(executed), 2)
        advisory_sql, advisory_params = executed[0]
        self.assertIn("pg_advisory_xact_lock", advisory_sql)
        self.assertEqual(advisory_params, {"file_path": "image.jpg"})
        sql, params = executed[1]
        self.assertIn("content_sha256 = :content_sha256", sql)
        self.assertIn("content_sha256 is distinct from :content_sha256", sql)
        self.assertEqual(
            params,
            {
                "source_url": "https://example.org/image.jpg",
                "content_sha256": "b" * 64,
                "image_file_id": 17,
            },
        )

    def test_non_authoritative_legacy_reconcile_merges_successful_paths(self) -> None:
        db = Database.__new__(Database)
        updates: list[object] = []

        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                if "select img_paths" in sql:
                    return _ExistingResult(["legacy.jpg"])
                if "update auction_artwork" in sql:
                    updates.append(params)
                    return _ScalarResult()
                raise AssertionError(f"Unexpected SQL: {sql}")

        def has_columns(table_name, columns):
            return table_name == "auction_artwork" and columns == {"img_paths"}

        db._get_session = lambda: FakeSession()  # type: ignore[attr-defined]
        db._table_has_columns = has_columns  # type: ignore[attr-defined]

        Database.set_auction_artwork_images(
            db,
            auction_artwork_id="artwork-id",
            image_paths=["replacement.jpg"],
            authoritative=False,
        )

        self.assertEqual(
            updates,
            [
                {
                    "img_paths": ["legacy.jpg", "replacement.jpg"],
                    "auction_artwork_id": "artwork-id",
                }
            ],
        )

    def test_non_authoritative_image_reconcile_retains_existing_links(self) -> None:
        db = Database.__new__(Database)
        executed: list[tuple[str, object]] = []

        class ExistingImagesResult:
            def scalars(self):
                return self

            def all(self):
                return ["legacy-image-id"]

        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                executed.append((sql, params))
                if "select image_file_id" in sql:
                    return ExistingImagesResult()
                if "insert into auction_artwork_image_file" in sql:
                    return _ScalarResult()
                raise AssertionError(f"Unexpected SQL: {sql}")

        db._get_session = lambda: FakeSession()  # type: ignore[attr-defined]
        db._table_has_columns = lambda *args, **kwargs: True  # type: ignore[attr-defined]
        db._get_table_columns = lambda *args: {  # type: ignore[attr-defined]
            "auction_artwork_id",
            "image_file_id",
        }
        db._ensure_image_file_id = (  # type: ignore[attr-defined]
            lambda **kwargs: "replacement-image-id"
        )

        Database.set_auction_artwork_images(
            db,
            auction_artwork_id="artwork-id",
            image_paths=["replacement.jpg"],
            image_source_urls={"replacement.jpg": "https://example.org/new.jpg"},
            authoritative=False,
        )

        statements = [sql.lower() for sql, _ in executed]
        self.assertFalse(
            any("delete from auction_artwork_image_file" in sql for sql in statements)
        )
        self.assertTrue(
            any("insert into auction_artwork_image_file" in sql for sql in statements)
        )

    def test_authoritative_image_reconcile_removes_superseded_links(self) -> None:
        db = Database.__new__(Database)
        executed: list[str] = []

        class ExistingImagesResult:
            def scalars(self):
                return self

            def all(self):
                return ["legacy-image-id"]

        class FakeSession:
            def execute(self, stmt, params=None):
                del params
                sql = str(stmt)
                executed.append(sql)
                if "select image_file_id" in sql:
                    return ExistingImagesResult()
                if "delete from auction_artwork_image_file" in sql:
                    return _ScalarResult()
                raise AssertionError(f"Unexpected SQL: {sql}")

        db._get_session = lambda: FakeSession()  # type: ignore[attr-defined]
        db._table_has_columns = lambda *args, **kwargs: True  # type: ignore[attr-defined]
        db._get_table_columns = lambda *args: set()  # type: ignore[attr-defined]

        Database.set_auction_artwork_images(
            db,
            auction_artwork_id="artwork-id",
            image_paths=[],
            authoritative=True,
        )

        self.assertTrue(
            any(
                "delete from auction_artwork_image_file" in sql.lower()
                for sql in executed
            )
        )

    def test_authoritative_empty_reconcile_invalidates_image_matching_state(
        self,
    ) -> None:
        db = Database.__new__(Database)
        executed: list[tuple[str, object]] = []

        class ExistingImagesResult:
            def scalars(self):
                return self

            def all(self):
                return ["legacy-image-id"]

        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                executed.append((sql, params))
                if "select image_file_id" in sql:
                    return ExistingImagesResult()
                return _ScalarResult()

        columns = {
            "auction_artwork_image_file": {
                "auction_artwork_id",
                "image_file_id",
                "is_image_matching_processed",
            },
            "auction_artwork": {
                "auction_artwork_id",
                "is_image_matching_processed",
                "is_image_matching_processed_at",
            },
            "match_score": {
                "auction_id",
                "metadata_final_score",
                "image_matching_confidence",
                "image_final_score",
                "image_blocking_similarity",
                "image_match_date",
                "image_matching_program",
                "image_visualization",
                "best_image_file_id",
            },
        }
        session = FakeSession()
        db._get_session = lambda: session  # type: ignore[attr-defined]
        db._table_has_columns = (  # type: ignore[attr-defined]
            lambda *args, **kwargs: True
        )
        db._get_table_columns = (  # type: ignore[attr-defined]
            lambda table: columns.get(table, set())
        )

        Database.set_auction_artwork_images(
            db,
            auction_artwork_id="artwork-id",
            image_paths=[],
            authoritative=True,
        )

        statements = [sql.lower() for sql, _ in executed]
        score_update_index = next(
            index
            for index, sql in enumerate(statements)
            if "update match_score set" in sql
        )
        score_delete_index = next(
            index
            for index, sql in enumerate(statements)
            if "delete from match_score" in sql
        )
        link_delete_index = next(
            index
            for index, sql in enumerate(statements)
            if "delete from auction_artwork_image_file" in sql
        )
        artwork_update_index = next(
            index
            for index, sql in enumerate(statements)
            if "update auction_artwork set" in sql
        )

        score_update = statements[score_update_index]
        self.assertIn("image_matching_confidence = null", score_update)
        self.assertIn("image_final_score = null", score_update)
        self.assertIn("image_blocking_similarity = null", score_update)
        self.assertIn("image_matching_program = null", score_update)
        self.assertIn("image_visualization = '{}'", score_update)
        self.assertIn("best_image_file_id = null", score_update)
        self.assertIn("metadata_final_score is not null", score_update)
        self.assertIn("metadata_final_score is null", statements[score_delete_index])
        self.assertLess(score_update_index, link_delete_index)
        self.assertLess(score_delete_index, link_delete_index)
        self.assertLess(link_delete_index, artwork_update_index)
        self.assertIn(
            "is_image_matching_processed = true", statements[artwork_update_index]
        )
        self.assertIn(
            "is_image_matching_processed_at = current_timestamp",
            statements[artwork_update_index],
        )
        for sql, params in executed:
            if "match_score" in sql or "update auction_artwork set" in sql:
                self.assertEqual(params, {"auction_artwork_id": "artwork-id"})

    def test_upsert_auction_artwork_filters_to_live_columns(self) -> None:
        db = Database.__new__(Database)
        executed = []

        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                executed.append((sql, params))
                if "information_schema.columns" in sql:
                    return _AllResult(
                        [
                            ("auction_artwork_id",),
                            ("lot_id",),
                            ("lot_url",),
                            ("title",),
                            ("raw_data",),
                        ]
                    )
                if "select auction_artwork_id from auction_artwork" in sql:
                    return _ScalarResult()
                if "insert into auction_artwork" in sql:
                    return _InsertResult("new-artwork-id")
                raise AssertionError(f"Unexpected SQL: {sql}")

        db._get_session = lambda: FakeSession()  # type: ignore[attr-defined]
        db._table_columns_cache = {}

        artwork = Database.upsert_auction_artwork(
            db,
            lot_id="LOT-1",
            lot_url="https://example.test/lot-1",
            title="Example",
            raw_data='{"ok":true}',
            unknown_field="ignored",
        )

        self.assertEqual(artwork.lot_id, "LOT-1")
        lookup_sql, _ = executed[1]
        self.assertIn("where lot_url = :lookup_value", lookup_sql)

        insert_sql, insert_params = executed[-1]
        self.assertIn("insert into auction_artwork", insert_sql)
        self.assertNotIn("unknown_field", insert_sql)
        self.assertEqual(insert_params["title"], "Example")
        self.assertEqual(insert_params["raw_data"], '{"ok":true}')

    def test_upsert_auction_artwork_scopes_lookup_by_platform_when_available(
        self,
    ) -> None:
        db = Database.__new__(Database)
        executed = []

        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                executed.append((sql, params))
                if "information_schema.columns" in sql:
                    return _AllResult(
                        [
                            ("auction_artwork_id",),
                            ("lot_url",),
                            ("title",),
                            ("auction_platform_id",),
                        ]
                    )
                if "select auction_artwork_id from auction_artwork" in sql:
                    return _ScalarResult()
                if "insert into auction_artwork" in sql:
                    return _InsertResult("new-artwork-id")
                raise AssertionError(f"Unexpected SQL: {sql}")

        db._get_session = lambda: FakeSession()  # type: ignore[attr-defined]
        db._table_columns_cache = {}

        Database.upsert_auction_artwork(
            db,
            lot_url="https://example.test/lot-2",
            title="Example",
            auction_platform_id="platform-1",
        )

        lookup_sql, lookup_params = executed[1]
        self.assertIn("and auction_platform_id = :lookup_platform_id", lookup_sql)
        self.assertEqual(lookup_params["lookup_platform_id"], "platform-1")

    def test_upsert_auction_artwork_never_updates_primary_key(self) -> None:
        db = Database.__new__(Database)
        executed = []

        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                executed.append((sql, params))
                if "information_schema.columns" in sql:
                    return _AllResult(
                        [
                            ("auction_artwork_id",),
                            ("lot_id",),
                            ("title",),
                        ]
                    )
                if "select auction_artwork_id from auction_artwork" in sql:
                    return _ExistingResult("existing-artwork-id")
                if "update auction_artwork set" in sql:
                    return _ScalarResult()
                raise AssertionError(f"Unexpected SQL: {sql}")

        db._get_session = lambda: FakeSession()  # type: ignore[attr-defined]
        db._table_columns_cache = {}

        Database.upsert_auction_artwork(
            db,
            lot_id="LOT-1",
            title="Updated title",
            auction_artwork_id="new-artwork-id",
        )

        update_sql, _ = executed[-1]
        self.assertIn("update auction_artwork set", update_sql)
        self.assertNotIn("set auction_artwork_id", update_sql)

    def test_upsert_auction_artwork_falls_back_to_lot_id_lookup(self) -> None:
        db = Database.__new__(Database)
        executed = []

        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                executed.append((sql, params))
                if "information_schema.columns" in sql:
                    return _AllResult(
                        [
                            ("auction_artwork_id",),
                            ("lot_id",),
                            ("lot_url",),
                            ("title",),
                            ("auction_platform_id",),
                        ]
                    )
                if (
                    "select auction_artwork_id from auction_artwork" in sql
                    and "where lot_url" in sql
                ):
                    return _ScalarResult()
                if (
                    "select auction_artwork_id from auction_artwork" in sql
                    and "where lot_id" in sql
                ):
                    return _ExistingResult("existing-artwork-id")
                if "update auction_artwork set" in sql:
                    return _ScalarResult()
                if "insert into auction_artwork" in sql:
                    raise AssertionError(
                        "insert should not run when lot_id lookup finds an existing row"
                    )
                raise AssertionError(f"Unexpected SQL: {sql}")

        db._get_session = lambda: FakeSession()  # type: ignore[attr-defined]
        db._table_columns_cache = {}

        artwork = Database.upsert_auction_artwork(
            db,
            lot_id="LOT-600",
            lot_url="https://example.test/updated-lot",
            title="Updated title",
            auction_platform_id="platform-1",
        )

        lookup_sqls = [
            sql
            for sql, _ in executed
            if "select auction_artwork_id from auction_artwork" in sql
        ]
        self.assertEqual(len(lookup_sqls), 2)
        self.assertIn("where lot_url = :lookup_value", lookup_sqls[0])
        self.assertIn("where lot_id = :lookup_value", lookup_sqls[1])
        self.assertEqual(artwork.auction_artwork_id, "existing-artwork-id")

    def test_upsert_auction_artwork_recovers_from_insert_unique_violation(self) -> None:
        db = Database.__new__(Database)
        executed = []

        class FakeSession:
            def __init__(self) -> None:
                self.lot_id_lookup_calls = 0
                self.rollback_called = False

            def execute(self, stmt, params=None):
                sql = str(stmt)
                executed.append((sql, params))

                if "information_schema.columns" in sql:
                    return _AllResult(
                        [
                            ("auction_artwork_id",),
                            ("lot_id",),
                            ("lot_url",),
                            ("title",),
                        ]
                    )

                if (
                    "select auction_artwork_id from auction_artwork" in sql
                    and "where lot_url" in sql
                ):
                    return _ScalarResult()

                if (
                    "select auction_artwork_id from auction_artwork" in sql
                    and "where lot_id" in sql
                ):
                    self.lot_id_lookup_calls += 1
                    if self.lot_id_lookup_calls >= 2:
                        return _ExistingResult("existing-artwork-id")
                    return _ScalarResult()

                if "insert into auction_artwork" in sql:
                    raise IntegrityError("insert", params or {}, Exception("duplicate"))

                if "update auction_artwork set" in sql:
                    return _ScalarResult()

                raise AssertionError(f"Unexpected SQL: {sql}")

            def rollback(self) -> None:
                self.rollback_called = True

        session = FakeSession()
        db._get_session = lambda: session  # type: ignore[attr-defined]
        db._table_columns_cache = {}

        artwork = Database.upsert_auction_artwork(
            db,
            lot_id="LOT-600",
            lot_url="https://example.test/lot-600",
            title="Updated title",
        )

        self.assertEqual(artwork.auction_artwork_id, "existing-artwork-id")
        self.assertTrue(session.rollback_called)
        self.assertGreaterEqual(session.lot_id_lookup_calls, 2)
        self.assertTrue(
            any("update auction_artwork set" in sql for sql, _ in executed),
            "Expected an update after conflict recovery",
        )

    def test_upsert_auction_artwork_forwards_img_paths_to_image_link_writer(
        self,
    ) -> None:
        db = Database.__new__(Database)
        image_calls = []

        class FakeSession:
            def execute(self, stmt, params=None):
                sql = str(stmt)
                if "information_schema.columns" in sql:
                    return _AllResult(
                        [
                            ("auction_artwork_id",),
                            ("lot_id",),
                            ("title",),
                        ]
                    )
                if "select auction_artwork_id from auction_artwork" in sql:
                    return _ScalarResult()
                if "insert into auction_artwork" in sql:
                    return _InsertResult("new-artwork-id")
                raise AssertionError(f"Unexpected SQL: {sql}")

        db._get_session = lambda: FakeSession()  # type: ignore[attr-defined]
        db._table_columns_cache = {}
        db.set_auction_artwork_images = (  # type: ignore[attr-defined]
            lambda *, auction_artwork_id, image_paths: image_calls.append(
                (auction_artwork_id, list(image_paths))
            )
        )

        artwork = Database.upsert_auction_artwork(
            db,
            lot_id="LOT-700",
            title="With images",
            img_paths=["one.jpg", "two.jpg"],
        )

        self.assertEqual(artwork.auction_artwork_id, "new-artwork-id")
        self.assertEqual(image_calls, [("new-artwork-id", ["one.jpg", "two.jpg"])])


class DatabaseLostArtworkTests(unittest.TestCase):
    def test_upsert_lost_artwork_forwards_img_paths_to_image_link_writer(self) -> None:
        db = Database.__new__(Database)
        image_calls = []

        class FakeSession:
            def __init__(self) -> None:
                self.flushed = False

            def flush(self) -> None:
                self.flushed = True

        session = FakeSession()
        existing = type(
            "ExistingLost", (), {"lost_artwork_id": "lost-1", "title": "old"}
        )()

        db._get_session = lambda: session  # type: ignore[attr-defined]
        db.find_lost_artwork_by_lost_art_id = (  # type: ignore[attr-defined]
            lambda lost_art_id: existing
        )
        db.set_lost_artwork_images = (  # type: ignore[attr-defined]
            lambda *, lost_artwork_id, image_paths: image_calls.append(
                (lost_artwork_id, list(image_paths))
            )
        )

        artwork = Database.upsert_lost_artwork(
            db,
            lost_art_id="LA-1",
            title="new title",
            img_paths=["lost.jpg"],
        )

        self.assertIs(artwork, existing)
        self.assertEqual(existing.title, "new title")
        self.assertTrue(session.flushed)
        self.assertEqual(image_calls, [("lost-1", ["lost.jpg"])])


if __name__ == "__main__":
    unittest.main()
