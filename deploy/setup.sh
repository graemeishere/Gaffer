#!/usr/bin/env bash
#
# One-time setup on a Hostinger VPS (or any Debian/Ubuntu box).
#
#   curl -fsSL https://raw.githubusercontent.com/graemeishere/Gaffer/HEAD/deploy/setup.sh | less   # read it first
#   sudo bash deploy/setup.sh                 install or update
#   sudo bash deploy/setup.sh --serve [host]  install nginx and publish the board
#                                             GAFFER_PORT=8080 if 80 is taken
#   sudo bash deploy/setup.sh --reset         remove everything and start again
#   sudo bash deploy/setup.sh --deploy-key    print the key to add to GitHub
#
# Idempotent: safe to re-run after a change. Read it before you run it — it
# installs packages, creates a user, and writes a systemd timer.

set -euo pipefail
# Move somewhere every user can enter, before anything drops privileges.
#
# git stats its working directory before doing anything at all, so a command run
# as the service user from a directory that user cannot enter fails immediately —
# and cloning this repo into /root, which is mode 700, is a completely natural
# thing to do. The failure reads "failed to stat '/root/...': Permission denied",
# names a local path, and looks nothing like an access problem, which sends you
# hunting through repository visibility, deploy keys and firewall rules for a
# fault that is none of them.
#
# Every path below is absolute, so there is nothing to lose by leaving.
cd /

# Default to HTTPS: a public repository needs no credentials at all, and many
# hosts block outbound port 22 so SSH can fail for reasons no key will fix. If
# HTTPS cannot reach it — a private repo, since GitHub stopped accepting
# passwords for git in 2021 — the script falls back to SSH and helps set up a
# deploy key.
REPO="${GAFFER_REPO:-https://github.com/graemeishere/Gaffer.git}"
REPO_SSH="${GAFFER_REPO_SSH:-git@github.com:graemeishere/Gaffer.git}"
# Empty means "whatever the remote's default branch is". Hardcoding a branch
# name is how you end up cloning one that does not exist.
BRANCH="${GAFFER_BRANCH:-}"
HOME_DIR="${GAFFER_HOME:-/srv/gaffer}"
USER_NAME="${GAFFER_USER:-gaffer}"

# Both live outside the checkout: anything the engine writes into a tracked file
# leaves the working tree dirty and breaks the next update. Declared here with
# the rest of the configuration because `set -u` makes a variable referenced
# before assignment a fatal error, and --serve reads them early.
STATE_DIR="${GAFFER_STATE_DIR:-/var/lib/gaffer}"
PUBLISH_DIR="${GAFFER_PUBLISH_DIR:-/var/www/gaffer}"

# Which port nginx listens on. Default 80, but plenty of boxes already have
# something there — a Docker reverse proxy is the common one — and nginx simply
# refuses to start rather than sharing. Set GAFFER_PORT to a free port and put
# the existing proxy in front.
PORT="${GAFFER_PORT:-80}"

log() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }

# The key types GitHub will accept in a deploy key field.
KEY_TYPES="ssh-rsa ssh-ed25519 ecdsa-sha2-nistp256 ecdsa-sha2-nistp384 ecdsa-sha2-nistp521 sk-ecdsa-sha2-nistp256@openssh.com sk-ssh-ed25519@openssh.com"

show_deploy_key() {
  # Generate the key if it is missing, then print the PUBLIC half and nothing
  # else. GitHub validates the first word of what you paste, so the usual
  # failure is pasting the private key — same directory, one character
  # shorter, and completely wrong. Printing only the checked value removes
  # the chance of picking the wrong file.
  local key="$HOME_DIR/.ssh/id_ed25519"
  if [[ ! -f "$key.pub" ]]; then
    log "Generating a deploy key"
    sudo -u "$USER_NAME" ssh-keygen -t ed25519 -f "$key" -N "" -C "gaffer@$(hostname)" -q
  fi

  local first
  first="$(awk '{print $1; exit}' "$key.pub")"
  if [[ " $KEY_TYPES " != *" $first "* ]]; then
    echo "  $key.pub does not look like a public key (starts with '$first')." >&2
    echo "  Delete $key and $key.pub and re-run to regenerate." >&2
    return 1
  fi

  cat <<BANNER

  ---------------------------------------------------------------------------
  Copy the single line between the markers — all of it, nothing either side.

  It is the PUBLIC key. The private one lives beside it in $key
  and must never leave this machine.
  ---------------------------------------------------------------------------

BANNER
  echo "----- copy from here -----"
  cat "$key.pub"
  echo "----- to here -----"
  cat <<BANNER

  Paste it into GitHub:
     the repository -> Settings -> Deploy keys -> Add deploy key
     Title: anything.  Allow write access: leave unticked.

  Then run this script again.

BANNER
}

