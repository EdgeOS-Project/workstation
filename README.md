# EdgeOS Workstation

EdgeOS Workstation is the QEMU-based desktop virtual machine manager for
developing and testing EdgeOS. It provides persistent VM storage, serial and
graphical consoles, snapshots, hardware acceleration, managed networking, and
x86_64 and ARM64 guest support.

## Repository layout

- `tools/vmm/` contains the CLI, Qt application, local control service, and
  regression tests.
- `tools/net/` contains optional Linux host networking helpers.
- `.edgeos-vms/` is created locally for VM definitions and storage and is never
  committed.

The Workstation and kernel repositories are intentionally separate. By
default, Workstation uses a sibling checkout:

```text
EdgeOS/
├── kernel/
└── workstation/
```

Set `EDGEOS_KERNEL_DIR` when the kernel checkout is elsewhere. A future
distribution source or extracted release can be selected with
`EDGEOS_DISTRIBUTION_DIR`. Existing ext2/ext4 images can always be imported
without distribution sources.

## Requirements

- Python 3.11 or newer
- QEMU for the guest architecture
- GNU Make and the EdgeOS kernel toolchain for kernel updates
- PyQt6 or PySide6 for the graphical application

On macOS:

```sh
brew install qemu make python@3.14 pyqt
```

## Start the application

```sh
python3 tools/vmm/launch_workstation.py
```

The command-line manager is also available directly:

```sh
python3 tools/vmm/edgeos_vm.py --help
python3 tools/vmm/edgeos_vm.py create dev --import-rootfs /path/to/rootfs.img
python3 tools/vmm/edgeos_vm.py start dev --display window
python3 tools/vmm/edgeos_vm.py update-kernel dev
```

VM state can be moved outside the checkout with
`EDGEOS_WORKSTATION_STATE_DIR`.

## Validate

```sh
python3 -m py_compile tools/vmm/*.py
QT_QPA_PLATFORM=offscreen python3 -m unittest \
  tools.vmm.test_workstation tools.vmm.test_resolution
```

See [the VMM reference](tools/vmm/README.md) for the complete command and
configuration guide.

## License

EdgeOS Workstation is distributed under the Mozilla Public License 2.0. See
[LICENSE](LICENSE).

