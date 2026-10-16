#!/bin/sh
set -eu
. "$(dirname -- "$0")/scripts/stack-common.sh"
stack_init
[ -f "$STACK_CONFIG" ] || { echo 'Run start_all first.' >&2; exit 1; }
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != --skip-ai ]; }; then
    echo 'Usage: sh health_all.sh [--skip-ai]' >&2; exit 2
fi
stack_compose ps
stack_compose run --rm --no-deps bootstrap python runtime_stack.py health "$@"
stack_http_health
