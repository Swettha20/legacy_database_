#!/bin/sh
set -eu

stack_init() {
    STACK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    STACK_CONFIG=${STACK_CONFIG_FILE:-"$STACK_ROOT/.env.stack"}
}

stack_value() {
    # Parse data, never execute/source a settings file as shell code.
    awk -v key="$1" -v fallback="$2" '
        index($0,"=") && substr($0,1,index($0,"=")-1)==key {
            value=substr($0,index($0,"=")+1); sub(/\r$/, "", value);
            if (value!="") {print value; found=1; exit}
        }
        END {if (!found) print fallback}
    ' "$STACK_CONFIG"
}

stack_compose() {
    docker compose --project-directory "$STACK_ROOT" --env-file "$STACK_CONFIG" \
        -f "$STACK_ROOT/compose.yaml" --profile local-ai --profile tools "$@"
}

stack_require_docker() {
    command -v docker >/dev/null 2>&1 || {
        echo 'Install Docker Desktop (macOS) or Docker Engine with Compose (Linux), then rerun start_all.' >&2
        echo 'https://docs.docker.com/get-started/get-docker/' >&2
        exit 1
    }
    docker compose version
    engine=$(docker info --format '{{.OSType}}') || {
        echo 'Start Docker and complete its first-run setup, then retry.' >&2; exit 1;
    }
    [ "$engine" = linux ] || { echo 'Linux containers are required.' >&2; exit 1; }
}

stack_new_config() {
    [ ! -f "$STACK_CONFIG" ] || return 0
    secret() { od -An -N12 -tx1 /dev/urandom | tr -d ' \n'; }
    (umask 077; cat > "$STACK_CONFIG" <<EOF
STACK_PROJECT_NAME=legacy-moderniser
STACK_ORACLE_PASSWORD=$(secret)
STACK_DEMO_PASSWORD=$(secret)
STACK_PG_PASSWORD=$(secret)
STACK_API_PORT=18000
STACK_WEB_PORT=18501
STACK_AI_PROVIDER=${STACK_AI_PROVIDER:-none}
STACK_OLLAMA_MODEL=qwen2.5-coder:1.5b
STACK_GROQ_API_KEY=${GROQ_API_KEY:-}
STACK_GROQ_MODEL=openai/gpt-oss-120b
EOF
    )
    echo 'Created private .env.stack settings.'
}

stack_http_health() {
    stack_compose exec -T api python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://api:8000/health/ready', timeout=20)); assert r['status']=='ready'; urllib.request.urlopen('http://web:8501/health/live', timeout=10); print('PASS  API and website HTTP readiness')"
}

stack_upgrade_config() {
    for key in STACK_META_PASSWORD STACK_ADMIN_PASSWORD STACK_READER_PASSWORD; do
        if ! awk -F= -v key="$key" '$1==key {found=1} END {exit !found}' "$STACK_CONFIG"; then
            value=$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')
            printf '\n%s=%s\n' "$key" "$value" >> "$STACK_CONFIG"
        fi
    done
}
