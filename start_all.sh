#!/bin/sh
set -eu
. "$(dirname -- "$0")/scripts/stack-common.sh"
stack_init
no_browser=false
skip_ai=false
for option in "$@"; do
    case "$option" in
        --no-browser) no_browser=true ;;
        --skip-ai) skip_ai=true ;;
        *) echo "Unknown option: $option" >&2; exit 2 ;;
    esac
done
trap 'echo "Startup stopped. Data is retained. Fix the error and rerun, or run sh stop_all.sh." >&2' 0
stack_new_config
stack_upgrade_config
stack_require_docker
stack_compose config --quiet
provider=$(stack_value STACK_AI_PROVIDER ollama)
case "$provider" in none) skip_ai=true ;; ollama|groq) ;; *) echo 'Invalid STACK_AI_PROVIDER' >&2; exit 1 ;; esac
if [ "$provider" = groq ] && [ "$skip_ai" = false ] && [ -z "$(stack_value STACK_GROQ_API_KEY '')" ]; then
    echo 'Add STACK_GROQ_API_KEY to .env.stack or choose STACK_AI_PROVIDER=ollama.' >&2
    exit 1
fi
echo '1/6 Building app dependencies...'
stack_compose build bootstrap
echo '2/6 Starting databases and waiting for readiness...'
if [ "$provider" = ollama ] && [ "$skip_ai" = false ]; then
    stack_compose up -d --wait --wait-timeout 1200 oracle postgres queue ollama
    echo '3/6 Checking the AI model cache...'
    model=$(stack_value STACK_OLLAMA_MODEL qwen2.5-coder:1.5b)
    if stack_compose exec -T ollama ollama show "$model" >/dev/null 2>&1; then
        echo 'Model already downloaded; reusing the local cache.'
    else
        stack_compose exec -T ollama ollama pull "$model"
    fi
else
    stack_compose up -d --wait --wait-timeout 1200 oracle postgres queue
    echo '3/6 AI model download not required for this mode.'
fi
echo '4/6 Initialising the empty demo schema only...'
stack_compose run --rm --no-deps bootstrap
echo '5/6 Checking databases, migration state and AI...'
if [ "$skip_ai" = true ]; then
    stack_compose run --rm --no-deps bootstrap python runtime_stack.py health --skip-ai
else
    stack_compose run --rm --no-deps bootstrap python runtime_stack.py health
fi
echo '6/6 Starting API and website...'
stack_compose up -d --wait --wait-timeout 1200 worker api web
stack_http_health
url="http://localhost:$(stack_value STACK_WEB_PORT 18501)"
echo "Ready: $url"
echo 'Login: admin. Password: STACK_ADMIN_PASSWORD in your private .env.stack file.'
echo "API: http://localhost:$(stack_value STACK_API_PORT 18000)/docs"
echo 'Stop: sh stop_all.sh (all data and downloads are retained).'
if [ "$skip_ai" = true ]; then echo 'AI readiness was explicitly skipped.'; fi
trap - 0
if [ "$no_browser" = false ]; then
    if command -v open >/dev/null 2>&1; then open "$url" || true
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
    fi
fi
