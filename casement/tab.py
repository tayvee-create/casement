"""A tab: its own location, history and content stack."""
from gi.repository import Gio, GObject, Gtk

from .archives import archive_root, archive_source
from .folderview import FolderView
from .homepage import HomePage, ThisPCPage
from .items import HOME, THISPC, is_special, loc_equal, location_icon_name, location_title


class Tab(GObject.Object):
    __gtype_name__ = "W11Tab"
    __gsignals__ = {
        "location-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.loc = None
        self.back_stack = []
        self.forward_stack = []
        self.search_text = ""
        self.title = ""
        self.stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.view = FolderView(window)
        self.stack.add_named(self.view, "folder")
        self.home = None
        self.thispc = None
        self.widget = None  # tab strip button, set by the window

    @property
    def is_folder(self):
        return self.loc is not None and not is_special(self.loc)

    def navigate(self, loc, record=True, select_name=None):
        if self.loc is not None and loc_equal(loc, self.loc):
            if self.is_folder:
                self.view.reload()
            return
        if record and self.loc is not None:
            self.back_stack.append(self.loc)
            self.forward_stack.clear()
        self._show(loc, select_name)

    def _show(self, loc, select_name=None):
        self.loc = loc
        self.search_text = ""
        self.title = location_title(loc)
        if loc == HOME:
            if self.home is None:
                self.home = HomePage(self.window)
                self.home.connect("navigate", lambda p, l, nt: self.window.open_location(l, nt, self))
                self.home.connect("open-file", lambda p, f: self.window.launch_files([f]))
                self.stack.add_named(self.home, "home")
            self.stack.set_visible_child(self.home)
        elif loc == THISPC:
            if self.thispc is None:
                self.thispc = ThisPCPage(self.window)
                self.thispc.connect("navigate", lambda p, l, nt: self.window.open_location(l, nt, self))
                self.stack.add_named(self.thispc, "thispc")
            self.stack.set_visible_child(self.thispc)
        else:
            settings = self.window.settings
            self.view.set_view_mode(settings.view_for(loc.get_uri()))
            self.view.set_show_hidden(settings["show_hidden"])
            self.view.load(loc)
            if select_name:
                self.view.select_later([select_name])
            self.stack.set_visible_child(self.view)
        self.emit("location-changed")

    def can_go_back(self):
        return bool(self.back_stack)

    def can_go_forward(self):
        return bool(self.forward_stack)

    def go_back(self):
        if self.back_stack:
            select = self._child_name(self.loc)
            self.forward_stack.append(self.loc)
            self._show(self.back_stack.pop(), select)

    def go_forward(self):
        if self.forward_stack:
            self.back_stack.append(self.loc)
            self._show(self.forward_stack.pop())

    def up_location(self):
        if self.loc == HOME:
            return None
        if self.loc == THISPC:
            return HOME
        root = archive_root(self.loc)
        if root is not None and root.equal(self.loc):
            # Up from the top of an archive goes to the folder holding the archive file.
            source = archive_source(root)
            return source.get_parent() if source else HOME
        parent = self.loc.get_parent()
        if parent is None:
            return THISPC if self.loc.get_path() else HOME
        return parent

    def go_up(self):
        up = self.up_location()
        if up is not None:
            name = self.loc.get_basename() if self.is_folder else None
            root = archive_root(self.loc)
            if root is not None and root.equal(self.loc):
                source = archive_source(root)
                name = source.get_basename() if source else None
            self.navigate(up, select_name=name)

    def _child_name(self, prev):
        """When going back to a parent, select the folder we came from."""
        if prev is None or is_special(prev) or not self.back_stack:
            return None
        target = self.back_stack[-1]
        if is_special(target):
            return None
        parent = prev.get_parent()
        if parent is not None and parent.equal(target):
            return prev.get_basename()
        return None

    @property
    def icon_name(self):
        return location_icon_name(self.loc) if self.loc is not None else "w11-folder"

    def refresh(self):
        if self.is_folder:
            self.view.reload()
        elif self.loc == HOME and self.home:
            self.home.refresh()
        elif self.loc == THISPC and self.thispc:
            self.thispc.refresh()
