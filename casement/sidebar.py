"""Navigation pane (left side)."""
import os

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from .folderview import _same_device
from .items import (HOME, THISPC, folder_icon_for, loc_equal, loc_key, location_icon_name,
                    translate_point)


TREE_ATTRS = "standard::name,standard::display-name,standard::type,standard::is-hidden"
MAX_TREE_CHILDREN = 500


class NavRow(Gtk.ListBoxRow):
    __gtype_name__ = "W11NavRow"

    def __init__(self, loc, title, icon, level=0, pinned=False, expander=False,
                 mount=None, volume=None, dynamic=False):
        super().__init__()
        self.loc = loc
        self.title = title
        self.level = level
        self.pinned = pinned
        self.mount = mount
        self.volume = volume
        self.dynamic = dynamic      # created by expanding its parent
        self.expandable = expander
        self.expanded = False
        self.loading = None         # Gio.Cancellable while children load
        self.set_tooltip_text(title)

        box = Gtk.Box(spacing=0)
        pill = Gtk.Box(valign=Gtk.Align.CENTER)
        pill.set_size_request(3, 16)
        pill.add_css_class("w11-pill")
        box.append(pill)
        if level:
            spacer = Gtk.Box()
            spacer.set_size_request(16 * level, -1)
            box.append(spacer)
        self.arrow = Gtk.Button(icon_name="pan-end-symbolic", valign=Gtk.Align.CENTER,
                                focus_on_click=False, can_focus=False)
        self.arrow.add_css_class("flat")
        self.arrow.add_css_class("w11-expander")
        box.append(self.arrow)
        self.set_expandable(expander)
        if isinstance(icon, str):
            img = Gtk.Image(icon_name=icon, pixel_size=16, margin_start=2, margin_end=10)
        else:
            img = Gtk.Image(gicon=icon, pixel_size=16, margin_start=2, margin_end=10)
        box.append(img)
        lbl = Gtk.Label(label=title, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        box.append(lbl)
        if pinned:
            pin = Gtk.Image(icon_name="view-pin-symbolic", pixel_size=12, margin_end=6)
            pin.add_css_class("w11-dim")
            box.append(pin)
        self.eject = None
        if mount is not None and (mount.can_eject() or mount.can_unmount()):
            self.eject = Gtk.Button(icon_name="media-eject-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text="Eject")
            self.eject.add_css_class("flat")
            self.eject.add_css_class("w11-expander")
            box.append(self.eject)
        self.set_child(box)

    def set_expandable(self, expandable):
        self.expandable = expandable
        self.arrow.set_opacity(1 if expandable else 0)
        self.arrow.set_sensitive(expandable)

    def set_arrow(self, expanded):
        self.expanded = expanded
        self.arrow.set_icon_name("pan-down-symbolic" if expanded else "pan-end-symbolic")

    @property
    def key(self):
        return loc_key(self.loc) if self.loc is not None else None


def _tree_expandable(loc):
    """Folders that can show subfolders in the tree (not Network / Recycle Bin)."""
    if not isinstance(loc, Gio.File):
        return False
    return loc.get_uri_scheme() not in ("network", "trash", "recent")


class Sidebar(Gtk.Box):
    __gtype_name__ = "W11Sidebar"
    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
        "files-dropped": (GObject.SignalFlags.RUN_FIRST, None, (object, object, int)),
    }

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.add_css_class("w11-nav")
        self.rows = []
        self.current = None
        # Rows the user expanded (by location key), remembered between sessions.
        self.expanded_keys = set(window.settings.get("nav_expanded", []))

        # Selection is shown with a CSS class rather than ListBox selection, which
        # would otherwise follow keyboard focus.
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("w11-navlist")
        self.listbox.connect("row-activated", self._on_row_activated)
        scroller = Gtk.ScrolledWindow(child=self.listbox, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(scroller)

        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_click)
        self.listbox.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.listbox.add_controller(keys)

        self.monitor = Gio.VolumeMonitor.get()
        for sig in ("mount-added", "mount-removed", "mount-changed", "volume-added",
                    "volume-removed", "volume-changed"):
            self.monitor.connect(sig, lambda *a: self._queue_rebuild())
        self._rebuild_id = 0
        self.rebuild()

    # ------------------------------------------------------------ building

    def _queue_rebuild(self):
        if not self._rebuild_id:
            self._rebuild_id = GLib.timeout_add(200, self._do_rebuild)

    def _do_rebuild(self):
        self._rebuild_id = 0
        self.rebuild()
        return False

    def _add(self, row, index=None):
        if index is None:
            self.listbox.append(row)
            self.rows.append(row)
        else:
            self.listbox.insert(row, index)
            self.rows.append(row)
        if row.expandable:
            row.arrow.connect("clicked", lambda b, r=row: self._toggle(r))
        if isinstance(row.loc, Gio.File) and not row.loc.get_uri().startswith("network:"):
            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
            drop.set_preload(True)
            drop.connect("motion", self._on_drop_motion, row)
            drop.connect("enter", self._on_drop_motion, row)
            drop.connect("drop", self._on_drop, row)
            row.add_controller(drop)
        if row.eject is not None:
            row.eject.connect("clicked", lambda b, r=row: self.window.eject_mount(r.mount))
        return row

    def _separator(self):
        row = Gtk.ListBoxRow(selectable=False, activatable=False, can_focus=False)
        row.add_css_class("w11-sep")
        row.set_child(Gtk.Separator(margin_top=6, margin_bottom=6, margin_start=8, margin_end=8))
        self.listbox.append(row)

    def rebuild(self):
        for row in self.rows:
            if row.loading is not None:
                row.loading.cancel()
        self.rows = []
        self.listbox.remove_all()
        self._add(NavRow(HOME, "Home", "w11-home"))
        self._separator()
        for path in self.window.settings["pins"]:
            f = Gio.File.new_for_path(path)
            title = os.path.basename(path.rstrip("/")) or path
            self._add(NavRow(f, title, location_icon_name(f), pinned=True, expander=True))
        self._separator()
        pc = self._add(NavRow(THISPC, "This PC", "w11-thispc", expander=True))
        for row in self._drive_rows():
            row.set_visible(False)
            self._add(row)
        self._add(NavRow(Gio.File.new_for_uri("network:///"), "Network", "w11-network"))
        self._add(NavRow(Gio.File.new_for_uri("trash:///"), "Recycle Bin", "w11-trash"))
        # Re-open whatever was expanded before.
        for row in list(self.rows):
            if row.level == 0 and row.key in self.expanded_keys:
                self._expand(row)
        self.select_location(self.current)

    def _drive_rows(self):
        rows = [NavRow(Gio.File.new_for_path("/"), "Local Disk (/)", "w11-drive", level=1,
                       expander=True)]
        seen = {"file:///"}
        for mount in self.monitor.get_mounts():
            if mount.is_shadowed():
                continue
            root = mount.get_root()
            if root.get_uri() in seen or root.get_uri_scheme() == "archive":
                continue  # open archives aren't drives
            seen.add(root.get_uri())
            icon = "w11-drive-removable" if mount.can_eject() else "w11-drive"
            rows.append(NavRow(root, mount.get_name(), icon, level=1, mount=mount,
                               expander=_tree_expandable(root)))
        for vol in self.monitor.get_volumes():
            if vol.get_mount() is None and vol.can_mount():
                icon = "w11-drive-removable" if vol.can_eject() else "w11-drive"
                rows.append(NavRow(None, vol.get_name(), icon, level=1, volume=vol))
        return rows

    def drives(self):
        """(title, Gio.File or None, icon, mount, volume) for the This PC page."""
        return [(r.title, r.loc, "w11-drive" if r.loc and r.loc.get_path() == "/" else
                 ("w11-drive-removable" if (r.mount and r.mount.can_eject()) or
                  (r.volume and r.volume.can_eject()) else "w11-drive"), r.mount, r.volume)
                for r in self._drive_rows()]

    # ------------------------------------------------------------ tree

    def _toggle(self, row):
        if row.expanded:
            self._collapse(row)
            self.expanded_keys.discard(row.key)
        else:
            self.expanded_keys.add(row.key)
            self._expand(row)
        self.window.settings["nav_expanded"] = sorted(self.expanded_keys)[:200]

    def _descendants(self, row):
        out = []
        idx = row.get_index() + 1
        while True:
            r = self.listbox.get_row_at_index(idx)
            if not isinstance(r, NavRow) or r.level <= row.level:
                return out
            out.append(r)
            idx += 1

    def _collapse(self, row):
        if row.loading is not None:
            row.loading.cancel()
            row.loading = None
        for r in self._descendants(row):
            if r.loading is not None:
                r.loading.cancel()
                r.loading = None
            if r.dynamic:
                self.listbox.remove(r)
                self.rows.remove(r)
            else:
                r.set_visible(False)
                r.set_arrow(False)
        row.set_arrow(False)

    def _expand(self, row):
        if not row.expandable or row.expanded:
            return
        row.set_arrow(True)
        if row.loc == THISPC:
            # Drives are always present, just hidden while This PC is collapsed.
            for r in self._descendants(row):
                r.set_visible(True)
                if r.key in self.expanded_keys:
                    self._expand(r)
            return
        cancellable = Gio.Cancellable()
        row.loading = cancellable
        row.loc.enumerate_children_async(TREE_ATTRS, Gio.FileQueryInfoFlags.NONE,
                                         GLib.PRIORITY_DEFAULT, cancellable,
                                         self._on_tree_enum, (row, cancellable, []))

    def _on_tree_enum(self, src, res, data):
        row, cancellable, found = data
        try:
            enum = src.enumerate_children_finish(res)
        except GLib.Error:
            if not cancellable.is_cancelled():
                row.loading = None
                self._show_children(row, [])
            return
        enum.next_files_async(200, GLib.PRIORITY_DEFAULT, cancellable, self._on_tree_files,
                              (row, cancellable, found))

    def _on_tree_files(self, enum, res, data):
        row, cancellable, found = data
        try:
            infos = enum.next_files_finish(res)
        except GLib.Error:
            infos = []
        if cancellable.is_cancelled():
            return
        show_hidden = self.window.settings["show_hidden"]
        for info in infos:
            if info.get_file_type() != Gio.FileType.DIRECTORY:
                continue
            name = info.get_name()
            if not show_hidden and (info.get_attribute_boolean("standard::is-hidden") or name.startswith(".")):
                continue
            found.append((GLib.utf8_collate_key_for_filename(
                (info.get_display_name() or name).casefold(), -1), info, enum.get_child(info)))
        if infos and len(found) < MAX_TREE_CHILDREN:
            enum.next_files_async(200, GLib.PRIORITY_DEFAULT, cancellable, self._on_tree_files,
                                  (row, cancellable, found))
            return
        enum.close_async(GLib.PRIORITY_DEFAULT, None, None)
        row.loading = None
        found.sort(key=lambda t: t[0])
        self._show_children(row, found[:MAX_TREE_CHILDREN])

    def _show_children(self, row, children):
        if row.get_parent() is None or not row.expanded:
            return
        if not children:
            # Like Explorer: the arrow disappears once we know there's nothing inside.
            row.set_arrow(False)
            row.set_expandable(False)
            return
        index = row.get_index() + 1
        new_rows = []
        for i, (_, info, child) in enumerate(children):
            r = NavRow(child, info.get_display_name() or info.get_name(), folder_icon_for(child),
                       level=row.level + 1, expander=True, dynamic=True)
            self._add(r, index + i)
            new_rows.append(r)
        for r in new_rows:
            if r.key in self.expanded_keys:
                self._expand(r)
        self.select_location(self.current)

    def _on_key(self, ctrl, keyval, keycode, state):
        row = self.listbox.get_focus_child()
        if not isinstance(row, NavRow):
            return False
        if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right) and row.expandable and not row.expanded:
            self._toggle(row)
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            if row.expanded:
                self._toggle(row)
                return True
            # Move to the parent row.
            idx = row.get_index() - 1
            while idx >= 0:
                r = self.listbox.get_row_at_index(idx)
                if isinstance(r, NavRow) and r.level < row.level:
                    r.grab_focus()
                    return True
                idx -= 1
        return False

    # ------------------------------------------------------------ selection

    def select_location(self, loc):
        self.current = loc
        found = False
        for row in self.rows:
            match = (not found and row.loc is not None and loc is not None and row.get_visible()
                     and loc_equal(row.loc, loc))
            if match:
                row.add_css_class("current")
                found = True
            else:
                row.remove_css_class("current")

    # ------------------------------------------------------------ events

    def _on_row_activated(self, listbox, row):
        if not isinstance(row, NavRow):
            return
        if row.loc is None and row.volume is not None:
            self.window.mount_volume(row.volume, lambda root: self.emit("navigate", root, False))
            return
        self.emit("navigate", row.loc, False)

    def _on_click(self, gesture, n, x, y):
        row = self.listbox.get_row_at_y(int(y))
        if not isinstance(row, NavRow) or row.loc is None:
            return
        button = gesture.get_current_button()
        if button == Gdk.BUTTON_MIDDLE:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.emit("navigate", row.loc, True)
        elif button == Gdk.BUTTON_SECONDARY:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._show_menu(row, x, y)

    def _show_menu(self, row, x, y):
        key = GLib.Variant("s", loc_key(row.loc))
        menu = Gio.Menu()
        top = Gio.Menu()

        def add(section, label, action):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(action, key)
            section.append_item(item)

        add(top, "Open in new tab", "win.open-new-tab")
        add(top, "Open in new window", "win.open-new-window")
        menu.append_section(None, top)
        mid = Gio.Menu()
        if isinstance(row.loc, Gio.File) and row.loc.get_path():
            add(mid, "Unpin from Quick access" if row.pinned else "Pin to Quick access",
                "win.toggle-pin")
            add(mid, "Open in Terminal", "win.terminal-at")
        if row.mount is not None:
            add(mid, "Eject", "win.eject-at")
        if row.loc.get_uri().startswith("trash:") if isinstance(row.loc, Gio.File) else False:
            mid.append("Empty Recycle Bin", "win.empty-trash")
        if isinstance(row.loc, Gio.File) and row.loc.get_uri_scheme() == "network":
            mid.append("Connect to server…", "win.connect-server")
        menu.append_section(None, mid)
        if isinstance(row.loc, Gio.File):
            bottom = Gio.Menu()
            add(bottom, "Properties", "win.properties-at")
            menu.append_section(None, bottom)
        pop = Gtk.PopoverMenu.new_from_model(menu)
        # Parent the menu to the pane, not the list: unpinning rebuilds the list with
        # remove_all(), which can't remove a popover and would loop forever.
        pop.set_parent(self)
        pop.set_has_arrow(False)
        pop.set_halign(Gtk.Align.START)
        point = translate_point(self.listbox, self, x, y)
        if point:
            x, y = point
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _drop_action(self, target, files, dest):
        if dest.get_uri().startswith("trash:"):
            return Gdk.DragAction.MOVE
        for f in files:
            if f.equal(dest) or dest.has_prefix(f):
                return 0
        state = target.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            return Gdk.DragAction.COPY
        if state & Gdk.ModifierType.SHIFT_MASK:
            return Gdk.DragAction.MOVE
        return Gdk.DragAction.MOVE if _same_device(files[0], dest) else Gdk.DragAction.COPY

    def _on_drop_motion(self, target, x, y, row):
        value = target.get_value()
        if value is None:
            return Gdk.DragAction.COPY
        return self._drop_action(target, value.get_files(), row.loc)

    def _on_drop(self, target, value, x, y, row):
        files = value.get_files()
        action = self._drop_action(target, files, row.loc)
        if not action:
            return False
        self.emit("files-dropped", files, row.loc, int(action))
        return True
