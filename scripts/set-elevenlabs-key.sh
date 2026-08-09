#!/usr/bin/env bash
set -euo pipefail

project_dir="${WQI_PROJECT_DIR:-/opt/warcraft-quest-immersion}"
compose_file="${WQI_COMPOSE_FILE:-/home/plex/docker-compose.yml}"
service="warcraft-quest-immersion"
env_file="$project_dir/.env"

umask 077
IFS= read -r api_key
api_key="${api_key%$'\r'}"

if [[ ! "$api_key" =~ ^sk_[A-Za-z0-9_-]{20,}$ ]]; then
  echo "Configuration stopped: the supplied value does not look like an ElevenLabs API key." >&2
  exit 1
fi
if [[ ! -f "$project_dir/.env.example" ]]; then
  echo "Configuration stopped: $project_dir is not a complete project checkout." >&2
  exit 1
fi
if [[ ! -f "$compose_file" ]]; then
  echo "Configuration stopped: $compose_file was not found." >&2
  exit 1
fi

if [[ ! -f "$env_file" ]]; then
  cp "$project_dir/.env.example" "$env_file"
fi

temporary_file="$(mktemp "$project_dir/.env.elevenlabs.XXXXXX")"
cleanup() {
  rm -f "$temporary_file"
  api_key=""
}
trap cleanup EXIT

found=0
while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" == ELEVENLABS_API_KEY=* ]]; then
    printf 'ELEVENLABS_API_KEY=%s\n' "$api_key" >> "$temporary_file"
    found=1
  else
    printf '%s\n' "$line" >> "$temporary_file"
  fi
done < "$env_file"
if [[ "$found" -eq 0 ]]; then
  printf '\nELEVENLABS_API_KEY=%s\n' "$api_key" >> "$temporary_file"
fi

chmod 600 "$temporary_file"
mv -f "$temporary_file" "$env_file"
chmod 600 "$env_file"
api_key=""

docker compose -f "$compose_file" config --quiet
docker compose -f "$compose_file" up -d --force-recreate --no-deps "$service"

for _ in {1..30}; do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service")"
  if [[ "$health" == "healthy" ]]; then
    curl --fail --silent --show-error http://127.0.0.1:8090/health >/dev/null
    echo "ElevenLabs configuration stored in the ignored server environment file."
    echo "The application container is healthy; use Alpha > Settings to verify the account read-only."
    exit 0
  fi
  if [[ "$health" == "unhealthy" || "$health" == "exited" ]]; then
    docker logs --tail 80 "$service"
    exit 1
  fi
  sleep 2
done

echo "Configuration was written, but the application did not become healthy in time." >&2
exit 1
