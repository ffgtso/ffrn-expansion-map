import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from expansion_map import (
    JsonHttpClient,
    MapItCache,
    Node,
    Position,
    Source,
    build_geojson,
    choose_area,
    merge_nodes,
    parse_nodes,
)


class FakeResponse:
    class Headers:
        @staticmethod
        def get_content_charset():
            return "utf-8"

    headers = Headers()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def read():
        return b'{"ok": true}'


class FakeMapIt:
    def resolve_area(self, position):
        if position.lat < 0:
            return None
        return {"id": 42, "name": "Teststadt", "type": "O08"}

    def area_geometry(self, area_id):
        return {
            "type": "Polygon",
            "coordinates": [[[8.0, 49.0], [8.1, 49.0], [8.1, 49.1], [8.0, 49.0]]],
        }


class ParseNodesTests(unittest.TestCase):
    def test_meshviewer_array(self):
        document = {
            "nodes": [
                {
                    "nodeinfo": {
                        "node_id": "a",
                        "location": {"latitude": 49.1, "longitude": 8.2},
                    }
                }
            ]
        }
        nodes = parse_nodes(document, Source("alpha", "unused", "auto"))
        self.assertEqual(nodes, [Node("a", Position(49.1, 8.2), "alpha")])

    def test_hopglass_mapping(self):
        document = {
            "nodes": {
                "fallback-id": {
                    "nodeinfo": {"location": {"latitude": 49.2, "longitude": 8.3}}
                }
            }
        }
        nodes = parse_nodes(document, Source("beta", "unused", "hopglass"))
        self.assertEqual(nodes[0].node_id, "fallback-id")

    def test_ffmap_and_nodelist(self):
        ffmap = parse_nodes(
            {"nodes": [{"id": "f", "geo": [49.3, 8.4]}]},
            Source("ffmap", "unused", "ffmap"),
        )
        nodelist = parse_nodes(
            {"nodes": [{"id": "n", "position": {"lat": 49.4, "long": 8.5}}]},
            Source("nodelist", "unused", "nodelist"),
        )
        self.assertEqual(ffmap[0].position, Position(49.3, 8.4))
        self.assertEqual(nodelist[0].position, Position(49.4, 8.5))

    def test_invalid_positions_are_skipped(self):
        nodes = parse_nodes(
            {"nodes": [{"id": "bad", "geo": [999, 8.4]}]},
            Source("ffmap", "unused", "ffmap"),
        )
        self.assertEqual(nodes, [])

    def test_empty_nodes_are_skipped_before_auto_detection(self):
        for empty_nodes in ([], {}):
            with self.subTest(empty_nodes=empty_nodes):
                nodes = parse_nodes(
                    {"version": 2, "nodes": empty_nodes, "timestamp": "2026-09-15T16:07:25.266Z"},
                    Source("empty", "unused", "auto"),
                )
                self.assertEqual(nodes, [])


class HttpClientTests(unittest.TestCase):
    def test_403_retry_uses_retry_after_header(self):
        error = HTTPError(
            "https://example.invalid/data.json",
            403,
            "Forbidden",
            {"Retry-After": "7"},
            None,
        )
        with (
            patch("expansion_map.urlopen", side_effect=[error, FakeResponse()]),
            patch("expansion_map.time.sleep") as sleep,
        ):
            document = JsonHttpClient(retries=1).get_json("https://example.invalid/data.json")

        self.assertEqual(document, {"ok": True})
        sleep.assert_called_once_with(7.0)

    def test_403_retry_enforces_minimum_backoff(self):
        error = HTTPError(
            "https://example.invalid/data.json",
            403,
            "Forbidden",
            {"Retry-After": "1"},
            None,
        )
        with (
            patch("expansion_map.urlopen", side_effect=[error, FakeResponse()]),
            patch("expansion_map.time.sleep") as sleep,
        ):
            JsonHttpClient(retries=1).get_json("https://example.invalid/data.json")

        sleep.assert_called_once_with(5.0)


class AggregationTests(unittest.TestCase):
    def test_choose_area_respects_priority(self):
        areas = [
            {"id": 6, "name": "District", "type": "O06"},
            {"id": 8, "name": "Town", "type": "O08"},
        ]
        self.assertEqual(choose_area(areas, ("O08", "O06"))["id"], 8)

    def test_merge_deduplicates_node_ids(self):
        first = Node("same", Position(49.1, 8.1), "a")
        second = Node("same", Position(49.1, 8.1), "b")
        self.assertEqual(merge_nodes([[first], [second]]), [first])

    def test_geojson_contains_source_breakdown(self):
        nodes = [
            Node("a", Position(49.1, 8.1), "one"),
            Node("b", Position(49.2, 8.2), "two"),
            Node("c", Position(-1.0, 8.2), "two"),
        ]
        result = build_geojson(nodes, FakeMapIt(), {"one": "u1", "two": "u2"})
        self.assertEqual(result["metadata"]["node_count"], 3)
        self.assertEqual(result["metadata"]["unresolved_nodes"], 1)
        self.assertEqual(result["features"][0]["properties"]["count"], 2)
        self.assertEqual(result["features"][0]["properties"]["sources"], {"one": 1, "two": 1})

    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            cache = MapItCache(path)
            cache.set_point(Position(49.1, 8.2), {"id": 8, "name": "Town", "type": "O08"})
            cache.set_area_geometry(8, {"type": "Polygon", "coordinates": []})
            cache.save()
            loaded = MapItCache(path)
            self.assertEqual(loaded.get_point(Position(49.1, 8.2))["id"], 8)
            self.assertEqual(loaded.get_area_geometry("8")["type"], "Polygon")


if __name__ == "__main__":
    unittest.main()
