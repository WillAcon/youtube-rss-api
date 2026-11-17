#!/usr/bin/env python3
"""HTTP API for the self-built YouTube feed system.

Runs behind nginx at /yt/api/ on 127.0.0.1. Every request needs the shared
token: ?token=..., X-API-Token: ... or Authorization: Bearer ...

Endpoints
  GET    /yt/api/health
  GET    /yt/api/channels[?videos=1]
  POST   /yt/api/channels            single object or array (batch)
  GET    /yt/api/channels/<key>      key = slug | @handle | channel id | name
  PATCH  /yt/api/channels/<key>
  PATCH  /yt/api/channels            array of {key, ...fields} (batch)
  DELETE /yt/api/channels/<key>
  DELETE /yt/api/channels?keys=a,b   or body {"keys": [...]} (batch)
  GET    /yt/api/channels/<key>/videos[?live=1&limit=20]
  GET    /yt/api/channels/<key>/<kind>[?live=1&limit=20]
                                     kind = videos | shorts | streams

Every route is also served under /api (e.g. /api/health, /api/channels,
/api/channel/<key>/<kind>). /channel/<key>/<kind> remains as a legacy alias.
  GET    /yt/api/updates?since=ISO&channel=<slug>&kind=videos&limit=50
  GET    /yt/api/resolve?q=<name>    search YouTube for a channel URL
  POST   /yt/api/check               {"channels": [...]} optional; refreshes and
                                     reports newly seen videos
"""

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate as gen  # noqa: E402

TOKEN = os.environ.get("YT_API_TOKEN", "")
if not TOKEN:
    sys.exit("YT_API_TOKEN is required (set it in the environment or a systemd unit)")
# Render/Heroku-style platforms inject PORT and expect a 0.0.0.0 bind.
HOST = os.environ.get("YT_API_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
PORT = int(os.environ.get("YT_API_PORT") or os.environ.get("PORT") or "8099")
# Comma-separated list; "*" (default) allows any origin. Empty disables CORS.
CORS_ORIGINS = [
    item.strip()
    for item in os.environ.get("YT_CORS_ORIGINS", "*").split(",")
    if item.strip()
]
SERVE_FEEDS = os.environ.get("YT_SERVE_FEEDS", "0").strip().lower() not in (
    "0",
    "",
    "false",
    "no",
    "off",
)
REFRESH_INTERVAL = int(os.environ.get("YT_REFRESH_INTERVAL", "0") or 0)
LOCK = threading.Lock()
STARTED = time.time()
LIMIT_DEFAULT = 20
LIMIT_MAX = 100
ADHOC_TTL = 3600
ADHOC = {}
ADHOC_LOCK = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc)


def env_flag(name, default="0"):
    return os.environ.get(name, default).strip().lower() not in (
        "0",
        "",
        "false",
        "no",
        "off",
    )


def cors_allow_origin(origin):
    """Origin to echo in Access-Control-Allow-Origin, or None for no CORS."""
    if not CORS_ORIGINS:
        return None
    if "*" in CORS_ORIGINS:
        return "*"
    if origin and origin in CORS_ORIGINS:
        return origin
    return None


def redact_token(text):
    """Keep ?token=... out of the logs (it is a shared secret)."""
    return re.sub(r"([?&]token=)[^&\s]*", r"\1***", text or "")


def feed_file(relative):
    """Map a /feeds/<name> request to a file inside feeds/, or None."""
    name = unquote(relative or "")
    if "/" in name or "\\" in name:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.xml", name):
        return None
    return os.path.join(gen.OUT_DIR, name)


def clamp_limit(raw, default=LIMIT_DEFAULT):
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"limit must be an integer, got {raw!r}")
    return max(1, min(LIMIT_MAX, value))


def clamp_kind(raw):
    kind = (raw or "videos").strip().lower()
    if kind not in gen.KINDS:
        raise ValueError(f"kind must be one of {', '.join(gen.KINDS)}, got {raw!r}")
    return kind


