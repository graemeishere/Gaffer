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

REPO="${GAFFER_REPO:-https://github.com/graemeishere/Timesplitters.git}"
BRANCH="${GAFFER_BRANCH:-main}"
HOME_DIR="${GAFFER_HOME:-/srv/gaffer}"
USER_NAME="${GAFFER_USER:-gaffer}"

log() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

log "Installing dependencies"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git coinor-cbc >/dev/null

log "Creating the service user"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$HOME_DIR"
chown -R "$USER_NAME:$USER_NAME" "$HOME_DIR"

log "Fetching the code"
if [[ -d "$HOME_DIR/.git" ]]; then
  sudo -u "$USER_NAME" git -C "$HOME_DIR" fetch --quiet origin "$BRANCH"
  sudo -u "$USER_NAME" git -C "$HOME_DIR" reset --hard --quiet "origin/$BRANCH"
else
  sudo -u "$USER_NAME" git clone --quiet --branch "$BRANCH" "$REPO" "$HOME_DIR"
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
