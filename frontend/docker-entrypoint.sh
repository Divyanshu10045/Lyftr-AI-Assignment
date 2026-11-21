#!/bin/sh
# Writes /config.js from the API_URL environment variable so one image works in any environment.
set -eu
url=$(printf '%s' "${API_URL:-}" | tr -d '"\\')
printf 'window.__CONFIG__ = { API_URL: "%s" };\n' "$url" > /usr/share/nginx/html/config.js
echo "runtime config: API_URL=${url:-<unset, using build-time default>}"
