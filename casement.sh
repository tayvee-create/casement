#!/bin/sh
# Run Casement from this checkout.
cd "$(dirname "$(readlink -f "$0")")" && exec python3 -m casement "$@"
