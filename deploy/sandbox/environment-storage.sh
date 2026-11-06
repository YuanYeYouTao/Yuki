#!/bin/sh
# Run on the Linux Manager host. Existing ext4 files only grow.
set -eu
root=/var/lib/yuki-sandbox
mkdir -p "$root"
exec 9>"$root/.storage.lock"
flock 9

mount_volume() {
    image="$root/$1.ext4"
    target="$root/$2"
    size=$3
    test ! -L "$image" && test ! -L "$target" || exit 1
    mkdir -p "$target"
    if mountpoint -q "$target"; then
        device=$(findmnt -n -o SOURCE --target "$target")
        test "$(losetup -n -O BACK-FILE "$device")" = "$image"
        test "$(findmnt -n -o FSTYPE --target "$target")" = ext4
    else
        test -z "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)"
        if [ ! -e "$image" ]; then
            pending=$(mktemp "$root/.volume.XXXXXX")
            trap 'rm -f "$pending"' EXIT
            fallocate -l "${size:-$4}M" "$pending"
            mkfs.ext4 -q -F -m 0 "$pending"
            mv "$pending" "$image"
            trap - EXIT
        fi
        mount -t ext4 -o loop,nodev,nosuid "$image" "$target"
        device=$(findmnt -n -o SOURCE --target "$target")
    fi
    if [ -n "$size" ]; then
        bytes=$(numfmt --from=iec "${size}M")
        current=$(stat -c %s "$image")
        if [ "$bytes" -lt "$current" ]; then
            echo "Keeping $image at $current bytes; shrinking requires an offline migration." >&2
        else
            if [ "$bytes" -gt "$current" ]; then
                fallocate -l "$bytes" "$image"
            fi
            # Also finish an expansion interrupted after allocation or loop resize.
            losetup -c "$device"
            resize2fs "$device"
        fi
    fi
    chmod 755 "$target"
}
mount_volume home home "${YUKI_SANDBOX_HOME_MIB:-}" 2048
chown 10001:10001 "$root/home"
mount_volume runtime environment-jobs "${YUKI_SANDBOX_RUNTIME_MIB:-}" 128
