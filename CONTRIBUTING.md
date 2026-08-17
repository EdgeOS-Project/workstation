# Contributing to EdgeOS Workstation

Keep changes focused and preserve both x86_64 and ARM64 behavior unless a
host-specific or guest-architecture-specific implementation is required.
Shared VM configuration, lifecycle behavior, and user-visible interfaces
belong in shared code.

Before submitting a change, run:

```sh
python3 -m py_compile tools/vmm/*.py
QT_QPA_PLATFORM=offscreen python3 -m unittest \
  tools.vmm.test_workstation tools.vmm.test_resolution
```

Do not commit VM images, local state, build products, credentials, or
machine-specific paths.

