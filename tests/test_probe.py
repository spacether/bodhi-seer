import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bodhi_seer import db
from bodhi_seer import server
from bodhi_seer.cli import cmd_add, cmd_update, estimate_fetch_volume
from bodhi_seer.gdelt import _calculate_retry_delay
from bodhi_seer.phases import _dt


class EstimateFetchVolumeTests(unittest.TestCase):
    def test_scales_sample_count_to_full_window(self):
        self.assertEqual(estimate_fetch_volume(12, days=30, probe_days=3), 120)
        self.assertEqual(estimate_fetch_volume(6, days=14, probe_days=2), 42)

    def test_handles_fractional_days(self):
        self.assertAlmostEqual(estimate_fetch_volume(10, days=5, probe_days=1.5), 33.333333333333336)


class ExistingTopicRefreshTests(unittest.TestCase):
    def test_cmd_add_refreshes_existing_topic_without_mutating_sqlite_row(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        old = os.environ.get("BODHI_SEER_DB")
        os.environ["BODHI_SEER_DB"] = path
        self.addCleanup(lambda: (os.environ.pop("BODHI_SEER_DB", None), old is not None and os.environ.__setitem__("BODHI_SEER_DB", old))[1])

        conn = db.connect(path)
        db.create_topic(conn, "paris violence", '"paris violence" sourcelang:english')

        args = SimpleNamespace(name="paris violence", query=None, sourcelang="english", days=30, yes=True)
        with patch("bodhi_seer.cli._fetch", return_value=0), patch("bodhi_seer.cli._analyze"):
            cmd_add(args)

        self.assertEqual(db.get_topic(conn, "paris violence")["query"], '"paris violence" sourcelang:english')


class UpdateCreatesMissingTopicTests(unittest.TestCase):
    def test_cmd_update_creates_missing_topic_instead_of_exiting(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        old = os.environ.get("BODHI_SEER_DB")
        os.environ["BODHI_SEER_DB"] = path
        self.addCleanup(lambda: (os.environ.pop("BODHI_SEER_DB", None), old is not None and os.environ.__setitem__("BODHI_SEER_DB", old))[1])

        args = SimpleNamespace(name="Chicago biotech", query=None, sourcelang="english", days=30, yes=True, all=False, reanalyze=False, no_analyze=False, location="Chicago")
        with patch("bodhi_seer.cli._fetch", return_value=1), patch("bodhi_seer.cli._analyze"), patch("bodhi_seer.cli._fetch_extra_sources", return_value=[]):
            cmd_update(args)

        self.assertIsNotNone(db.get_topic(db.connect(path), "Chicago biotech"))


class AddContinuesAfterGdeltFailureTests(unittest.TestCase):
    def test_cmd_add_continues_with_other_sources_when_gdelt_raises(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        old = os.environ.get("BODHI_SEER_DB")
        os.environ["BODHI_SEER_DB"] = path
        self.addCleanup(lambda: (os.environ.pop("BODHI_SEER_DB", None), old is not None and os.environ.__setitem__("BODHI_SEER_DB", old))[1])

        args = SimpleNamespace(name="Chicago biotech", query=None, sourcelang="english", days=30, yes=True, location="Chicago")
        with patch("bodhi_seer.cli._fetch", side_effect=RuntimeError("GDELT rate-limited")), \
             patch("bodhi_seer.cli._fetch_extra_sources", return_value=[{"title": "trial", "url": "https://example.com"}]) as mock_extra, \
             patch("bodhi_seer.cli.tree.ingest") as mock_ingest, \
             patch("bodhi_seer.cli._analyze"):
            cmd_add(args)

        self.assertTrue(mock_extra.called)
        self.assertTrue(mock_ingest.called)


class FrontEndSummaryApiTests(unittest.TestCase):
    def test_topics_api_includes_summary_and_create_response_returns_it(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        old = os.environ.get("BODHI_SEER_DB")
        os.environ["BODHI_SEER_DB"] = path
        self.addCleanup(lambda: (os.environ.pop("BODHI_SEER_DB", None), old is not None and os.environ.__setitem__("BODHI_SEER_DB", old))[1])

        conn = db.connect(path)
        topic = db.create_topic(conn, "Front-end summary", '"Front-end summary" sourcelang:english')
        with conn:
            conn.execute("UPDATE topics SET summary = ?, analyzed_at = ? WHERE id = ?", ("This is a stored summary.", "2026-01-01T00:00:00Z", topic["id"]))

        rows = server.topics()
        self.assertTrue(rows)
        self.assertIn("summary", rows[0])
        self.assertEqual(rows[0]["summary"], "This is a stored summary.")


class GdeltRetryBackoffTests(unittest.TestCase):
    def test_rate_limit_backoff_is_exponential_and_capped(self):
        self.assertEqual(_calculate_retry_delay(0), 15)
        self.assertEqual(_calculate_retry_delay(1), 30)
        self.assertEqual(_calculate_retry_delay(2), 60)
        self.assertEqual(_calculate_retry_delay(4), 60)

    def test_date_only_strings_parse_for_phase_analysis(self):
        self.assertEqual(_dt("2003-11-12"), _dt("2003-11-12T00:00:00Z"))

    def test_probe_window_does_not_retry_after_429(self):
        class FakeResponse:
            status_code = 429
            text = "rate limit"

            def json(self):
                return {}

        with patch("bodhi_seer.gdelt.requests.get", return_value=FakeResponse()) as mock_get:
            with self.assertRaises(RuntimeError):
                list(__import__("bodhi_seer.gdelt", fromlist=["fetch"]).fetch("probe test", __import__("datetime").datetime.utcnow() - __import__("datetime").timedelta(days=1), __import__("datetime").datetime.utcnow(), allow_retries=False))
        self.assertEqual(mock_get.call_count, 1)


class TopicSourceMergeTests(unittest.TestCase):
    def test_cmd_add_ingests_source_items_when_location_is_provided(self):
        args = SimpleNamespace(name="Chicago biotech", query=None, sourcelang="english", days=30, yes=True, location="Chicago")

        with patch("bodhi_seer.cli._fetch", return_value=1), \
             patch("bodhi_seer.cli._analyze"), \
             patch("bodhi_seer.cli.sources.collect_sources", return_value=[{
                 "title": "Trial item",
                 "url": "https://example.com/trial",
                 "location": "Chicago",
                 "published_at": "2024-01-02T00:00:00Z",
             }]), \
             patch("bodhi_seer.cli.tree.ingest") as mock_ingest, \
             patch("bodhi_seer.cli.db.get_topic", return_value={"id": 7, "name": "Chicago biotech", "query": '"Chicago biotech" sourcelang:english'}), \
             patch("bodhi_seer.cli.db.create_topic", return_value={"id": 7, "name": "Chicago biotech", "query": '"Chicago biotech" sourcelang:english'}):
            cmd_add(args)

        self.assertTrue(mock_ingest.called)
        self.assertEqual(mock_ingest.call_args.args[2][0]["title"], "Trial item")


if __name__ == "__main__":
    unittest.main()
