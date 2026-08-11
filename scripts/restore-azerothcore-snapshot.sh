#!/usr/bin/env bash
set -euo pipefail

project_dir="${WQI_PROJECT_DIR:-/opt/warcraft-quest-immersion}"
default_compose_file="/home/plex/docker-compose.yml"
[[ -f "$default_compose_file" ]] || default_compose_file="${project_dir}/docker-compose.yml"
compose_file="${WQI_COMPOSE_FILE:-$default_compose_file}"
env_file="${project_dir}/.env"
source_root="${project_dir}/data/sources/azerothcore/3.3.5/enUS"
service="warcraft-quest-source-db"
database="${AZEROTHCORE_MYSQL_DATABASE:-acore_world}"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <world.sql|world.sql.gz> [dbc-export.sql|dbc-export.sql.gz ...]" >&2
  exit 2
fi
if [[ ! "$database" =~ ^[A-Za-z0-9_]+$ ]]; then
  echo "AZEROTHCORE_MYSQL_DATABASE contains unsupported characters." >&2
  exit 2
fi

source_root="$(realpath "$source_root")"
source_files=()
for source_argument in "$@"; do
  source_file="$(realpath "$source_argument")"
  if [[ ! -f "$source_file" || "$source_file" != "$source_root/"* ]]; then
    echo "Every source artifact must be a file beneath $source_root." >&2
    exit 2
  fi
  case "$source_file" in
    *.sql|*.sql.gz) ;;
    *) echo "Source artifacts must end in .sql or .sql.gz." >&2; exit 2 ;;
  esac
  source_files+=("$source_file")
done

if [[ ! -f "$env_file" ]] || ! grep -Eq '^AZEROTHCORE_MYSQL_PASSWORD=.+$' "$env_file"; then
  echo "Set AZEROTHCORE_MYSQL_PASSWORD in $env_file before restoring a snapshot." >&2
  exit 2
fi

docker compose --env-file "$env_file" -f "$compose_file" --profile corpus-build up -d "$service"
for _ in {1..30}; do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service")"
  [[ "$health" == "healthy" ]] && break
  if [[ "$health" == "unhealthy" || "$health" == "exited" ]]; then
    docker logs --tail 80 "$service"
    exit 1
  fi
  sleep 2
done

printf 'DROP DATABASE IF EXISTS `%s`;\nCREATE DATABASE `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n' \
  "$database" "$database" | docker exec -i "$service" sh -lc \
  'mariadb -uroot --password="$MARIADB_ROOT_PASSWORD"'

for source_file in "${source_files[@]}"; do
  if [[ "$source_file" == *.gz ]]; then
    gzip -dc -- "$source_file" | docker exec -i "$service" sh -lc \
      'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" "'$database'"'
  else
    docker exec -i "$service" sh -lc \
      'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" "'$database'"' < "$source_file"
  fi
done

sha256sum "${source_files[@]}"
echo "AzerothCore snapshot restored into the private $service container."
