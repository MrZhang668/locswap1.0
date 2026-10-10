#!/usr/bin/env bash
# Run ON THE SERVER, once, after the domain's A records point here:
#
#   bash /opt/locswap/deploy/enable-https.sh locswap.cn www.locswap.cn
#
# Issues a Let's Encrypt certificate through the HTTP server block that is
# already running (it serves /.well-known/acme-challenge/ from the certbot
# webroot volume), then installs a daily renewal job.
#
# Order matters, and it is why this is a separate step from push.sh: nginx
# refuses to start if a TLS server block names a certificate file that does not
# exist yet. So the certificate comes first, through the plain-HTTP config, and
# only then is the TLS config pushed.
#
# No email is registered with Let's Encrypt. Its only use was expiry warnings,
# which the renewal job below makes unnecessary (and which Let's Encrypt stopped
# sending in 2025).
set -euo pipefail

[ $# -ge 1 ] || { echo "usage: $0 domain [more domains...]"; exit 2; }
REMOTE=/opt/locswap
CERTS=$REMOTE/deploy/certs
WEBROOT_VOL=locswap_certbot-webroot
IP=$(curl -s -m 10 http://100.100.100.200/latest/meta-data/eipv4 || true)
[ -n "$IP" ] || IP=$(curl -s -m 10 https://api.ipify.org)

echo "== this server: $IP"
for d in "$@"; do
  got=$(getent ahostsv4 "$d" | awk '{print $1}' | sort -u | tr '\n' ' ')
  echo "  $d -> ${got:-nothing}"
  case " $got " in *" $IP "*) ;; *) echo "[FAIL] $d does not resolve to $IP yet — Let's Encrypt would fail the same way. Wait for DNS and rerun."; exit 1 ;; esac
done

echo "== the challenge path is reachable over plain HTTP"
probe=probe-$RANDOM
docker run --rm -v $WEBROOT_VOL:/w alpine sh -c "mkdir -p /w/.well-known/acme-challenge && echo ok > /w/.well-known/acme-challenge/$probe"
for d in "$@"; do
  r=$(curl -s -m 10 "http://$d/.well-known/acme-challenge/$probe" || true)
  [ "$r" = ok ] || { echo "[FAIL] http://$d/.well-known/acme-challenge/ did not serve the probe (got: ${r:0:80})"; exit 1; }
  echo "  [ok] $d"
done
docker run --rm -v $WEBROOT_VOL:/w alpine rm -f /w/.well-known/acme-challenge/$probe

echo "== requesting the certificate"
args=(); for d in "$@"; do args+=(-d "$d"); done
mkdir -p "$CERTS"
docker run --rm -v "$CERTS":/etc/letsencrypt -v $WEBROOT_VOL:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  --cert-name "$1" "${args[@]}" \
  --agree-tos --register-unsafely-without-email --non-interactive --keep-until-expiring
ls -l "$CERTS/live/$1/"

echo "== daily renewal"
# certbot renews only inside the last 30 days, so running daily is cheap. The
# reload makes nginx pick up the new files; without it the old certificate
# stays in memory until the container restarts, and expires there.
cat > /etc/cron.d/locswap-certbot <<EOF
SHELL=/bin/bash
17 3 * * * root docker run --rm -v $CERTS:/etc/letsencrypt -v $WEBROOT_VOL:/var/www/certbot certbot/certbot renew --webroot -w /var/www/certbot -q && docker exec locswap-web-1 nginx -s reload >> /var/log/locswap-certbot.log 2>&1
EOF
chmod 644 /etc/cron.d/locswap-certbot
echo "[ok] certificate for $* issued; renewal installed in /etc/cron.d/locswap-certbot"
echo "     next: push the TLS nginx.conf (bash deploy/push.sh root@$IP)"