[[ ${1:-} == "--deploy-key" ]] && DEPLOY_KEY_ONLY=1 || DEPLOY_KEY_ONLY=0
[[ ${1:-} == "--reset" ]] && RESET=1 || RESET=0
[[ ${1:-} == "--serve" ]] && SERVE=1 || SERVE=0
SERVER_NAME="${2:-_}"

install_web_server() {
  # Serve the board over http. There is no application server involved — the
  # engine writes files and exits — so this is nginx pointed at a directory.
  # Check the port before installing anything. nginx will not share a port, and
  # finding that out after an install and a config write is a worse experience
  # than being told up front.
  local holder
  holder="$(ss -tlnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$" {print $NF; exit}')"
  if [[ -n "$holder" && "$holder" != *nginx* ]]; then
    cat <<CONFLICT

  Port $PORT is already taken by:
      $holder

  nginx will not share a port. Two options:

  1. Serve on a spare port and route to it from whatever already owns $PORT.
     If that is a Docker reverse proxy, this is what you want:

         sudo GAFFER_PORT=8080 bash deploy/setup.sh --serve $SERVER_NAME

     then add a proxy host in it pointing at http://172.17.0.1:8080
     (or the host's LAN address) for $SERVER_NAME.

  2. Stop whatever is holding $PORT, if it is not doing anything you need.

CONFLICT
    exit 1
  fi

  log "Installing nginx"
  apt-get install -y -qq nginx >/dev/null

  # nginx runs as www-data and has to traverse the service user's home to reach
  # the files. Only the published directory is opened up; the rest of the home,
  # including .env and the ssh key, stays private.
  chmod o+rx "$PUBLISH_DIR"
  chmod -R o+rX "$PUBLISH_DIR"

  log "Writing the site"
  cat > /etc/nginx/sites-available/gaffer <<NGINX
server {
    listen $PORT;
    listen [::]:$PORT;
    server_name $SERVER_NAME;

    root $PUBLISH_DIR;
    index report.html index.html;

    # Rewritten every run, so it must never be cached.
    location ~* \.(json|html)\$ {
        add_header Cache-Control "no-store, must-revalidate";
        try_files \$uri =404;
    }

    location / {
        try_files \$uri \$uri/ /report.html;
    }

    location ~ /\. { deny all; }
}
NGINX

  rm -f /etc/nginx/sites-enabled/default
  ln -sf /etc/nginx/sites-available/gaffer /etc/nginx/sites-enabled/gaffer

  log "Checking the configuration"
  if ! nginx -t >/dev/null 2>&1; then
    nginx -t
    echo
    echo "  nginx rejected the configuration. Nothing has been started."
    exit 1
  fi

  log "Starting nginx"
  systemctl enable nginx >/dev/null 2>&1 || true
  # restart, not reload: reload fails outright on a service that is not already
  # running, and a fresh install often is not. restart is correct either way.
  if ! systemctl restart nginx; then
    echo
    echo "  nginx would not start. The usual cause is something else already"
    echo "  holding port 80:"
    echo
    ss -tlnp 2>/dev/null | grep -E ':80\s|:443\s' || echo "    (nothing else listening on 80/443)"
    echo
    echo "  Recent log:"
    journalctl -u nginx -n 15 --no-pager 2>/dev/null | sed 's/^/    /'
    exit 1
  fi

  systemctl is-active --quiet nginx || { echo "  nginx did not stay running."; exit 1; }

  local address
  address="$(hostname -I 2>/dev/null | awk '{print $1}')"
  cat <<DONE

  Serving $PUBLISH_DIR on port $PORT

     http://${address:-your-server-ip}:$PORT/

  If a domain points here, re-run as
     sudo bash deploy/setup.sh --serve fpl.yourdomain.com
  and then add a certificate:
     sudo apt install -y certbot python3-certbot-nginx
     sudo certbot --nginx -d fpl.yourdomain.com

  If the page does not load, the firewall is the usual reason:
     sudo ufw allow 80/tcp    # and 443/tcp once certbot has run

DONE
}

reset_everything() {
  # Remove the timer, the unit files, the service user and its directory.
  # Safe to run: the prediction log lives in the repository, not here, so a wipe
  # loses nothing that a fetch will not bring straight back.
  echo "Removing the timer and unit files"
  systemctl disable --now gaffer.timer >/dev/null 2>&1 || true
  systemctl stop gaffer.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/gaffer.service /etc/systemd/system/gaffer.timer
  systemctl daemon-reload >/dev/null 2>&1 || true

  echo "Removing the published container, if any"
  docker rm -f "${GAFFER_CONTAINER:-gaffer-web}" >/dev/null 2>&1 || true

  echo "Removing the nginx site"
  rm -f /etc/nginx/sites-enabled/gaffer /etc/nginx/sites-available/gaffer
  # Only reload if nginx is running and still has a valid config; on a box where
  # nginx never started, leave it alone rather than reporting a failure that is
  # not one.
  if systemctl is-active --quiet nginx && nginx -t >/dev/null 2>&1; then
    systemctl reload nginx >/dev/null 2>&1 || true
  fi

  echo "Removing $HOME_DIR, $STATE_DIR and $PUBLISH_DIR"
  rm -rf "$HOME_DIR" "$STATE_DIR" "$PUBLISH_DIR"

  echo "Removing the $USER_NAME user"
  userdel "$USER_NAME" >/dev/null 2>&1 || true

  echo
  echo "  Clean. Nothing else on the box was touched — any Docker stack,"
  echo "  nginx itself and its other sites are all untouched."
  echo
  echo "  Run 'sudo bash deploy/setup.sh' to start again."
}

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }


