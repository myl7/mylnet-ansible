#!/usr/bin/env bash
set -euo pipefail

cat "$(dirname "$0")"/../inventories/host_vars/*.yaml | grep '_domain:' | awk '{print $2}'
