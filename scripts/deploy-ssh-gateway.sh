#!/usr/bin/env bash
# Forced SSH command for the dedicated deployment account.
set -Eeuo pipefail

readonly expected_path=/srv/apps/yt-downloader
readonly deploy_helper=/usr/local/sbin/yt-downloader-deploy

original_command=${SSH_ORIGINAL_COMMAND:-}
if [[ ! "$original_command" =~ ^deploy[[:space:]]([0-9a-f]{40})[[:space:]]([A-Za-z0-9+/]+={0,2})$ ]]; then
  echo "Only a validated deployment command is allowed." >&2
  exit 1
fi
deploy_sha=${BASH_REMATCH[1]}
path_b64=${BASH_REMATCH[2]}
if ! deploy_path="$(printf '%s' "$path_b64" | base64 --decode 2>/dev/null)"; then
  echo "Invalid deployment path encoding." >&2
  exit 1
fi
if [[ "$(printf '%s' "$deploy_path" | base64 -w 0)" != "$path_b64" || "$deploy_path" != "$expected_path" ]]; then
  echo "Deployment path is not allowed." >&2
  exit 1
fi
exec sudo -n "$deploy_helper" "$deploy_sha" "$deploy_path"
