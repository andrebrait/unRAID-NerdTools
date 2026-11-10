"""Run with the private Python and GI_TYPELIB_PATH on the untouched Unraid baseline."""
import importlib
import locale
import os
from pathlib import Path

import cairo
import gi

assert os.environ["GI_TYPELIB_PATH"] == "/opt/nerdtools/lib64/girepository-1.0"
locale.setlocale(locale.LC_ALL, "")
assert locale.getpreferredencoding(False).lower().replace("-", "") == "utf8"
gi.require_foreign("cairo")
for namespace, version in (
    ("GLib", "2.0"), ("GObject", "2.0"), ("GModule", "2.0"), ("Gio", "2.0"),
    ("Atk", "1.0"), ("GdkPixbuf", "2.0"), ("HarfBuzz", "0.0"),
    ("Pango", "1.0"), ("PangoCairo", "1.0"), ("PangoFc", "1.0"),
    ("PangoFT2", "1.0"), ("PangoOT", "1.0"), ("PangoXft", "1.0"),
    ("Gdk", "3.0"), ("GdkX11", "3.0"), ("Gtk", "3.0"),
):
    assert Path(os.environ["GI_TYPELIB_PATH"], f"{namespace}-{version}.typelib").is_file()
    gi.require_version(namespace, version)
    importlib.import_module(f"gi.repository.{namespace}")

from gi.repository import Atk, GdkPixbuf, Gio, GLib, Gtk, HarfBuzz, Pango, PangoCairo

assert GLib.check_version(2, 90, 1) is None
assert Gio.File.new_for_path("/opt/nerdtools").get_path() == "/opt/nerdtools"
assert GLib.Variant("(si)", ("Zenmap", 7)).unpack() == ("Zenmap", 7)
assert Atk.get_version() == "2.38.0"
assert Atk.role_for_name("push button") == Atk.Role.PUSH_BUTTON
assert GdkPixbuf.PIXBUF_VERSION == "2.44.8"
pixels = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 2, 2)
pixels.fill(0x336699FF)
ok, png = pixels.save_to_bufferv("png", [], [])
assert ok and bytes(png).startswith(b"\x89PNG\r\n\x1a\n")
loader = GdkPixbuf.PixbufLoader.new_with_type("png")
assert loader.write(png) and loader.close()
assert loader.get_pixbuf().get_width() == 2
assert HarfBuzz.version_string() == "14.6.0"
assert Pango.version_string() == "1.58.2"
context = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 256, 64))
layout = PangoCairo.create_layout(context)
layout.set_text("Zenmap — Ελληνικά", -1)
assert layout.get_pixel_size()[0] > 0
PangoCairo.show_layout(context, layout)
assert (Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()) == (3, 24, 52)
model = Gtk.ListStore(str, int)
row = model.append(["localhost", 7])
assert model[row][0] == "localhost" and model[row][1] == 7
# A display is deliberately not required: real window creation needs a GUI session.
