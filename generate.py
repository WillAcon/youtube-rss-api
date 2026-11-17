#!/usr/bin/env python3
"""Build RSS feeds for YouTube channels from the server-rendered channel page.

YouTube retired its public Atom endpoint (/feeds/videos.xml returns 404), so the
feed is rebuilt from ytInitialData. Publish dates are not in that payload; each
video's first-seen time is stored in state.json and used as pubDate, with the
relative label ("3d ago") only seeding the first run.

This module is also imported by api.py; the functions below are the shared
implementation for both the CLI/cron generator and the HTTP API.
"""

import email.utils
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


def load_dotenv(path):
    """Read KEY=VALUE lines into os.environ (real env wins, no dependency).

    `export KEY=...`, quoted values and # comments are accepted. A missing
    file is not an error. This lets api.py and generate.py share one .env.
    Duplicate keys follow shell semantics: the last line wins.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    pairs = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs[key] = value
    for key, value in pairs.items():
        os.environ.setdefault(key, value)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.environ.get("YT_ENV_FILE", os.path.join(SCRIPT_DIR, ".env")))

BASE = os.environ.get("YT_FEEDS_BASE", SCRIPT_DIR)
CHANNELS_PATH = os.path.join(BASE, "channels.json")
STATE_PATH = os.path.join(BASE, "state.json")
CACHE_DIR = os.path.join(BASE, "cache")
OUT_DIR = os.path.join(BASE, "feeds")
PUBLIC_BASE = os.environ.get("YT_FEEDS_PUBLIC_BASE", "").rstrip("/")
if not PUBLIC_BASE:
    # Render exposes its public URL; feeds are served by api.py at /feeds/.
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    PUBLIC_BASE = f"{render_url}/feeds" if render_url else "http://localhost/yt"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_ITEMS = int(os.environ.get("YT_MAX_ITEMS", "20"))
AGGREGATE_ITEMS = 50

# Content tabs that can be harvested from a channel page. "videos" and
# "streams" share the lockupViewModel markup; "shorts" uses shortsLockupViewModel.
KINDS = ("videos", "shorts", "streams")

DEFAULT_LANG = os.environ.get("YT_LANG", "en")

REL_RE = re.compile(
    r"(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d"
    r"|weeks?|wks?|w|months?|mos?|mo|years?|yrs?|y)\s+ago",
    re.IGNORECASE,
)
# "hace 3 h", "hace 16 min", "hace 2 semanas", "hace 1 año"
REL_ES_RE = re.compile(
    r"hace\s+(\d+)\s*(segundos?|segs?|s|minutos?|mins?|min|horas?|hrs?|h"
    r"|d[ií]as?|semanas?|sem|meses|mes|años?|anos?|d|a)\b",
    re.IGNORECASE,
)
UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
    "mo": 2592000, "mos": 2592000, "month": 2592000, "months": 2592000,
    "y": 31536000, "yr": 31536000, "yrs": 31536000, "year": 31536000, "years": 31536000,
    # Spanish labels ("hace 3 h")
    "seg": 1, "segs": 1, "segundo": 1, "segundos": 1,
    "minuto": 60, "minutos": 60,
    "hora": 3600, "horas": 3600,
    "día": 86400, "días": 86400, "dia": 86400, "dias": 86400,
    "semana": 604800, "semanas": 604800,
    "mes": 2592000, "meses": 2592000,
    "año": 31536000, "años": 31536000, "ano": 31536000, "anos": 31536000, "a": 31536000,
}


class ChannelError(Exception):
    """Raised when a channel cannot be resolved or fetched."""


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- io


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, payload):
    write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=1))


def write_atomic(path, content):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def artifact_name(slug, kind="videos", ext=".json"):
    """Cache/feed filename for one channel kind.

    "videos" keeps the historical name so existing files stay valid; other
    kinds get a suffix (<slug>-shorts.json, <slug>-streams.xml, ...).
    """
    suffix = "" if kind == "videos" else f"-{kind}"
    return f"{slug}{suffix}{ext}"


def cache_path(slug, kind="videos"):
    return os.path.join(CACHE_DIR, artifact_name(slug, kind, ".json"))


def feed_path(slug, kind="videos"):
    return os.path.join(OUT_DIR, artifact_name(slug, kind, ".xml"))


def channel_kinds(channel):
    """Kinds to harvest for a channel: explicit list, else YT_KINDS, else videos."""
    configured = channel.get("kinds")
    if isinstance(configured, list) and configured:
        picked = [str(item).strip().lower() for item in configured]
    else:
        picked = [
            part.strip().lower()
            for part in os.environ.get("YT_KINDS", "videos").split(",")
        ]
    return [kind for kind in picked if kind in KINDS] or ["videos"]


# ----------------------------------------------------------------------- fetch


def fetch_once(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", "ignore")


def retryable(exc):
    """4xx answers are final (unknown handle/channel); 429 is not."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return True