if [[ $RESET -eq 1 ]]; then
  reset_everything
  exit 0
fi

if [[ $SERVE -eq 1 ]]; then
  [[ -d "$PUBLISH_DIR" ]] || { echo "Run setup.sh first — $PUBLISH_DIR does not exist."; exit 1; }
  apt-get update -qq
  install_web_server
  exit 0
fi

log "Installing dependencies"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git coinor-cbc >/dev/null

log "Creating the service user"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/bash "$USER_NAME"
mkdir -p "$HOME_DIR/.ssh"
chmod 700 "$HOME_DIR/.ssh"
# Trust GitHub's host key up front so the first clone is not blocked by a prompt
# the service user can never answer.
sudo -u "$USER_NAME" ssh-keyscan -t ed25519 github.com >> "$HOME_DIR/.ssh/known_hosts" 2>/dev/null || true
sort -u "$HOME_DIR/.ssh/known_hosts" -o "$HOME_DIR/.ssh/known_hosts" 2>/dev/null || true
chown -R "$USER_NAME:$USER_NAME" "$HOME_DIR"

if [[ $DEPLOY_KEY_ONLY -eq 1 ]]; then
  show_deploy_key
  exit $?
fi

as_service_user() {
  # Set HOME explicitly. Whether sudo does this for you depends on the sudoers
  # configuration, and ssh looks for its keys under $HOME — so leaving it to
  # chance means the key is found on one machine and not the next.
  ( cd / && sudo -u "$USER_NAME" env HOME="$HOME_DIR" GIT_TERMINAL_PROMPT=0 "$@" )
}

LAST_GIT_ERROR=""

reachable() {
  # Keep git's own message. It is the most useful line in any failure here:
  # "Repository not found" (private, or the wrong name) and "could not read
  # Username" (credentials wanted) need completely different fixes, and
  # checking only the exit code throws both away.
  LAST_GIT_ERROR="$(as_service_user git ls-remote --heads "$1" 2>&1 >/dev/null)"
  [[ -z "$LAST_GIT_ERROR" ]]
}

diagnose() {
  # Say what is actually broken instead of guessing. Each layer is checked
  # separately, because "cannot reach the repository" has several very different
  # causes and the fix for one is useless for the others.
  echo
  echo "  Diagnosing:"
  if getent hosts github.com >/dev/null 2>&1; then
    echo "    DNS for github.com          ok"
  else
    echo "    DNS for github.com          FAILED — this box cannot resolve names"
    echo "    Nothing else will work until that is fixed."
    return
  fi

  if timeout 8 bash -c ':> /dev/tcp/github.com/443' 2>/dev/null; then
    echo "    outbound 443 (https)        ok"
    local https_ok=1
  else
    echo "    outbound 443 (https)        BLOCKED — check the firewall"
    local https_ok=0
  fi

  if timeout 8 bash -c ':> /dev/tcp/github.com/22' 2>/dev/null; then
    echo "    outbound 22 (ssh)           ok"
  else
    echo "    outbound 22 (ssh)           BLOCKED"
    echo "                                No deploy key can work over a blocked port."
    if [[ $https_ok -eq 1 ]]; then
      echo "                                Use https instead — see below."
    fi
  fi

  if [[ -f "$HOME_DIR/.ssh/id_ed25519" ]]; then
    echo "    deploy key present          yes ($HOME_DIR/.ssh/id_ed25519)"
    local probe
    probe="$(timeout 15 sudo -u "$USER_NAME" env HOME="$HOME_DIR" ssh \
             -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
             -T git@github.com 2>&1 | head -1)"
    echo "    github ssh says             ${probe:-(no response)}"
  else
    echo "    deploy key present          no"
  fi

  # Can root see it when the service user cannot? Then this is credentials, not
  # connectivity: root very often holds a token cached from an earlier clone
  # that the service user has no access to.
  local as_root
  as_root="$(GIT_TERMINAL_PROMPT=0 timeout 25 git ls-remote --heads "$REPO" 2>&1 >/dev/null)"
  if [[ -z "$as_root" ]]; then
    echo "    as root over https          ok  <- root sees it, $USER_NAME does not"
    echo "                                    Credentials, not the network. Either the"
    echo "                                    repository is still private, or root has a"
    echo "                                    token cached from when you cloned it."
  else
    echo "    as root over https          also fails"
  fi
  echo
}

