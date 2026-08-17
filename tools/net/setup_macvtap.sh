#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "run as root: sudo $0 [macvtap_if] [parent_if] [mode]"
    exit 1
fi

MACVTAP_IF="${1:-edge-macvtap0}"
PARENT_IF="${2:-${EDGEOS_PARENT_IF:-}}"
MODE="${3:-${EDGEOS_MACVTAP_MODE:-bridge}}"
OWNER="${SUDO_USER:-root}"

if [[ -z "${PARENT_IF}" ]]; then
    PARENT_IF="$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')"
fi

if [[ -z "${PARENT_IF}" ]]; then
    echo "could not detect parent interface; pass it explicitly or set EDGEOS_PARENT_IF"
    exit 1
fi

if ! ip link show dev "${PARENT_IF}" >/dev/null 2>&1; then
    echo "parent interface not found: ${PARENT_IF}"
    exit 1
fi

if ip link show dev "${MACVTAP_IF}" >/dev/null 2>&1; then
    ip link delete "${MACVTAP_IF}"
fi

ip link add link "${PARENT_IF}" name "${MACVTAP_IF}" type macvtap mode "${MODE}"
ip link set dev "${MACVTAP_IF}" up

IFINDEX="$(cat "/sys/class/net/${MACVTAP_IF}/ifindex")"
TAP_DEV="/dev/tap${IFINDEX}"

if [[ -e "${TAP_DEV}" ]]; then
    chown "${OWNER}" "${TAP_DEV}" || true
    chmod 660 "${TAP_DEV}" || true
fi

echo "macvtap setup complete:"
echo "  macvtap interface: ${MACVTAP_IF}"
echo "  parent interface:  ${PARENT_IF}"
echo "  mode:              ${MODE}"
echo "  qemu tap device:   ${TAP_DEV}"
echo "  owner:             ${OWNER}"
echo
echo "VM network spec:"
echo "  macvtap,ifname=${MACVTAP_IF},model=e1000"
