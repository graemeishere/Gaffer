# Deploying to a VPS

The engine runs to completion and exits — it is not a server, so there is
nothing to keep alive, nothing to restart and no socket to manage. Deployment is
a timer, a virtualenv, and a directory of static files.

## If the repository is public, there is nothing to set up

`setup.sh` clones over https, which needs no credentials at all. No key, no
token, no account. Skip to the next section.

If you hit this while the repo was private and the script keeps printing a
deploy key even after you have added it, the key is almost certainly not the
problem. **Many hosts block outbound port 22**, and no key can work over a
blocked port. Check:

```bash
timeout 5 bash -c ':> /dev/tcp/github.com/22'  && echo "22 open" || echo "22 blocked"
timeout 5 bash -c ':> /dev/tcp/github.com/443' && echo "443 open" || echo "443 blocked"
```

If 443 is open and 22 is not, use https and forget the key:

```bash
sudo GAFFER_REPO=https://github.com/graemeishere/Gaffer.git bash deploy/setup.sh
```

Current versions of the script try https first, fall back to ssh, and print a
layer-by-layer diagnosis rather than assuming the cause.

## If the repository is private, do this first

GitHub removed password authentication for git over HTTPS in 2021. Cloning a
private repo that way fails with:

```
remote: Invalid username or token. Password authentication is not supported
```

No password works, because none is accepted. Pick one of these instead.

**A deploy key (recommended).** Read-only, scoped to this one repository, never
expires, and grants nothing else on your account. Let the script produce it:

```bash
sudo bash deploy/setup.sh --deploy-key
```

That generates the key if it does not exist, checks it is the public half, and
prints just that line between two markers. Copy everything between them into the
repository's **Settings → Deploy keys → Add deploy key**, leaving *Allow write
access* unticked, then re-run `sudo bash deploy/setup.sh`.

### "Begins with 'ssh-rsa', 'ssh-ed25519', …"

GitHub checks the **first word** of what you paste. That message means the first
word was not a key type, and it is nearly always one of two things:

- **You pasted the private key.** The two files sit side by side and differ by
  four characters: `id_ed25519` is private, `id_ed25519.pub` is public. The
  private one begins `-----BEGIN OPENSSH PRIVATE KEY-----`, spans many lines,
  and must never leave the machine. The public one is a single line beginning
  `ssh-ed25519`.
- **The copy picked up something extra** — a shell prompt, the command itself,
  or a line break inserted by the terminal. The public key is one unbroken line;
  if what you pasted wraps, it is wrong.

Running with `--deploy-key` avoids both, because it prints only the value it has
already checked.

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

## Seeing it in a browser

```bash
sudo bash deploy/setup.sh --serve
```

Installs nginx, points it at `/srv/gaffer/web`, and prints the address. The
board is a static file — the engine writes it and exits — so there is no
application server in the path and nothing to keep running.

With a domain pointing at the box:

```bash
sudo bash deploy/setup.sh --serve fpl.yourdomain.com
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d fpl.yourdomain.com
```

If the page will not load, the firewall is the usual reason: `sudo ufw allow
80/tcp`, and 443 once certbot has run.

### Something already owns port 80

Common on a VPS that came with Docker — a reverse proxy container binds 80 and
443, and nginx refuses to start rather than sharing them:

```
nginx: [emerg] bind() to 0.0.0.0:80 failed (98: Address already in use)
```

Check what has it:

```bash
sudo ss -tlnp | grep -E ':80|:443'
docker ps
```

Do not fight it — route through the proxy that is already there.

**If it is Traefik** (an n8n stack ships one), skip host nginx entirely:

```bash
sudo bash deploy/traefik.sh fpl.yourdomain.com
```

Traefik routes by Docker labels, so the board is served from a small
`nginx:alpine` container carrying the right ones, mounting `/var/www/gaffer`
read-only. The script reads Traefik's network, certificate resolver and HTTPS
entrypoint off the running container rather than assuming them — those names
differ between stacks, and a guess produces a router Traefik silently ignores.
TLS stays with Traefik, which is already doing it.

**For any other proxy**, serve on a spare port and point it there:

```bash
sudo GAFFER_PORT=8080 bash deploy/setup.sh --serve fpl.yourdomain.com
```

then add a host in the proxy pointing at `http://172.17.0.1:8080` — the host as
seen from inside Docker. Let it handle the certificate; it already terminates
TLS on 443, so certbot on the host is neither needed nor able to bind.

`setup.sh --serve` now checks the port first and names whatever holds it,
instead of installing nginx and failing afterwards.

**For a quick look without installing anything**, serve the directory
temporarily and stop it with ctrl-c:

```bash
cd /srv/gaffer/web && sudo python3 -m http.server 8080
# then open http://your-server-ip:8080/report.html
```

`nginx.conf.example` is still there if you would ratherconfigure it yourself.

## Updating

```bash
sudo bash deploy/setup.sh      # idempotent: fetches, reinstalls, restarts the timer
```

## Your team and league IDs

The engine only gives personalised advice once it knows which team is yours.
Write them into `/srv/gaffer/.env` — no editor required:

```bash
sudo tee /srv/gaffer/.env >/dev/null <<'EOF'
GAFFER_ENTRY=1234567
GAFFER_LEAGUE=987654
EOF
sudo chown gaffer:gaffer /srv/gaffer/.env
sudo chmod 600 /srv/gaffer/.env
```

Both are visible in the Fantasy Premier League site's own URLs: your team ID is
the number in `/entry/1234567/event/1`, and the league ID is the number in
`/leagues/987654/standings/c`.

