"""Safety and readiness tests; no Docker or live database is required."""
import importlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_stack import sample_state, seed


class DemoSeedSafetyTests(unittest.TestCase):
    def test_only_empty_schema_can_be_seeded(self):
        self.assertEqual(sample_state([]), "empty")
        self.assertEqual(sample_state(["BOOKS"]), "partial")
        self.assertEqual(sample_state(["OTHER_DATA"]), "partial")
        self.assertEqual(sample_state(["books", "members", "loans"]), "ready")

    def test_existing_demo_records_are_never_reloaded(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.__iter__.return_value = iter([("BOOKS",), ("MEMBERS",), ("LOANS",)])
        with patch.dict(os.environ, {"DEMO_PASSWORD": "test", "ORACLE_PASSWORD": "test", "PG_PASSWORD": "test"}), \
                patch("oracledb.connect", return_value=conn), patch("upload_pipeline.run_script") as run:
            seed()
            run.assert_not_called()

    def test_partial_schema_is_not_overwritten(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.__iter__.return_value = iter([("BOOKS",)])
        with patch.dict(os.environ, {"DEMO_PASSWORD": "test", "ORACLE_PASSWORD": "test", "PG_PASSWORD": "test"}), \
                patch("oracledb.connect", return_value=conn), patch("upload_pipeline.run_script") as run:
            with self.assertRaisesRegex(RuntimeError, "Nothing was replaced"):
                seed()
            run.assert_not_called()


class HttpReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"ORACLE_PASSWORD": "test", "PG_PASSWORD": "test"}):
            cls.api = importlib.import_module("legacy_api")

    def test_unavailable_dependency_returns_503_without_secrets(self):
        from runtime_stack import database_checks
        with patch("oracledb.connect", side_effect=RuntimeError("secret-password")), \
                patch("psycopg2.connect", side_effect=RuntimeError("secret-password")):
            checks = database_checks()
        self.assertTrue(all(c["status"] == "FAIL" for c in checks))
        self.assertNotIn("secret-password", str(checks))
        with patch("runtime_stack.database_checks", return_value=checks):
            response = self.api.health_ready()
        self.assertEqual(response.status_code, 503)

    def test_successful_dependencies_return_200(self):
        with patch("runtime_stack.database_checks", return_value=[{"service": "Oracle", "status": "PASS"},
                                                                 {"service": "PostgreSQL", "status": "PASS"}]):
            self.assertEqual(self.api.health_ready().status_code, 200)
        self.assertEqual(self.api.health_live()["service"], "legacy-db-modernizer")


if __name__ == "__main__":
    unittest.main()

