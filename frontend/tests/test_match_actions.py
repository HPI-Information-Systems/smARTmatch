import unittest
from unittest.mock import patch

from frontend import app
from frontend.routes import _match_actions as actions


class MatchActionTests(unittest.TestCase):
    def test_discard_updates_rating_and_clears_bookmark_atomically(self):
        form_data = {
            "lost-artwork-id": "00000000-0000-0000-0000-000000000001",
            "auction-artwork-id": "00000000-0000-0000-0000-000000000002",
        }

        with app.app.test_request_context(
            "/api/match/discard", method="POST", data=form_data
        ):
            with (
                patch.object(actions.app_module, "engine") as engine,
                patch.object(actions.app_module.session, "expire_all") as expire_all,
            ):
                actions.discard_match("match-id")

        connection = engine.connect.return_value.__enter__.return_value
        statement = connection.execute.call_args.args[0]
        values = statement.compile().params

        self.assertEqual(values["rating"], -1)
        self.assertIs(values["bookmarked"], False)
        connection.execute.assert_called_once()
        connection.commit.assert_called_once_with()
        expire_all.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
