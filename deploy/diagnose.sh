#!/usr/bin/env bash
#
# Why can't this box reach the repository?
#
#   sudo bash deploy/diagnose.sh [url]
#
# Answers three questions that look identical from the outside and need
# different fixes: is the repository public, can root see it, and can the
# service user see it.

URL="${1:-https://github.com/graemeishere/Gaffer.git}"
SERVICE_USER="${GAFFER_USER:-gaffer}"
SERVICE_HOME="${GAFFER_HOME:-/srv/gaffer}"

echo "== repository: $URL"
echo

for who in root "$SERVICE_USER"; do
  if [[ "$who" == root ]]; then
    out=$(GIT_TERMINAL_PROMPT=0 timeout 25 git ls-remote --heads "$URL" 2>&1); rc=$?
  else
    if ! id -u "$who" >/dev/null 2>&1; then
      echo "-- as $who: user does not exist yet (setup.sh creates it)"
      continue
    fi
    out=$(sudo -u "$who" env HOME="$SERVICE_HOME" GIT_TERMINAL_PROMPT=0 \
          timeout 25 git ls-remote --heads "$URL" 2>&1); rc=$?
  fi

  echo "-- as $who: exit=$rc"
  if [[ $rc -eq 0 ]]; then
    echo "   reachable. branches:"
    printf '%s\n' "$out" | sed 's|.*refs/heads/|     |'
  else
    printf '   %s\n' "$(printf '%s' "$out" | head -2)"
  fi
done

echo
echo "== anonymous check (no credentials of any kind)"
code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/info/refs?service=git-upload-pack")
case "$code" in
  200) echo "   HTTP 200 -> PUBLIC. No key or token is needed; clone over https." ;;
  401|403) echo "   HTTP $code -> PRIVATE. Make it public, or give this box a deploy key." ;;
  404) echo "   HTTP 404 -> not found. Private, or the owner/name is wrong (check the case)." ;;
  000) echo "   no response -> the network is blocking you, not GitHub." ;;
  *)   echo "   HTTP $code -> unexpected. Something is intercepting the connection." ;;
esac

cat <<'READING'

== reading this

  Anonymous says PUBLIC but the service user fails
      Not a permissions problem. Look at the service user's git message above —
      usually a proxy setting or a git config that only root has.

  Anonymous says PRIVATE and root succeeds
      root is using a token cached when you cloned. The service user has none.
      Make the repository public, or add a deploy key.

  Everything fails
      The box cannot reach GitHub at all. Check DNS and outbound 443.
READING
