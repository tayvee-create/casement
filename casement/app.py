"""Application: shared state, styling, accelerators and windows."""
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .fileops import JobManager  # noqa: E402
from .items import HOME  # noqa: E402
from .settings import Settings  # noqa: E402

APP_ID = "io.github.tayvee_create.Casement"
HERE = os.path.dirname(os.path.abspath(__file__))

ACCELS = {
    "win.new-tab": ["<Control>t"],
    "win.close-tab": ["<Control>w", "<Control>F4"],
    "win.new-window": ["<Control>n"],
    "win.next-tab": ["<Control>Tab", "<Control>Page_Down"],
    "win.prev-tab": ["<Control><Shift>Tab", "<Control>Page_Up"],
    "win.back": ["<Alt>Left", "Back"],
    "win.forward": ["<Alt>Right", "Forward"],
    "win.up": ["<Alt>Up"],
    "win.refresh": ["F5", "<Control>r"],
    "win.focus-address": ["<Control>l", "<Alt>d", "F4"],
    "win.focus-search": ["<Control>e", "<Control>f", "F3"],
    "win.new-folder": ["<Control><Shift>n"],
    "win.rename": ["F2"],
    "win.properties": ["<Alt>Return"],
    "win.right-pane::preview": ["<Alt>p"],
    "win.right-pane::details": ["<Alt><Shift>p"],
    "win.show-hidden": ["<Control>h"],
    "win.copy-path": ["<Control><Shift>c"],
    "win.view-mode::xlarge": ["<Control><Shift>1"],
    "win.view-mode::large": ["<Control><Shift>2"],
    "win.view-mode::medium": ["<Control><Shift>3"],
    "win.view-mode::small": ["<Control><Shift>4"],
    "win.view-mode::details": ["<Control><Shift>6"],
    "win.view-mode::tiles": ["<Control><Shift>7"],
    "app.quit": ["<Control>q"],
}

SHORTCUTS = [
    ("Navigation", [
        ("<Alt>Left", "Back"), ("<Alt>Right", "Forward"), ("<Alt>Up", "Up one level"),
        ("BackSpace", "Back"), ("F5", "Refresh"), ("<Control>l", "Select the address bar"),
        ("<Control>e", "Select the search box"),
    ]),
    ("Tabs and windows", [
        ("<Control>t", "New tab"), ("<Control>w", "Close tab"), ("<Control>Tab", "Next tab"),
        ("<Control><Shift>Tab", "Previous tab"), ("<Control>n", "New window"),
    ]),
    ("Files", [
        ("<Control>c", "Copy"), ("<Control>x", "Cut"), ("<Control>v", "Paste"),
        ("<Control>z", "Undo"), ("F2", "Rename"), ("Delete", "Move to Recycle Bin"),
        ("<Shift>Delete", "Delete permanently"), ("<Control><Shift>n", "New folder"),
        ("<Alt>Return", "Properties"), ("<Control><Shift>c", "Copy as path"),
        ("<Control>a", "Select all"),
    ]),
    ("View", [
        ("<Alt>p", "Preview pane"), ("<Alt><Shift>p", "Details pane"),
        ("<Control>h", "Show hidden items"), ("<Control><Shift>1", "Extra large icons"),
        ("<Control><Shift>2", "Large icons"), ("<Control><Shift>3", "Medium icons"),
        ("<Control><Shift>4", "Small icons"), ("<Control><Shift>6", "Details"),
        ("<Control><Shift>7", "Tiles"),
    ]),
]


