# EdgeOS Alpine XFCE4 Test Image

This bundle contains the EdgeOS boot ISO and a separate Alpine rootfs image
with XFCE4 packages already installed.

Boot it from the directory containing these files:

```sh
./run-edgeos-xfce4-image.sh
```

Log in on the serial console as:

```text
root
root
```

Start the desktop from the guest shell:

```sh
/root/startxfce4.sh
```

Alpine does not install Bash in this image; run the script directly or with
`sh /root/startxfce4.sh`.

The launcher writes its log to:

```text
/tmp/edgeos-startxfce4-current.log
```

The QEMU command uses virtio-gpu, virtio-blk, e1000 networking, and explicit
xHCI USB keyboard/mouse devices, matching the verified `x11-verify` setup.
Set `KVM=0` before running the script if the target machine has no KVM access.
