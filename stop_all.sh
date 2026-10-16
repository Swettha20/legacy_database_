#!/bin/sh
set -eu
. "$(dirname -- "$0")/scripts/stack-common.sh"
stack_init
if [ ! -f "$STACK_CONFIG" ]; then echo 'This checkout has not been started; nothing to stop.'; exit 0; fi
echo 'Stopping this project only; records, downloads and models are retained.'
stack_compose stop --timeout 30
echo 'Stopped. Run sh start_all.sh to resume.'