log "Checking access to the repository"
if reachable "$REPO"; then
  log "Reachable over https — no credentials needed"
elif reachable "$REPO_SSH"; then
  log "https failed but ssh works — using $REPO_SSH"
  REPO="$REPO_SSH"
else
  echo
  echo "  Cannot reach the repository by either route:"
  echo "    $REPO"
  echo "    $REPO_SSH"
  echo
  echo "  git said:"
  printf '    %s\n' "${LAST_GIT_ERROR:-(no message)}"
  diagnose
  cat <<'HELP'
  Read git's message above first — it names the fault.

    "Repository not found"      Private, or the owner/name is wrong. GitHub
                                answers this rather than "denied" so that
                                private repositories stay unlisted, so check
                                the visibility AND the spelling, case included.

    "could not read Username"   It wants credentials. Make the repository
                                public, or add a deploy key (printed below).

    "Permission denied"         The key was rejected. Check the deploy key is on
                                THIS repository and not another one.

    "Could not resolve host" /  Network. See the layers above.
    "Connection timed out"

  If every network layer above is ok and root can see the repository while the
  service user cannot, it is still private and root is using a cached token.
  Making it public fixes that outright.

HELP
  if [[ -f "$HOME_DIR/.ssh/id_ed25519.pub" ]]; then
    echo "  A deploy key already exists here. If you have added it to GitHub and"
    echo "  this still fails, the problem is not the key — see the diagnosis above."
    echo
  else
    show_deploy_key || true
  fi
  exit 1
fi

# Ask the remote what its default branch is rather than assuming.
if [[ -z "$BRANCH" ]]; then
  BRANCH="$(as_service_user git ls-remote --symref "$REPO" HEAD 2>/dev/null \
            | sed -n 's|^ref: refs/heads/\([^\t]*\).*|\1|p' | head -1)"
  [[ -n "$BRANCH" ]] || BRANCH="$(as_service_user git ls-remote --heads "$REPO" \
            | sed -n 's|.*refs/heads/||p' | head -1)"
fi
log "Using branch $BRANCH"

log "Fetching the code"
if [[ -d "$HOME_DIR/.git" ]]; then
  as_service_user git -C "$HOME_DIR" remote set-url origin "$REPO"
  as_service_user git -C "$HOME_DIR" fetch --quiet origin "$BRANCH"
  # Discard anything the engine has written into tracked files before switching.
  # They are all regenerated on the next run, and leaving them makes the update
  # fail with "local changes would be overwritten".
  as_service_user git -C "$HOME_DIR" reset --hard --quiet HEAD
  as_service_user git -C "$HOME_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
  as_service_user git -C "$HOME_DIR" reset --hard --quiet "origin/$BRANCH"
else
  # Initialise in place rather than cloning. The directory usually already
  # exists by now — creating the service user makes its home, and a deploy key
  # may have been written into it — and `git clone` refuses any target that is
  # not empty. Fetching into an initialised directory has the same result and
  # leaves whatever was already there untouched.
  as_service_user git init --quiet "$HOME_DIR"
  as_service_user git -C "$HOME_DIR" remote add origin "$REPO" 2>/dev/null \
    || as_service_user git -C "$HOME_DIR" remote set-url origin "$REPO"
  as_service_user git -C "$HOME_DIR" fetch --quiet origin "$BRANCH"
  as_service_user git -C "$HOME_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
fi

