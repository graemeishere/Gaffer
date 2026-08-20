#!/usr/bin/env bash
#
# One-time setup on a Hostinger VPS (or any Debian/Ubuntu box).
#
#   curl -fsSL https://raw.githubusercontent.com/graemeishere/Timesplitters/main/deploy/setup.sh | less   # read it first
#   sudo bash deploy/setup.sh
#
# Idempotent: safe to re-run after a change. Read it before you run it — it
# installs packages, creates a user, and writes a systemd timer.

set -euo pipefail

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

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

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
  sudo -u "$USER_NAME" env HOME="$HOME_DIR" GIT_TERMINAL_PROMPT=0 "$@"
}

reachable() { as_service_user git ls-remote --heads "$1" >/dev/null 2>&1; }

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
    probe="$(as_service_user ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
             -T git@github.com 2>&1 | head -1)"
    echo "    github ssh says             ${probe:-(no response)}"
  else
    echo "    deploy key present          no"
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
  diagnose
  cat <<'HELP'
  Most likely one of:

    * The repository is private and this box has no credentials. Add a deploy
      key (below), or make the repository public — nothing in it is secret.
    * Outbound 22 is blocked, so ssh cannot work whatever key you add. If 443 is
      open and the repo is public, https will work: re-run with
          sudo GAFFER_REPO=https://github.com/OWNER/REPO.git bash deploy/setup.sh
    * The URL is wrong. Check the owner and repository name, including its case.

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
  as_service_user git -C "$HOME_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
  as_service_user git -C "$HOME_DIR" reset --hard --quiet "origin/$BRANCH"
else
  as_service_user git clone --quiet --branch "$BRANCH" "$REPO" "$HOME_DIR"
fi

log "Building the virtualenv"
sudo -u "$USER_NAME" python3 -m venv "$HOME_DIR/.venv"
sudo -u "$USER_NAME" "$HOME_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$USER_NAME" "$HOME_DIR/.venv/bin/pip" install --quiet -e "$HOME_DIR"

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
ExecStart=$HOME_DIR/.venv/bin/python -m gaffer.run --quiet
ExecStartPost=/bin/sh -c 'cp $HOME_DIR/data/latest.json $HOME_DIR/data/report.html $HOME_DIR/web/ 2>/dev/null || true'
TimeoutStartSec=600
Nice=10

# It only needs its own directory and outbound HTTPS.
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
NoNewPrivileges=true
ReadWritePaths=$HOME_DIR

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
sudo -u "$USER_NAME" "$HOME_DIR/.venv/bin/python" -m gaffer.run --quiet || {
  echo "First run failed — check: sudo journalctl -u gaffer.service -n 50"; exit 1; }
sudo -u "$USER_NAME" cp "$HOME_DIR/data/latest.json" "$HOME_DIR/data/report.html" "$HOME_DIR/web/" 2>/dev/null || true

cat <<DONE

  Done.

  Serve it          point nginx at $HOME_DIR/web  (see deploy/nginx.conf.example)
  Next runs         systemctl list-timers gaffer.timer
  Logs              journalctl -u gaffer.service -n 50
  Run it now        sudo -u $USER_NAME $HOME_DIR/.venv/bin/python -m gaffer.run
  Score the model   sudo -u $USER_NAME $HOME_DIR/.venv/bin/python -m gaffer.score

  Add your IDs to $HOME_DIR/.env once you have them:
      GAFFER_ENTRY=your_team_id
      GAFFER_LEAGUE=your_league_id

DONE
