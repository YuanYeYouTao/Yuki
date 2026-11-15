"""A GTK 3 test window through ctypes, for ``elements`` on Yuki's own computer.

Usage: python gtk_window.py <title> <log>
A button "Press me", an entry and a label. Each button click appends ``clicked`` to <log>;
the entry's text is written to <log>.entry on every change. Runs with the environment's
own libgtk-3 (installed with Chromium); no Python GTK bindings are needed.
"""

import ctypes
import sys
from pathlib import Path

TITLE, LOG = sys.argv[1], Path(sys.argv[2])
gtk = ctypes.CDLL("libgtk-3.so.0")
gobject = ctypes.CDLL("libgobject-2.0.so.0")
P = ctypes.c_void_p
for name in ("gtk_window_new", "gtk_button_new_with_label", "gtk_entry_new", "gtk_box_new"):
    getattr(gtk, name).restype = P
gtk.gtk_label_new.restype = P
gtk.gtk_label_new.argtypes = [ctypes.c_char_p]
gtk.gtk_init.argtypes = [P, P]
gtk.gtk_button_new_with_label.argtypes = [ctypes.c_char_p]
gtk.gtk_box_new.argtypes = [ctypes.c_int, ctypes.c_int]
gtk.gtk_window_set_title.argtypes = [P, ctypes.c_char_p]
gtk.gtk_window_set_default_size.argtypes = [P, ctypes.c_int, ctypes.c_int]
gtk.gtk_window_move.argtypes = [P, ctypes.c_int, ctypes.c_int]
gtk.gtk_container_add.argtypes = [P, P]
gtk.gtk_box_pack_start.argtypes = [P, P, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
gtk.gtk_widget_show_all.argtypes = [P]
gtk.gtk_entry_get_text.restype = ctypes.c_char_p
gtk.gtk_entry_get_text.argtypes = [P]
CALLBACK = ctypes.CFUNCTYPE(None, P, P)
gobject.g_signal_connect_data.argtypes = [P, ctypes.c_char_p, CALLBACK, P, P, ctypes.c_int]

gtk.gtk_init(None, None)
window = gtk.gtk_window_new(0)
gtk.gtk_window_set_title(window, TITLE.encode())
gtk.gtk_window_set_default_size(window, 420, 200)
gtk.gtk_window_move(window, 900, 520)
box = gtk.gtk_box_new(1, 6)
button = gtk.gtk_button_new_with_label(b"Press me")
entry = gtk.gtk_entry_new()


@CALLBACK
def clicked(widget: int, data: int) -> None:
    with LOG.open("a") as stream:
        stream.write("clicked\n")


@CALLBACK
def changed(widget: int, data: int) -> None:
    LOG.with_suffix(".entry").write_bytes(gtk.gtk_entry_get_text(entry) or b"")


gobject.g_signal_connect_data(button, b"clicked", clicked, None, None, 0)
gobject.g_signal_connect_data(entry, b"changed", changed, None, None, 0)
for widget in (button, entry, gtk.gtk_label_new(b"label text")):
    gtk.gtk_box_pack_start(box, widget, 0, 0, 0)
gtk.gtk_container_add(window, box)
gtk.gtk_widget_show_all(window)
LOG.touch()
gtk.gtk_main()
