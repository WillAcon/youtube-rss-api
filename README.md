# youtube-rss-api

Self-hosted RSS feeds and a small JSON API for YouTube channels.

YouTube retired its public Atom endpoint: `https://www.youtube.com/feeds/videos.xml?channel_id=...`
now returns 404 for every channel, and the usual mirrors (Piped, Invidious) are unreliable or
blocked. This project rebuilds the feed from the channel page's server-rendered `ytInitialData`,
so you get normal RSS subscriptions without an API key, a headless browser, or a third-party service.

Standard library only (Python 3.8+). No pip install.

## Features

- RSS 2.0 feed per channel and kind (videos, shorts, streams), plus one aggregate feed
- JSON API to search channels, check for new uploads, and add/update/delete channels in bulk
- Per-kind selection: `videos`, `shorts` or `streams` from the same channel
- Token authentication on every endpoint (optional public read for `/api/channel/...`)
- CORS ready for browser clients (configurable origins, `OPTIONS` preflight, `HEAD`)
- One-container deployment: Docker Compose on port 9012, plus a Render Blueprint
- Full manual in Spanish: [`manual.html`](manual.html)
- Hourly refresh via cron (or in-process with `YT_REFRESH_INTERVAL`); on-demand refresh through the API
- Per-channel cache so read endpoints answer instantly

## How it works

1. Fetch `https://www.youtube.com/channel/<channel_id>/<kind>?hl=en`, where `<kind>` is
   `videos`, `shorts` or `streams`.
2. Parse `ytInitialData`. `videos` and `streams` use `lockupViewModel` nodes (video id, title,
   views, relative age). `shorts` uses `shortsLockupViewModel` nodes: the id comes from
   `reelWatchEndpoint`, and the title/views from `overlayMetadata`.
3. Emit RSS. YouTube does not expose exact publish dates on those pages, so each video's
   **first-seen timestamp** is stored in `state.json` and used as `pubDate`; for videos and
   streams the relative label ("3d ago") seeds the very first run. Shorts have no relative
   age at all, so their first-seen time is the only date available.

## Quick start

```bash
git clone https://github.com/<you>/youtube-rss-api.git
cd youtube-rss-api

# 1. edit channels.json (slug / name / handle / cid, optional kinds / lang)
python3 generate.py          # writes feeds/*.xml and cache/*.json

# 2. create a local .env once, with a fixed token
cp .env.example .env
TOKEN=$(openssl rand -hex 16)
perl -pi -e "s/^YT_API_TOKEN=.*/YT_API_TOKEN=$TOKEN/" .env
chmod 600 .env

# 3. run the JSON API on :8000 (both scripts read .env automatically)
python3 api.py

# 4. serve the XML feeds in another terminal (by default the API only serves JSON;
#    with YT_SERVE_FEEDS=1 it also serves them at /feeds/)
python3 -m http.server 8001 --directory feeds

# 5. last 20 Shorts of any channel, no prior registration needed:
set -a; . ./.env; set +a
curl -s "http://127.0.0.1:8000/api/channel/@tvperu/shorts?limit=20&token=$YT_API_TOKEN"
```

Both `api.py` and `generate.py` load `.env` from the project directory at startup; variables
already present in the environment win, and `YT_ENV_FILE=/path/to/file` points at another file.
Inline variables still work for one-off runs (`YT_API_PORT=9000 python3 api.py`). In production,
systemd uses `EnvironmentFile=/etc/default/yt-feeds-api` instead (see `deploy/`).

Get a channel id from a handle:

```bash
curl -sL "https://www.youtube.com/@Fireship" | grep -o '"externalId":"UC[^"]*"' | head -1
```

or let the API find it for you with `/resolve`.

## Part of a three-repository setup

| Repository | Role | Where it runs |
|---|---|---|
| **`youtube-rss-api`** (this one) | resolve channels and videos, serve RSS | server with a domain |
| `youtube-dub-codespace` | download, Demucs separation, WhisperX, translation | GitHub Codespaces |
| `youtube-dub-colab` | GPT-SoVITS voice synthesis, mixing, lip-sync | Google Colab |

`/resolve` and `/channels/<key>/videos` are what feed the dubbing pipeline with
source URLs.

## Configuration

`channels.json` is the channel list and is rewritten by the API:

```json
[
  {"slug": "fireship", "name": "Fireship", "handle": "@Fireship", "cid": "UCsBjURrPoezykLs9EqgamOA"},
  {
    "slug": "tvperu",
    "name": "TVPerú",
    "handle": "@tvperu",
    "cid": "UCrAb_x80PtTiN3lCkQOSFPg",
    "kinds": ["videos", "shorts"],
    "lang": "es"
  }
]
```

Two optional fields per channel:

- `kinds` — tabs to harvest (default `YT_KINDS`, i.e. `videos`). Adds `feeds/<slug>-shorts.xml`
  and `cache/<slug>-shorts.json` for each extra kind.
- `lang` — YouTube UI language (`es`, `en`, `es-PE`…, default `YT_LANG`). It decides whether
  titles are translated and how relative dates read ("3h ago" vs "hace 3 h"). Set `es` for
  Spanish-language channels so titles stay in the original language.

Both fields can also be sent to the API: `POST /channels {"handle": "@tvperu", "kinds": ["videos","shorts"], "lang": "es"}`.

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `YT_FEEDS_BASE` | script directory | both | Data directory (`channels.json`, `state.json`, `cache/`, `feeds/`) |
| `YT_FEEDS_PUBLIC_BASE` | `http://localhost/yt` | both | Public URL prefix written into feed self-links and API `feed_url` |
| `YT_API_TOKEN` | — (required) | `api.py` | Shared secret for every API request |
| `YT_API_HOST` | `127.0.0.1` | `api.py` | Bind address (automatically `0.0.0.0` when `PORT` is set, e.g. on Render) |
| `YT_API_PORT` | `8099` | `api.py` | Bind port; `$PORT` is honoured as a fallback |
| `YT_CORS_ORIGINS` | `*` | `api.py` | Comma-separated browser origins, `*` for any, empty to disable CORS |
| `YT_SERVE_FEEDS` | `0` | `api.py` | `1` also serves `feeds/*.xml` at `/feeds/` (single-port Docker/Render) |
| `YT_REFRESH_INTERVAL` | `0` | `api.py` | Seconds between in-process harvests (0 disables; containers have no cron) |
| `YT_KINDS` | `videos` | `generate.py` | Comma-separated tabs harvested by cron for channels without `kinds` |
| `YT_LANG` | `en` | both | Fallback YouTube UI language for channels without `lang` |
| `YT_MAX_ITEMS` | `20` | both | Items kept per channel and kind |
| `YT_PUBLIC_READ` | `0` | `api.py` | `1` serves `GET /api/channel/...` without a token (every other route still requires it) |

In local development all of these can live in `.env` (copy `.env.example`); both scripts load it
at startup and real environment variables take precedence. `.env` is gitignored.

## HTTP API

The examples below use `/yt/api` because that is how nginx is wired in `deploy/`. `api.py`
serves the same routes under `/api` as well, which is the recommended prefix for clients:
`http://localhost:8000/api/channel/@tvperu/shorts`. `/channel/...` remains as a legacy alias.

Authentication: `?token=<token>`, `X-API-Token: <token>`, or `Authorization: Bearer <token>`.
Missing or wrong token returns `401`. With `YT_PUBLIC_READ=1`, `GET /api/channel/...` is served
without a token (handy for a public frontend); every other route still requires it.
`GET /api/health` is always public (no token) so container and platform health checks work.

CORS is enabled by default for every origin (`YT_CORS_ORIGINS=*`), on both JSON and XML
responses. Set `YT_CORS_ORIGINS=https://app.example.com,https://admin.example.com` to restrict
it, or `YT_CORS_ORIGINS=` to disable it. `OPTIONS` answers preflights with `204` without a token
and allows `GET, HEAD, POST, PATCH, DELETE, OPTIONS` plus the `Content-Type`, `X-API-Token` and
`Authorization` headers. `HEAD` is accepted wherever `GET` is.

`<key>` accepts a slug, `@handle`, channel id (`UC...`), or a channel name (exact, or a unique substring).
For `/api/channel/<key>/<kind>` and `/channels/<key>/<kind>`, an untracked `@handle`, id or URL is
resolved on YouTube on the fly (kept in memory for an hour, never written to `channels.json`)
and fetched live, so no registration is needed.

