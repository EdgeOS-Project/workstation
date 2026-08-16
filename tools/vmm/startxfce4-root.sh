#!/bin/sh
# EdgeOS Alpine XFCE launcher.
#
# This script does not replace XFCE or patch Alpine userland. It prepares the
# Linux desktop runtime state that a normal booted desktop system usually has
# already: /run, D-Bus, XDG runtime/config directories, and a concrete Xorg
# fbdev/evdev configuration for EdgeOS' current framebuffer/input devices.

set -eu

DISPLAY_NUM="${DISPLAY_NUM:-1}"
VT_NUM="${VT_NUM:-1}"
XORG_CONF="${XORG_CONF:-/tmp/edgeos-xorg-xfce.conf}"
XORG_LOG="${XORG_LOG:-/tmp/edgeos-xorg-${DISPLAY_NUM}.log}"
LOG="${LOG:-/tmp/edgeos-startxfce4-current.log}"
export DISPLAY_NUM VT_NUM XORG_CONF XORG_LOG

if [ -w /dev/console ]; then
    exec 3>/dev/console
else
    exec 3>&1
fi
exec >"$LOG" 2>&1

step() {
    ts="$(date +%s 2>/dev/null || echo 0)"
    printf 'XFCE_STEP %s %s\n' "$ts" "$1"
    printf 'XFCE_STEP %s %s\n' "$ts" "$1" >&3
}

step prepare-runtime
mkdir -p /tmp/.X11-unix /run /run/dbus
rm -f "/tmp/.X11-unix/X${DISPLAY_NUM}" "$XORG_LOG"

SESSION_HOME="${EDGEOS_XFCE_SESSION_HOME:-/tmp/edgeos-xfce-session-0}"
export XDG_RUNTIME_DIR="${EDGEOS_XDG_RUNTIME_DIR:-$SESSION_HOME/runtime}"
export XDG_CONFIG_HOME="${EDGEOS_XDG_CONFIG_HOME:-$SESSION_HOME/config}"
export XDG_CACHE_HOME="${EDGEOS_XDG_CACHE_HOME:-$SESSION_HOME/cache}"
export XDG_DATA_HOME="${EDGEOS_XDG_DATA_HOME:-$SESSION_HOME/data}"

# These directories belong exclusively to this launcher.  A console login may
# already export /run/user/<uid>; deleting that active mount is both incorrect
# and guaranteed to fail on a normally configured Debian system.
rm -rf "$SESSION_HOME"
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"
chmod 700 "$XDG_RUNTIME_DIR"

mkdir -p \
    "$HOME/Desktop" "$HOME/Downloads" "$HOME/Templates" "$HOME/Public" \
    "$HOME/Documents" "$HOME/Music" "$HOME/Pictures" "$HOME/Videos" \
    "$XDG_CONFIG_HOME/xfce4/xfconf/xfce-perchannel-xml"

cat >"$XDG_CONFIG_HOME/user-dirs.dirs" <<'EOF_USER_DIRS'
XDG_DESKTOP_DIR="$HOME/Desktop"
XDG_DOWNLOAD_DIR="$HOME/Downloads"
XDG_TEMPLATES_DIR="$HOME/Templates"
XDG_PUBLICSHARE_DIR="$HOME/Public"
XDG_DOCUMENTS_DIR="$HOME/Documents"
XDG_MUSIC_DIR="$HOME/Music"
XDG_PICTURES_DIR="$HOME/Pictures"
XDG_VIDEOS_DIR="$HOME/Videos"
EOF_USER_DIRS

cat >"$XDG_CONFIG_HOME/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml" <<'EOF_XFCE_DESKTOP'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitor0" type="empty">
        <property name="workspace0" type="empty">
          <property name="color-style" type="int" value="0"/>
          <property name="image-style" type="int" value="0"/>
          <property name="rgba1" type="array">
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="1.0"/>
          </property>
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-blue.jpg"/>
        </property>
      </property>
      <!-- Xorg fbdev exposes its RandR connector as lowercase "default". -->
      <property name="monitorDefault" type="empty">
        <property name="workspace0" type="empty">
          <property name="color-style" type="int" value="0"/>
          <property name="image-style" type="int" value="0"/>
          <property name="rgba1" type="array">
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="1.0"/>
          </property>
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-blue.jpg"/>
        </property>
      </property>
      <property name="monitorS" type="empty">
        <property name="workspace0" type="empty">
          <property name="color-style" type="int" value="0"/>
          <property name="image-style" type="int" value="0"/>
          <property name="rgba1" type="array">
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="0.0"/>
            <value type="double" value="1.0"/>
          </property>
          <property name="last-image" type="string" value="/usr/share/backgrounds/xfce/xfce-blue.jpg"/>
        </property>
      </property>
    </property>
  </property>
</channel>
EOF_XFCE_DESKTOP

