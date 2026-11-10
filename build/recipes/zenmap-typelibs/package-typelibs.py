"""Copy only source-generated metadata whose library names match Unraid 7.3.4."""
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

sdk, destination = map(Path, sys.argv[1:])
# Source: each pinned project's GIR generator; no GIRepository-2.0 from legacy GI.
libraries = {
    "GLib-2.0": {"libglib-2.0.so.0", "libgobject-2.0.so.0"},
    "GObject-2.0": {"libgobject-2.0.so.0"},
    "GModule-2.0": {"libgmodule-2.0.so.0"},
    "Gio-2.0": {"libgio-2.0.so.0"},
    "cairo-1.0": {"libcairo-gobject.so.2"},
    "fontconfig-2.0": set(),
    "freetype2-2.0": set(),
    "xlib-2.0": set(),
    "xft-2.0": set(),
    "Atk-1.0": {"libatk-1.0.so.0"},
    "GdkPixbuf-2.0": {"libgdk_pixbuf-2.0.so.0"},
    "HarfBuzz-0.0": {"libharfbuzz-gobject.so.0", "libharfbuzz.so.0"},
    "Pango-1.0": {"libpango-1.0.so.0"},
    "PangoCairo-1.0": {"libpangocairo-1.0.so.0"},
    "PangoFc-1.0": {"libpangoft2-1.0.so.0"},
    "PangoFT2-1.0": {"libpangoft2-1.0.so.0"},
    "PangoOT-1.0": {"libpangoft2-1.0.so.0"},
    "PangoXft-1.0": {"libpangoxft-1.0.so.0"},
    "Gdk-3.0": {"libgdk-3.so.0"},
    "GdkX11-3.0": {"libgdk-3.so.0"},
    "Gtk-3.0": {"libgtk-3.so.0"},
}
ns = {"gi": "http://www.gtk.org/introspection/core/1.0"}
for name, expected in libraries.items():
    gir = ET.parse(sdk / "share/gir-1.0" / f"{name}.gir").getroot()
    namespace = gir.find("gi:namespace", ns)
    assert namespace is not None, f"Missing namespace: {name}"
    assert f'{namespace.attrib["name"]}-{namespace.attrib["version"]}' == name
    actual = set(filter(None, namespace.get("shared-library", "").split(",")))
    assert actual == expected, f"Non-baseline library reference in {name}: {actual}"
    for include in gir.findall("gi:include", ns):
        dependency = f'{include.attrib["name"]}-{include.attrib["version"]}'
        assert dependency in libraries, f"Unpackaged typelib dependency: {name} -> {dependency}"
    typelib = sdk / "lib64/girepository-1.0" / f"{name}.typelib"
    assert typelib.is_file() and typelib.stat().st_size > 0, f"Missing generated typelib: {name}"
    shutil.copyfile(typelib, destination / typelib.name)
