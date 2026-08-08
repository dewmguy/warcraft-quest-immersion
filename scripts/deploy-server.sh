#!/usr/bin/env bash
set -euo pipefail

project_dir="/opt/warcraft-quest-immersion"
compose_file="/home/plex/docker-compose.yml"
service="warcraft-quest-immersion"

if [[ -n "$(git -C "$project_dir" status --porcelain)" ]]; then
  echo "Deployment stopped: $project_dir has uncommitted changes." >&2
  exit 1
fi

git -C "$project_dir" pull --ff-only
docker compose -f "$compose_file" config --quiet
docker compose -f "$compose_file" build --pull "$service"
docker compose -f "$compose_file" up -d --no-deps "$service"

for _ in {1..30}; do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service")"
  if [[ "$health" == "healthy" ]]; then
    curl --fail --silent --show-error http://127.0.0.1:8090/health
    echo
    echo "Deployment healthy."
    exit 0
  fi
  if [[ "$health" == "unhealthy" || "$health" == "exited" ]]; then
    docker logs --tail 80 "$service"
    exit 1
  fi
  sleep 2
done

echo "Deployment timed out waiting for a healthy container." >&2
docker logs --tail 80 "$service"
exit 1
