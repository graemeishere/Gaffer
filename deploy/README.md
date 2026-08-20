# Deploying to a VPS

The engine runs to completion and exits — it is not a server, so there is
nothing to keep alive, nothing to restart and no socket to manage. Deployment is
a timer, a virtualenv, and a directory of static files.

## If the repository is private, do this first

GitHub removed password authentication for git over HTTPS in 2021. Cloning a
private repo that way fails with:

```
remote: Invalid username or token. Password authentication is not supported
```

No password works, because none is accepted. Pick one of these instead.

**A deploy key (recommended).** Read-only, scoped to this one repository, never
expires, and grants nothing else on your account:

```bash
sudo -u gaffer ssh-keygen -t ed25519 -f /srv/gaffer/.ssh/id_ed25519 -N "" -C "gaffer vps"
sudo cat /srv/gaffer/.ssh/id_ed25519.pub
```

Paste that into the repository's **Settings → Deploy keys → Add deploy key**,
leaving *Allow write access* unticked. `setup.sh` already clones over SSH, so
nothing else changes.

**A personal access token.** Fine-grained, `Contents: Read` on this repository
only, used as the password over HTTPS. Rotate it when it expires.

**Or make the repository public.** Nothing in it is secret — no credentials, no
tokens, and the prediction log is just numbers. This removes the problem
entirely and is the least work.

## Once, on the box

```bash
git clone git@github.com:graemeishere/Gaffer.git
cd Gaffer
sudo bash deploy/setup.sh
```

The script asks the remote which branch is its default rather than assuming
`main` — this repo's default is the working branch, and a hardcoded name is how
you end up cloning one that does not exist.

Override either if you need to:

```bash
sudo GAFFER_REPO=git@github.com:you/Gaffer.git GAFFER_BRANCH=main bash deploy/setup.sh
```

That installs Python and CBC, creates a `gaffer` service user, builds a
virtualenv, and enables a systemd timer that fires hourly.

**Hourly is deliberate.** The engine reads the next deadline and decides what is
due — idle beyond 48 hours, a full solve at 48, a final solve at 3 to catch team
news, and a sync afterwards to read the squads people actually picked. FPL
deadlines land on four different weekdays at six different clock times, so
anything on a fixed weekly schedule misses most of the season, including every
midweek round.

## Serving it

Copy `nginx.conf.example`, point it at `/srv/gaffer/web`, and run certbot. The
board is a static file, so there is no application server in the path.

## Updating

```bash
sudo bash deploy/setup.sh      # idempotent: pulls, reinstalls, restarts the timer
```

## Deploying automatically instead

`.github/workflows/deploy.yml` will do the above over SSH on every push, if you
add three repository secrets: `VPS_HOST`, `VPS_USER` and `VPS_SSH_KEY`. Generate
a key that can only do this job:

```bash
ssh-keygen -t ed25519 -f gaffer-deploy -C "gaffer deploy" -N ""
ssh-copy-id -i gaffer-deploy.pub youruser@your-vps
# paste the PRIVATE key into the VPS_SSH_KEY secret, then delete your local copy
```

Put the key in GitHub's secret store, never in a chat window or a commit.

## Checking it

```bash
systemctl list-timers gaffer.timer     # when it next fires
journalctl -u gaffer.service -n 50     # what happened last time
```
