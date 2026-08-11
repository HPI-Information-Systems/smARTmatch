"""Offline unit tests for matching_pipeline.shared.db."""

from __future__ import annotations

import os
from unittest import mock

from matching_pipeline.shared import db
from tests.matching_pipeline._shared_test_support import TemporaryPipelineTest


class DatabaseTests(TemporaryPipelineTest):
    def setUp(self) -> None:
        super().setUp()
        os.environ.update(
            POSTGRES_DB="database",
            POSTGRES_USER="user",
            POSTGRES_PASSWORD="password",
            POSTGRES_HOST="db.example",
        )

    @mock.patch.object(db.psycopg, "connect", autospec=True)
    def test_connects_over_existing_unix_socket(self, connect) -> None:
        socket_dir = self.root / "socket"
        socket_dir.mkdir()
        (socket_dir / ".s.PGSQL.5432").touch()
        os.environ["POSTGRES_SOCKET_DIR"] = str(socket_dir)

        sentinel = connect.return_value
        self.assertIs(db.connect_db(), sentinel)
        connect.assert_called_once_with(
            dbname="database", user="user", password="password", host=str(socket_dir)
        )

    @mock.patch.object(db.psycopg, "connect", autospec=True)
    def test_connects_over_tcp_when_socket_is_absent(self, connect) -> None:
        os.environ["POSTGRES_SOCKET_DIR"] = str(self.root / "missing-socket")
        os.environ["POSTGRES_PORT"] = "5432"
        db.connect_db()
        connect.assert_called_once_with(
            dbname="database",
            user="user",
            password="password",
            host="db.example",
            port=5432,
        )

    def test_tcp_connection_validation(self) -> None:
        os.environ.pop("POSTGRES_PORT", None)
        with self.assertRaisesRegex(ValueError, "POSTGRES_PORT is required"):
            db.connect_db()
        for port in ("0", "65536"):
            os.environ["POSTGRES_PORT"] = port
            with self.assertRaisesRegex(ValueError, "must be in \\[1, 65535\\]"):
                db.connect_db()
        os.environ["POSTGRES_PORT"] = "5432"
        os.environ.pop("POSTGRES_HOST")
        with self.assertRaisesRegex(ValueError, "POSTGRES_HOST is required"):
            db.connect_db()