def fetch_retry(url, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            return fetch_once(url)
        except Exception as exc:  # noqa: BLE001 - retry transient TLS resets
            last_error = exc
            if not retryable(exc):
                raise
            time.sleep(3 * (attempt + 1))
    raise last_error


def channel_lang(channel):
    """UI language per channel: `lang` in channels.json, else YT_LANG, else en.

    It controls title translation and the relative date labels ("3h ago" vs
    "hace 3 h"), so a Spanish channel should set "lang": "es".
    """
    return (channel.get("lang") or DEFAULT_LANG or "en").strip() or "en"


def channel_url(cid, kind="videos", lang=None):
    if kind not in KINDS:
        raise ChannelError(f"unknown kind {kind!r} (expected one of {', '.join(KINDS)})")
    return f"https://www.youtube.com/channel/{cid}/{kind}?hl={lang or DEFAULT_LANG}&persist_hl=1"


def fetch_videos(cid, kind="videos", lang=None):
    """Fetch and parse a channel page, retrying when YouTube throttles us."""
    last_error = None
    for attempt in range(3):
        try:
            videos = parse_videos(fetch_once(channel_url(cid, kind, lang)), kind)
            if videos:
                return videos
            last_error = ChannelError(f"no {kind} parsed from channel page")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if not retryable(exc):
                raise
        time.sleep(3 * (attempt + 1))
    raise last_error


def _lockup_item(lockup):
    """Regular video / stream entry (lockupViewModel)."""
    video_id = lockup.get("contentId")
    meta = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = meta.get("title", {}).get("content", "")
    if not video_id or not title:
        return None
    parts = []
    rows = (
        meta.get("metadata", {})
        .get("contentMetadataViewModel", {})
        .get("metadataRows", [])
    )
    for row in rows:
        for part in row.get("metadataParts", []):
            text = part.get("text", {}).get("content")
            if text:
                parts.append(text)
    return {"id": video_id, "title": title, "meta": parts}


def _shorts_item(lockup):
    """Short entry (shortsLockupViewModel).

    Unlike /videos, shorts carry no relative age in the payload: only a title
    and a view count, so their first-seen time is the only usable date.
    """
    command = (lockup.get("onTap") or {}).get("innertubeCommand") or {}
    video_id = (command.get("reelWatchEndpoint") or {}).get("videoId")
    if not video_id:
        entity = lockup.get("entityId") or ""
        prefix = "shorts-shelf-item-"
        video_id = entity[len(prefix):] if entity.startswith(prefix) else entity
    overlay = lockup.get("overlayMetadata") or {}
    title = ((overlay.get("primaryText") or {}).get("content") or "").strip()
    if not title:
        # Fallback: "<title>, 47 views - play Short"
        title = (lockup.get("accessibilityText") or "").rsplit(",", 1)[0].strip()
    if not video_id or not title:
        return None
    views = (overlay.get("secondaryText") or {}).get("content")
    return {"id": video_id, "title": title, "meta": [views] if views else []}


def parse_videos(page, kind="videos"):
    if kind not in KINDS:
        raise ChannelError(f"unknown kind {kind!r} (expected one of {', '.join(KINDS)})")
    match = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    node_key = "shortsLockupViewModel" if kind == "shorts" else "lockupViewModel"
    extract = _shorts_item if kind == "shorts" else _lockup_item
    found = []

    def walk(node):
        if isinstance(node, dict):
            lockup = node.get(node_key)
            if isinstance(lockup, dict):
                item = extract(lockup)
                if item:
                    found.append(item)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)

    seen, videos = set(), []
    for item in found:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        videos.append(item)
    return videos


def resolve_channel_id(value):
    """Turn a handle, /@handle URL, /channel/<id> URL or raw id into a channel id."""
    return channel_meta(value)[0]


