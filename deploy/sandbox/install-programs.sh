#!/bin/sh
set -efu
mode=$1
shift
case "$mode" in
    headed) set -- "$@" tigervnc-standalone-server openbox socat xdotool x11-utils imagemagick ;;
    headless|none) ;;
    *) echo "Unknown browser mode: $mode" >&2; exit 2 ;;
esac
if [ "$mode" != none ]; then
    set -- "$@" chromium fonts-noto-cjk fonts-noto-color-emoji
fi
if [ "$#" -gt 0 ]; then
    apt-get update
    apt-get install -y --no-install-recommends -- "$@"
    rm -rf /var/lib/apt/lists/*
fi
chmod 755 /usr/local/bin/yuki-computer
python /opt/yuki-runtime/desktop-manifest.py "$mode" "$@"
