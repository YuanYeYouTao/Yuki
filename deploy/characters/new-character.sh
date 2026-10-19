#!/bin/sh
set -eu
exec python3 "$(dirname "$0")/new_character.py" "$@"