def channel_meta(value):
    """Resolve a handle/URL to (cid, handle, name) by reading the channel page."""
    value = (value or "").strip()
    if not value:
        raise ChannelError("empty channel reference")

    if value.startswith("http"):
        url = value
    elif value.startswith("@"):
        url = f"https://www.youtube.com/{value}"
    elif re.fullmatch(r"UC[\w-]{22}", value):
        cid = value
        page = fetch_retry(f"https://www.youtube.com/channel/{cid}?hl=en")
        return cid, _extract_handle(page) or "", _extract_name(page) or cid
    else:
        url = f"https://www.youtube.com/@{value.lstrip('@')}"

    page = fetch_retry(url)
    cid = _first_match(r'"externalId":"(UC[\w-]{22})"', page) or _first_match(
        r'"channelId":"(UC[\w-]{22})"', page
    )
    if not cid:
        raise ChannelError(f"channel id not found for {value}")
    return cid, _extract_handle(page) or "", _extract_name(page) or cid


def _first_match(pattern, text):
    found = re.search(pattern, text)
    return found.group(1) if found else None


def _extract_handle(page):
    found = _first_match(r'"canonicalBaseUrl":"(/@[\w.\-]+)"', page)
    return found.replace("/", "") if found else ""


def _extract_name(page):
    found = _first_match(r'<meta property="og:title" content="([^"]+)"', page)
    return html.unescape(found) if found else ""


def search_channels(query, limit=8):
    """Search YouTube's channel results, for 'find this person's channel URL'."""
    from urllib.parse import quote_plus

    url = (
        "https://www.youtube.com/results?search_query="
        f"{quote_plus(query)}&sp=EgIQAg%253D%253D&hl=en"
    )
    page = fetch_retry(url)
    match = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if not match:
        return []
    data = json.loads(match.group(1))

    results = []

    def walk(node):
        if isinstance(node, dict):
            renderer = node.get("channelRenderer")
            if isinstance(renderer, dict):
                cid = renderer.get("channelId")
                title = renderer.get("title", {}).get("simpleText", "")
                handle = ""
                try:
                    handle = renderer["navigationEndpoint"]["browseEndpoint"]["canonicalBaseUrl"]
                except (KeyError, TypeError):
                    pass
                subs = (
                    renderer.get("videoCountText", {}).get("simpleText")
                    or renderer.get("subscriberCountText", {}).get("simpleText")
                    or ""
                )
                if cid and title:
                    results.append(
                        {
                            "name": title,
                            "cid": cid,
                            "handle": handle,
                            "url": f"https://www.youtube.com{handle or '/channel/' + cid}",
                            "subscribers": subs,
                        }
                    )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return results[:limit]


# ----------------------------------------------------------------------- dates


def relative_to_datetime(label, now):
    label = label or ""
    match = REL_RE.search(label)
    if not match:
        match = REL_ES_RE.search(label)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = UNIT_SECONDS.get(unit)
    return now - timedelta(seconds=amount * seconds) if seconds else None


def seed_time(video, now):
    for part in video["meta"]:
        parsed = relative_to_datetime(part, now)
        if parsed:
            return parsed
    return now


# ----------------------------------------------------------------------- feeds


def xml_escape(text):
    return html.escape(text or "", quote=True)


def rfc822(dt):
    return email.utils.format_datetime(dt.astimezone(timezone.utc))


def video_link(video_id, kind="videos"):
    if kind == "shorts":
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def video_thumb(video_id, kind="videos"):
    # oar* preserves the original (vertical) aspect ratio used by Shorts.
    name = "oar2" if kind == "shorts" else "hqdefault"
    return f"https://i.ytimg.com/vi/{video_id}/{name}.jpg"


def build_items(channel, videos, state, now, kind="videos"):
    """Build feed items for one channel kind and update state in place.

    Returns (items, new_ids) where new_ids are videos absent from state before
    this call. Videos and Shorts share one state file; ids are unique.
    """
    items, new_ids = [], []
    for video in videos[:MAX_ITEMS]:
        entry = state.get(video["id"])
        if entry:
            first_seen = datetime.fromisoformat(entry["t"])
        else:
            first_seen = seed_time(video, now)
            new_ids.append(video["id"])
        state[video["id"]] = {
            "t": first_seen.isoformat(),
            "c": channel["slug"],
            "title": video["title"],
        }
        items.append(
            {
                "id": video["id"],
                "kind": kind,
                "title": video["title"],
                "link": video_link(video["id"], kind),
                "guid": video["id"],
                "date": first_seen,
                "meta": video["meta"],
                "thumb": video_thumb(video["id"], kind),
            }
        )
    items.sort(key=lambda entry: entry["date"], reverse=True)
    return items, new_ids


