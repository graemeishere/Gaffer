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