def normalize_kinds(value):
    """Validate a channel `kinds` list (["videos", "shorts"]). None clears it."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("kinds must be a list such as [\"videos\", \"shorts\"]")
    picked = []
    for item in value:
        kind = str(item).strip().lower()
        if kind:
            picked.append(clamp_kind(kind))
    return picked or None


def normalize_lang(value):
    """Validate a channel `lang` tag ("es", "es-PE"). None clears it."""
    if value is None:
        return None
    lang = str(value).strip().lower()
    if not re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", lang):
        raise ValueError(f"lang must look like 'es' or 'es-PE', got {value!r}")
    return lang


# ------------------------------------------------------------------- lookups


def load_channels():
    return gen.load_json(gen.CHANNELS_PATH, [])


def find_channel(channels, key):
    key = unquote(key or "").strip()
    if not key:
        return None
    low = key.lower()
    bare = low.lstrip("@")
    for channel in channels:
        if (
            channel["slug"].lower() == low
            or channel["cid"] == key
            or channel["handle"].lstrip("@").lower() == bare
        ):
            return channel
    for channel in channels:
        if channel["name"].lower() == low:
            return channel
    matches = [channel for channel in channels if low in channel["name"].lower()]
    return matches[0] if len(matches) == 1 else None


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "channel"


def unique_slug(channels, base):
    used = {channel["slug"] for channel in channels}
    slug, n = base, 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    return slug


def resolve_target(key):
    """Find a tracked channel, else resolve the key on YouTube without saving it.

    Returns (channel, on_the_fly). On-the-fly channels are kept in memory so
    polling /channel/@handle/shorts does not re-resolve the handle every time.
    """
    channel = find_channel(load_channels(), key)
    if channel:
        return channel, False

    key = unquote(key or "").strip()
    if not key:
        return None, False
    with ADHOC_LOCK:
        cached = ADHOC.get(key.lower())
        if cached and time.time() - cached[1] < ADHOC_TTL:
            return cached[0], True
    try:
        cid, handle, name = gen.channel_meta(key)
    except Exception:  # noqa: BLE001 - unknown handle, id or name
        return None, False
    channel = {
        "slug": slugify(handle or name or cid),
        "name": (name or handle or cid).strip(),
        "handle": (handle or f"/channel/{cid}").strip(),
        "cid": cid,
    }
    with ADHOC_LOCK:
        if len(ADHOC) >= 512:
            ADHOC.clear()
        ADHOC[key.lower()] = (channel, time.time())
    return channel, True


def video_public(channel, item):
    return {
        "channel": channel["slug"],
        "kind": item.get("kind", "videos"),
        "id": item["id"],
        "title": item["title"],
        "link": item["link"],
        "thumbnail": item.get("thumb", ""),
        "first_seen": item["date"].isoformat(),
        "meta": item.get("meta", []),
    }


def channel_public(channel, with_videos=False):
    items = gen.load_cache(channel["slug"])
    counts = {
        kind: len(gen.load_cache(channel["slug"], kind))
        for kind in gen.KINDS
        if os.path.exists(gen.cache_path(channel["slug"], kind))
    }
    payload = {
        "slug": channel["slug"],
        "name": channel["name"],
        "handle": channel["handle"],
        "cid": channel["cid"],
        "youtube_url": f"https://www.youtube.com/{channel['handle']}",
        "feed_url": f"{gen.PUBLIC_BASE}/{channel['slug']}.xml",
        "items": len(items),
        "counts": counts,
        "latest": video_public(channel, items[0]) if items else None,
    }
    if channel.get("kinds"):
        payload["kinds"] = channel["kinds"]
    if channel.get("lang"):
        payload["lang"] = channel["lang"]
    if with_videos:
        payload["videos"] = [video_public(channel, item) for item in items]
    return payload


# ---------------------------------------------------------------- mutations


def add_one(spec, channels):
    """Resolve and append one channel spec. Returns (status, channel|error)."""
    if not isinstance(spec, dict):
        return "error", "channel spec must be an object"
    try:
        kinds = normalize_kinds(spec.get("kinds"))
        lang = normalize_lang(spec.get("lang"))
    except ValueError as exc:
        return "error", str(exc)

    reference = spec.get("cid") or spec.get("url") or spec.get("handle")
    resolved_from = None
    if not reference:
        name = (spec.get("name") or "").strip()
        if not name:
            return "error", "need cid, url, handle or name"
        candidates = gen.search_channels(name, limit=1)
        if not candidates:
            return "error", f"no YouTube channel found for {name!r}"
        reference = candidates[0]["url"]
        resolved_from = candidates[0]

    try:
        cid, handle, name = gen.channel_meta(reference)
    except Exception as exc:  # noqa: BLE001
        return "error", f"resolve failed: {exc}"

    for existing in channels:
        if existing["cid"] == cid:
            return "exists", existing

    channel = {
        "slug": unique_slug(channels, slugify(spec.get("slug") or spec.get("handle") or name)),
        "name": (spec.get("name") or name or handle or cid).strip(),
        "handle": (spec.get("handle") or handle or "").strip(),
        "cid": cid,
    }
    if not channel["handle"]:
        channel["handle"] = f"/channel/{cid}"
    if kinds:
        channel["kinds"] = kinds
    if lang:
        channel["lang"] = lang
    channels.append(channel)
    result = dict(channel)
    if resolved_from:
        result["resolved_from"] = resolved_from
    return "created", result


def update_one(key, patch, channels):
    channel = find_channel(channels, key)
    if not channel:
        return "missing", f"no channel matches {key!r}"
    if not isinstance(patch, dict):
        return "error", "patch must be an object"

    old_slug = channel["slug"]
    touched = False
    if "kinds" in patch:
        try:
            kinds = normalize_kinds(patch["kinds"])
        except ValueError as exc:
            return "error", str(exc)
        if kinds:
            channel["kinds"] = kinds
        else:
            channel.pop("kinds", None)
        touched = True
    if "lang" in patch:
        try:
            lang = normalize_lang(patch["lang"])
        except ValueError as exc:
            return "error", str(exc)
        if lang:
            channel["lang"] = lang
        else:
            channel.pop("lang", None)
        touched = True

    for field in ("name", "slug"):
        if field in patch and patch[field]:
            value = str(patch[field]).strip()
            if field == "slug":
                value = slugify(value)
                others = [c for c in channels if c is not channel]
                if any(c["slug"] == value for c in others):
                    return "error", f"slug {value!r} already in use"
            channel[field] = value
            touched = True

    reference = patch.get("url") or patch.get("handle") or patch.get("cid")
    if reference:
        try:
            cid, handle, name = gen.channel_meta(reference)
        except Exception as exc:  # noqa: BLE001
            return "error", f"resolve failed: {exc}"
        if cid != channel["cid"]:
            clash = next((c for c in channels if c["cid"] == cid and c is not channel), None)
            if clash:
                return "error", f"channel id already tracked as {clash['slug']!r}"
        channel["cid"] = cid
        channel["handle"] = (patch.get("handle") or handle or channel["handle"]).strip()
        if not patch.get("name"):
            channel["name"] = name or channel["name"]
        touched = True

    if channel["slug"] != old_slug:
        move_artifacts(old_slug, channel["slug"])

    return ("updated", channel) if touched else ("unchanged", channel)


def move_artifacts(old_slug, new_slug):
    """Keep every feed and cache attached to the channel when its slug changes."""
    for kind in gen.KINDS:
        for build_path in (gen.feed_path, gen.cache_path):
            source = build_path(old_slug, kind)
            target = build_path(new_slug, kind)
            if os.path.exists(source):
                os.replace(source, target)


def delete_one(key, channels):
    channel = find_channel(channels, key)
    if not channel:
        return "missing", f"no channel matches {key!r}"
    channels.remove(channel)
    for kind in gen.KINDS:
        for path in (gen.feed_path(channel["slug"], kind), gen.cache_path(channel["slug"], kind)):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    return "deleted", channel


def prune_state(state, channels):
    keep = {channel["slug"] for channel in channels}
    return {video_id: entry for video_id, entry in state.items() if entry.get("c") in keep}


# ------------------------------------------------------------------- actions


def run_check(keys=None, kinds=None):
    channels = load_channels()
    targets = channels
    missing = []
    if keys:
        targets, seen = [], set()
        for key in keys:
            channel = find_channel(channels, key)
            if not channel:
                missing.append(key)
            elif channel["slug"] not in seen:
                seen.add(channel["slug"])
                targets.append(channel)

    state = gen.load_json(gen.STATE_PATH, {})
    moment = utcnow()
    report = []
    for channel in targets:
        for kind in kinds or gen.channel_kinds(channel):
            if report:
                time.sleep(1.5)
            try:
                items, new_ids = gen.refresh_channel(channel, state, moment, kind)
                gen.save_json(gen.STATE_PATH, state)
                report.append(
                    {
                        "channel": channel["slug"],
                        "kind": kind,
                        "ok": True,
                        "checked": len(items),
                        "new": [video_public(channel, i) for i in items if i["id"] in new_ids],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                report.append(
                    {"channel": channel["slug"], "kind": kind, "ok": False, "error": str(exc)}
                )

    gen.write_aggregate(channels)
    return {"checked": len(report), "report": report, "unknown": missing}


def collect_updates(since_iso=None, slug=None, limit=50, kind=None):
    channels = load_channels()
    since = None
    if since_iso:
        since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    merged = []
    for channel in channels:
        if slug and channel["slug"] != slug:
            continue
        for item_kind in ([kind] if kind else gen.KINDS):
            for item in gen.load_cache(channel["slug"], item_kind):
                if since and item["date"] < since:
                    continue
                merged.append(video_public(channel, item))
    merged.sort(key=lambda entry: entry["first_seen"], reverse=True)
    return merged[:limit]


# -------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server_version = "yt-feeds-api/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        gen.log(redact_token(f"api {self.address_string()} {fmt % args}"))

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        # CORS preflight never needs a token: the browser sends no headers yet.
        self.send_response(204)
        self._cors_headers(preflight=True)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- plumbing

    def _cors_headers(self, preflight=False):
        allowed = cors_allow_origin(self.headers.get("Origin"))
        if not allowed:
            return
        self.send_header("Access-Control-Allow-Origin", allowed)
        if allowed != "*":
            self.send_header("Vary", "Origin")
        if preflight:
            requested = self.headers.get("Access-Control-Request-Headers") or ""
            self.send_header(
                "Access-Control-Allow-Methods", "GET, HEAD, POST, PATCH, DELETE, OPTIONS"
            )
            self.send_header(
                "Access-Control-Allow-Headers",
                requested or "Content-Type, X-API-Token, Authorization",
            )
            self.send_header("Access-Control-Max-Age", "600")
        else:
            self.send_header("Access-Control-Expose-Headers", "Content-Length")

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_feed(self, path, method):
        """Serve feeds/*.xml when YT_SERVE_FEEDS=1 (single-port Docker/Render)."""
        if not SERVE_FEEDS or not path.startswith("/feeds/"):
            return False
        if method not in ("GET", "HEAD"):
            self._send(405, {"ok": False, "error": "method not allowed"})
            return True
        target = feed_file(path[len("/feeds/"):])
        if not target:
            self._send(404, {"ok": False, "error": "not found"})
            return True
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send(404, {"ok": False, "error": "feed not generated yet"})
            return True
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=300")
        self._cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return True

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("body is not valid JSON")

    def _authorized(self, query):
        supplied = (query.get("token") or [None])[0] or self.headers.get("X-API-Token") or ""
        if not supplied:
            header = self.headers.get("Authorization") or ""
            if header.lower().startswith("bearer "):
                supplied = header[7:].strip()
        return supplied == TOKEN

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        # HEAD behaves exactly like GET; _send skips the body.
        method = "GET" if method == "HEAD" else method

        if self._serve_feed(path, method):
            return

        if path.startswith("/yt/api"):
            rest = path[len("/yt/api"):]
        elif path.startswith("/api/channel/"):
            # /api/channel/@handle/shorts -> /channels/@handle/shorts
            rest = "/channels/" + path[len("/api/channel/"):]
        elif path.startswith("/api/channels/"):
            rest = "/channels/" + path[len("/api/channels/"):]
        elif path == "/api" or path.startswith("/api/"):
            rest = path[len("/api"):]
        elif path.startswith("/channel/"):
            # Legacy alias kept for links shared before /api existed.
            rest = "/channels/" + path[len("/channel/"):]
        else:
            return self._send(404, {"ok": False, "error": "not found"})
        parts = [unquote(part) for part in rest.strip("/").split("/") if part]

        public_health = path.rstrip("/") in ("/api/health", "/yt/api/health")
        open_read = public_health or (
            env_flag("YT_PUBLIC_READ")
            and method == "GET"
            and (path.startswith("/channel/") or path.startswith("/api/channel/"))
        )
        if not open_read and not self._authorized(query):
            return self._send(401, {"ok": False, "error": "invalid or missing token"})

        try:
            return self._route(method, parts, query)
        except ValueError as exc:
            return self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            gen.log(f"api error {method} {path}: {exc}")
            return self._send(500, {"ok": False, "error": str(exc)})

    # -- routes

    def _route(self, method, parts, query):
        if not parts or parts[0] == "health":
            if method != "GET":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            channels = load_channels()
            return self._send(
                200,
                {
                    "ok": True,
                    "service": "yt-feeds",
                    "channels": len(channels),
                    "feeds_served": SERVE_FEEDS,
                    "uptime_seconds": round(time.time() - STARTED),
                },
            )

        if parts[0] == "resolve":
            if method != "GET":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            q = (query.get("q") or [""])[0].strip()
            if not q:
                raise ValueError("missing q")
            limit = int((query.get("limit") or ["8"])[0])
            found = gen.search_channels(q, limit=limit)
            channels = load_channels()
            tracked_by_cid = {channel["cid"]: channel["slug"] for channel in channels}
            for item in found:
                item["tracked_slug"] = tracked_by_cid.get(item["cid"])
            return self._send(
                200,
                {
                    "ok": True,
                    "query": q,
                    "results": found,
                    "already_tracked": sum(1 for item in found if item["tracked_slug"]),
                },
            )

        if parts[0] == "check":
            if method != "POST":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            body = self._body() or {}
            keys = body.get("channels")
            if isinstance(keys, str):
                keys = [keys]
            kinds = body.get("kinds") or body.get("kind")
            if isinstance(kinds, str):
                kinds = [kinds]
            if kinds:
                kinds = [clamp_kind(kind) for kind in kinds]
            with LOCK:
                result = run_check(keys or None, kinds or None)
            return self._send(200, {"ok": all(r["ok"] for r in result["report"]), **result})

        if parts[0] == "updates":
            if method != "GET":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            since = (query.get("since") or [None])[0]
            slug = (query.get("channel") or [None])[0]
            kind = (query.get("kind") or [None])[0]
            kind = clamp_kind(kind) if kind else None
            limit = clamp_limit((query.get("limit") or ["50"])[0], default=50)
            updates = collect_updates(since, slug, limit, kind)
            return self._send(
                200, {"ok": True, "since": since, "count": len(updates), "updates": updates}
            )

        if parts[0] != "channels":
            return self._send(404, {"ok": False, "error": "unknown endpoint"})

        # /channels...
        if len(parts) == 1:
            if method == "GET":
                with_videos = (query.get("videos") or ["0"])[0] not in ("0", "", "false")
                return self._send(
                    200,
                    {
                        "ok": True,
                        "count": len(load_channels()),
                        "channels": [channel_public(c, with_videos) for c in load_channels()],
                    },
                )
            if method == "POST":
                return self._add(self._body())
            if method == "PATCH":
                return self._batch_update(self._body())
            if method == "DELETE":
                return self._batch_delete(query)
            return self._send(405, {"ok": False, "error": "method not allowed"})

        key = parts[1]
        if len(parts) == 3 and parts[2] in gen.KINDS:
            if method != "GET":
                return self._send(405, {"ok": False, "error": "method not allowed"})
            return self._videos(key, query, parts[2])
        if len(parts) == 3:
            raise ValueError(f"kind must be one of {', '.join(gen.KINDS)}, got {parts[2]!r}")
        if len(parts) > 2:
            return self._send(404, {"ok": False, "error": "unknown endpoint"})

        if method == "GET":
            channel, ad_hoc = resolve_target(key)
            if not channel:
                return self._send(404, {"ok": False, "error": f"no channel matches {key!r}"})
            payload = channel_public(channel, True)
            payload["tracked"] = not ad_hoc
            return self._send(200, {"ok": True, "channel": payload})
        if method == "PATCH":
            return self._update(key, self._body())
        if method == "DELETE":
            return self._delete(key)
        return self._send(405, {"ok": False, "error": "method not allowed"})

    # -- handlers

    def _add(self, body):
        specs = body if isinstance(body, list) else (body or {}).get("channels")
        if specs is None:
            specs = [body] if isinstance(body, dict) else None
        if not specs:
            raise ValueError("expected a channel object, an array, or {channels: [...]}")

        with LOCK:
            channels = load_channels()
            results = []
            for spec in specs:
                status, payload = add_one(spec, channels)
                results.append(
                    {
                        "status": status,
                        "channel": payload if status in ("created", "exists") else None,
                        "error": payload if status == "error" else None,
                    }
                )
            gen.save_json(gen.CHANNELS_PATH, channels)

        ok = all(item["status"] != "error" for item in results)
        return self._send(
            200 if ok else 207,
            {"ok": ok, "count": len(results), "results": results},
        )

    def _update(self, key, body):
        with LOCK:
            channels = load_channels()
            status, payload = update_one(key, body, channels)
            if status not in ("updated", "unchanged"):
                code = 404 if status == "missing" else 400
                return self._send(code, {"ok": False, "error": payload})
            gen.save_json(gen.CHANNELS_PATH, channels)
            gen.write_aggregate(channels)
        return self._send(
            200, {"ok": True, "status": status, "channel": channel_public(payload)}
        )

    def _batch_update(self, body):
        items = body if isinstance(body, list) else (body or {}).get("channels")
        if not isinstance(items, list) or not items:
            raise ValueError("expected an array of {key, ...fields}")
        results = []
        with LOCK:
            channels = load_channels()
            for item in items:
                key = item.get("key") or item.get("slug") or item.get("cid")
                status, payload = update_one(key, item, channels)
                results.append(
                    {
                        "key": key,
                        "status": status,
                        "channel": payload if status in ("updated", "unchanged") else None,
                        "error": payload if status not in ("updated", "unchanged") else None,
                    }
                )
            gen.save_json(gen.CHANNELS_PATH, channels)
            gen.write_aggregate(channels)
        ok = all(item["status"] in ("updated", "unchanged") for item in results)
        return self._send(200 if ok else 207, {"ok": ok, "results": results})

    def _delete(self, key):
        with LOCK:
            channels = load_channels()
            status, payload = delete_one(key, channels)
            if status != "deleted":
                return self._send(404, {"ok": False, "error": payload})
            gen.save_json(gen.CHANNELS_PATH, channels)
            state = prune_state(gen.load_json(gen.STATE_PATH, {}), channels)
            gen.save_json(gen.STATE_PATH, state)
            gen.write_aggregate(channels)
        return self._send(
            200,
            {
                "ok": True,
                "status": "deleted",
                "channel": {"slug": payload["slug"], "name": payload["name"]},
            },
        )

    def _batch_delete(self, query):
        keys = query.get("keys") or []
        if len(keys) == 1 and "," in keys[0]:
            keys = [part.strip() for part in keys[0].split(",") if part.strip()]
        if not keys:
            body = self._body() or {}
            keys = body.get("keys") or []
            if isinstance(keys, str):
                keys = [keys]
        if not keys:
            raise ValueError("expected ?keys=a,b or {\"keys\": [...]}")

        results = []
        with LOCK:
            channels = load_channels()
            for key in keys:
                status, payload = delete_one(key, channels)
                results.append(
                    {
                        "key": key,
                        "status": status,
                        "slug": payload["slug"] if status == "deleted" else None,
                        "error": payload if status != "deleted" else None,
                    }
                )
            gen.save_json(gen.CHANNELS_PATH, channels)
            state = prune_state(gen.load_json(gen.STATE_PATH, {}), channels)
            gen.save_json(gen.STATE_PATH, state)
            gen.write_aggregate(channels)
        ok = all(item["status"] == "deleted" for item in results)
        return self._send(200 if ok else 207, {"ok": ok, "results": results})

    def _videos(self, key, query, kind="videos"):
        kind = clamp_kind((query.get("kind") or [kind])[0])
        channel, ad_hoc = resolve_target(key)
        if not channel:
            return self._send(404, {"ok": False, "error": f"no channel matches {key!r}"})
        # An untracked channel has no cache yet, so fetch it unless live=0.
        live = (query.get("live") or ["1" if ad_hoc else "0"])[0] not in ("0", "", "false")
        limit = clamp_limit((query.get("limit") or [str(LIMIT_DEFAULT)])[0])

        items = None
        if live:
            with LOCK:
                state = gen.load_json(gen.STATE_PATH, {})
                try:
                    items, _ = gen.refresh_channel(channel, state, utcnow(), kind)
                    gen.save_json(gen.STATE_PATH, state)
                    if not ad_hoc:
                        gen.write_aggregate(load_channels())
                except Exception as exc:  # noqa: BLE001
                    return self._send(502, {"ok": False, "error": f"fetch failed: {exc}"})
        if items is None:
            items = gen.load_cache(channel["slug"], kind)
        return self._send(
            200,
            {
                "ok": True,
                "channel": channel["slug"],
                "handle": channel["handle"],
                "kind": kind,
                "tracked": not ad_hoc,
                "live": live,
                "count": len(items),
                "returned": min(limit, len(items)),
                "feed_url": f"{gen.PUBLIC_BASE}/{gen.artifact_name(channel['slug'], kind, '.xml')}",
                "videos": [video_public(channel, item) for item in items[:limit]],
            },
        )


def refresh_loop(interval):
    """Re-harvest every channel periodically (containers have no cron)."""
    while True:
        time.sleep(interval)
        if not LOCK.acquire(blocking=False):
            continue  # an on-demand check is already running
        try:
            result = run_check()
            ok = sum(1 for item in result["report"] if item["ok"])
            gen.log(f"auto refresh: {ok}/{len(result['report'])} fetches ok")
        except Exception as exc:  # noqa: BLE001 - keep the thread alive
            gen.log(f"auto refresh failed: {exc}")
        finally:
            LOCK.release()


def main():
    if REFRESH_INTERVAL > 0:
        threading.Thread(
            target=refresh_loop, args=(REFRESH_INTERVAL,), daemon=True
        ).start()
        gen.log(f"auto refresh every {REFRESH_INTERVAL}s")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    gen.log(f"yt-feeds api listening on {HOST}:{PORT} (cors={CORS_ORIGINS or 'off'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