`<kind>` is `videos` (default), `shorts` or `streams`. It can also be passed as `?kind=`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Service status and channel count |
| GET | `/channels` | List channels with their latest video; `?videos=1` includes all cached videos |
| GET | `/channels/<key>` | One channel with its cached videos |
| GET | `/channels/<key>/videos` | Cached videos; `?live=1` fetches now, `?limit=N` (max 100) |
| GET | `/channels/<key>/shorts` | Cached Shorts; same options |
| GET | `/channels/<key>/streams` | Cached streams; same options |
| GET | `/api/channel/<key>/<kind>` | Same as above, short prefix (`/channel/...` is a legacy alias) |
| POST | `/channels` | Add one channel, or an array (batch) |
| PATCH | `/channels/<key>` | Update `name` / `slug` / `handle` / `url` / `cid` |
| PATCH | `/channels` | Batch update: `[{"key": "...", ...fields}]` |
| DELETE | `/channels/<key>` | Delete one channel |
| DELETE | `/channels?keys=a,b` | Batch delete, or body `{"keys": [...]}` |
| GET | `/updates` | Recent videos; `?since=ISO&channel=<slug>&limit=N` |
| GET | `/resolve?q=<name>` | Search YouTube for a channel (name, handle, id, URL, subscribers) |
| POST | `/check` | Refresh now and report newly seen videos; body `{"channels": [...]}` optional |

Batch writes return `200` when everything succeeded and `207` when some items failed; each item
carries a `status` (`created`, `exists`, `updated`, `deleted`, `missing`, `error`) and an `error` field.

Adding a channel accepts `url`, `handle`, `cid`, or `name`. With `name` alone the API searches
YouTube, takes the first hit, and echoes it back as `resolved_from` so you can verify the match.

### Examples

```bash
T=<token>; B=http://127.0.0.1:8099/yt/api

# find someone's channel URL
curl -s "$B/resolve?q=mckay+wrigley&token=$T"

# latest videos, fetched live
curl -s "$B/channels/mckay-wrigley/videos?live=1&limit=5&token=$T"

# last 20 Shorts of an untracked channel (short route, no /yt/api)
curl -s "http://127.0.0.1:8099/api/channel/@tvperu/shorts?limit=20&token=$T"

# select the tab explicitly, even on the /videos route
curl -s "$B/channels/@tvperu/videos?kind=streams&limit=5&token=$T"

# add by URL
curl -s -X POST -H "X-API-Token: $T" \
  -d '{"url":"https://www.youtube.com/@mkbhd"}' "$B/channels"

# batch add
curl -s -X POST -H "X-API-Token: $T" \
  -d '[{"handle":"@veritasium"},{"name":"Fireship"}]' "$B/channels"

# batch rename / re-slug
curl -s -X PATCH -H "X-API-Token: $T" \
  -d '[{"key":"mkbhd","name":"MKBHD"},{"key":"veritasium","slug":"veritasium-science"}]' "$B/channels"

# check for new uploads
curl -s -X POST -H "X-API-Token: $T" -d '{"channels":["fireship"]}' "$B/check"

# everything new in the last 24h
curl -s "$B/updates?since=$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ)&token=$T"

# batch delete
curl -s -X DELETE -H "X-API-Token: $T" "$B/channels?keys=veritasium-science,mkbhd"
```

`check` fetches every targeted channel page and kind; roughly 20 seconds for 12 channels
with `YT_KINDS=videos`, or about twice that with Shorts enabled.

To expose the short `/api/channel/...` route on port 8000 without a token (local frontend):

```bash
YT_API_PORT=8000 YT_PUBLIC_READ=1 python3 api.py
```

## Tests

Offline tests (no network), using fixtures of the real `ytInitialData` shapes:

```bash
python3 -m unittest discover -s tests -v
```

## Deployment

Three options, all reading `YT_API_TOKEN` from the environment (never hardcoded).

### Docker Compose (host port 9012)

`Dockerfile` + `docker-compose.yml` run the JSON API **and** the XML feeds in one container.
The container listens on `:8000` internally; Compose publishes it on **9012**. Data is kept in
the `yt-data` volume (`/data`), and `YT_REFRESH_INTERVAL=1800` re-harvests every 30 minutes, so
no cron is needed.

```bash
cp .env.example .env       # set YT_API_TOKEN=...
docker compose up -d --build

curl http://SERVER:9012/api/health                     # public
curl "http://SERVER:9012/api/channel/@tvperu/shorts"   # public with YT_PUBLIC_READ=1
curl -o tvperu.xml http://SERVER:9012/feeds/tvperu.xml # XML served by the API
```

`channels.json` ships in the image and is copied into `/data` on first boot; later changes made
through the API persist in the volume. `docker compose down` keeps it, `down -v` deletes it.
To edit it by hand: `docker compose exec yt-feeds vi /data/channels.json`.

