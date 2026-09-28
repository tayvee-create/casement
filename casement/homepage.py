"""The 'Home' and 'This PC' pages."""
import os

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from .items import folder_icon_for, format_date, format_size, loc_key, special_dirs


def _popup_menu(parent, menu, x, y):
    pop = Gtk.PopoverMenu.new_from_model(menu)
    pop.set_parent(parent)
    pop.set_has_arrow(False)
    pop.set_halign(Gtk.Align.START)
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    pop.set_pointing_to(rect)
    pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
    pop.popup()


def _menu_item(label, action, value):
    item = Gio.MenuItem.new(label, None)
    item.set_action_and_target_value(action, GLib.Variant("s", value))
    return item


class Section(Gtk.Box):
    """Collapsible section with an Explorer-style chevron header."""

    def __init__(self, title, expanded=True, on_toggle=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("w11-section")
        self.header = Gtk.Button(halign=Gtk.Align.START)
        self.header.add_css_class("flat")
        self.header.add_css_class("w11-section-header")
        hb = Gtk.Box(spacing=6)
        self.arrow = Gtk.Image(icon_name="pan-down-symbolic")
        hb.append(self.arrow)
        hb.append(Gtk.Label(label=title))
        self.header.set_child(hb)
        self.revealer = Gtk.Revealer(reveal_child=expanded,
                                     transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.append(self.header)
        self.append(self.revealer)
        self._on_toggle = on_toggle
        self.header.connect("clicked", self._toggle)
        self._sync()

    def set_content(self, widget):
        self.revealer.set_child(widget)

    def _toggle(self, *a):
        self.revealer.set_reveal_child(not self.revealer.get_reveal_child())
        self._sync()
        if self._on_toggle:
            self._on_toggle(self.revealer.get_reveal_child())

    def _sync(self):
        self.arrow.set_from_icon_name("pan-down-symbolic" if self.revealer.get_reveal_child()
                                      else "pan-end-symbolic")


class Tile(Gtk.FlowBoxChild):
    __gtype_name__ = "W11Tile"

    def __init__(self, loc, title, subtitle, gicon, pinned=False, extra=None):
        super().__init__()
        self.loc = loc
        self.add_css_class("w11-tile")
        box = Gtk.Box(spacing=12)
        img = Gtk.Image(gicon=gicon, pixel_size=48)
        box.append(img)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True,
                      spacing=2)
        name = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        col.append(name)
        if extra is not None:
            col.append(extra)
        if subtitle:
            sub = Gtk.Box(spacing=4)
            if pinned:
                pin = Gtk.Image(icon_name="view-pin-symbolic", pixel_size=12)
                pin.add_css_class("w11-dim")
                sub.append(pin)
            self.sub_label = Gtk.Label(label=subtitle, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            self.sub_label.add_css_class("w11-dim")
            sub.append(self.sub_label)
            col.append(sub)
        box.append(col)
        self.set_child(box)
        self.set_size_request(250, -1)


def _flowbox():
    fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=False,
                     min_children_per_line=1, max_children_per_line=12, homogeneous=True,
                     column_spacing=4, row_spacing=4, valign=Gtk.Align.START)
    fb.add_css_class("w11-tiles")
    return fb


class _Page(Gtk.ScrolledWindow):
    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
        "open-file": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, window):
        super().__init__(vexpand=True, hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.window = window
        self.add_css_class("w11-page")
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=16,
                           margin_end=16, margin_top=8, margin_bottom=16)
        self.set_child(self.box)

    def _clear(self):
        child = self.box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.box.remove(child)
            child = nxt

    def _wire_flowbox(self, fb):
        fb.connect("child-activated", self._on_tile_activated)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_tile_click, fb)
        fb.add_controller(click)

    def _on_tile_activated(self, fb, child):
        if child.loc is not None:
            self.emit("navigate", child.loc, False)

    def _on_tile_click(self, gesture, n, x, y, fb):
        child = fb.get_child_at_pos(int(x), int(y))
        button = gesture.get_current_button()
        if child is None:
            fb.unselect_all()
            return
        if button == Gdk.BUTTON_MIDDLE and child.loc is not None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.emit("navigate", child.loc, True)
        elif button == Gdk.BUTTON_SECONDARY and child.loc is not None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            fb.select_child(child)
            self._tile_menu(fb, child, x, y)

    def _tile_menu(self, fb, child, x, y):
        key = loc_key(child.loc)
        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append_item(_menu_item("Open", "win.open-location", key))
        s1.append_item(_menu_item("Open in new tab", "win.open-new-tab", key))
        s1.append_item(_menu_item("Open in new window", "win.open-new-window", key))
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        if isinstance(child.loc, Gio.File) and child.loc.get_path():
            pinned = child.loc.get_path() in self.window.settings["pins"]
            s2.append_item(_menu_item("Unpin from Quick access" if pinned else "Pin to Quick access",
                                      "win.toggle-pin", key))
            s2.append_item(_menu_item("Open in Terminal", "win.terminal-at", key))
        menu.append_section(None, s2)
        if isinstance(child.loc, Gio.File):
            s3 = Gio.Menu()
            s3.append_item(_menu_item("Properties", "win.properties-at", key))
            menu.append_section(None, s3)
        _popup_menu(fb, menu, x, y)


