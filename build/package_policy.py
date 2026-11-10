"""Independent allowlist for files that affect the host outside the private prefix."""
from pathlib import PurePosixPath

PRIVATE = PurePosixPath("opt/nerdtools")
INTEGRATION = {
    "audit": {"etc/rc.d/rc.auditd.new"},
    "screen": {"etc/screenrc.new", "etc/skel/.screenrc.new", "etc/pam.d/screen.new"},
    "bluez": {
        "etc/bluetooth/input.conf.new", "etc/bluetooth/main.conf.new", "etc/bluetooth/network.conf.new",
        "etc/bluetooth/mesh-main.conf.new", "etc/default/bluetooth.new", "etc/rc.d/rc.bluetooth.new",
        "etc/modprobe.d/btusb.conf", "etc/udev/rules.d/97-hid2hci.rules", "etc/xdg/autostart/mpris-proxy.desktop",
        "usr/share/dbus-1/system.d/bluetooth.conf", "usr/share/dbus-1/system.d/obex.conf",
        "usr/share/dbus-1/system.d/bluetooth-mesh.conf", "usr/share/dbus-1/services/org.bluez.obex.service",
    },
}
COMMON_DIRS = {PurePosixPath(name) for name in (".", "opt", "opt/nerdtools", "install", "usr", "usr/local", "usr/local/bin")}
METADATA = {PurePosixPath("install/slack-desc"), PurePosixPath("install/doinst.sh")}


def check_destination(tool, member, path):
    if tool == "rar":
        raise ValueError("RAR is external only")
    if path.is_relative_to(PRIVATE):
        return
    integrations = {PurePosixPath(name) for name in INTEGRATION.get(tool, ())}
    if member.isdir():
        allowed = path in COMMON_DIRS or any(path in item.parents for item in integrations)
        if tool == "bluez" and path == PurePosixPath("var/lib/bluetooth"):
            if member.mode & 0o7777 != 0o700:
                raise ValueError("Bluetooth state directory must have mode0700")
            return
        if tool == "bluez" and path in {PurePosixPath("var"), PurePosixPath("var/lib")}:
            allowed = True
        if not allowed or member.mode & 0o7777 != 0o755:
            raise ValueError(f"Unapproved integration directory: {member.name}")
        return
    if path in integrations and member.isfile() and member.mode & 0o7777 == 0o644:
        return
    if path in METADATA and member.isfile() and member.mode & 0o7777 in (0o644, 0o755):
        return
    raise ValueError(f"Package touches forbidden host paths: {member.name}")
