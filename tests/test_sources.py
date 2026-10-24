import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from bodhi_seer import server
from bodhi_seer.llm import complete, complete_json
from bodhi_seer.sources import _clinicaltrials_source, collect_sources, list_sources


class SourceIntegrationTests(unittest.TestCase):
    def test_list_sources_includes_chicago_biotech_sources(self):
        names = [s["name"] for s in list_sources()]
        self.assertIn("gdelt", names)
        self.assertIn("clinicaltrials", names)
        self.assertIn("chicagobusiness_rss", names)

    def test_collect_sources_runs_in_parallel(self):
        started = []

        def fake_source(topic, location, days, logger):
            started.append((topic, location, days))
            time.sleep(0.25)
            return [{"title": topic, "url": "https://example.com"}]

        with patch.dict("bodhi_seer.sources._SOURCE_FACTORIES", {
            "one": "_fake_one",
            "two": "_fake_two",
            "three": "_fake_three",
        }, clear=False), \
             patch("bodhi_seer.sources._fake_one", new=fake_source, create=True), \
             patch("bodhi_seer.sources._fake_two", new=fake_source, create=True), \
             patch("bodhi_seer.sources._fake_three", new=fake_source, create=True):
            start = time.perf_counter()
            results = collect_sources("biotech", "Chicago", days=7, include=["one", "two", "three"])
            elapsed = time.perf_counter() - start

        self.assertEqual(len(results), 3)
        self.assertLess(elapsed, 0.7)

    def test_collect_sources_logs_progress_and_returns_results(self):
        seen = []

        def fake_logger(msg):
            seen.append(msg)

        with patch("bodhi_seer.sources._gdelt_source", return_value=[{"title": "GDELT item", "url": "https://example.com/gdelt"}]), \
             patch("bodhi_seer.sources._clinicaltrials_source", return_value=[{"title": "Trial item", "url": "https://example.com/trial"}]), \
             patch("bodhi_seer.sources._chicagobusiness_rss_source", return_value=[{"title": "Chicago item", "url": "https://example.com/chicago"}]), \
             patch("bodhi_seer.sources._google_patents_source", return_value=[]), \
             patch("bodhi_seer.sources._crunchbase_source", return_value=[]):
            results = collect_sources("biotech", "Chicago", days=7, logger=fake_logger)

        self.assertGreaterEqual(len(results), 3)
        self.assertTrue(any("gdelt" in msg.lower() for msg in seen))
        self.assertTrue(any("clinicaltrials" in msg.lower() for msg in seen))

    def test_extra_sources_exclude_gdelt_from_parallel_fan_out(self):
        with patch("bodhi_seer.cli.sources.collect_sources") as mock_collect:
            mock_collect.return_value = []
            _ = __import__("bodhi_seer.cli", fromlist=["_fetch_extra_sources"])._fetch_extra_sources("biotech", "Chicago", 30)

        self.assertIn("clinicaltrials", mock_collect.call_args.kwargs["include"])
        self.assertNotIn("gdelt", mock_collect.call_args.kwargs["include"])

    def test_local_llm_provider_uses_openai_compatible_api(self):
        env = {
            "BODHI_SEER_LLM_PROVIDER": "ollama",
            "BODHI_SEER_MODEL": "deepseek-r1:7b",
            "BODHI_SEER_BASE_URL": "http://localhost:11434/v1",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("bodhi_seer.llm.requests.post") as mock_post:
                mock_post.return_value.raise_for_status.return_value = None
                mock_post.return_value.json.return_value = {
                    "choices": [{"message": {"content": '{"ok": true}'}}]
                }
                self.assertEqual(complete("system", "prompt"), '{"ok": true}')
                self.assertEqual(complete_json("system", "prompt"), {"ok": True})

    def test_collect_sources_keeps_gdelt_sequential(self):
        order = []

        def fake_gdelt(topic, location, days, logger):
            order.append("gdelt")
            time.sleep(0.20)
            return [{"title": "GDELT item", "url": "https://example.com/gdelt"}]

        def fake_other(topic, location, days, logger):
            order.append("other")
            time.sleep(0.15)
            return [{"title": topic, "url": "https://example.com"}]

        with patch.dict("bodhi_seer.sources._SOURCE_FACTORIES", {
            "gdelt": "_fake_gdelt",
            "one": "_fake_one",
            "two": "_fake_two",
        }, clear=False), \
             patch("bodhi_seer.sources._fake_gdelt", new=fake_gdelt, create=True), \
             patch("bodhi_seer.sources._fake_one", new=fake_other, create=True), \
             patch("bodhi_seer.sources._fake_two", new=fake_other, create=True):
            results = collect_sources("biotech", "Chicago", days=7, include=["gdelt", "one", "two"])

        self.assertEqual(len(results), 3)
        self.assertEqual(order[:1], ["gdelt"])
        self.assertEqual(order.count("other"), 2)

    def test_clinicaltrials_v2_term_search_filters_by_location(self):
        payload = {
            "studies": [
                {
                    "protocolSection": {
                        "identificationModule": {"nctId": "NCT0001", "briefTitle": "Chicago biotech study"},
                        "statusModule": {"overallStatus": "RECRUITING"}
                    },
                    "locationModule": {"locations": [{"facility": {"address": {"city": "Chicago", "state": "Illinois", "country": "United States"}}}]},
                },
                {
                    "protocolSection": {
                        "identificationModule": {"nctId": "NCT0002", "briefTitle": "Boston biotech study"},
                        "statusModule": {"overallStatus": "ACTIVE_NOT_RECRUITING"}
                    },
                    "locationModule": {"locations": [{"facility": {"address": {"city": "Boston", "state": "Massachusetts", "country": "United States"}}}]},
                },
            ]
        }

        with patch("bodhi_seer.sources.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.return_value = payload

            items = _clinicaltrials_source("biotech", "Chicago", 30, None)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Chicago biotech study")
        self.assertIn("query.term", mock_get.call_args.kwargs["params"])
        self.assertNotIn("query.location", mock_get.call_args.kwargs["params"])


class TopicApiTests(unittest.TestCase):
    def test_topic_creation_api_accepts_language_and_source_mode(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            db_path = handle.name
        with patch.dict(os.environ, {"BODHI_SEER_DB": db_path}, clear=False):
            with patch("bodhi_seer.server.create_topic_from_form") as mock_create:
                mock_create.return_value = {
                    "id": 1,
                    "name": "Chicago biotech",
                    "query": '"Chicago biotech" sourcelang:english',
                    "source_mode": "news",
                }
                client = TestClient(server.app)
                response = client.post(
                    "/api/topics",
                    json={
                        "name": "Chicago biotech",
                        "days": 30,
                        "language": "english",
                        "source_mode": "news",
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["name"], "Chicago biotech")
        self.assertEqual(body["source_mode"], "news")
        mock_create.assert_called_once_with(
            name="Chicago biotech",
            days=30,
            language="english",
            source_mode="news",
            location=None,
        )


if __name__ == "__main__":
    unittest.main()
