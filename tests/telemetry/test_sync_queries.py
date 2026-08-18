"""Focused selective synchronization tests."""

from __future__ import annotations

import unittest
from unittest import mock
from uuid import UUID

from telemetry import sync_budget, sync_constants, sync_errors, sync_inventory
from telemetry import sync_queries as sync


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class SyncQueryTests(unittest.TestCase):
    def _connection(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        return connection, cursor

    def test_match_lookup_joins_paired_uuid_arrays(self) -> None:
        connection, cursor = self._connection()
        lost_id = "10000000-0000-4000-8000-000000000001"
        auction_id = "20000000-0000-4000-8000-000000000001"

        self.assertEqual(
            sync._fetch_requested_match_rows(connection, [(lost_id, auction_id)]),
            [],
        )

        statement, parameters = cursor.execute.call_args.args
        self.assertIn("unnest(%s::uuid[], %s::uuid[])", statement)
        self.assertIn("JOIN match_score ms USING (lost_id, auction_id)", statement)
        self.assertNotIn("lost_id::text ||", statement)
        self.assertEqual(parameters, ([UUID(lost_id)], [UUID(auction_id)]))

    def test_entity_and_link_predicates_keep_uuid_columns_typed(self) -> None:
        entity_connection, entity_cursor = self._connection()
        entity_id = "10000000-0000-4000-8000-000000000001"
        sync._fetch_entities(
            entity_connection,
            "artist",
            "artist_id",
            {entity_id},
        )
        entity_statement, entity_parameters = entity_cursor.execute.call_args.args
        self.assertIn("WHERE artist_id = ANY(%s::uuid[])", entity_statement)
        self.assertNotIn("WHERE artist_id::text", entity_statement)
        self.assertEqual(entity_parameters, ([UUID(entity_id)],))

        link_connection, link_cursor = self._connection()
        sync._fetch_link_rows(
            link_connection,
            "lost_artwork_image_file",
            "lost_artwork_id",
            {entity_id},
        )
        link_statement, link_parameters = link_cursor.execute.call_args.args
        self.assertIn("WHERE lost_artwork_id = ANY(%s::uuid[])", link_statement)
        self.assertNotIn("WHERE lost_artwork_id::text", link_statement)
        self.assertEqual(link_parameters, ([UUID(entity_id)],))


class SyncInventoryQueryTests(unittest.TestCase):
    def _connection(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        return connection, cursor

    def test_inventory_query_loads_only_identifiers_before_bounded_closures(
        self,
    ) -> None:
        connection, cursor = self._connection()
        lost_id = UUID("10000000-0000-4000-8000-000000000001")
        auction_id = UUID("20000000-0000-4000-8000-000000000001")
        cursor.fetchall.return_value = [(lost_id, auction_id)]

        rows = sync_inventory._fetch_inventory_rows(connection, None, 500)

        statement = cursor.execute.call_args.args[0]
        self.assertNotIn("to_jsonb", statement)
        self.assertEqual(
            rows,
            [{"lost_id": lost_id, "auction_id": auction_id}],
        )


class SyncQueryResourceLimitTests(unittest.TestCase):
    def test_closure_queries_check_sizes_before_loading_json_rows(self) -> None:
        lost_id = "10000000-0000-4000-8000-000000000001"
        auction_id = "20000000-0000-4000-8000-000000000001"
        cases = (
            (
                "artwork",
                lambda connection, budget: sync._fetch_entities(
                    connection,
                    "lost_artwork",
                    "lost_artwork_id",
                    {lost_id},
                    materialization_budget=budget,
                ),
            ),
            (
                "links",
                lambda connection, budget: sync._fetch_link_rows(
                    connection,
                    "lost_artwork_image_file",
                    "lost_artwork_id",
                    {lost_id},
                    materialization_budget=budget,
                ),
            ),
            (
                "images",
                lambda connection, budget: sync._fetch_integer_entities(
                    connection,
                    {1},
                    materialization_budget=budget,
                ),
            ),
            (
                "matches",
                lambda connection, budget: sync._fetch_requested_match_rows(
                    connection,
                    [(lost_id, auction_id)],
                    materialization_budget=budget,
                ),
            ),
        )

        for name, fetch in cases:
            with self.subTest(name=name):
                connection = mock.MagicMock()
                cursor = connection.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value = (1, 1)
                budget = sync_budget._ClosureMaterializationBudget(
                    sync_constants._MATERIALIZATION_FIXED_OVERHEAD_BYTES
                )

                with self.assertRaises(sync_errors._ClosureMaterializationLimit):
                    fetch(connection, budget)

                self.assertEqual(cursor.execute.call_count, 1)
                statement = cursor.execute.call_args.args[0]
                self.assertIn("SUM(octet_length(to_jsonb", statement)
                cursor.fetchall.assert_not_called()
