#!/bin/sh

set -eu

old_route='`/finance/realtime`'
new_route='`/realtime/finance`'
route_file="$(grep -RlF "$old_route" dist | head -n 1 || true)"

if [ -n "$route_file" ]; then
    sed -i "s|$old_route|$new_route|" "$route_file"
elif ! grep -RlF "$new_route" dist >/dev/null; then
    echo "Warning: unable to patch the Zaobao finance route" >&2
fi

exec npm run start
