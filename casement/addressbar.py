"""Breadcrumb address bar that turns into a text entry, like Explorer."""
import os

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from .archives import archive_root, archive_source
from .items import (HOME, THISPC, is_special, location_icon_name, location_title,
                    mount_for)


def build_crumbs(loc):
    """List of (title, location) from the root to loc."""
    if loc == HOME:
        return [("Home", HOME)]
    if loc == THISPC:
        return [("This PC", THISPC)]
    crumbs = []
    uri = loc.get_uri()
    path = loc.get_path()
    home = os.path.normpath(GLib.get_home_dir())
    if uri.startswith("trash:"):
        root = Gio.File.new_for_uri("trash:///")
        crumbs.append(("Recycle Bin", root))
    elif uri.startswith("archive:") and archive_root(loc) is not None:
        # Inside an archive: the path to the archive file, then the archive, then its folders.
        root = archive_root(loc)
        source = archive_source(root)
        parent = source.get_parent() if source else None
        if parent is not None:
            crumbs.extend(build_crumbs(parent))
        crumbs.append((source.get_basename() if source else "Archive", root))
    elif path:
        path = os.path.normpath(path)
        mount = mount_for(loc)
        mroot = mount.get_root().get_path() if mount else None
        crumbs.append(("This PC", THISPC))
        if path == home or path.startswith(home + os.sep):
            root = Gio.File.new_for_path(home)
            crumbs.append((GLib.get_user_name(), root))
        elif mroot and mroot != "/" and (path == mroot or path.startswith(mroot + os.sep)):
            root = mount.get_root()
            crumbs.append((mount.get_name(), root))
        else:
            root = Gio.File.new_for_path("/")
            crumbs.append(("Local Disk (/)", root))
    else:
        mount = mount_for(loc)
        root = mount.get_root() if mount else loc
        while not mount and root.get_parent() is not None:
            root = root.get_parent()
        crumbs.append((location_title(root), root))
    parts = []
    f = loc
    while f is not None and not f.equal(root):
        parts.append(f)
        f = f.get_parent()
    for p in reversed(parts):
        crumbs.append((p.get_basename() if p.get_path() else location_title(p), p))
    return crumbs


def location_text(loc):
    if loc == HOME:
        return "Home"
    if loc == THISPC:
        return "This PC"
    if loc.get_uri_scheme() == "archive":
        # Show "~/Downloads/photos.zip/2024" rather than the internal archive:// address.
        root = archive_root(loc)
        source = archive_source(root) if root else None
        if source is not None and source.get_path():
            inner = root.get_relative_path(loc) or ""
            return source.get_path() + ("/" + inner if inner else "")
    if loc.get_uri().rstrip("/") == "trash:":
        return "Recycle Bin"
    return loc.get_path() or loc.get_uri()


