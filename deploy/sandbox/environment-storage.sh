#!/bin/sh
# Fixed operator paths; never accepts model-supplied filesystem arguments.
set -eu
test "$(id -u)" = 0
root=/var/lib/yuki-sandbox
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
elif [ ! -e "$image" ]; then
    test "$(df -B1 --output=avail "$root" | tail -n1)" -gt 4294967296
    umask 077
    fallocate -l 2G "$image.pending"
    mkfs.ext4 -q -F -m 0 "$image.pending"
    mv "$image.pending" "$image"
fi
test "$(stat -c %s "$image")" = 2147483648
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
    if [ ! -e "$runtime_image" ]; then
        test -z "$(find "$runtime_mount" -mindepth 1 -maxdepth 1 -print -quit)"
        fallocate -l 128M "$runtime_image.pending"
        mkfs.ext4 -q -F -m 0 "$runtime_image.pending"
        mv "$runtime_image.pending" "$runtime_image"
    fi
    test "$(stat -c %s "$runtime_image")" = 134217728
    mount -o loop,nodev,nosuid "$runtime_image" "$runtime_mount"
fi
chmod 755 "$runtime_mount"
