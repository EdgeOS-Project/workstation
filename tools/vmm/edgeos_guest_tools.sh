#!/bin/sh
set -eu

CONFIG_FILE=/etc/edgeos-workstation-shares
INSTALL_PATH=/usr/local/sbin/edgeos-guest-tools

mount_share() {
    tag=$1
    target=$2
    access=${3:-rw}
    mkdir -p "$target"
    options=trans=virtio,version=9p2000.L
    if [ "$access" = ro ]; then
        options="$options,ro"
    fi
    if mountpoint -q "$target" 2>/dev/null; then
        return 0
    fi
    mount -t 9p -o "$options" "$tag" "$target"
}

mount_all() {
    [ -f "$CONFIG_FILE" ] || return 0
    while IFS='|' read -r tag target access; do
        case $tag in
            ""|\#*) continue ;;
        esac
        mount_share "$tag" "$target" "$access"
    done < "$CONFIG_FILE"
}

status() {
    echo "EdgeOS Workstation Guest Tools"
    if grep -qw 9p /proc/filesystems 2>/dev/null; then
        echo "9p filesystem: available"
    else
        echo "9p filesystem: unavailable"
    fi
    [ -f "$CONFIG_FILE" ] && cat "$CONFIG_FILE" || true
}

install_tools() {
    mkdir -p "$(dirname "$INSTALL_PATH")"
    cp "$0" "$INSTALL_PATH"
    chmod 0755 "$INSTALL_PATH"
    if command -v rc-update >/dev/null 2>&1; then
        cat >/etc/init.d/edgeos-guest-tools <<'EOF'
#!/sbin/openrc-run
description="EdgeOS Workstation Guest Tools"
command="/usr/local/sbin/edgeos-guest-tools"
command_args="mount-all"
depend() {
    need localmount
}
EOF
        chmod 0755 /etc/init.d/edgeos-guest-tools
        rc-update add edgeos-guest-tools default >/dev/null 2>&1 || true
    fi
    echo "Guest Tools installed at $INSTALL_PATH"
}

case ${1:-status} in
    install)
        install_tools
        ;;
    mount-all)
        mount_all
        ;;
    mount)
        mount_share "$2" "$3" "${4:-rw}"
        ;;
    status)
        status
        ;;
    *)
        echo "usage: $0 {install|mount-all|mount TAG TARGET [ro|rw]|status}" >&2
        exit 2
        ;;
esac