Set the public prefix so RSS self-links and `feed_url` point at the VPS (default is
`http://localhost:9012/feeds`):

```bash
YT_PUBLIC_BASE=https://feeds.example.com docker compose up -d
```

The container runs as UID 10001. With a bind mount instead of the named volume, `chown -R 10001`
the host directory first.

### Render

`render.yaml` is a Blueprint: **New → Blueprint**, pick the repo. It builds the same Dockerfile
and sets `YT_API_HOST=0.0.0.0`, `YT_SERVE_FEEDS=1`, `YT_REFRESH_INTERVAL=1800`,
`YT_PUBLIC_READ=1`, CORS on, and a generated `YT_API_TOKEN`. `api.py` honours Render's `PORT`.

- `healthCheckPath: /api/health` works because health is always public.
- Feeds are served at `https://<service>.onrender.com/feeds/<slug>.xml`; when
  `YT_FEEDS_PUBLIC_BASE` is unset it is derived from `RENDER_EXTERNAL_URL`.
- The free plan has an **ephemeral** filesystem: feeds regenerate after each deploy. On a paid
  plan uncomment the `disk` block in `render.yaml` (mounted at `/data`) to persist
  `channels.json`, `state.json`, `cache/` and `feeds/`.

### systemd + nginx

`deploy/` contains ready-to-adapt files:

- `yt-feeds-api.service` — systemd unit for `api.py` (bound to 127.0.0.1), reading
  `/etc/default/yt-feeds-api` for the token
- `cron.d-yt-feeds` — hourly refresh
- `nginx-yt.conf` — static feed route with a token check, plus reverse proxy for `/yt/api/`
- `logrotate-yt-feeds` — log rotation for the cron output

Install roughly like this:

```bash
sudo mkdir -p /opt/yt-feeds && sudo cp generate.py api.py channels.json /opt/yt-feeds/
printf 'YT_API_TOKEN=%s\nYT_FEEDS_PUBLIC_BASE=https://feeds.example.com/yt\n' "$(openssl rand -hex 16)" \
  | sudo tee /etc/default/yt-feeds-api >/dev/null && sudo chmod 600 /etc/default/yt-feeds-api
sudo cp deploy/yt-feeds-api.service /etc/systemd/system/ && sudo systemctl enable --now yt-feeds-api
sudo cp deploy/cron.d-yt-feeds /etc/cron.d/yt-feeds     # edit the domain first
sudo cp deploy/nginx-yt.conf /etc/nginx/snippets/      # include it inside your server block
```

The nginx snippet repeats the token so feeds can be subscribed to in a reader without HTTP auth.
Keep it in sync with `YT_API_TOKEN`, or drop the `if ($arg_token ...)` line if the feeds are public.

## Caveats

- `pubDate` is first-seen time, not the real publish time; the first run approximates it from
  relative labels ("3d ago") and can be hours off. Shorts carry no relative age, so their
  date is always the moment they were first seen.
- Only the newest `YT_MAX_ITEMS` (20) entries per channel and kind are kept.
- Parsing depends on channel pages shipping `lockupViewModel` / `shortsLockupViewModel`.
  If YouTube changes the markup, `parse_videos()` returns nothing; the generator retries
  three times and then leaves the previous feed file untouched rather than emptying it.
- Each tab is fetched separately, so `videos` does not include Shorts and vice versa.
  `all.xml` aggregates the `videos` tab only.
- `POST /check` and `/updates` work on tracked channels only; for an untracked handle use
  `/api/channel/<key>/<kind>`, which fetches live.

## Security

- `YT_API_TOKEN` is read from the environment; nothing is hardcoded.
- Never commit `state.json`, `cache/`, or `feeds/` — `.gitignore` already excludes them.
- The API binds to `127.0.0.1` by default; it binds to `0.0.0.0` only when `YT_API_HOST` says so
  (the Docker image and `render.yaml` set it). Expose it through a reverse proxy or a firewall.
- `?token=` is redacted from the API log lines, but prefer `X-API-Token` so it never appears in
  proxy access logs either.
- `/api/health` is public and only reports status, channel count and uptime.
- With `YT_SERVE_FEEDS=1` the XML feeds are public (feed readers cannot send headers), exactly
  like the nginx route with the token in the query string. Use a reverse proxy if they must be
  restricted.
- CORS defaults to `*`; with cookie-less token auth that is safe, but setting
  `YT_CORS_ORIGINS` to your frontend origins is tighter.
