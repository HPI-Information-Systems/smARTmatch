from __future__ import annotations

import unittest
from unittest.mock import patch

from scrapers.christies.api import ChristiesAPIConfig
from scrapers.utils.browser import _USER_AGENTS as BROWSER_USER_AGENTS
from scrapers.utils.request_handler import USER_AGENTS, generate_headers
from scrapers.utils.user_agents import (
    VERIFIED_USER_AGENTS,
    choose_user_agent,
    user_agent_pool,
)


class VerifiedUserAgentTests(unittest.TestCase):
    def test_pool_contains_twenty_unique_verified_identities(self) -> None:
        self.assertEqual(len(VERIFIED_USER_AGENTS), 20)
        self.assertEqual(len(set(VERIFIED_USER_AGENTS)), 20)
        self.assertTrue(
            all(value.startswith("Mozilla/5.0") for value in VERIFIED_USER_AGENTS)
        )

    def test_shared_consumers_use_verified_pool(self) -> None:
        self.assertIs(USER_AGENTS, VERIFIED_USER_AGENTS)
        self.assertIs(BROWSER_USER_AGENTS, VERIFIED_USER_AGENTS)
        self.assertIs(ChristiesAPIConfig().user_agents, VERIFIED_USER_AGENTS)

    def test_operator_override_is_first_and_deduplicated(self) -> None:
        custom = "Mozilla/5.0 custom"
        pool = user_agent_pool(custom)
        self.assertEqual(pool[0], custom)
        self.assertEqual(len(pool), 21)

        existing = VERIFIED_USER_AGENTS[-1]
        deduplicated = user_agent_pool(existing)
        self.assertEqual(deduplicated[0], existing)
        self.assertEqual(len(deduplicated), 20)

    def test_header_generation_selects_from_verified_pool(self) -> None:
        selected = VERIFIED_USER_AGENTS[-1]
        with patch("scrapers.utils.user_agents.random.choice", return_value=selected):
            headers = generate_headers()
        self.assertEqual(headers["User-Agent"], selected)

    def test_empty_pool_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            choose_user_agent(())


if __name__ == "__main__":
    unittest.main()
