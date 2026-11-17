"""Offline tests for kind-aware parsing (videos / shorts / streams).

Fixtures reproduce the shape of the real ytInitialData nodes: /videos and
/streams use lockupViewModel, /shorts uses shortsLockupViewModel.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("YT_API_TOKEN", "test-token")

import generate as gen  # noqa: E402
import api  # noqa: E402

CHANNEL = {"slug": "demo", "name": "Demo", "handle": "@demo", "cid": "UC" + "x" * 22}
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def page_with(*nodes):
    payload = {"contents": [{"itemSectionRenderer": {"contents": list(nodes)}}]}
    return f"<script>var ytInitialData = {json.dumps(payload)};</script>"


SHORTS_NODE = {
    "shortsLockupViewModel": {
        "entityId": "shorts-shelf-item-kfXg_exB_F8",
        "accessibilityText": "¡LA FE SE VIVE!, 188 views - play Short",
        "onTap": {"innertubeCommand": {"reelWatchEndpoint": {"videoId": "kfXg_exB_F8"}}},
        "overlayMetadata": {
            "primaryText": {"content": "¡LA FE SE VIVE EN MARÍA AUXILIADORA!"},
            "secondaryText": {"content": "188 vistas"},
        },
    }
}

VIDEO_NODE = {
    "lockupViewModel": {
        "contentId": "ytmawkPTP24",
        "metadata": {
            "lockupMetadataViewModel": {
                "title": {"content": "Microsoft just broke email"},
                "metadata": {
                    "contentMetadataViewModel": {
                        "metadataRows": [
                            {
                                "metadataParts": [
                                    {"text": {"content": "665K"}},
                                    {"text": {"content": "5h ago"}},
                                ]
                            }
                        ]
                    }
                },
            }
        },
    }
}


class ParseTests(unittest.TestCase):
    def test_shorts_are_parsed_from_shortsLockupViewModel(self):
        videos = gen.parse_videos(page_with(SHORTS_NODE), "shorts")
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["id"], "kfXg_exB_F8")
        self.assertEqual(videos[0]["title"], "¡LA FE SE VIVE EN MARÍA AUXILIADORA!")
        self.assertEqual(videos[0]["meta"], ["188 vistas"])

    def test_videos_tab_ignores_shorts_nodes(self):
        self.assertEqual(gen.parse_videos(page_with(SHORTS_NODE), "videos"), [])
        self.assertEqual(gen.parse_videos(page_with(SHORTS_NODE)), [])

    def test_shorts_tab_ignores_regular_lockups(self):
        self.assertEqual(gen.parse_videos(page_with(VIDEO_NODE), "shorts"), [])

    def test_videos_and_streams_share_lockup_markup(self):
        for kind in ("videos", "streams"):
            videos = gen.parse_videos(page_with(VIDEO_NODE), kind)
            self.assertEqual(videos[0]["id"], "ytmawkPTP24")
            self.assertEqual(videos[0]["meta"], ["665K", "5h ago"])

    def test_shorts_id_falls_back_to_entity_id(self):
        node = json.loads(json.dumps(SHORTS_NODE))
        del node["shortsLockupViewModel"]["onTap"]
        videos = gen.parse_videos(page_with(node), "shorts")
        self.assertEqual(videos[0]["id"], "kfXg_exB_F8")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(gen.ChannelError):
            gen.parse_videos("<html></html>", "podcasts")
        with self.assertRaises(gen.ChannelError):
            gen.channel_url(CHANNEL["cid"], "podcasts")


class BuildItemsTests(unittest.TestCase):
    def build(self, kind):
        videos = [{"id": "abc123", "title": "T", "meta": ["188 vistas"]}]
        items, new_ids = gen.build_items(CHANNEL, videos, {}, NOW, kind)
        return items[0], new_ids

    def test_shorts_use_shorts_url_and_vertical_thumb(self):
        item, new_ids = self.build("shorts")
        self.assertEqual(item["kind"], "shorts")
        self.assertEqual(item["link"], "https://www.youtube.com/shorts/abc123")
        self.assertTrue(item["thumb"].endswith("/oar2.jpg"))
        self.assertEqual(new_ids, ["abc123"])

    def test_videos_keep_watch_url(self):
        item, _ = self.build("videos")
        self.assertEqual(item["kind"], "videos")
        self.assertEqual(item["link"], "https://www.youtube.com/watch?v=abc123")
        self.assertTrue(item["thumb"].endswith("/hqdefault.jpg"))

    def test_state_is_shared_across_kinds(self):
        state = {}
        gen.build_items(CHANNEL, [{"id": "abc123", "title": "T", "meta": []}], state, NOW, "videos")
        _, new_ids = gen.build_items(
            CHANNEL, [{"id": "abc123", "title": "T", "meta": []}], state, NOW, "shorts"
        )
        self.assertEqual(new_ids, [], "same video id must not be reported as new twice")


class DateAndLangTests(unittest.TestCase):
    def test_english_relative_labels(self):
        self.assertEqual(gen.relative_to_datetime("3d ago", NOW), NOW - timedelta(days=3))
        self.assertEqual(gen.relative_to_datetime("5h ago", NOW), NOW - timedelta(hours=5))

    def test_spanish_relative_labels(self):
        self.assertEqual(gen.relative_to_datetime("hace 3 h", NOW), NOW - timedelta(hours=3))
        self.assertEqual(gen.relative_to_datetime("hace 16 min", NOW), NOW - timedelta(minutes=16))
        self.assertEqual(
            gen.relative_to_datetime("hace 2 semanas", NOW), NOW - timedelta(weeks=2)
        )
        self.assertEqual(
            gen.relative_to_datetime("hace 1 año", NOW), NOW - timedelta(days=365)
        )
        # YouTube abbreviates days as "d" in Spanish too ("hace 1 d")
        self.assertEqual(gen.relative_to_datetime("hace 1 d", NOW), NOW - timedelta(days=1))
        self.assertEqual(
            gen.relative_to_datetime("hace 1 a", NOW), NOW - timedelta(days=365)
        )

    def test_views_labels_are_not_dates(self):
        self.assertIsNone(gen.relative_to_datetime("306", NOW))
        self.assertIsNone(gen.relative_to_datetime("3.5K views", NOW))
        self.assertIsNone(gen.relative_to_datetime("", NOW))

    def test_channel_lang_drives_the_url(self):
        self.assertIn("hl=es", gen.channel_url(CHANNEL["cid"], "shorts", "es"))
        self.assertIn("hl=en", gen.channel_url(CHANNEL["cid"], "shorts", "en"))
        self.assertEqual(gen.channel_lang({"lang": "es"}), "es")
        self.assertEqual(gen.channel_lang({}), gen.DEFAULT_LANG)

    def test_shorts_seed_from_spanish_labels(self):
        item = {"id": "x", "title": "T", "meta": ["306", "hace 3 h"]}
        state = {}
        gen.build_items(CHANNEL, [item], state, NOW, "videos")
        self.assertEqual(state["x"]["t"], (NOW - timedelta(hours=3)).isoformat())


class DotenvTests(unittest.TestCase):
    def test_env_file_is_loaded_without_overriding_real_env(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write(
                "# comment\n"
                'export YT_TEST_QUOTED="hola mundo"\n'
                "YT_TEST_PLAIN=valor\n"
                "YT_TEST_PREEXISTING=desde-archivo\n"
                "YT_TEST_DUPLICATE=primero\n"
                "YT_TEST_DUPLICATE=segundo\n"
                "not a pair\n"
            )
            path = handle.name
        os.environ["YT_TEST_PREEXISTING"] = "desde-entorno"
        try:
            gen.load_dotenv(path)
            self.assertEqual(os.environ["YT_TEST_QUOTED"], "hola mundo")
            self.assertEqual(os.environ["YT_TEST_PLAIN"], "valor")
            self.assertEqual(os.environ["YT_TEST_PREEXISTING"], "desde-entorno")
            self.assertEqual(os.environ["YT_TEST_DUPLICATE"], "segundo")
            self.assertNotIn("not a pair", os.environ)
        finally:
            os.unlink(path)
            for key in (
                "YT_TEST_QUOTED",
                "YT_TEST_PLAIN",
                "YT_TEST_PREEXISTING",
                "YT_TEST_DUPLICATE",
            ):
                os.environ.pop(key, None)

    def test_missing_env_file_is_not_an_error(self):
        gen.load_dotenv("/nonexistent/path/.env")


class ChannelFieldTests(unittest.TestCase):
    def test_kinds_validation(self):
        self.assertEqual(api.normalize_kinds("shorts"), ["shorts"])
        self.assertEqual(api.normalize_kinds(["Videos", "shorts"]), ["videos", "shorts"])
        self.assertIsNone(api.normalize_kinds(None))
        self.assertIsNone(api.normalize_kinds([]))
        with self.assertRaises(ValueError):
            api.normalize_kinds(["videos", "podcasts"])

    def test_lang_validation(self):
        self.assertEqual(api.normalize_lang("ES"), "es")
        self.assertEqual(api.normalize_lang("es-pe"), "es-pe")
        self.assertIsNone(api.normalize_lang(None))
        with self.assertRaises(ValueError):
            api.normalize_lang("spanish")


class CorsAndDeploymentTests(unittest.TestCase):
    def setUp(self):
        self._original = api.CORS_ORIGINS

    def tearDown(self):
        api.CORS_ORIGINS = self._original

    def test_wildcard_allows_any_origin(self):
        api.CORS_ORIGINS = ["*"]
        self.assertEqual(api.cors_allow_origin("https://app.example.com"), "*")
        self.assertEqual(api.cors_allow_origin(None), "*")

    def test_explicit_origins_are_echoed(self):
        api.CORS_ORIGINS = ["https://app.example.com", "http://localhost:3000"]
        self.assertEqual(
            api.cors_allow_origin("https://app.example.com"), "https://app.example.com"
        )
        self.assertIsNone(api.cors_allow_origin("https://evil.example"))
        self.assertIsNone(api.cors_allow_origin(None))

    def test_empty_list_disables_cors(self):
        api.CORS_ORIGINS = []
        self.assertIsNone(api.cors_allow_origin("https://app.example.com"))

    def test_token_is_redacted_from_logs(self):
        line = 'GET /api/health?token=secret123&limit=2 HTTP/1.1'
        self.assertEqual(
            api.redact_token(line), "GET /api/health?token=***&limit=2 HTTP/1.1"
        )
        self.assertEqual(api.redact_token(""), "")

    def test_feed_paths_cannot_escape_the_feeds_directory(self):
        self.assertTrue(api.feed_file("tvperu-shorts.xml").endswith("tvperu-shorts.xml"))
        for bad in ("../../etc/passwd", "state.json", "tvperu.xml/../x.xml", ""):
            self.assertIsNone(api.feed_file(bad), bad)


class NamingTests(unittest.TestCase):
    def test_artifact_names_stay_backward_compatible(self):
        self.assertEqual(gen.artifact_name("demo", "videos", ".json"), "demo.json")
        self.assertEqual(gen.artifact_name("demo", "shorts", ".json"), "demo-shorts.json")
        self.assertEqual(gen.artifact_name("demo", "streams", ".xml"), "demo-streams.xml")

    def test_artifact_paths_differ_per_kind(self):
        paths = {gen.cache_path("demo", kind) for kind in gen.KINDS}
        self.assertEqual(len(paths), len(gen.KINDS))


class LimitTests(unittest.TestCase):
    def test_defaults_and_clamping(self):
        self.assertEqual(api.clamp_limit(None), 20)
        self.assertEqual(api.clamp_limit(""), 20)
        self.assertEqual(api.clamp_limit("5"), 5)
        self.assertEqual(api.clamp_limit("-3"), 1)
        self.assertEqual(api.clamp_limit("0"), 1)
        self.assertEqual(api.clamp_limit("5000"), 100)
        self.assertEqual(api.clamp_limit(None, default=50), 50)

    def test_bad_limit_is_a_400_error(self):
        with self.assertRaises(ValueError):
            api.clamp_limit("abc")

    def test_kind_validation(self):
        self.assertEqual(api.clamp_kind("Shorts"), "shorts")
        self.assertEqual(api.clamp_kind(None), "videos")
        with self.assertRaises(ValueError):
            api.clamp_kind("podcasts")


if __name__ == "__main__":
    unittest.main()
