#!/bin/sh
# Operator-selected storage root; never accepts model-supplied filesystem arguments.
set -eu
test "$(id -u)" = 0
test "$#" -le 1
root=${1:-/var/lib/yuki-sandbox}
case "$root" in /*) ;; *) echo 'Storage root must be absolute.' >&2; exit 1 ;; esac
image="$root/home.ext4"
home_mount="$root/home"
mkdir -p "$root" "$home_mount"
test ! -L "$root"
test ! -L "$image"
test ! -L "$home_mount"
if mountpoint -q "$home_mount"; then
    test "$(findmnt -n -o FSTYPE --target "$home_mount")" = ext4
    device=$(findmnt -n -o SOURCE --target "$home_mount")
    test "$(losetup -n -O BACK-FILE "$device")" = "$image"
else
    test -z "$(find "$home_mount" -mindepth 1 -maxdepth 1 -print -quit)" || {
        echo 'Unmounted HOME contains files; preserve them and plan the storage migration explicitly.' >&2
        exit 1
    }
fi
if [ ! -e "$image" ]; then
    umask 077
    fallocate -l 16G "$image.pending"
    mkfs.ext4 -q -F -m 0 "$image.pending"
    mv "$image.pending" "$image"
fi
test -s "$image" # Existing and explicitly expanded filesystems retain their real capacity.
mountpoint -q "$home_mount" || mount -o loop,nodev,nosuid "$image" "$home_mount"
chmod 755 "$home_mount"
chown 10001:10001 "$home_mount"
# Runtime logs cannot bypass the home quota by writing through their bind mount.
runtime_image="$root/runtime.ext4"
runtime_mount="$root/environment-jobs"
test ! -L "$runtime_image"
test ! -L "$runtime_mount"
mkdir -p "$runtime_mount"
if mountpoint -q "$runtime_mount"; then
    device=$(findmnt -n -o SOURCE --target "$runtime_mount")
    test "$(findmnt -n -o FSTYPE --target "$runtime_mount")" = ext4
    test "$(losetup -n -O BACK-FILE "$device")" = "$runtime_image"
else
    test -z "$(find "$runtime_mount" -mindepth 1 -maxdepth 1 -print -quit)" || {
        echo 'Unmounted runtime contains files; preserve them and plan the storage migration explicitly.' >&2
        exit 1
    }
    if [ ! -e "$runtime_image" ]; then
        fallocate -l 128M "$runtime_image.pending"
        mkfs.ext4 -q -F -m 0 "$runtime_image.pending"
        mv "$runtime_image.pending" "$runtime_image"
    fi
    test "$(stat -c %s "$runtime_image")" = 134217728
    mount -o loop,nodev,nosuid "$runtime_image" "$runtime_mount"
fi
chmod 755 "$runtime_mount"
