#!/bin/sh
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ISO="${ISO:-$DIR/edgeos-alpine-xfce4.iso}"
ROOTFS="${ROOTFS:-$DIR/edgeos-alpine-xfce4-rootfs.img}"
MEMORY="${MEMORY:-6144M}"
CPUS="${CPUS:-4}"
DISPLAY_BACKEND="${DISPLAY_BACKEND:-gtk}"

if [ ! -f "$ISO" ]; then
    echo "missing ISO: $ISO" >&2
    exit 1
fi
if [ ! -f "$ROOTFS" ]; then
    echo "missing rootfs: $ROOTFS" >&2
    exit 1
fi

KVM_ARGS="-enable-kvm -cpu host,migratable=off -machine pc,accel=kvm,i8042=off"
if [ "${KVM:-1}" = "0" ]; then
    KVM_ARGS="-cpu qemu64 -machine pc,accel=tcg,i8042=off"
fi

exec qemu-system-x86_64 \
    $KVM_ARGS \
    -nodefaults \
    -smp "$CPUS,sockets=1,cores=$CPUS,threads=1" \
    -m "$MEMORY" \
    -rtc base=utc,clock=host \
    -monitor none \
    -display "$DISPLAY_BACKEND" \
    -vga none \
    -device virtio-gpu-pci \
    -serial stdio \
    -drive "file=$ROOTFS,format=raw,if=none,id=disk0,cache=none,aio=native,discard=unmap" \
    -device virtio-blk-pci,drive=disk0 \
    -cdrom "$ISO" \
    -netdev user,id=net0 \
    -device e1000,netdev=net0 \
    -device qemu-xhci,id=usb0 \
    -device usb-kbd,bus=usb0.0 \
    -device usb-mouse,bus=usb0.0 \
    -boot d
