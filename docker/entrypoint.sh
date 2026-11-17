#!/bin/sh
# Prepare the data directory, then hand over to api.py (the container's CMD).
set -eu

DATA="${YT_FEEDS_BASE:-/data}"
mkdir -p "$DATA/cache" "$DATA/feeds"

# The image exposes :8000; platforms such as Render inject PORT instead.
if [ -z "${YT_API_PORT:-}" ] && [ -z "${PORT:-}" ]; then
  export YT_API_PORT=8000
fi

# channels.json is configuration and ships inside the image. Seed it into the
# volume on first boot; later edits are made through the API and survive restarts.
if [ ! -f "$DATA/channels.json" ] && [ -f /app/channels.json ]; then
  cp /app/channels.json "$DATA/channels.json"
fi

# Warm feeds/cache once, in the background, so the HTTP listener comes up at once.
if [ "${YT_BOOTSTRAP:-1}" != "0" ] && [ ! -f "$DATA/feeds/all.xml" ]; then
  (python3 /app/generate.py || echo "bootstrap generate.py failed" >&2) &
fi

exec "$@"
