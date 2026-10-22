import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import *  # noqa: F401,F403,E402

from weaverbird import connectors  # noqa: E402


class ConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = connectors.load_registry(
            Path(__file__).resolve().parents[1] / "src" / "weaverbird" / "templates" / "connectors.yaml"
        )

    def test_registry_lists_expected_connectors(self):
        ids = {c.id for c in self.registry.connectors}
        for expected in ("github", "itch", "steam", "google_play",
                         "apple_signing", "llm_provider", "feedback_sources"):
            self.assertIn(expected, ids)

    def test_allowed_alone_action(self):
        self.assertEqual(
            connectors.check_capability(self.registry, "github", "commit_per_iteration"),
            connectors.ALLOWED_ALONE,
        )

    def test_needs_approval_action(self):
        self.assertEqual(
            connectors.check_capability(self.registry, "itch", "publish"),
            connectors.NEEDS_APPROVAL,
        )
        self.assertEqual(
            connectors.check_capability(self.registry, "steam", "publish"),
            connectors.NEEDS_APPROVAL,
        )

    def test_user_only_action(self):
        self.assertEqual(
            connectors.check_capability(self.registry, "steam", "create_partner_account"),
            connectors.USER_ONLY,
        )

    def test_unknown_action_fails_closed(self):
        self.assertEqual(
            connectors.classify_capability(self.registry, "github", "delete_everything"),
            connectors.UNKNOWN,
        )
        self.assertEqual(
            connectors.check_capability(self.registry, "github", "delete_everything"),
            connectors.NEEDS_APPROVAL,
        )
        self.assertFalse(
            connectors.is_allowed_alone(self.registry, "github", "delete_everything")
        )

    def test_unknown_connector_fails_closed(self):
        self.assertEqual(
            connectors.check_capability(self.registry, "no_such", "anything"),
            connectors.NEEDS_APPROVAL,
        )


if __name__ == "__main__":
    unittest.main()
