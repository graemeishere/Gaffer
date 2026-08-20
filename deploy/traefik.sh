#!/usr/bin/env bash
#
# Publish the board through an existing Traefik.
#
#   sudo bash deploy/traefik.sh fpl.example.com
#
# For a box that already runs Traefik on 80/443 — an n8n stack, for instance.
# Rather than installing a second web server and fighting over the port, this
# serves the files from a small container that Traefik already knows how to
# route to, and lets Traefik keep doing the TLS it is already doing.

set -euo pipefail
cd /

HOST="${1:-}"
PUBLISH_DIR="${GAFFER_PUBLISH_DIR:-/var/www/gaffer}"
NAME="${GAFFER_CONTAINER:-gaffer-web}"

log() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
[[ -n "$HOST" ]] || { echo "Usage: sudo bash deploy/traefik.sh fpl.example.com"; exit 1; }
command -v docker >/dev/null || { echo "docker is not installed."; exit 1; }
[[ -d "$PUBLISH_DIR" ]] || { echo "$PUBLISH_DIR does not exist — run setup.sh first."; exit 1; }
if ! compgen -G "$PUBLISH_DIR/*.html" >/dev/null; then
  echo "$PUBLISH_DIR holds no HTML — run setup.sh first so there is something to serve."
  exit 1
fi

log "Finding Traefik"
TRAEFIK="$(docker ps --filter ancestor=traefik --format '{{.Names}}' | head -1)"
[[ -n "$TRAEFIK" ]] || TRAEFIK="$(docker ps --format '{{.Names}}' | grep -i traefik | head -1)"
[[ -n "$TRAEFIK" ]] || { echo "No running Traefik container found."; exit 1; }
echo "    container: $TRAEFIK"

# The router has to sit on the same Docker network as Traefik, or Traefik cannot
# reach it however correct the labels are.
NETWORK="$(docker inspect "$TRAEFIK" \
  --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -1)"
[[ -n "$NETWORK" ]] || { echo "Could not determine Traefik's network."; exit 1; }
echo "    network:   $NETWORK"

# Reuse whatever certificate resolver the existing routers use, so the
# certificate is issued the same way as everything else on this box.
RESOLVER="${GAFFER_CERTRESOLVER:-}"
if [[ -z "$RESOLVER" ]]; then
  RESOLVER="$(docker inspect "$TRAEFIK" --format '{{join .Config.Cmd " "}}' 2>/dev/null \
    | grep -oE 'certificatesresolvers\.[a-zA-Z0-9_-]+' | head -1 | cut -d. -f2)"
fi
if [[ -z "$RESOLVER" ]]; then
  RESOLVER="$(docker ps -q | xargs -r docker inspect \
    --format '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' 2>/dev/null \
    | grep -oE 'tls\.certresolver=[a-zA-Z0-9_-]+' | head -1 | cut -d= -f2)"
fi
echo "    resolver:  ${RESOLVER:-none found — will serve without TLS}"

# Which entrypoint name Traefik uses for 443 varies by stack; take it from an
# existing router rather than assuming "websecure".
ENTRYPOINT="${GAFFER_ENTRYPOINT:-}"
if [[ -z "$ENTRYPOINT" ]]; then
  ENTRYPOINT="$(docker ps -q | xargs -r docker inspect \
    --format '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' 2>/dev/null \
    | grep -oE 'entrypoints=[a-zA-Z0-9_,-]+' | head -1 | cut -d= -f2)"
fi
ENTRYPOINT="${ENTRYPOINT:-websecure}"
echo "    entrypoint: $ENTRYPOINT"

# Give the container its own nginx config rather than relying on the image's.
# The stock one looks for index.html and nothing else, so a directory holding
# report.html and no index answers / with 403 Forbidden — a permissions-shaped
# error for what is really a missing default document. Kept outside the served
# directory so it cannot be fetched.
CONF="/etc/gaffer/nginx-site.conf"
mkdir -p /etc/gaffer
cat > "$CONF" <<'NGINX'
server {
    listen 80;
    root /usr/share/nginx/html;

    # The full board first, then the live sortable table.
    index report.html index.html;

    # Rewritten on every engine run, so it must never be cached.
    location ~* \.(json|html)$ {
        add_header Cache-Control "no-store, must-revalidate";
        try_files $uri =404;
    }

    location / {
        try_files $uri $uri/ /report.html;
    }
}
NGINX

log "Starting $NAME"
docker rm -f "$NAME" >/dev/null 2>&1 || true

ARGS=(
  -d --name "$NAME" --restart unless-stopped
  --network "$NETWORK"
  -v "$PUBLISH_DIR:/usr/share/nginx/html:ro"
  -v "$CONF:/etc/nginx/conf.d/default.conf:ro"
  --label "traefik.enable=true"
  --label "traefik.docker.network=$NETWORK"
  --label "traefik.http.routers.gaffer.rule=Host(\`$HOST\`)"
  --label "traefik.http.routers.gaffer.entrypoints=$ENTRYPOINT"
  --label "traefik.http.services.gaffer.loadbalancer.server.port=80"
)
[[ -n "$RESOLVER" ]] && ARGS+=( --label "traefik.http.routers.gaffer.tls.certresolver=$RESOLVER" )

docker run "${ARGS[@]}" nginx:alpine >/dev/null
sleep 2

if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo
  echo "  The container did not stay up:"
  docker logs "$NAME" 2>&1 | tail -15 | sed 's/^/    /'
  exit 1
fi

cat <<DONE

  Serving $PUBLISH_DIR through $TRAEFIK

     https://$HOST/

  The certificate is issued by Traefik on first request, so give it a few
  seconds. If it does not appear:

     docker logs $TRAEFIK --tail 30
     docker logs $NAME --tail 30

  DNS must already point at this box, or the certificate cannot be issued.

  To remove it again:
     docker rm -f $NAME

DONE