class AddressBar(Gtk.Box):
    __gtype_name__ = "W11AddressBar"
    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "navigate-text": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, window):
        super().__init__(hexpand=True)
        self.window = window
        self.loc = None
        self.add_css_class("w11-address")
        self.stack = Gtk.Stack(hexpand=True, transition_type=Gtk.StackTransitionType.NONE)
        self.append(self.stack)

        crumbs_page = Gtk.Box(hexpand=True)
        self.icon = Gtk.Image(pixel_size=16, margin_start=8, margin_end=4)
        crumbs_page.append(self.icon)
        self.lead_chevron = self._chevron(lambda: "explorer:root")
        crumbs_page.append(self.lead_chevron)
        self.crumb_box = Gtk.Box()
        self.crumb_scroll = Gtk.ScrolledWindow(child=self.crumb_box,
                                               hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                               vscrollbar_policy=Gtk.PolicyType.NEVER,
                                               propagate_natural_width=True)
        crumbs_page.append(self.crumb_scroll)
        filler = Gtk.Box(hexpand=True)
        crumbs_page.append(filler)
        for w in (filler, self.icon):
            click = Gtk.GestureClick()
            click.connect("released", lambda *a: self.edit())
            w.add_controller(click)
        self.stack.add_named(crumbs_page, "crumbs")

        self.entry = Gtk.Entry(hexpand=True, has_frame=False)
        self.entry.connect("activate", self._on_entry_activate)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *a: GLib.idle_add(self._leave_edit))
        self.entry.add_controller(focus)
        self.stack.add_named(self.entry, "entry")

        # Recent locations dropdown (the "v" at the end of Explorer's address bar).
        self.recent_btn = Gtk.MenuButton(icon_name="pan-down-symbolic", tooltip_text="Previous Locations")
        self.recent_btn.add_css_class("flat")
        self.recent_btn.add_css_class("w11-addr-btn")
        self.recent_btn.set_create_popup_func(self._build_recent)
        self.append(self.recent_btn)

    # ------------------------------------------------------------ crumbs

    def set_location(self, loc):
        self.loc = loc
        self.icon.set_from_icon_name(location_icon_name(loc))
        child = self.crumb_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.crumb_box.remove(child)
            child = nxt
        crumbs = build_crumbs(loc)
        for i, (title, cloc) in enumerate(crumbs):
            btn = Gtk.Button()
            # Crumbs keep their full width (up to 28 chars); on deep paths the
            # leading ones scroll off to the left, as in Explorer.
            lbl = Gtk.Label(label=title, ellipsize=Pango.EllipsizeMode.MIDDLE,
                            width_chars=min(len(title), 28), max_width_chars=28)
            btn.set_child(lbl)
            btn.add_css_class("flat")
            btn.add_css_class("w11-crumb")
            btn.connect("clicked", lambda b, l=cloc: self.emit("navigate", l))
            self.crumb_box.append(btn)
            self.crumb_box.append(self._chevron(lambda l=cloc: l))
        if not self.stack.get_visible_child_name() == "entry":
            self.entry.set_text(location_text(loc))
        GLib.idle_add(self._scroll_end)

    def _scroll_end(self):
        adj = self.crumb_scroll.get_hadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _chevron(self, loc_func):
        btn = Gtk.MenuButton(icon_name="pan-end-symbolic")
        btn.add_css_class("flat")
        btn.add_css_class("w11-chevron")
        pop = Gtk.Popover(has_arrow=False, halign=Gtk.Align.START)
        pop.add_css_class("w11-crumb-menu")
        btn.set_popover(pop)
        pop.connect("show", lambda p: self._fill_chevron(p, loc_func()))
        pop.connect("show", lambda p: btn.set_icon_name("pan-down-symbolic"))
        pop.connect("closed", lambda p: btn.set_icon_name("pan-end-symbolic"))
        return btn

    def _menu_button(self, pop, title, icon, loc):
        b = Gtk.Button()
        row = Gtk.Box(spacing=8)
        row.append(Gtk.Image(icon_name=icon, pixel_size=16) if isinstance(icon, str)
                   else Gtk.Image(gicon=icon, pixel_size=16))
        row.append(Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                             max_width_chars=40))
        b.set_child(row)
        b.add_css_class("flat")
        b.add_css_class("w11-menu-row")

        def go(_b):
            pop.popdown()
            self.emit("navigate", loc)
        b.connect("clicked", go)
        return b

    def _fill_chevron(self, pop, loc):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller = Gtk.ScrolledWindow(child=box, propagate_natural_height=True,
                                      propagate_natural_width=True, max_content_height=420,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        pop.set_child(scroller)
        if loc == "explorer:root":
            box.append(self._menu_button(pop, "Home", "w11-home", HOME))
            for path in self.window.settings["pins"]:
                f = Gio.File.new_for_path(path)
                box.append(self._menu_button(pop, os.path.basename(path), location_icon_name(f), f))
            box.append(self._menu_button(pop, "This PC", "w11-thispc", THISPC))
            box.append(self._menu_button(pop, "Network", "w11-network",
                                         Gio.File.new_for_uri("network:///")))
            box.append(self._menu_button(pop, "Recycle Bin", "w11-trash",
                                         Gio.File.new_for_uri("trash:///")))
            return
        if loc == HOME:
            for path in self.window.settings["pins"]:
                f = Gio.File.new_for_path(path)
                box.append(self._menu_button(pop, os.path.basename(path), location_icon_name(f), f))
            return
        if loc == THISPC:
            box.append(self._menu_button(pop, GLib.get_user_name(), "w11-home",
                                         Gio.File.new_for_path(GLib.get_home_dir())))
            for title, f, icon, mount, vol in self.window.sidebar.drives():
                if f is not None:
                    box.append(self._menu_button(pop, title, icon, f))
            return
        if is_special(loc):
            return

        def on_enum(src, res):
            try:
                enum = src.enumerate_children_finish(res)
            except GLib.Error:
                return
            dirs = []
            while True:
                try:
                    infos = enum.next_files(200, None)
                except GLib.Error:
                    break
                if not infos:
                    break
                for info in infos:
                    if info.get_file_type() == Gio.FileType.DIRECTORY and not info.get_attribute_boolean("standard::is-hidden"):
                        dirs.append((GLib.utf8_collate_key_for_filename(
                            info.get_display_name().casefold(), -1), info, enum.get_child(info)))
            dirs.sort(key=lambda t: t[0])
            from .items import folder_icon_for
            for _, info, child in dirs[:400]:
                box.append(self._menu_button(pop, info.get_display_name(), folder_icon_for(child), child))
            if not dirs:
                lbl = Gtk.Label(label="No subfolders", margin_top=6, margin_bottom=6,
                                margin_start=12, margin_end=12)
                lbl.add_css_class("w11-dim")
                box.append(lbl)
        loc.enumerate_children_async("standard::name,standard::display-name,standard::type,"
                                     "standard::is-hidden", Gio.FileQueryInfoFlags.NONE,
                                     GLib.PRIORITY_DEFAULT, None, on_enum)

    def _build_recent(self, btn):
        pop = Gtk.Popover(has_arrow=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pop.set_child(box)
        seen = []
        for loc in reversed(self.window.recent_locations):
            key = loc if is_special(loc) else loc.get_uri()
            if key in seen:
                continue
            seen.append(key)
            box.append(self._menu_button(pop, location_text(loc), location_icon_name(loc), loc))
            if len(seen) >= 12:
                break
        btn.set_popover(pop)

    # ------------------------------------------------------------ editing

    def edit(self):
        if self.loc is not None:
            self.entry.set_text(location_text(self.loc))
        self.stack.set_visible_child_name("entry")
        self.add_css_class("editing")
        self.entry.grab_focus()
        self.entry.select_region(0, -1)

    def _leave_edit(self):
        if self.stack.get_visible_child_name() == "entry" and not self.entry.has_focus():
            self.stack.set_visible_child_name("crumbs")
            self.remove_css_class("editing")
        return False

    def cancel_edit(self):
        self.stack.set_visible_child_name("crumbs")
        self.remove_css_class("editing")
        self.window.focus_content()

    def _on_entry_activate(self, entry):
        text = entry.get_text().strip()
        self.cancel_edit()
        if text:
            self.emit("navigate-text", text)

    def _on_entry_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.cancel_edit()
            return True
        return False