The file is read by the package itself, not only by systemd, so a run started by
hand, by cron or by the timer all behave the same. Real environment variables
take precedence, so `--entry` on the command line still overrides it. `.env` is
gitignored and never committed.

Check it took:

```bash
sudo -u gaffer /srv/gaffer/.venv/bin/python -m gaffer.run
```

## "local changes would be overwritten"

The engine writes a prediction log and a published board on every run. If those
land inside the git checkout they leave the working tree dirty, and the next
update fails — hourly, once the timer is running.

`setup.sh` now keeps both outside the checkout:

| | |
|---|---|
| `/var/lib/gaffer` | the prediction log and results |
| `/var/www/gaffer` | the board nginx serves |

Both are configurable with `GAFFER_STATE_DIR` and `GAFFER_PUBLISH_DIR`. Left
unset they stay in the repository, which is what CI wants — there the checkout
is thrown away after every run and committing the log is the whole point.

An existing `record/` inside the checkout is copied across on the next install,
so no history is lost. If an update still complains, discard the generated files
and re-run:

```bash
sudo -u gaffer git -C /srv/gaffer reset --hard
sudo bash deploy/setup.sh
```

## 403 Forbidden from the published site

Not a permissions problem, despite what the status code suggests. nginx answers
403 when a request for `/` finds no default document and directory listing is
off — so a publish directory holding `report.html` but no `index.html` produces
it, and looks exactly like a file-mode fault that it is not.

Two things now prevent it: the whole static site is published rather than only
the two generated files, and the container is given an nginx config naming
`report.html` as the index instead of relying on the image's default of
`index.html`.

If an older install is still doing it:

```bash
sudo cp /srv/gaffer/web/index.html /var/www/gaffer/
ls -la /var/www/gaffer          # expect index.html, report.html, latest.json
```

## Starting over

```bash
sudo bash deploy/setup.sh --reset
```

Removes the timer, the unit files, the service user and `/srv/gaffer`. Safe: the
prediction log lives in the repository, not on the box, so a wipe loses nothing
a fetch will not bring straight back.

You should not normally need this. The install initialises the checkout in place
rather than cloning, because the directory usually already exists by the time it
runs — creating the service user makes its home — and `git clone` refuses any
target that is not empty.

## Recording the team you have picked (the Edit team tab)

The public FPL API will not reveal a gameweek's picks until its deadline locks
them, so between making your transfers and the deadline the board can only read
your last locked side. The **Edit team** tab lets you record the changes you
have made, so the board shows — and advises on — the team you are actually going
to field. It clears itself once the deadline passes and the real team comes
through.

`setup.sh` installs everything for this automatically:

- **`gaffer-api.service`** — a tiny endpoint (`gaffer.serve`) that accepts one
  validated team and writes it to `myteam.json` in the state directory. It runs
  as the service user, listens only on the Docker bridge (never a public port),
  and refuses every write without the key.
- **A write key** — generated once into `<state dir>/write.env`, surviving
  redeploys and database wipes. `setup.sh` prints it at the end; paste it into
  the Edit team tab once (it is kept in your browser). Reprint it any time with
  `sudo sed -n 's/^GAFFER_WRITE_TOKEN=//p' <state dir>/write.env`.
- **A scoped sudoers rule** (`/etc/sudoers.d/gaffer`) letting the endpoint ask
  the engine to republish after a save — that one command, nothing else — so the
  board refreshes in seconds rather than at the next hourly run.

`deploy/traefik.sh` routes the single path `/<your domain>/api/` to the endpoint
through the same container that serves the board; the rest of the site stays
static files.

**Do not open port 8081 at the firewall.** Only 80/443 are public; the endpoint
is reached from the proxy container over the Docker bridge, and a write needs the
key regardless. If the tab reports it cannot reach the server, check the endpoint
is up (`systemctl status gaffer-api.service`) and that `traefik.sh` has been
re-run since this was added, so the container carries the `/api` route.

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

`VPS_USER` must be able to run `sudo bash setup.sh` without being prompted for a
password — the workflow is non-interactive. `root` is simplest (running `sudo` as
root never prompts); a normal account works too if it has `NOPASSWD` sudo for
that command. After each push the run's summary says whether it deployed or
skipped, and a deploy only goes green once the VPS is confirmed on the pushed
commit.

## "failed to stat '/root/...': Permission denied"

Not an access problem, despite appearances. `git` stats its working directory
before it does anything, so any command run as the service user from a directory
that user cannot enter fails immediately — and `/root` is mode 700, so cloning
this repository into `/root/Gaffer` produces exactly that.

The error names a local path and looks nothing like a permissions or network
fault, which is why it sends people hunting through repository visibility,
deploy keys and firewall rules for a cause that is none of them.

`setup.sh` now moves to `/` before dropping privileges, so where you launch it
from no longer matters. With an older copy, either pull, or run it from anywhere
readable:

```bash
cd / && sudo bash /root/Gaffer/Gaffer/deploy/setup.sh
```

## When it will not clone

```bash
sudo bash deploy/diagnose.sh
```

Separates the three faults that look identical from the outside: whether the
repository is public at all, whether root can see it, and whether the service
user can. Root succeeding while the service user fails is the common one — root
holds a token cached from when you cloned, and the service user has nothing.

## Checking it

```bash
systemctl list-timers gaffer.timer     # when it next fires
journalctl -u gaffer.service -n 50     # what happened last time
```
