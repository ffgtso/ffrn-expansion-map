#!/usr/bin/env python3
"""Generate administrative-area GeoJSON from one or more Freifunk node feeds."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger("ffrn-expansion-map")
DEFAULT_MAPIT_URL = "https://global.mapit.mysociety.org/"
DEFAULT_AREA_TYPES = ("O08", "O07", "O06")
SUPPORTED_FORMATS = ("auto", "meshviewer", "hopglass", "ffmap", "nodelist")
USER_AGENT = "ffrn-expansion-map/2 (+https://github.com/ffgtso/ffrn-expansion-map)"


class ExpansionMapError(RuntimeError):
    """Raised for expected input/network/configuration errors."""


@dataclass(frozen=True, slots=True)
class Position:
    lat: float
    lng: float


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    url: str
    format: str = "auto"


@dataclass(frozen=True, slots=True)
class Node:
    node_id: str
    position: Position
    source: str


class JsonHttpClient:
    def __init__(self, timeout: float = 20.0, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries

    def get_json(self, location: str) -> Any:
        parsed = urlparse(location)
        if parsed.scheme in ("", "file"):
            path = Path(parsed.path if parsed.scheme == "file" else location)
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ExpansionMapError(f"Cannot read JSON from {location}: {exc}") from exc

        request = Request(
            location,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - user-provided feeds are intentional
                    charset = response.headers.get_content_charset() or "utf-8"
                    return json.loads(response.read().decode(charset))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.5 * (2**attempt))

        raise ExpansionMapError(f"Cannot fetch JSON from {location}: {last_error}")


class MapItCache:
    VERSION = 1

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data: dict[str, Any] = {"version": self.VERSION, "points": {}, "areas": {}}
        self.dirty = False
        if path and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if loaded.get("version") == self.VERSION:
                    self.data = loaded
                else:
                    LOG.warning("Ignoring incompatible cache version in %s", path)
            except (OSError, json.JSONDecodeError) as exc:
                LOG.warning("Ignoring unreadable cache %s: %s", path, exc)

    @staticmethod
    def point_key(position: Position) -> str:
        return f"{position.lat:.6f},{position.lng:.6f}"

    def get_point(self, position: Position) -> Mapping[str, Any] | None:
        value = self.data["points"].get(self.point_key(position))
        return value if isinstance(value, Mapping) else None

    def set_point(self, position: Position, area: Mapping[str, Any]) -> None:
        self.data["points"][self.point_key(position)] = dict(area)
        self.dirty = True

    def get_area_geometry(self, area_id: str) -> Mapping[str, Any] | None:
        value = self.data["areas"].get(str(area_id))
        return value if isinstance(value, Mapping) else None

    def set_area_geometry(self, area_id: str, geometry: Mapping[str, Any]) -> None:
        self.data["areas"][str(area_id)] = dict(geometry)
        self.dirty = True

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False


class MapItClient:
    def __init__(
        self,
        http: JsonHttpClient,
        base_url: str,
        area_types: Sequence[str],
        cache: MapItCache,
        request_delay: float = 0.1,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/") + "/"
        self.area_types = tuple(area_types)
        self.cache = cache
        self.request_delay = max(0.0, request_delay)
        self._last_request = 0.0

    def _get_json(self, url: str) -> Any:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        result = self.http.get_json(url)
        self._last_request = time.monotonic()
        return result

    def resolve_area(self, position: Position) -> Mapping[str, Any] | None:
        cached = self.cache.get_point(position)
        if cached is not None:
            return cached

        endpoint = f"point/4326/{position.lng:.7f},{position.lat:.7f}"
        document = self._get_json(urljoin(self.base_url, endpoint))
        if not isinstance(document, Mapping):
            raise ExpansionMapError("MapIt point response is not a JSON object")

        candidates = [v for v in document.values() if isinstance(v, Mapping)]
        area = choose_area(candidates, self.area_types)
        if area is not None:
            self.cache.set_point(position, area)
        return area

    def area_geometry(self, area_id: str | int) -> Mapping[str, Any]:
        key = str(area_id)
        cached = self.cache.get_area_geometry(key)
        if cached is not None:
            return cached

        document = self._get_json(urljoin(self.base_url, f"area/{key}.geojson"))
        if not isinstance(document, Mapping) or document.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ExpansionMapError(f"MapIt area {key} returned no Polygon/MultiPolygon geometry")
        geometry = {"type": document["type"], "coordinates": document.get("coordinates", [])}
        self.cache.set_area_geometry(key, geometry)
        return geometry


def choose_area(areas: Iterable[Mapping[str, Any]], area_types: Sequence[str]) -> Mapping[str, Any] | None:
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for area in areas:
        area_type = str(area.get("type", ""))
        by_type.setdefault(area_type, []).append(area)

    for area_type in area_types:
        matches = by_type.get(area_type)
        if matches:
            return matches[0]
    return None


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _valid_position(lat: Any, lng: Any) -> Position | None:
    latitude = _as_float(lat)
    longitude = _as_float(lng)
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return Position(latitude, longitude)


def _node_records(document: Mapping[str, Any]) -> Iterable[tuple[str | None, Mapping[str, Any]]]:
    nodes = document.get("nodes")
    if isinstance(nodes, Mapping):
        for key, value in nodes.items():
            if isinstance(value, Mapping):
                yield str(key), value
    elif isinstance(nodes, list):
        for value in nodes:
            if isinstance(value, Mapping):
                yield None, value
    else:
        raise ExpansionMapError("JSON does not contain a 'nodes' object or array")


def detect_format(document: Mapping[str, Any]) -> str:
    for _, node in _node_records(document):
        nodeinfo = node.get("nodeinfo")
        if isinstance(nodeinfo, Mapping) and isinstance(nodeinfo.get("location"), Mapping):
            return "meshviewer"
        if isinstance(node.get("geo"), (list, tuple)):
            return "ffmap"
        if isinstance(node.get("position"), Mapping):
            return "nodelist"
    raise ExpansionMapError("Cannot auto-detect node format")


def parse_nodes(document: Any, source: Source) -> list[Node]:
    if not isinstance(document, Mapping):
        raise ExpansionMapError(f"Source {source.name!r} is not a JSON object")

    fmt = source.format
    if fmt == "auto":
        fmt = detect_format(document)
    if fmt == "hopglass":
        fmt = "meshviewer"
    if fmt not in {"meshviewer", "ffmap", "nodelist"}:
        raise ExpansionMapError(f"Unsupported format {source.format!r}")

    parsed: list[Node] = []
    skipped = 0
    for index, (mapping_key, record) in enumerate(_node_records(document)):
        position: Position | None = None
        node_id: Any = record.get("id") or record.get("node_id") or mapping_key

        if fmt == "meshviewer":
            nodeinfo = record.get("nodeinfo")
            if isinstance(nodeinfo, Mapping):
                location = nodeinfo.get("location")
                if isinstance(location, Mapping):
                    position = _valid_position(location.get("latitude"), location.get("longitude"))
                node_id = nodeinfo.get("node_id") or nodeinfo.get("id") or node_id
        elif fmt == "ffmap":
            geo = record.get("geo")
            if isinstance(geo, (list, tuple)) and len(geo) >= 2:
                position = _valid_position(geo[0], geo[1])
        elif fmt == "nodelist":
            pos = record.get("position")
            if isinstance(pos, Mapping):
                position = _valid_position(pos.get("lat"), pos.get("long", pos.get("lng")))

        if position is None:
            skipped += 1
            continue
        if node_id in (None, ""):
            node_id = f"{source.name}:{index}"
        parsed.append(Node(str(node_id), position, source.name))

    if skipped:
        LOG.info("%s: skipped %d node(s) without a valid position", source.name, skipped)
    LOG.info("%s: loaded %d positioned node(s) as %s", source.name, len(parsed), fmt)
    return parsed


def merge_nodes(node_lists: Iterable[Iterable[Node]]) -> list[Node]:
    merged: dict[str, Node] = {}
    conflicts = 0
    duplicates = 0
    for nodes in node_lists:
        for node in nodes:
            previous = merged.get(node.node_id)
            if previous is None:
                merged[node.node_id] = node
                continue
            duplicates += 1
            if previous.position != node.position:
                conflicts += 1
                LOG.warning(
                    "Duplicate node id %s has different positions (%s vs %s); keeping first occurrence from %s",
                    node.node_id,
                    previous.position,
                    node.position,
                    previous.source,
                )
    if duplicates:
        LOG.info("Deduplicated %d repeated node id(s); %d coordinate conflict(s)", duplicates, conflicts)
    return list(merged.values())


def build_geojson(
    nodes: Sequence[Node],
    mapit: MapItClient,
    source_urls: Mapping[str, str],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    distribution: dict[str, dict[str, Any]] = {}
    unresolved = 0

    for number, node in enumerate(nodes, start=1):
        try:
            area = mapit.resolve_area(node.position)
        except ExpansionMapError as exc:
            if strict:
                raise
            unresolved += 1
            LOG.warning("MapIt lookup failed for %s at %s: %s", node.node_id, node.position, exc)
            continue

        if area is None:
            unresolved += 1
            LOG.warning("No configured administrative area found for %s at %s", node.node_id, node.position)
            continue

        area_id = str(area.get("id", ""))
        if not area_id:
            unresolved += 1
            LOG.warning("MapIt area without id for %s", node.node_id)
            continue

        item = distribution.setdefault(
            area_id,
            {
                "name": str(area.get("name") or area_id),
                "type": str(area.get("type") or ""),
                "count": 0,
                "sources": Counter(),
            },
        )
        item["count"] += 1
        item["sources"][node.source] += 1

        if number % 100 == 0:
            LOG.info("Resolved %d/%d nodes", number, len(nodes))

    features: list[dict[str, Any]] = []
    for area_id, data in sorted(distribution.items(), key=lambda item: item[1]["name"].casefold()):
        try:
            geometry = mapit.area_geometry(area_id)
        except ExpansionMapError as exc:
            if strict:
                raise
            LOG.warning("Skipping area %s because geometry lookup failed: %s", area_id, exc)
            continue

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "area_id": area_id,
                    "area_type": data["type"],
                    "name": data["name"],
                    "count": data["count"],
                    "sources": dict(sorted(data["sources"].items())),
                },
                "geometry": geometry,
            }
        )

    return {
        "type": "FeatureCollection",
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "node_count": len(nodes),
            "unresolved_nodes": unresolved,
            "area_count": len(features),
            "sources": [{"name": name, "url": url} for name, url in source_urls.items()],
        },
        "features": features,
    }


def source_name_from_url(url: str, fallback_index: int) -> str:
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        name = parsed.hostname or f"source-{fallback_index}"
        return name.removeprefix("www.")
    stem = Path(parsed.path or url).stem
    return stem or f"source-{fallback_index}"


def parse_named_source(value: str, fallback_index: int) -> Source:
    if "=" not in value:
        return Source(source_name_from_url(value, fallback_index), value)
    name, url = value.split("=", 1)
    if not name.strip() or not url.strip():
        raise ExpansionMapError(f"Invalid --source value: {value!r}; expected NAME=URL")
    return Source(name.strip(), url.strip())


def load_config(path: Path) -> tuple[list[Source], dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExpansionMapError(f"Cannot read config {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ExpansionMapError("Config root must be a JSON object")

    sources: list[Source] = []
    raw_sources = raw.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ExpansionMapError("Config 'sources' must be an array")

    for index, item in enumerate(raw_sources, start=1):
        if isinstance(item, str):
            sources.append(Source(source_name_from_url(item, index), item))
        elif isinstance(item, Mapping):
            url = str(item.get("url", "")).strip()
            if not url:
                raise ExpansionMapError(f"Config source #{index} has no URL")
            name = str(item.get("name") or source_name_from_url(url, index)).strip()
            fmt = str(item.get("format", "auto")).strip().lower()
            if fmt not in SUPPORTED_FORMATS:
                raise ExpansionMapError(f"Config source {name!r} has unsupported format {fmt!r}")
            sources.append(Source(name, url, fmt))
        else:
            raise ExpansionMapError(f"Config source #{index} must be a URL string or object")
    return sources, dict(raw)


def write_geojson(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build administrative-area GeoJSON from Hopglass/Meshviewer node feeds.",
    )
    parser.add_argument("urls", nargs="*", metavar="URL", help="Node JSON URL/path (repeatable)")
    parser.add_argument("-c", "--config", type=Path, help="JSON config file containing a sources list")
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="Named source; may be specified multiple times",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=SUPPORTED_FORMATS,
        default="auto",
        help="Format for positional URL arguments (default: auto)",
    )
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output GeoJSON (default: nodes.geojson)")
    parser.add_argument("--mapit-url", default=None, help=f"MapIt base URL (default: {DEFAULT_MAPIT_URL})")
    parser.add_argument(
        "--area-types",
        default=None,
        help="Comma-separated MapIt area types, most preferred first (default: O08,O07,O06)",
    )
    parser.add_argument("--cache", type=Path, default=None, help="Persistent MapIt cache (default: .cache/mapit.json)")
    parser.add_argument("--no-cache", action="store_true", help="Disable persistent MapIt cache")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds (default: 20)")
    parser.add_argument("--retries", type=int, default=2, help="HTTP retries after the first attempt (default: 2)")
    parser.add_argument(
        "--request-delay",
        type=float,
        default=None,
        help="Minimum delay between MapIt requests in seconds (default: 0.1)",
    )
    parser.add_argument("--keep-going", action="store_true", help="Skip node feeds that cannot be loaded")
    parser.add_argument("--strict", action="store_true", help="Abort on unresolved MapIt nodes/geometries")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase logging verbosity")
    return parser


def run(args: argparse.Namespace) -> int:
    config_sources: list[Source] = []
    config: dict[str, Any] = {}
    if args.config:
        config_sources, config = load_config(args.config)

    sources = list(config_sources)
    for index, value in enumerate(args.source, start=len(sources) + 1):
        sources.append(parse_named_source(value, index))
    for index, url in enumerate(args.urls, start=len(sources) + 1):
        sources.append(Source(source_name_from_url(url, index), url, args.format))

    if not sources:
        raise ExpansionMapError("No node feeds configured. Add URLs, --source, or --config.")

    # Detect accidental duplicate display names because they would collapse source statistics.
    names = [source.name for source in sources]
    duplicate_names = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicate_names:
        raise ExpansionMapError(f"Source names must be unique: {', '.join(duplicate_names)}")

    output = args.output or Path(str(config.get("output", "nodes.geojson")))
    mapit_url = args.mapit_url or str(config.get("mapit_url", DEFAULT_MAPIT_URL))
    area_types_raw = args.area_types or config.get("area_types") or DEFAULT_AREA_TYPES
    if isinstance(area_types_raw, str):
        area_types = tuple(value.strip() for value in area_types_raw.split(",") if value.strip())
    elif isinstance(area_types_raw, (list, tuple)):
        area_types = tuple(str(value).strip() for value in area_types_raw if str(value).strip())
    else:
        raise ExpansionMapError("area_types must be a comma-separated string or JSON array")
    if not area_types:
        raise ExpansionMapError("At least one MapIt area type is required")

    request_delay = args.request_delay
    if request_delay is None:
        request_delay = float(config.get("request_delay", 0.1))

    cache_path: Path | None
    if args.no_cache:
        cache_path = None
    elif args.cache:
        cache_path = args.cache
    else:
        configured_cache = config.get("cache", ".cache/mapit.json")
        cache_path = Path(str(configured_cache)) if configured_cache else None

    http = JsonHttpClient(timeout=args.timeout, retries=max(0, args.retries))
    parsed_lists: list[list[Node]] = []
    source_urls: dict[str, str] = {}
    for source in sources:
        try:
            document = http.get_json(source.url)
            parsed_lists.append(parse_nodes(document, source))
            source_urls[source.name] = source.url
        except ExpansionMapError as exc:
            if not args.keep_going:
                raise
            LOG.error("Skipping source %s: %s", source.name, exc)

    nodes = merge_nodes(parsed_lists)
    if not nodes:
        raise ExpansionMapError("No positioned nodes were loaded from the configured feeds")

    cache = MapItCache(cache_path)
    mapit = MapItClient(http, mapit_url, area_types, cache, request_delay)
    try:
        geojson = build_geojson(nodes, mapit, source_urls, strict=args.strict)
        write_geojson(output, geojson)
    finally:
        cache.save()

    metadata = geojson["metadata"]
    LOG.info(
        "Wrote %s: %d nodes, %d areas, %d unresolved",
        output,
        metadata["node_count"],
        metadata["area_count"],
        metadata["unresolved_nodes"],
    )
    return 0


def cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = logging.DEBUG if args.verbose >= 2 else logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")
    try:
        return run(args)
    except ExpansionMapError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    sys.exit(cli())