class HomePage(_Page):
    __gtype_name__ = "W11HomePage"

    def __init__(self, window):
        super().__init__(window)
        self.recent = Gtk.RecentManager.get_default()
        self.recent.connect("changed", lambda *a: self.refresh() if self.get_mapped() else None)
        self.connect("map", lambda *a: self.refresh())

    def refresh(self):
        self._clear()
        sections = self.window.settings["home_sections"]

        def toggler(key):
            def cb(expanded):
                sections[key] = expanded
                self.window.settings["home_sections"] = sections
            return cb

        quick = Section("Quick access", sections.get("quick", True), toggler("quick"))
        fb = _flowbox()
        self._wire_flowbox(fb)
        for path in self.window.settings["pins"]:
            f = Gio.File.new_for_path(path)
            title = os.path.basename(path.rstrip("/")) or path
            sub = "Stored locally" if os.path.isdir(path) else "Not available"
            fb.append(Tile(f, title, sub, folder_icon_for(f), pinned=True))
        quick.set_content(fb)
        self.box.append(quick)

        recent = Section("Recent", sections.get("recent", True), toggler("recent"))
        recent.set_content(self._recent_list())
        self.box.append(recent)

    def _recent_list(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = self._recent_row("Name", "Date accessed", "File location", header=True)
        box.append(header)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=False)
        lb.add_css_class("w11-recent")
        items = [i for i in self.recent.get_items() if i.exists()]
        items.sort(key=lambda i: i.get_visited().to_unix() if i.get_visited() else 0, reverse=True)
        for info in items[:30]:
            f = Gio.File.new_for_uri(info.get_uri())
            parent = f.get_parent()
            loc_text = ""
            if parent is not None:
                loc_text = parent.get_path() or parent.get_uri()
                home = GLib.get_home_dir()
                if loc_text.startswith(home):
                    loc_text = GLib.get_user_name() + loc_text[len(home):]
            row = self._recent_row(info.get_display_name(), format_date(info.get_visited()), loc_text,
                                   gicon=info.get_gicon())
            lbrow = Gtk.ListBoxRow()
            lbrow.set_child(row)
            lbrow.uri = info.get_uri()
            lb.append(lbrow)
        if not items:
            empty = Gtk.Label(label="After you've opened some files, we'll show the most recent ones here.",
                              margin_top=12, wrap=True)
            empty.add_css_class("w11-dim")
            box.append(empty)
        lb.connect("row-activated", lambda l, r: self.emit("open-file", Gio.File.new_for_uri(r.uri)))
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", self._on_recent_click, lb)
        lb.add_controller(click)
        box.append(lb)
        return box

    def _recent_row(self, name, date, location, header=False, gicon=None):
        row = Gtk.Box(spacing=12, margin_start=8, margin_end=8)
        if header:
            row.add_css_class("w11-recent-header")
        name_box = Gtk.Box(spacing=8, hexpand=True)
        if gicon is not None:
            name_box.append(Gtk.Image(gicon=gicon, pixel_size=16))
        elif header:
            spacer = Gtk.Box()
            spacer.set_size_request(16, -1)
            name_box.append(spacer)
        name_box.append(Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        name_box.set_size_request(260, -1)
        row.append(name_box)
        for text, width in ((date, 150), (location, 280)):
            lbl = Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            lbl.set_size_request(width, -1)
            if not header:
                lbl.add_css_class("w11-dim")
            row.append(lbl)
        return row

    def _on_recent_click(self, gesture, n, x, y, lb):
        row = lb.get_row_at_y(int(y))
        if row is None:
            return
        lb.select_row(row)
        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append_item(_menu_item("Open", "win.open-uri", row.uri))
        s1.append_item(_menu_item("Open file location", "win.open-file-location", row.uri))
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append_item(_menu_item("Remove from Recent", "win.remove-recent", row.uri))
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append_item(_menu_item("Properties", "win.properties-at", row.uri))
        menu.append_section(None, s3)
        _popup_menu(lb, menu, x, y)


class ThisPCPage(_Page):
    __gtype_name__ = "W11ThisPCPage"

    def __init__(self, window):
        super().__init__(window)
        self.connect("map", lambda *a: self.refresh())
        self._cancellable = None

    def refresh(self):
        if self._cancellable:
            self._cancellable.cancel()
        self._cancellable = Gio.Cancellable()
        self._clear()
        folders = Section("Folders")
        fb = _flowbox()
        self._wire_flowbox(fb)
        home = Gio.File.new_for_path(GLib.get_home_dir())
        fb.append(Tile(home, GLib.get_user_name(), "System folder", Gio.ThemedIcon.new("w11-folder")))
        for path in sorted(special_dirs(), key=lambda p: os.path.basename(p).casefold()):
            if os.path.isdir(path):
                f = Gio.File.new_for_path(path)
                fb.append(Tile(f, os.path.basename(path), "System folder", folder_icon_for(f)))
        folders.set_content(fb)
        self.box.append(folders)

        drives = Section("Devices and drives")
        dfb = _flowbox()
        self._wire_flowbox(dfb)
        dfb.connect("child-activated", self._on_drive_activated)
        for title, f, icon, mount, vol in self.window.sidebar.drives():
            bar = Gtk.LevelBar(min_value=0, max_value=1, value=0, hexpand=True)
            bar.set_size_request(180, 12)
            bar.remove_offset_value(Gtk.LEVEL_BAR_OFFSET_LOW)
            bar.remove_offset_value(Gtk.LEVEL_BAR_OFFSET_HIGH)
            bar.remove_offset_value(Gtk.LEVEL_BAR_OFFSET_FULL)
            bar.add_css_class("w11-usage")
            tile = Tile(f, title, "Not mounted" if f is None else " ", Gio.ThemedIcon.new(icon),
                        extra=bar if f is not None else None)
            tile.volume = vol
            if f is not None:
                f.query_filesystem_info_async("filesystem::size,filesystem::free",
                                              GLib.PRIORITY_DEFAULT, self._cancellable,
                                              self._on_fs_info, (tile, bar))
            dfb.append(tile)
        drives.set_content(dfb)
        self.box.append(drives)

        network = Section("Network locations")
        nfb = _flowbox()
        self._wire_flowbox(nfb)
        for addr in self.window.settings.get("servers", []):
            f = Gio.File.new_for_uri(addr)
            scheme, _, rest = addr.partition("://")
            title = rest.rstrip("/").split("@")[-1] or addr
            nfb.append(Tile(f, title, scheme.upper(), Gio.ThemedIcon.new("w11-network")))
        add = Tile(None, "Connect to server", "SMB, SFTP, FTP, WebDAV",
                   Gio.ThemedIcon.new("list-add-symbolic"))
        add.is_connect_tile = True
        nfb.append(add)
        nfb.connect("child-activated", lambda fb, child: self.window.show_connect_server()
                    if getattr(child, "is_connect_tile", False) else None)
        network.set_content(nfb)
        self.box.append(network)

    def _on_fs_info(self, f, res, data):
        tile, bar = data
        try:
            info = f.query_filesystem_info_finish(res)
        except GLib.Error:
            return
        size = info.get_attribute_uint64("filesystem::size")
        free = info.get_attribute_uint64("filesystem::free")
        if size:
            used = (size - free) / size
            bar.set_value(used)
            if used > 0.9:
                bar.add_css_class("w11-usage-full")
            tile.sub_label.set_label(f"{format_size(free)} free of {format_size(size)}")
        else:
            bar.set_visible(False)
            tile.sub_label.set_label("")

    def _on_drive_activated(self, fb, child):
        if child.loc is None and getattr(child, "volume", None) is not None:
            self.window.mount_volume(child.volume, lambda root: self.emit("navigate", root, False))
