#!/usr/bin/env bash
# Run ON THE SERVER, before building anything:
#
#   bash deploy/preflight.sh /opt/locswap/.env.production
#
# The one check that decides whether this server can host LocSwap at all is the
# first: a real model call. OpenAI refuses requests that originate in regions it
# does not serve — Hong Kong and mainland China among them — and that refusal
# reaches you through OpenRouter too, as `unsupported_country_region_territory`
# or "not available in your region". Every network check below would pass on
# such a server and the site would still be broken: search, listing and the
# seller advisor all need the model. So find out in ten seconds, before a
# twenty-minute build, and while the instance can still be released cheaply.
set -uo pipefail

ENV_FILE=${1:-/opt/locswap/.env.production}
[ -f "$ENV_FILE" ] || { echo "[FAIL] $ENV_FILE not found"; exit 1; }

val() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
BASE=$(val LLM_BASE_URL); KEY=$(val LLM_API_KEY); MODEL=$(val LLM_MODEL_MAIN)
fail=0

echo "== model call: $MODEL via $BASE"
resp=$(curl -sS -m 60 -w '\n%{http_code}' "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"回复 ok\"}]}" 2>&1)
code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
if [ "$code" = "200" ] && echo "$body" | grep -q '"choices"'; then
  echo "  [ok] HTTP 200 — the model answers from this server"
else
  fail=1
  echo "  [FAIL] HTTP $code"
  echo "$body" | head -c 600; echo
  if echo "$body" | grep -qiE 'region|country|territory'; then
    echo "  >>> Region block. This server's location is not served by the model's"
    echo "  >>> provider. Release it and use a Tokyo or Singapore instance instead."
  fi
fi

echo "== reachability (needed to build and to serve the map)"
# Small URLs only: pypi.org/simple/ is the full index (tens of MB) and timed out
# mid-download on the first real run, which printed "HTTP 200000".
for url in https://registry-1.docker.io/v2/ https://pypi.org/simple/pip/ \
           https://download.pytorch.org/whl/cpu/ https://huggingface.co/ \
           https://registry.npmjs.org/ https://tile.openstreetmap.org/0/0/0.png; do
  read -r c t < <(curl -sS -o /dev/null -m 20 -w '%{http_code} %{time_total}' -A "LocSwap-preflight" "$url" 2>/dev/null)
  c=${c:-000}; t=${t:--}
  if [ "$c" = "000" ] || [ "${c:0:1}" = "5" ]; then fail=1; mark=FAIL; else mark=ok; fi
  printf '  [%s] %-48s HTTP %s  %ss\n' "$mark" "$url" "$c" "$t"
done

echo "== resources"
mem=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
disk=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
echo "  RAM ${mem} MB, free disk ${disk} GB"
# Measured 2026-10-07: the app peaks at ~740 MiB (including the startup index
# rebuild of 754 items) and passed the full acceptance run capped at 1.2 GiB.
# A 2 GiB instance therefore runs it; the swap from server-setup.sh covers the
# build, which peaks higher than the running app.
[ "$mem" -lt 1700 ] && { echo "  [FAIL] under 2 GB RAM: the app (~740 MiB peak) plus the OS and Docker will not fit"; fail=1; }
[ "$disk" -lt 15 ] && { echo "  [FAIL] under 15 GB free: the image alone is ~3 GB, the build needs several times that"; fail=1; }

echo
if [ $fail = 0 ]; then echo "[ok] preflight passed"; else echo "[FAIL] preflight failed — see above"; fi
exit $fail
