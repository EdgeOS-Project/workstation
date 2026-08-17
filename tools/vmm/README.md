# EdgeOS Virtual Machine Manager

The VMM supports `x86_64` and `arm64` guests. It queries the installed QEMU
binary for available accelerators and supports KVM, HVF, NVMM, WHPX, and TCG
when the host/QEMU combination exposes them. TCG is the portable fallback.

The standalone repository expects the EdgeOS kernel in the sibling `kernel`
directory. Set `EDGEOS_KERNEL_DIR` to use another checkout. Set
`EDGEOS_WORKSTATION_STATE_DIR` to move persistent VM state, and
`EDGEOS_DISTRIBUTION_DIR` to use distribution image-building sources when
available.

On Apple Silicon, create and run the Generic UEFI ARM64 port with:

```sh
python3 tools/vmm/edgeos_vm.py create arm-dev --architecture arm64 --template alpine
python3 tools/vmm/edgeos_vm.py start arm-dev --display window --accelerator hvf --cpu-model host
```

Use TCG only as an explicit portability fallback. Performance and release
validation must use HVF for ARM64 on Apple Silicon or KVM for x86_64 on Linux.

ARM64 instances keep a private ext4 rootfs image. `update-kernel` rebuilds
`BOOTAA64.EFI` and repackages the UEFI system partition without modifying that
rootfs. The current ARM64 boot path attaches the rootfs as a writable virtio
block device, so installed packages and other guest-side changes survive
kernel updates and reboots.

`edgeos_vm.py` manages persistent EdgeOS test VMs. It stores VM state under
`.edgeos-vms/instances/<name>/` and keeps the rootfs image separate from kernel
build outputs, so `update-kernel` can compile and boot a newer kernel without
recreating the rootfs or removing installed packages.

## GUI

For users who do not want to learn the CLI, run the Qt frontend:

```sh
python3 tools/vmm/edgeos_vm_gui.py
```

The GUI requires PyQt6 or PySide6:

```sh
python3 -m pip install PyQt6
```

## Basic usage

```sh
python3 tools/vmm/edgeos_vm.py create dev --template edgeos
python3 tools/vmm/edgeos_vm.py start dev --display window
python3 tools/vmm/edgeos_vm.py update-kernel dev
python3 tools/vmm/edgeos_vm.py update-kernel dev --jobs 8
python3 tools/vmm/edgeos_vm.py snapshot dev before-syscall-test
python3 tools/vmm/edgeos_vm.py clone dev dev-copy
```

Use Alpine or another ext2/ext4 rootfs image:

```sh
python3 tools/vmm/edgeos_vm.py create alpine --template alpine
python3 tools/vmm/edgeos_vm.py create debian-arm --architecture arm64 --template debian
python3 tools/vmm/edgeos_vm.py create debian-x86 --architecture x86_64 --template debian
python3 tools/vmm/edgeos_vm.py create imported --import-rootfs /path/to/rootfs.img --memory 2048M
```

The Debian profile can build a Debian userspace when `EDGEOS_DISTRIBUTION_DIR`
points to a compatible distribution source tree. Otherwise, import an existing
ext2/ext4 image with `--import-rootfs`.
Use `edgeos` / `edgeos` for the graphical workstation account or `root` /
`root` for serial-console recovery. The regular account uses Debian's normal
password-gated `sudo` policy.

The Qt VM settings expose `Parallel build jobs` on the Processors page.
`Automatic` uses all available host CPUs. The value is stored per VM and is
used for both initial creation and later kernel updates. ARM64 sources are
compiled into independent objects so the selected job count controls real
compiler parallelism before the final UEFI link.

GPU, display, and input examples:

```sh
python3 tools/vmm/edgeos_vm.py create desktopvm --template debian --usb virtio-input
python3 tools/vmm/edgeos_vm.py create gpuvm --template alpine --gpu virtio-gpu --usb xhci-input
python3 tools/vmm/edgeos_vm.py create gltest --template alpine --gpu virtio-gpu-gl-pci --virgl --display-backend gtk
python3 tools/vmm/edgeos_vm.py start gpuvm --display window
```

New x86_64 and ARM64 virtual machines use VirtIO keyboard and tablet devices
by default so both architectures exercise the same guest input interface.
xHCI and UHCI modes remain available for compatibility testing and USB device
passthrough.

Use `virtio-gpu` for the supported EdgeOS VT/fbconsole path. The QEMU GL
devices are exposed for driver experiments, but EdgeOS does not yet have a full
VirGL/3D userspace rendering path.

The Qt Config dialog can edit CPU, memory, KVM, disk controller, network,
USB/input, GPU, VirGL, display backend, and extra QEMU arguments without
hand-editing `.edgeos-vms/instances/<name>/vm.json`.

Networking examples:

```sh
python3 tools/vmm/edgeos_vm.py tap tap0
python3 tools/vmm/edgeos_vm.py create tapvm --template edgeos --template tap
python3 tools/vmm/edgeos_vm.py create sshvm --net user,model=e1000,hostfwd=tcp::2222-:22
python3 tools/vmm/edgeos_vm.py create bridgevm --net bridge,bridge=br0,model=e1000
python3 tools/vmm/edgeos_vm.py macvtap edge-macvtap0 --parent eth0
python3 tools/vmm/edgeos_vm.py create lanvm --net macvtap,ifname=edge-macvtap0,model=e1000
```

Bridge mode uses QEMU's bridge backend with an existing Linux bridge. Macvtap
mode creates an external-network macvlan/macvtap interface and passes its
`/dev/tapN` file descriptor to QEMU.

Configuration can be inspected and edited directly:

```sh
python3 tools/vmm/edgeos_vm.py show dev
$EDITOR .edgeos-vms/instances/dev/vm.json
```

Important configuration keys include `cpus`, `memory`, `networks`,
`disk_controller`, `disk_model`, `usb`, `gpu`, `virgl`, `display_backend`,
`boot_params`, `qemu_args`, `passthrough`, and `storage`. Save reusable
configurations with:

```sh
python3 tools/vmm/edgeos_vm.py template-save dev workstation
python3 tools/vmm/edgeos_vm.py create next --template workstation
```

`start` uses serial stdio and no graphical window by default. Use
`--display window` for a normal QEMU VM window. For background runs, use
`--background`; serial output is written to the VM's `serial.log`. KVM is used
by default when enabled in the VM config. On hosts where KVM is unavailable,
boot with:

```sh
python3 tools/vmm/edgeos_vm.py start dev --display window --no-kvm
```

Temporary launch overrides such as `--memory 2048M`, `--machine`, and
`--cpu-model` are available for one-off tests without editing the saved VM
configuration.

## X11 apps

From a text login, Linux does not set `DISPLAY` until an X server/session is
running. For Alpine VMs, the VMM can drive the running guest through serial,
install the expected Xorg fbdev/input package set, start Xorg on `:1`, and
launch a first app:

The `x11-run` command can prepare an existing Alpine guest and launch the first
application through its serial channel.

```sh
python3 tools/vmm/edgeos_vm.py start alpine --background --display window
python3 tools/vmm/edgeos_vm.py x11-run alpine --app xterm
```

`x11-run` leaves the active serial shell with `DISPLAY=:1` exported, so
additional serial commands can start X11 apps directly. On a separate VT login,
export the same display first:

```sh
export DISPLAY=:1
xterm &
```

For an existing X server, skip the package/server setup:

```sh
python3 tools/vmm/edgeos_vm.py x11-run alpine --no-install-deps --no-start-server --app xclock
```
