#!/bin/sh
# Restart a separate test instance on a Broadway display (view at http://localhost:8085).
# It never touches a copy of the app running on your desktop.
SP="${SCRATCH:-/tmp/casement-dev}"
mkdir -p "$SP"
[ -f "$SP/app.pid" ] && kill "$(cat "$SP/app.pid")" 2>/dev/null
sleep 0.5
if ! [ -f "$SP/broadway.pid" ] || ! kill -0 "$(cat "$SP/broadway.pid")" 2>/dev/null; then
    gtk4-broadwayd :5 >"$SP/broadway.log" 2>&1 &
    echo $! >"$SP/broadway.pid"
    sleep 0.5
fi
cd "$(dirname "$0")/.."
GDK_BACKEND=broadway BROADWAY_DISPLAY=:5 W11_NON_UNIQUE=1 XDG_CONFIG_HOME="$SP/cfg" \
    python3 -m casement "$@" >"$SP/app.log" 2>&1 &
echo $! >"$SP/app.pid"
sleep 3
cat "$SP/app.log"