cat >"$XDG_CONFIG_HOME/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml" <<'EOF_XFWM4'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfwm4" version="1.0">
  <property name="general" type="empty">
    <property name="use_compositing" type="bool" value="false"/>
  </property>
</channel>
EOF_XFWM4

cat >"$XDG_CONFIG_HOME/mimeapps.list" <<'EOF_MIMEAPPS'
[Default Applications]
inode/directory=thunar.desktop;

[Added Associations]
inode/directory=thunar.desktop;
EOF_MIMEAPPS

mkdir -p "$XDG_CONFIG_HOME/xfce4"
cat >"$XDG_CONFIG_HOME/xfce4/helpers.rc" <<'EOF_XFCE_HELPERS'
FileManager=Thunar
TerminalEmulator=xfce4-terminal
EOF_XFCE_HELPERS

if command -v chromium >/dev/null 2>&1; then
    mkdir -p /etc/chromium.d
    cat >/etc/chromium.d/edgeos-renderer <<'EOF_CHROMIUM_RENDERER'
CHROMIUM_FLAGS="$CHROMIUM_FLAGS --enable-unsafe-swiftshader --use-gl=angle --use-angle=swiftshader"
EOF_CHROMIUM_RENDERER
fi

FB_BPP="$(cat /sys/class/graphics/fb0/bits_per_pixel 2>/dev/null || true)"
case "$FB_BPP" in
    8|16|32) ;;
    *) FB_BPP=32 ;;
esac

cat >"$XORG_CONF" <<EOF_XORG_CONF
Section "ServerFlags"
    Option "AutoAddDevices" "false"
    Option "AllowMouseOpenFail" "false"
EndSection
Section "Device"
    Identifier "D"
    Driver "fbdev"
    Option "fbdev" "/dev/fb0"
    Option "ShadowFB" "false"
EndSection
Section "Monitor"
    Identifier "M"
EndSection
Section "Screen"
    Identifier "S"
    Device "D"
    Monitor "M"
    DefaultFbBpp $FB_BPP
EndSection
Section "InputDevice"
    Identifier "K"
    Driver "evdev"
    Option "Device" "/dev/input/event0"
    Option "CoreKeyboard" "true"
EndSection
Section "InputDevice"
    Identifier "P"
    Driver "evdev"
    Option "Device" "/dev/input/event1"
    Option "CorePointer" "true"
    Option "Mode" "Absolute"
EndSection
Section "ServerLayout"
    Identifier "L"
    Screen "S"
    InputDevice "K" "CoreKeyboard"
    InputDevice "P" "CorePointer"
EndSection
EOF_XORG_CONF

# Keep the current fbdev Xorg path away from Mesa/GL probing. This preserves
# real XFCE while avoiding renderer choices that require kernel DRM features
# EdgeOS does not expose yet.
export GDK_GL=disable
export GDK_RENDERING=image
export GSK_RENDERER=cairo
export NO_AT_BRIDGE=1
export XDG_CURRENT_DESKTOP=XFCE
export DESKTOP_SESSION=xfce
export XDG_SESSION_DESKTOP=xfce

step start-system-dbus
if command -v dbus-daemon >/dev/null 2>&1; then
    if [ ! -S /run/dbus/system_bus_socket ] ||
       ! timeout 3 dbus-send --system --type=method_call --print-reply \
           --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames \
           >/tmp/edgeos-dbus-system-check.log 2>&1; then
        rm -f /run/dbus/system_bus_socket /run/dbus/pid
        dbus-daemon --system --fork >/tmp/edgeos-dbus-system.log 2>&1 || true
    fi
fi
if command -v rc-service >/dev/null 2>&1 && [ -x /etc/init.d/elogind ]; then
    rc-service elogind status >/dev/null 2>&1 ||
        rc-service elogind start </dev/null >/tmp/edgeos-elogind.log 2>&1 || true
fi

step start-session
unset DISPLAY
printf 'Starting XFCE4 on DISPLAY=:%s vt%s; log=%s xorg_log=%s\n' \
    "$DISPLAY_NUM" "$VT_NUM" "$LOG" "$XORG_LOG" >&3
exec dbus-run-session -- sh -c '
printf "DBUS_SESSION_BUS_ADDRESS=%s\n" "$DBUS_SESSION_BUS_ADDRESS" >"/tmp/edgeos-dbus-${DISPLAY_NUM}.env"
if command -v dbus-update-activation-environment >/dev/null 2>&1; then
    dbus-update-activation-environment \
        DBUS_SESSION_BUS_ADDRESS DISPLAY XDG_RUNTIME_DIR \
        >/tmp/edgeos-dbus-activation-env-${DISPLAY_NUM}.log 2>&1 || true
fi
exec startxfce4 -- ":${DISPLAY_NUM}" "vt${VT_NUM}" -config "$XORG_CONF" -br -audit 0 -verbose 0 -logverbose 0
'