def render_feed(title, link, description, items, self_url):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"'
        ' xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>{xml_escape(title)}</title>",
        f"<link>{xml_escape(link)}</link>",
        f"<description>{xml_escape(description)}</description>",
        f'<atom:link href="{xml_escape(self_url)}" rel="self" type="application/rss+xml"/>',
        f"<lastBuildDate>{rfc822(datetime.now(timezone.utc))}</lastBuildDate>",
    ]
    for item in items:
        lines.append("<item>")
        lines.append(f"<title>{xml_escape(item.get('rss_title') or item['title'])}</title>")
        lines.append(f"<link>{xml_escape(item['link'])}</link>")
        lines.append(f'<guid isPermaLink="false">{xml_escape(item["guid"])}</guid>')
        lines.append(f"<pubDate>{rfc822(item['date'])}</pubDate>")
        if item.get("meta"):
            lines.append(f"<description>{xml_escape(' · '.join(item['meta']))}</description>")
        lines.append(f'<media:thumbnail url="{xml_escape(item["thumb"])}"/>')
        lines.append("</item>")
    lines.extend(["</channel>", "</rss>", ""])
    return "\n".join(lines)


def write_channel_feed(channel, items, kind="videos"):
    label = "" if kind == "videos" else f" {kind.title()}"
    feed = render_feed(
        f"{channel['name']} · YouTube{label}",
        f"https://www.youtube.com/{channel['handle']}",
        f"New {kind} from {channel['name']} ({channel['handle']})",
        items,
        f"{PUBLIC_BASE}/{artifact_name(channel['slug'], kind, '.xml')}",
    )
    write_atomic(feed_path(channel["slug"], kind), feed)


def write_cache(channel, items, kind="videos"):
    payload = [dict(item, date=item["date"].isoformat()) for item in items]
    write_atomic(
        cache_path(channel["slug"], kind),
        json.dumps(payload, ensure_ascii=False),
    )


def load_cache(slug, kind="videos"):
    raw = load_json(cache_path(slug, kind), [])
    items = []
    for item in raw:
        try:
            item = dict(item)
            item["date"] = datetime.fromisoformat(item["date"])
        except (KeyError, TypeError, ValueError):
            continue
        items.append(item)
    return items


def write_aggregate(channels):
    merged = []
    for channel in channels:
        for item in load_cache(channel["slug"]):
            merged.append(
                dict(
                    item,
                    channel=channel["name"],
                    rss_title=f"[{channel['name']}] {item['title']}",
                    guid=f"{channel['slug']}:{item['id']}",
                )
            )
    merged.sort(key=lambda entry: entry["date"], reverse=True)
    write_atomic(
        os.path.join(OUT_DIR, "all.xml"),
        render_feed(
            f"YouTube 订阅聚合 · {len(channels)} channels",
            f"{PUBLIC_BASE}/all.xml",
            "Aggregated uploads from the tracked YouTube channels",
            merged[:AGGREGATE_ITEMS],
            f"{PUBLIC_BASE}/all.xml",
        ),
    )
    return merged


# ------------------------------------------------------------------- generator


def refresh_channel(channel, state, now, kind="videos"):
    """Fetch one channel kind, write its feed and cache, return (items, new_ids)."""
    videos = fetch_videos(channel["cid"], kind, channel_lang(channel))
    items, new_ids = build_items(channel, videos, state, now, kind)
    write_channel_feed(channel, items, kind)
    write_cache(channel, items, kind)
    return items, new_ids


def main():
    channels = load_json(CHANNELS_PATH, [])
    state = load_json(STATE_PATH, {})
    now = datetime.now(timezone.utc)
    failures = 0
    attempts = 0
    fresh_state = {}

    for channel in channels:
        for kind in channel_kinds(channel):
            if attempts:
                time.sleep(1.5)  # space out requests; bursts get throttled
            attempts += 1
            try:
                items, _ = refresh_channel(channel, state, now, kind)
                for item in items:
                    fresh_state[item["id"]] = state[item["id"]]
                log(f"OK   {channel['slug']} [{kind}]: {len(items)} items")
            except Exception as exc:  # noqa: BLE001 - keep other channels going
                failures += 1
                for video_id, entry in state.items():
                    if entry.get("c") == channel["slug"]:
                        fresh_state[video_id] = entry
                log(f"FAIL {channel['slug']} [{kind}]: {exc}")

    write_aggregate(channels)
    # The API may have added channels or recorded videos while this run was in
    # flight; merge onto the current file instead of overwriting it.
    merged_state = load_json(STATE_PATH, {})
    merged_state.update(fresh_state)
    save_json(STATE_PATH, merged_state)
    log(f"done: {attempts - failures}/{attempts} fetches OK")
    return 1 if failures == attempts else 0


if __name__ == "__main__":
    sys.exit(main())
