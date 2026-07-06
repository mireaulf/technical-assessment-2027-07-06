#!/usr/bin/env bash
# Manage the whole stack (docker-compose.yml): Postgres, the API, and the
# ingestion worker. This is the supported way to run everything locally -
# prefer it over starting `uvicorn` by hand or installing Postgres on the host.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed or not on PATH. Install Docker Desktop (or the docker CLI) first." >&2
  exit 1
fi

usage() {
  cat <<EOF
Usage: $(basename "$0") <command>

Commands:
  start     Start Postgres + the API (hot reload) + the ingestion worker (docker compose up -d)
  stop      Stop them, keep persisted data (docker compose down)
  restart   stop then start
  reset     Stop and DELETE the database volume (docker compose down -v)
  status    Show container status (docker compose ps)
  logs      Tail logs for all services, or one: logs [db|api|ingestion]
EOF
  exit 1
}

require_env_file() {
  if [ ! -f .env ]; then
    echo "No .env file found. It's checked into the repo (holds 1Password references, not secrets) - restore it, e.g. git checkout -- .env" >&2
    exit 1
  fi
}

require_1password_signin() {
  if ! command -v op >/dev/null 2>&1; then
    echo "1Password CLI (op) is not installed or not on PATH. Install it first: https://developer.1password.com/docs/cli/get-started/" >&2
    exit 1
  fi
  if ! op whoami >/dev/null 2>&1; then
    eval "$(op signin)"
  fi
}

case "${1:-}" in
  start)
    require_1password_signin
    require_env_file
    op run --env-file .env -- docker compose up -d
    docker compose ps
    ;;
  stop)
    docker compose down
    ;;
  restart)
    require_1password_signin
    require_env_file
    docker compose down
    op run --env-file .env -- docker compose up -d
    docker compose ps
    ;;
  reset)
    read -r -p "This deletes all persisted data (pgdata volume). Continue? [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
      docker compose down -v
    else
      echo "Aborted."
    fi
    ;;
  status)
    docker compose ps
    ;;
  logs)
    shift || true
    docker compose logs -f "$@"
    ;;
  *)
    usage
    ;;
esac