class ExplorerApp(Adw.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if os.environ.get("W11_NON_UNIQUE"):
            # Separate instance (used for testing alongside a running copy).
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.settings = None
        self.jobs = None
        self.cut_uris = set()
        self.undo_stack = []
        self.opened_archives = set()  # archive mount roots we opened (to close later)

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name("Casement")
        self.settings = Settings()
        self.jobs = JobManager()

        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(os.path.join(HERE, "icons"))
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        for name, cb in (("quit", self._quit), ("about", self._about),
                         ("shortcuts", self._shortcuts)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, accels)
        for i in range(1, 10):
            self.set_accels_for_action(f"win.goto-tab({i})", [f"<Control>{i}"])

    def do_activate(self):
        self.new_window(HOME)

    def do_open(self, files, n_files, hint):
        for f in files:
            self.new_window(f)

    def new_window(self, loc):
        from .window import ExplorerWindow
        win = ExplorerWindow(self, loc)
        win.present()
        return win

    def all_tabs(self):
        for w in self.get_windows():
            yield from getattr(w, "tabs", [])

    def set_cut_uris(self, uris):
        self.cut_uris = set(uris)
        for tab in self.all_tabs():
            tab.view.rebind()

    def push_undo(self, entry):
        self.undo_stack.append(entry)
        del self.undo_stack[:-50]
        self._update_all()

    def pop_undo(self):
        entry = self.undo_stack.pop() if self.undo_stack else None
        self._update_all()
        return entry

    def _update_all(self):
        for w in self.get_windows():
            if hasattr(w, "update_actions"):
                w.update_actions()

    def _archive_in_use(self, root_uri, closing=None):
        root = Gio.File.new_for_uri(root_uri)
        for w in self.get_windows():
            if w is closing:
                continue
            for tab in getattr(w, "tabs", []):
                loc = tab.loc
                if isinstance(loc, Gio.File) and (loc.equal(root) or loc.has_prefix(root)):
                    return True
        return False

    def cleanup_archives(self, closing=None, wait=False):
        """Close archives that no tab is showing any more."""
        from .archives import archive_mounts
        pending = []

        def unmounted(mount, res):
            try:
                mount.unmount_with_operation_finish(res)
            except GLib.Error:
                pass  # still busy, or already gone: GVfs tidies up at logout anyway
            if mount in pending:
                pending.remove(mount)

        for mount in archive_mounts():
            uri = mount.get_root().get_uri()
            if uri in self.opened_archives and not self._archive_in_use(uri, closing):
                self.opened_archives.discard(uri)
                pending.append(mount)
                mount.unmount_with_operation(Gio.MountUnmountFlags.NONE, None, None, unmounted)
        if wait:
            # On exit, give the unmount requests a moment to go through.
            ctx = GLib.MainContext.default()
            deadline = GLib.get_monotonic_time() + 1_500_000
            while pending and GLib.get_monotonic_time() < deadline:
                if not ctx.iteration(False):
                    GLib.usleep(10_000)
        return False

    def do_shutdown(self):
        self.cleanup_archives(wait=True)
        Adw.Application.do_shutdown(self)

    def show_progress(self, parent=None):
        if getattr(self, "progress_window", None) is None:
            from .progress import ProgressWindow
            self.progress_window = ProgressWindow(self)
        self.progress_window.show_for(parent)

    def pins_changed(self):
        for w in self.get_windows():
            if hasattr(w, "sidebar"):
                w.sidebar.rebuild()
                for tab in w.tabs:
                    if tab.home is not None and tab.home.get_mapped():
                        tab.home.refresh()
                w.update_actions()

    def _quit(self, *a):
        for w in list(self.get_windows()):
            w.close()

    def _about(self, *a):
        about = Adw.AboutDialog(
            application_name="Casement",
            application_icon=APP_ID,
            version="0.1.0",
            comments="A GNOME file manager inspired by the Windows 11 File Explorer.",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self.get_active_window())

    def _shortcuts(self, *a):
        dialog = Adw.Dialog(title="Keyboard shortcuts", content_width=520, content_height=620)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        for title, rows in SHORTCUTS:
            group = Adw.PreferencesGroup(title=title)
            for accel, label in rows:
                row = Adw.ActionRow(title=label)
                row.add_suffix(Gtk.ShortcutLabel(accelerator=accel, valign=Gtk.Align.CENTER))
                group.add(row)
            page.add(group)
        toolbar.set_content(page)
        dialog.set_child(toolbar)
        dialog.present(self.get_active_window())


def main():
    app = ExplorerApp()
    return app.run(sys.argv)
