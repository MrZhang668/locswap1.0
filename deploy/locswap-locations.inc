# Shared by the HTTP and HTTPS server blocks so the two cannot drift apart —
# a rule that exists on one and not the other is how a protection quietly stops
# applying the moment TLS is switched on.

client_max_body_size 12m;          # listing photos
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

# Some advisor turns run several model calls and a vector search; the default
# 60s read timeout cuts them off mid-answer.
proxy_read_timeout 180s;
proxy_send_timeout 180s;

add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
add_header Referrer-Policy strict-origin-when-cross-origin always;

# ---- the API ----
# Ordered most specific first; nginx picks the longest matching prefix, so the
# two expensive prefixes below win over the catch-all regardless of order, but
# keeping them in cost order makes the intent readable.

# Costs money on every call: an LLM round trip plus a vector search.
location /api/search/ {
    limit_req zone=ai burst=10 nodelay;
    proxy_pass http://app:8010;
}

# The sales advisor is the most expensive thing here — several model calls per
# turn, and a turn can run tools. Only chat and publish/approve (which re-runs
# the compliance check) spend model calls; the thread list / open / create /
# rename / delete under the same prefix are local SQL and fall through to the
# `browse` catch-all. They used to share this zone, and since every one of those
# actions also reloads the list, three or four clicks hit 503 (2026-10-10).
location = /api/seller/advisor/chat {
    limit_req zone=ai burst=5 nodelay;
    proxy_pass http://app:8010;
}

location = /api/seller/advisor/publish/approve {
    limit_req zone=ai burst=5 nodelay;
    proxy_pass http://app:8010;
}

location /api/auth/ {
    limit_req zone=auth burst=5 nodelay;
    proxy_pass http://app:8010;
}

# Everything else: local SQL, cheap, and one page view makes several calls.
location /api/ {
    limit_req zone=browse burst=60 nodelay;
    proxy_pass http://app:8010;
}

# ---- uploaded photos ----
# Served by the app, which mounts only DATA_DIR/uploads here. Nothing else
# under DATA_DIR is reachable: the database and the advisor's thread store live
# beside it and used to be served too — see the note in backend/app/main.py.
location /static/uploads/ {
    proxy_pass http://app:8010;
    proxy_cache_valid 200 7d;
    expires 7d;
    add_header Cache-Control "public";
}

# ---- map tiles (see proxy_cache_path in nginx.conf) ----
# Only the zoom range the map allows (minZoom 12 .. maxZoom 19 in
# CampusMap.jsx); anything else is not a request this site makes.
#
# The upstream goes through a variable so nginx resolves it per request. A
# literal hostname is resolved once at startup, and if DNS hiccups at that
# moment nginx refuses to start — taking the whole site down over the map.
location ~ ^/tiles/(1[2-9])/(\d+)/(\d+)\.png$ {
    limit_req zone=tiles burst=80 nodelay;
    resolver 127.0.0.11 8.8.8.8 valid=300s ipv6=off;
    set $osm_host tile.openstreetmap.org;
    rewrite ^/tiles(/.*)$ $1 break;
    proxy_pass https://$osm_host;
    proxy_ssl_server_name on;
    proxy_ssl_name $osm_host;
    # A proxy_set_header here replaces every inherited one, so Host has to be
    # set again — the inherited $host would be this site's name, not OSM's.
    proxy_set_header Host $osm_host;
    # OSM blocks anonymous or library-default user agents.
    proxy_set_header User-Agent "LocSwap/1.0 (campus marketplace demo)";
    proxy_hide_header Set-Cookie;
    proxy_ignore_headers Set-Cookie Cache-Control Expires;
    proxy_cache tile_cache;
    proxy_cache_key $uri;
    proxy_cache_valid 200 30d;
    proxy_cache_valid 404 1m;
    proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
    proxy_cache_lock on;
    expires 7d;
    add_header Cache-Control "public";
    add_header X-Tile-Cache $upstream_cache_status;
}
location /tiles/ {
    return 404;
}

# ---- the built frontend ----
location / {
    root /usr/share/nginx/html;
    try_files $uri $uri/ /index.html;   # single-page app
}

# Vite writes content-hashed filenames, so these can be cached hard; index.html
# must not be, or a deploy ships new assets to browsers still holding the old
# HTML that does not reference them.
location /assets/ {
    root /usr/share/nginx/html;
    expires 1y;
    add_header Cache-Control "public, immutable";
}

location = /index.html {
    root /usr/share/nginx/html;
    add_header Cache-Control "no-cache";
}