log "Preparing state and publish directories"
# Outside the git checkout on purpose: anything the engine writes into a tracked
# file dirties the working tree and breaks the next update.
mkdir -p "$STATE_DIR" "$PUBLISH_DIR"
chown -R "$USER_NAME:$USER_NAME" "$STATE_DIR" "$PUBLISH_DIR"
# Carry over a record from a previous layout, so history is not lost.
if [[ -f "$HOME_DIR/record/predictions.csv" && ! -f "$STATE_DIR/predictions.csv" ]]; then
  cp "$HOME_DIR/record/predictions.csv" "$STATE_DIR/" 2>/dev/null || true
  cp "$HOME_DIR/record/actuals.csv" "$STATE_DIR/" 2>/dev/null || true
  chown -R "$USER_NAME:$USER_NAME" "$STATE_DIR"
fi

log "Building the virtualenv"
sudo -u "$USER_NAME" python3 -m venv "$HOME_DIR/.venv"
sudo -u "$USER_NAME" "$HOME_DIR/.venv/bin/pip" install --quiet --upgrade pip
( cd "$HOME_DIR" && sudo -u "$USER_NAME" "$HOME_DIR/.venv/bin/pip" install --quiet -e "$HOME_DIR" )

log "Installing the hourly timer"
# Hourly on purpose. The engine reads the next deadline and decides what is due
# (see gaffer/schedule.py) — FPL deadlines land on four weekdays at six clock
# times, so anything on a fixed weekly schedule misses most of the season.
cat > /etc/systemd/system/gaffer.service <<UNIT
[Unit]
Description=Gaffer — FPL engine run
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$HOME_DIR
# Optional, hence the leading dash: the unit starts fine before the file exists.
EnvironmentFile=-$HOME_DIR/.env
Environment=GAFFER_STATE_DIR=$STATE_DIR
Environment=GAFFER_PUBLISH_DIR=$PUBLISH_DIR
ExecStart=$HOME_DIR/.venv/bin/python -m gaffer.run --quiet
ExecStartPost=/bin/sh -c 'cp -f $HOME_DIR/web/*.html $PUBLISH_DIR/ 2>/dev/null; cp -f $HOME_DIR/data/latest.json $HOME_DIR/data/report.html $PUBLISH_DIR/ 2>/dev/null || true'
TimeoutStartSec=600
Nice=10

# It only needs its own directory and outbound HTTPS.
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
NoNewPrivileges=true
ReadWritePaths=$HOME_DIR $STATE_DIR $PUBLISH_DIR

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/gaffer.timer <<UNIT
[Unit]
Description=Run Gaffer hourly; the engine decides what work is due

[Timer]
OnCalendar=hourly
# Spread the load off the hour, and catch up after a reboot.
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now gaffer.timer >/dev/null

log "First run (this may take a minute)"
( cd "$HOME_DIR" && sudo -u "$USER_NAME" env HOME="$HOME_DIR" \
    GAFFER_STATE_DIR="$STATE_DIR" GAFFER_PUBLISH_DIR="$PUBLISH_DIR" \
    "$HOME_DIR/.venv/bin/python" -m gaffer.run --quiet ) || {
  echo "First run failed — check: sudo journalctl -u gaffer.service -n 50"; exit 1; }
# Publish the whole static site, into the publish directory rather than the
# checkout. index.html ships in the repo and is what a bare nginx looks for; a
# directory holding only report.html gives every request for / a 403 Forbidden,
# which reads like a permissions fault and is really a missing default document.
( cd / && sudo -u "$USER_NAME" sh -c \
  "cp -f '$HOME_DIR'/web/*.html '$PUBLISH_DIR'/ 2>/dev/null; \
   cp -f '$HOME_DIR'/data/latest.json '$HOME_DIR'/data/report.html '$PUBLISH_DIR'/ 2>/dev/null" ) || true

cat <<DONE

  Done.

  See it in a browser   sudo bash deploy/setup.sh --serve
  Next runs         systemctl list-timers gaffer.timer
  Logs              journalctl -u gaffer.service -n 50
  Run it now        sudo -u $USER_NAME $HOME_DIR/.venv/bin/python -m gaffer.run
  Score the model   sudo -u $USER_NAME $HOME_DIR/.venv/bin/python -m gaffer.score

  Add your IDs once you have them — no editor needed:

      sudo tee $HOME_DIR/.env >/dev/null <<'EOF'
      GAFFER_ENTRY=your_team_id
      GAFFER_LEAGUE=your_league_id
      EOF
      sudo chown $USER_NAME:$USER_NAME $HOME_DIR/.env && sudo chmod 600 $HOME_DIR/.env

  Then check they were picked up:

      sudo -u $USER_NAME $HOME_DIR/.venv/bin/python -m gaffer.run --quiet && echo ok

DONE
