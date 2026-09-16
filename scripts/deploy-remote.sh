#!/usr/bin/env bash
# Root-only SSH deployment entry point. It receives a git archive on stdin.
set -Eeuo pipefail

readonly expected_path=/srv/apps/yt-downloader
readonly project=yt-downloader
readonly runtime_volume=yt-downloader_yt_data
readonly caddy_data_volume=yt-downloader_caddy_data
readonly caddy_config_volume=yt-downloader_caddy_config
readonly releases="$expected_path/releases"
readonly overrides="$expected_path/.cd-overrides"
readonly backup_root=/srv/backups/yt-downloader-cd
readonly caddy_image=caddy:2.10.2-alpine

if [[ "$(id -u)" != "0" ]]; then
  echo "This deployment entry point must run as root." >&2
  exit 1
fi
if [[ "$#" -ne 2 ]]; then
  echo "Usage: yt-downloader-deploy <40-char git SHA> /srv/apps/yt-downloader" >&2
  exit 1
fi

deploy_sha=$1
deploy_path=$2
if [[ ! "$deploy_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "The deployment SHA is invalid." >&2
  exit 1
fi
if [[ "$deploy_path" != "$expected_path" ]]; then
  echo "The deployment path is not allowed." >&2
  exit 1
fi

if [[ -L "$expected_path" ]]; then
  echo "The deployment root must not be a symbolic link." >&2
  exit 1
fi
install -d -o root -g root -m 0755 "$expected_path"
install -d -o root -g root -m 0755 "$releases" "$overrides"
install -d -o root -g root -m 0700 "$backup_root"
for protected_path in "$releases" "$overrides" "$backup_root"; do
  if [[ -L "$protected_path" ]]; then
    echo "A deployment directory must not be a symbolic link: $protected_path" >&2
    exit 1
  fi
done

if [[ ! -f "$expected_path/.env" || -L "$expected_path/.env" ]]; then
  echo "A regular production .env file is required at $expected_path/.env." >&2
  exit 1
fi
chmod 0600 "$expected_path/.env"

for volume in "$runtime_volume" "$caddy_data_volume" "$caddy_config_volume"; do
  if ! docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "Required Docker volume is missing: $volume" >&2
    exit 1
  fi
  volume_project="$(docker volume inspect --format '{{ index .Labels "com.docker.compose.project" }}' "$volume")"
  if [[ "$volume_project" != "$project" ]]; then
    echo "Docker volume $volume does not belong to Compose project $project." >&2
    exit 1
  fi
done

if [[ ! -L "$expected_path/.cd-current" ]]; then
  echo "The current release link is missing: $expected_path/.cd-current" >&2
  exit 1
fi
if ! previous_release="$(readlink -f -- "$expected_path/.cd-current")"; then
  echo "The current release link cannot be resolved." >&2
  exit 1
fi
case "$previous_release" in
  "$releases"/*) ;;
  *)
    echo "The current release points outside the release directory." >&2
    exit 1
    ;;
esac
if [[ ! -d "$previous_release" || -L "$previous_release" ]]; then
  echo "The current release directory is invalid." >&2
  exit 1
fi
previous_sha="$(basename -- "$previous_release")"
if [[ ! "$previous_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "The current release name is invalid." >&2
  exit 1
fi
if [[ ! -f "$overrides/$previous_sha.yml" || -L "$overrides/$previous_sha.yml" ]]; then
  echo "The current release override is missing." >&2
  exit 1
fi

archive_file="$(mktemp /tmp/yt-downloader-deploy.XXXXXXXX.tar)"
stage_dir=""
release_dir="$releases/$deploy_sha"
config_file=""
temporary_override=""
backup_dir=""
services_stopped=0
new_stack_started=0

rollback() {
  status=$?
  if (( status != 0 )); then
    echo "Deployment failed; attempting to restore the previous stack." >&2
    if (( new_stack_started )); then
      compose_run "$release_dir" down --remove-orphans >/dev/null 2>&1 || true
    fi
    if (( services_stopped )); then
      compose_run "$previous_release" up -d --no-build app worker cleanup caddy >/dev/null 2>&1 || true
    fi
  fi
  if [[ -n "$temporary_override" ]]; then
    rm -f -- "$temporary_override"
  fi
  if [[ -n "$stage_dir" ]]; then
    rm -rf -- "$stage_dir"
  fi
  if [[ -n "$config_file" ]]; then
    rm -f -- "$config_file"
  fi
  rm -f -- "$archive_file"
  exit "$status"
}
trap rollback EXIT

compose_run() {
  local compose_dir=$1
  shift
  local compose_sha
  compose_sha="$(basename -- "$compose_dir")"
  docker compose \
    --project-name "$project" \
    --env-file "$expected_path/.env" \
    -f "$compose_dir/docker-compose.yml" \
    -f "$overrides/$compose_sha.yml" \
    "$@"
}

python3 -c '
import sys

output_path = sys.argv[1]
limit = 512 * 1024 * 1024
total = 0
with open(output_path, "wb") as output:
    while True:
        chunk = sys.stdin.buffer.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise SystemExit("The deployment archive exceeds 512 MiB.")
        output.write(chunk)
' "$archive_file"

if [[ ! -e "$release_dir" ]]; then
  stage_dir="$(mktemp -d "$releases/.stage.XXXXXXXX")"
  python3 - "$stage_dir" "$archive_file" <<'PY'
import os
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

stage = Path(sys.argv[1])
archive = Path(sys.argv[2])
max_unpacked = 2 * 1024 * 1024 * 1024
blocked_roots = {".env", "runtime", "backups", "releases", ".cd-current", ".cd-overrides"}
seen = set()
total = 0

with tarfile.open(archive, mode="r:") as tar:
    members = tar.getmembers()
    for member in members:
        name = member.name
        if not name or name.startswith("/") or "\\" in name or "\x00" in name:
            raise SystemExit(f"Unsafe archive path: {name!r}")
        parts = PurePosixPath(name).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise SystemExit(f"Unsafe archive path: {name!r}")
        if parts[0] in blocked_roots:
            raise SystemExit(f"Blocked archive path: {name!r}")
        normalized = "/".join(parts)
        if normalized in seen:
            raise SystemExit(f"Duplicate archive path: {name!r}")
        seen.add(normalized)
        if member.isdir():
            continue
        if not member.isfile() or member.issym() or member.islnk():
            raise SystemExit(f"Unsupported archive member: {name!r}")
        if member.size < 0:
            raise SystemExit(f"Invalid archive size: {name!r}")
        total += member.size
        if total > max_unpacked:
            raise SystemExit("The unpacked deployment exceeds 2 GiB.")

    for member in members:
        destination = stage.joinpath(*PurePosixPath(member.name).parts)
        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            os.chmod(destination, stat.S_IMODE(member.mode))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise SystemExit(f"Archive extraction would overwrite: {destination}")
        source = tar.extractfile(member)
        if source is None:
            raise SystemExit(f"Cannot read archive member: {member.name!r}")
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        os.chmod(destination, stat.S_IMODE(member.mode))
PY
  if [[ ! -f "$stage_dir/Dockerfile" || ! -f "$stage_dir/docker-compose.yml" || ! -f "$stage_dir/Caddyfile" ]]; then
    echo "The release archive is missing Dockerfile, docker-compose.yml, or Caddyfile." >&2
    exit 1
  fi
  if [[ -e "$release_dir" || -L "$release_dir" ]]; then
    echo "The release directory appeared while it was being prepared." >&2
    exit 1
  fi
  mv -T -- "$stage_dir" "$release_dir"
  stage_dir=""
elif [[ ! -d "$release_dir" || -L "$release_dir" ]]; then
  echo "The requested release path is invalid." >&2
  exit 1
fi

for required_file in Dockerfile docker-compose.yml Caddyfile; do
  if [[ ! -f "$release_dir/$required_file" || -L "$release_dir/$required_file" ]]; then
    echo "The release is missing a regular $required_file." >&2
    exit 1
  fi
done

if [[ -e "$release_dir/.env" || -L "$release_dir/.env" ]]; then
  if [[ ! -L "$release_dir/.env" || "$(readlink -f -- "$release_dir/.env")" != "$expected_path/.env" ]]; then
    echo "The release .env must point to the protected production .env." >&2
    exit 1
  fi
else
  ln -s -- "$expected_path/.env" "$release_dir/.env"
fi

app_image="yt-downloader-app:$deploy_sha"
worker_image="yt-downloader-worker:$deploy_sha"
cleanup_image="yt-downloader-cleanup:$deploy_sha"
temporary_override="$(mktemp "$overrides/.override.XXXXXXXX.yml")"
cat > "$temporary_override" <<EOF
services:
  app:
    image: $app_image
  worker:
    image: $worker_image
  cleanup:
    image: $cleanup_image
volumes:
  yt_data:
    external: true
    name: $runtime_volume
  caddy_data:
    external: true
    name: $caddy_data_volume
  caddy_config:
    external: true
    name: $caddy_config_volume
EOF
chmod 0600 "$temporary_override"
mv -T -- "$temporary_override" "$overrides/$deploy_sha.yml"
temporary_override=""

config_file="$(mktemp /tmp/yt-downloader-compose.XXXXXXXX.json)"
compose_run "$release_dir" config --format json > "$config_file"
python3 - "$config_file" "$release_dir" "$app_image" "$worker_image" "$cleanup_image" "$caddy_image" "$runtime_volume" "$caddy_data_volume" "$caddy_config_volume" <<'PY'
import json
import os
import sys

config_path, release, app_image, worker_image, cleanup_image, caddy_image, runtime_volume, caddy_data, caddy_config = sys.argv[1:]

with open(config_path, encoding="utf-8") as stream:
    document = json.load(stream)

services = document.get("services", {})
required = {"app", "worker", "cleanup", "caddy"}
if set(services) != required:
    raise SystemExit(f"Unexpected Compose services: {sorted(services)}")

def unsafe_service_options(service):
    for key in ("privileged", "devices", "volumes_from", "network_mode", "pid", "ipc", "cap_add"):
        value = service.get(key)
        if value not in (None, False, [], ""):
            raise SystemExit(f"Unsafe Compose option {key!r} is set.")

def check_runtime_service(name, image):
    service = services[name]
    if service.get("image") != image:
        raise SystemExit(f"{name} does not use the expected immutable image.")
    build = service.get("build")
    if not isinstance(build, dict) or os.path.realpath(build.get("context", "")) != os.path.realpath(release):
        raise SystemExit(f"{name} does not build from the release directory.")
    if service.get("ports") not in (None, []):
        raise SystemExit(f"{name} exposes a host port.")
    unsafe_service_options(service)
    data_mounts = [
        mount for mount in service.get("volumes", [])
        if mount.get("target") == "/data"
    ]
    if len(data_mounts) != 1 or data_mounts[0].get("type") != "volume" or data_mounts[0].get("source") != "yt_data":
        raise SystemExit(f"{name} does not use the protected runtime volume.")

check_runtime_service("app", app_image)
check_runtime_service("worker", worker_image)
check_runtime_service("cleanup", cleanup_image)

caddy = services["caddy"]
if caddy.get("image") != caddy_image or caddy.get("build") not in (None, {}):
    raise SystemExit("Caddy must use the pinned image without a build context.")
unsafe_service_options(caddy)
expected_ports = {
    ("127.0.0.1", 80, 80, "tcp"),
    ("127.0.0.1", 443, 443, "tcp"),
}
actual_ports = {
    (
        port.get("host_ip"),
        int(port.get("published")),
        int(port.get("target")),
        port.get("protocol"),
    )
    for port in caddy.get("ports", [])
}
if actual_ports != expected_ports:
    raise SystemExit(f"Caddy ports are not loopback-only: {actual_ports}")

expected_mounts = {
    "/etc/caddy/Caddyfile": ("bind", os.path.realpath(os.path.join(release, "Caddyfile")), True),
    "/data": ("volume", "caddy_data", None),
    "/config": ("volume", "caddy_config", None),
}
actual_targets = {mount.get("target") for mount in caddy.get("volumes", [])}
if actual_targets != set(expected_mounts):
    raise SystemExit(f"Unexpected Caddy mounts: {sorted(actual_targets)}")
for mount in caddy.get("volumes", []):
    expected = expected_mounts[mount["target"]]
    if mount.get("type") != expected[0] or mount.get("source") != expected[1]:
        raise SystemExit(f"Unexpected Caddy mount: {mount}")
    if expected[2] is True and mount.get("read_only") is not True:
        raise SystemExit("The Caddyfile bind mount must be read-only.")
unsafe_service_options(caddy)

volumes = document.get("volumes", {})
expected_volumes = {
    "yt_data": runtime_volume,
    "caddy_data": caddy_data,
    "caddy_config": caddy_config,
}
if set(volumes) != set(expected_volumes):
    raise SystemExit(f"Unexpected Compose volumes: {sorted(volumes)}")
for key, expected_name in expected_volumes.items():
    volume = volumes[key]
    if volume.get("external") is not True or volume.get("name") != expected_name:
        raise SystemExit(f"Compose volume {key} is not the expected external volume.")
PY
rm -f -- "$config_file"
config_file=""

if ! docker image inspect "$caddy_image" >/dev/null 2>&1; then
  echo "The pinned Caddy image is not available locally." >&2
  exit 1
fi
compose_run "$release_dir" build app worker cleanup

domain="$(sed -n 's/^DOMAIN=//p' "$expected_path/.env" | head -n 1 | tr -d '\r')"
if [[ ! "$domain" =~ ^[A-Za-z0-9.-]+$ || "$domain" == .* || "$domain" == *. || "$domain" == *..* ]]; then
  echo "DOMAIN in the production .env is invalid." >&2
  exit 1
fi

compose_run "$previous_release" stop app worker cleanup caddy
services_stopped=1

backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$(mktemp -d "$backup_root/$backup_stamp-$deploy_sha.XXXXXXXX")"
chmod 0700 "$backup_dir"
app_backup_image="$app_image"
docker run --rm \
  --network none \
  --user 0:0 \
  --mount "type=volume,src=$runtime_volume,dst=/source" \
  --mount "type=bind,src=$backup_dir,dst=/backup" \
  --entrypoint python \
  "$app_backup_image" \
  -c '
import os
import sqlite3
import tarfile
from pathlib import Path

source = Path("/source")
backup = Path("/backup")
database = source / "app.db"
if database.exists():
    source_connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    backup_database = backup / "sqlite-consistent.sqlite3"
    destination_connection = sqlite3.connect(backup_database)
    with destination_connection:
        source_connection.backup(destination_connection)
    destination_connection.close()
    source_connection.close()
    check_connection = sqlite3.connect(backup_database)
    result = check_connection.execute("PRAGMA integrity_check").fetchone()[0]
    check_connection.close()
    if result != "ok":
        raise SystemExit(f"SQLite integrity check failed: {result}")
    os.chmod(backup_database, 0o600)
with tarfile.open(backup / "runtime-before.tar.gz", "w:gz") as archive:
    archive.add(source, arcname="runtime", recursive=True)
os.chmod(backup / "runtime-before.tar.gz", 0o600)
'
chmod 0600 "$backup_dir"/*
tar -tzf "$backup_dir/runtime-before.tar.gz" >/dev/null
(
  cd "$backup_dir"
  sha256sum -- *.sqlite3 *.tar.gz 2>/dev/null || true
  printf '%s\n' "$deploy_sha" > deployment-sha
  chmod 0600 deployment-sha
)

new_stack_started=1
compose_run "$release_dir" up -d --no-build app worker cleanup caddy

for attempt in $(seq 1 60); do
  if compose_run "$release_dir" exec -T app python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=3)' >/dev/null 2>&1 &&
     compose_run "$release_dir" exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 &&
     curl --silent --show-error --fail --insecure --max-time 5 --resolve "$domain:443:127.0.0.1" "https://$domain/healthz" >/dev/null; then
    break
  fi
  if (( attempt == 60 )); then
    echo "The application did not become healthy within 120 seconds." >&2
    exit 1
  fi
  sleep 2
done

temporary_link="$expected_path/.cd-current.$$.tmp"
rm -f -- "$temporary_link"
ln -s -- "$release_dir" "$temporary_link"
mv -Tf -- "$temporary_link" "$expected_path/.cd-current"
services_stopped=0
new_stack_started=0
echo "Deployed $deploy_sha to $expected_path."
