#!/usr/bin/env bash
# Deploy LocSwap from this machine to a server, in one command (Git Bash):
#
#   bash deploy/push.sh root@<server-ip>                 # code + config, build, start
#   bash deploy/push.sh root@<server-ip> --seed          # ...and copy the local database + photos up
#   bash deploy/push.sh root@<server-ip> --skip-preflight
#
# Needs key-based SSH to the server (password auth would prompt at every step):
#   ssh-keygen -t ed25519            # once, if ~/.ssh/id_ed25519 does not exist
#   ssh-copy-id root@<server-ip>     # or paste ~/.ssh/id_ed25519.pub into the ECS console
#
# Re-running is how you ship an update. --seed REPLACES the server's database;
# the previous one is kept beside it as locswap.db.before-seed-<time>.
set -euo pipefail

HOST=${1:?usage: bash deploy/push.sh user@host [--seed] [--skip-preflight]}; shift
SEED=0; PREFLIGHT=1
for a in "$@"; do
  case "$a" in
    --seed) SEED=1 ;;
    --skip-preflight) PREFLIGHT=0 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done

ROOT=$(cd "$(dirname "$0")/.." && pwd); cd "$ROOT"
REMOTE=/opt/locswap
COMPOSE="docker compose -f deploy/docker-compose.yml"
SSH="ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 $HOST"
IP=${HOST#*@}

echo "== 1/6 checking .env.production locally"
python scripts/check_production_config.py --env-file .env.production >/dev/null \
  || { python scripts/check_production_config.py --env-file .env.production; exit 1; }
grep -qE "^CORS_ORIGINS=.*$IP" .env.production \
  || echo "  note: CORS_ORIGINS does not mention $IP (harmless — the site calls its own origin — but set it to http://$IP or your domain)"

echo "== 2/6 packaging"
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
# Only what the images are built from. Local data and secrets stay here; .env.production goes up separately with 600 permissions.
tar -czf "$STAGE/src.tgz" \
  --exclude='node_modules' --exclude='dist' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='*.log' --exclude='deploy/certs' \
  .dockerignore backend prompts scripts frontend deploy
ls -lh "$STAGE/src.tgz" | awk '{print "  source bundle " $5}'

echo "== 3/6 uploading to $HOST:$REMOTE"
$SSH "mkdir -p $REMOTE && cd $REMOTE && rm -rf backend prompts scripts frontend"
$SSH "tar -xzf - -C $REMOTE" < "$STAGE/src.tgz"
$SSH "umask 077 && cat > $REMOTE/.env.production" < .env.production

echo "== 4/6 server setup + preflight"
$SSH "bash $REMOTE/deploy/server-setup.sh"
if [ $PREFLIGHT = 1 ]; then
  $SSH "bash $REMOTE/deploy/preflight.sh $REMOTE/.env.production" \
    || { echo; echo "Stopped before building. Fix the above, or --skip-preflight if you know better."; exit 1; }
fi

echo "== 5/6 building and starting (first build: 10-20 min)"
$SSH "cd $REMOTE && $COMPOSE up -d --build --remove-orphans"

if [ $SEED = 1 ]; then
  echo "== seeding the database and photos from this machine"
  # sqlite's backup API gives a consistent copy even with the dev server running.
  python -c "import sqlite3,sys; s=sqlite3.connect('data/locswap.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close()" "$STAGE/locswap.db"
  tar -czf "$STAGE/uploads.tgz" -C data/uploads .
  $SSH "rm -rf /tmp/locswap-seed && mkdir -p /tmp/locswap-seed"
  scp -q "$STAGE/locswap.db" "$STAGE/uploads.tgz" "$HOST:/tmp/locswap-seed/"
  $SSH "cd $REMOTE && $COMPOSE stop app && docker run --rm \
      -v locswap_locswap-data:/data -v /tmp/locswap-seed:/seed alpine sh -c '
        ts=\$(date +%Y%m%d-%H%M%S)
        [ -f /data/locswap.db ] && mv /data/locswap.db /data/locswap.db.before-seed-\$ts
        rm -f /data/locswap.db-wal /data/locswap.db-shm
        cp /seed/locswap.db /data/locswap.db
        mkdir -p /data/uploads && tar -xzf /seed/uploads.tgz -C /data/uploads
        chown -R 10001:10001 /data' \
    && $COMPOSE start app && rm -rf /tmp/locswap-seed"
fi

echo "== 6/6 waiting for the app (the index rebuild takes a few minutes on first start)"
$SSH "for i in \$(seq 1 84); do
        s=\$(docker inspect -f '{{.State.Health.Status}}' locswap-app-1 2>/dev/null || echo missing)
        [ \"\$s\" = healthy ] && break; [ \"\$s\" = unhealthy ] && break; sleep 5; done
      echo \"  app: \$s\"
      cd $REMOTE && $COMPOSE logs app 2>&1 | grep -E 'index ready|rebuild done|hashing|Refusing|Traceback' | tail -8
      $COMPOSE up -d web >/dev/null 2>&1
      # nginx.conf and the .inc are single-file bind mounts. Extracting the
      # bundle replaces those files (new inodes), and a running container keeps
      # the old ones — so a config change does nothing until web restarts. That
      # is how the switch to HTTPS first deployed as a no-op. Test the new
      # config in a throwaway container first: a broken one stops here with the
      # old nginx still serving, instead of taking the site down on restart.
      if docker run --rm --network locswap_default \
           -v $REMOTE/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro \
           -v $REMOTE/deploy/locswap-locations.inc:/etc/nginx/conf.d/locswap-locations.inc:ro \
           -v $REMOTE/deploy/certs:/etc/nginx/certs:ro \
           locswap-web nginx -t >/dev/null 2>&1; then
        $COMPOSE restart web >/dev/null 2>&1 && echo '  web: restarted with the new nginx config'
      else
        echo '  [!!] new nginx config fails nginx -t; web left running the old one:'
        docker run --rm --network locswap_default \
           -v $REMOTE/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro \
           -v $REMOTE/deploy/locswap-locations.inc:/etc/nginx/conf.d/locswap-locations.inc:ro \
           -v $REMOTE/deploy/certs:/etc/nginx/certs:ro locswap-web nginx -t 2>&1 | tail -3
      fi
      $COMPOSE ps"

echo
# The public address is the first CORS origin (https://locswap.cn once TLS is
# on); plain http://IP now only redirects there, so checking it would report
# a 301, not whether the site works.
SITE=$(grep -E '^CORS_ORIGINS=' .env.production | cut -d= -f2- | cut -d, -f1)
SITE=${SITE:-http://$IP}
code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$SITE/api/health" || true)
echo "$SITE/api/health -> $code"
[ "$code" = 200 ] && echo "[ok] deployed: $SITE/" || echo "[!!] not answering from here yet — check the security group allows ports 80/443, then: ssh $HOST 'cd $REMOTE && $COMPOSE logs --tail 100'"
