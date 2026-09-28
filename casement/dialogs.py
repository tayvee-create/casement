"""Dialogs: rename, conflicts, delete confirmation, properties."""
import os
import stat
import threading

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .fileops import split_ext
from .items import (ATTRS, format_bytes_exact, format_date_long, type_description,
                    folder_icon_for)

INVALID_CHARS = "/\0"


def ask_rename(parent, item_name, is_dir, callback, title="Rename"):
    """callback(new_name) if confirmed and changed."""
    dialog = Adw.AlertDialog(heading=title)
    entry = Gtk.Entry(text=item_name, activates_default=True)
    error = Gtk.Label(xalign=0, wrap=True, visible=False)
    error.add_css_class("error")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.append(entry)
    box.append(error)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("rename", "Rename")
    dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("rename")
    dialog.set_close_response("cancel")

    def validate(*_):
        name = entry.get_text()
        msg = None
        if any(c in name for c in INVALID_CHARS):
            msg = "A file name can't contain the character /"
        elif name in (".", ".."):
            msg = f'"{name}" is not a valid name.'
        elif name.strip() == "":
            msg = None
        error.set_label(msg or "")
        error.set_visible(bool(msg))
        dialog.set_response_enabled("rename", not msg and name.strip() != "")
    entry.connect("changed", validate)

    def on_response(d, response):
        name = entry.get_text().strip()
        if response == "rename" and name and name != item_name:
            callback(name)
    dialog.connect("response", on_response)
    dialog.present(parent)
    # Explorer selects the name without the extension.
    stem, _ = split_ext(item_name, is_dir)
    GLib.idle_add(lambda: (entry.grab_focus(), entry.select_region(0, len(stem))) and False)


def ask_conflict(parent, n_conflicts, is_move, callback):
    """callback('replace' | 'skip' | 'keep' | None)."""
    dialog = Adw.AlertDialog(
        heading="Replace or Skip Files",
        body=(f"The destination already has {'a file' if n_conflicts == 1 else f'{n_conflicts} files'}"
              " with the same name."))
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("skip", "Skip")
    dialog.add_response("keep", "Keep both")
    dialog.add_response("replace", "Replace")
    dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("skip")
    dialog.set_close_response("cancel")
    dialog.set_prefer_wide_layout(True)
    dialog.connect("response", lambda d, r: callback(None if r == "cancel" else r))
    dialog.present(parent)


def confirm(parent, heading, body, action_label, callback, destructive=True):
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("no", "No")
    dialog.add_response("yes", action_label)
    dialog.set_response_appearance(
        "yes", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("yes")
    dialog.set_close_response("no")
    dialog.connect("response", lambda d, r: r == "yes" and callback())
    dialog.present(parent)


def show_error(parent, heading, body):
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("ok", "OK")
    dialog.present(parent)


# ---------------------------------------------------------------- properties

class PropertiesDialog(Adw.Dialog):
    __gtype_name__ = "W11PropertiesDialog"

    def __init__(self, window, files):
        super().__init__(content_width=440, content_height=560)
        self.window = window
        self.files = files
        self._stop = threading.Event()
        self.connect("closed", lambda *a: self._stop.set())
        single = len(files) == 1
        self.infos = []
        for f in files:
            try:
                self.infos.append(f.query_info(ATTRS + ",filesystem::free,standard::description",
                                               Gio.FileQueryInfoFlags.NONE, None))
            except GLib.Error:
                self.infos.append(None)
        name = files[0].get_basename() if single else f"{len(files)} items"
        self.set_title(f"{name} Properties")

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        toolbar.add_top_bar(header)

        stack.add_titled_with_icon(self._general_page(), "general", "General",
                                   "document-properties-symbolic")
        if single and self.infos[0] is not None and self.infos[0].has_attribute("unix::mode"):
            stack.add_titled_with_icon(self._security_page(), "security", "Security",
                                       "system-lock-screen-symbolic")

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8, margin_bottom=12,
                          margin_end=12)
        ok = Gtk.Button(label="OK")
        ok.add_css_class("suggested-action")
        cancel = Gtk.Button(label="Cancel")
        self.apply_btn = Gtk.Button(label="Apply", sensitive=False)
        ok.connect("clicked", lambda b: (self._apply(), self.close()))
        cancel.connect("clicked", lambda b: self.close())
        self.apply_btn.connect("clicked", lambda b: self._apply())
        for b in (ok, cancel, self.apply_btn):
            b.set_size_request(80, -1)
            buttons.append(b)
        toolbar.set_content(stack)
        toolbar.add_bottom_bar(buttons)
        self.set_child(toolbar)

    def _row(self, grid, r, key, value, selectable=True):
        k = Gtk.Label(label=key, xalign=0, valign=Gtk.Align.START)
        if isinstance(value, Gtk.Widget):
            v = value
        else:
            v = Gtk.Label(label=value, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                          selectable=selectable, hexpand=True)
        grid.attach(k, 0, r, 1, 1)
        grid.attach(v, 1, r, 1, 1)
        return v

    def _sep(self, grid, r):
        grid.attach(Gtk.Separator(margin_top=4, margin_bottom=4), 0, r, 2, 1)

    def _general_page(self):
        grid = Gtk.Grid(column_spacing=16, row_spacing=8, margin_start=18, margin_end=18,
                        margin_top=12, margin_bottom=12)
        files, infos = self.files, self.infos
        single = len(files) == 1
        info = infos[0]
        r = 0
        top = Gtk.Box(spacing=12)
        if single and info is not None:
            is_dir = info.get_file_type() == Gio.FileType.DIRECTORY
            icon = folder_icon_for(files[0]) if is_dir else (
                info.get_attribute_object("standard::icon") or Gio.ThemedIcon.new("text-x-generic"))
            top.append(Gtk.Image(gicon=icon, pixel_size=40))
            self.name_entry = Gtk.Entry(text=info.get_display_name(), hexpand=True)
            self.name_entry.connect("changed", lambda *a: self.apply_btn.set_sensitive(True))
            top.append(self.name_entry)
        else:
            self.name_entry = None
            top.append(Gtk.Image(icon_name="edit-copy-symbolic", pixel_size=40))
            nfiles = sum(1 for i in infos if i and i.get_file_type() != Gio.FileType.DIRECTORY)
            ndirs = len(files) - nfiles
            top.append(Gtk.Label(label=f"{nfiles:,} Files, {ndirs:,} Folders", xalign=0))
        grid.attach(top, 0, r, 2, 1)
        r += 1
        self._sep(grid, r)
        r += 1
        if single and info is not None:
            is_dir = info.get_file_type() == Gio.FileType.DIRECTORY
            self._row(grid, r, "Type of file:" if not is_dir else "Type:", type_description(info)
                      + ("" if is_dir else f" ({os.path.splitext(info.get_name())[1] or 'no extension'})"))
            r += 1
            if not is_dir:
                ct = info.get_attribute_string("standard::content-type")
                app = Gio.AppInfo.get_default_for_type(ct, False) if ct else None
                if app:
                    opens = Gtk.Box(spacing=8)
                    if app.get_icon():
                        opens.append(Gtk.Image(gicon=app.get_icon(), pixel_size=16))
                    opens.append(Gtk.Label(label=app.get_display_name(), xalign=0, hexpand=True))
                    change = Gtk.Button(label="Change...")
                    change.connect("clicked", lambda b: self.window.open_with([files[0]]))
                    opens.append(change)
                    self._row(grid, r, "Opens with:", opens)
                    r += 1
            self._sep(grid, r)
            r += 1
        else:
            types = {type_description(i) for i in infos if i}
            self._row(grid, r, "Type:", f"All of type {types.pop()}" if len(types) == 1 else "Multiple types")
            r += 1
        parent = files[0].get_parent()
        if parent is not None:
            self._row(grid, r, "Location:", parent.get_path() or parent.get_uri())
            r += 1
        self.size_label = self._row(grid, r, "Size:", "Calculating...")
        r += 1
        self.disk_label = self._row(grid, r, "Size on disk:", "Calculating...")
        r += 1
        any_dir = any(i and i.get_file_type() == Gio.FileType.DIRECTORY for i in infos)
        self.contains_label = None
        if any_dir:
            self.contains_label = self._row(grid, r, "Contains:", "Calculating...")
            r += 1
        self._sep(grid, r)
        r += 1
        if single and info is not None:
            for key, getter in (("Created:", "get_creation_date_time"),
                                ("Modified:", "get_modification_date_time"),
                                ("Accessed:", "get_access_date_time")):
                fn = getattr(info, getter, None)
                dt = fn() if fn else None
                if dt is not None:
                    self._row(grid, r, key, format_date_long(dt))
                    r += 1
            self._sep(grid, r)
            r += 1
            attrs = Gtk.Box(spacing=16)
            can_write = info.get_attribute_boolean("access::can-write") if info.has_attribute(
                "access::can-write") else True
            self.readonly = Gtk.CheckButton(label="Read-only", active=not can_write)
            self.readonly.set_sensitive(files[0].get_path() is not None)
            self.readonly.connect("toggled", lambda *a: self.apply_btn.set_sensitive(True))
            hidden = Gtk.CheckButton(label="Hidden", active=info.get_name().startswith("."),
                                     sensitive=False,
                                     tooltip_text="On Linux, names starting with a dot are hidden")
            attrs.append(self.readonly)
            attrs.append(hidden)
            self._row(grid, r, "Attributes:", attrs)
            r += 1
        else:
            self.readonly = None
        self._start_measure()
        return grid

    def _security_page(self):
        info = self.infos[0]
        grid = Gtk.Grid(column_spacing=16, row_spacing=8, margin_start=18, margin_end=18,
                        margin_top=12, margin_bottom=12)
        self._row(grid, 0, "Object name:", self.files[0].get_path() or self.files[0].get_uri())
        self._row(grid, 1, "Owner:", info.get_attribute_string("owner::user") or "")
        self._row(grid, 2, "Group:", info.get_attribute_string("owner::group") or "")
        mode = info.get_attribute_uint32("unix::mode")
        self._row(grid, 3, "Permissions:", stat.filemode(mode) + f"  ({oct(mode & 0o7777)[2:]})")
        table = Gtk.Grid(column_spacing=18, row_spacing=6, margin_top=12)
        heads = ["", "Read", "Write", "Execute"]
        for c, h in enumerate(heads):
            lbl = Gtk.Label(label=h, xalign=0)
            lbl.add_css_class("heading")
            table.attach(lbl, c, 0, 1, 1)
        self.perm_checks = {}
        is_owner = os.geteuid() == info.get_attribute_uint32("unix::uid")
        for r, (who, shift) in enumerate((("Owner", 6), ("Group", 3), ("Others", 0)), start=1):
            table.attach(Gtk.Label(label=who, xalign=0), 0, r, 1, 1)
            for c, bit in enumerate((4, 2, 1), start=1):
                cb = Gtk.CheckButton(active=bool(mode & (bit << shift)), sensitive=is_owner)
                cb.connect("toggled", lambda *a: self.apply_btn.set_sensitive(True))
                self.perm_checks[(shift, bit)] = cb
                table.attach(cb, c, r, 1, 1)
        grid.attach(table, 0, 4, 2, 1)
        self._orig_mode = mode
        return grid

    def _start_measure(self):
        stop = self._stop
        paths = [f.get_path() for f in self.files]

        def work():
            total = disk = nfiles = ndirs = 0
            last = 0
            for p in paths:
                if p is None:
                    continue
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    for root, dirs, names in os.walk(p):
                        if stop.is_set():
                            return
                        ndirs += len(dirs)
                        for n in names:
                            try:
                                s = os.lstat(os.path.join(root, n))
                            except OSError:
                                continue
                            nfiles += 1
                            total += s.st_size
                            disk += s.st_blocks * 512
                        last += 1
                        if last % 200 == 0:
                            GLib.idle_add(self._set_sizes, total, disk, nfiles, ndirs, False)
                else:
                    nfiles += 1
                    total += st.st_size
                    disk += st.st_blocks * 512
            GLib.idle_add(self._set_sizes, total, disk, nfiles, ndirs, True)
        threading.Thread(target=work, daemon=True).start()

    def _set_sizes(self, total, disk, nfiles, ndirs, done):
        self.size_label.set_label(format_bytes_exact(total))
        self.disk_label.set_label(format_bytes_exact(disk))
        if self.contains_label is not None:
            self.contains_label.set_label(f"{nfiles:,} Files, {ndirs:,} Folders")
        return False

    def _apply(self):
        if not self.apply_btn.get_sensitive():
            return
        self.apply_btn.set_sensitive(False)
        f = self.files[0]
        path = f.get_path()
        if self.readonly is not None and path:
            try:
                mode = os.stat(path).st_mode
                new = mode & ~0o222 if self.readonly.get_active() else mode | stat.S_IWUSR
                if hasattr(self, "perm_checks"):
                    new = self._mode_from_checks(new)
                if new != mode:
                    os.chmod(path, stat.S_IMODE(new))
            except OSError as e:
                show_error(self, "Couldn't change attributes", str(e))
        elif hasattr(self, "perm_checks") and path:
            try:
                os.chmod(path, stat.S_IMODE(self._mode_from_checks(os.stat(path).st_mode)))
            except OSError as e:
                show_error(self, "Couldn't change permissions", str(e))
        if self.name_entry is not None:
            new_name = self.name_entry.get_text().strip()
            if new_name and new_name != self.infos[0].get_display_name():
                self.window.rename_file(f, new_name)

    def _mode_from_checks(self, mode):
        if self.readonly is not None and self.readonly.get_active() != (not (self._orig_mode & 0o200)):
            # The Read-only box wins over the permission grid for owner-write.
            self.perm_checks[(6, 2)].set_active(not self.readonly.get_active())
        for (shift, bit), cb in self.perm_checks.items():
            if cb.get_active():
                mode |= bit << shift
            else:
                mode &= ~(bit << shift)
        return mode


# ---------------------------------------------------------------- connect to server

SERVER_SCHEMES = ("smb", "sftp", "ssh", "ftp", "ftps", "dav", "davs", "afp", "nfs")


def normalise_server_address(text):
    """Accept 'smb://host/share', Windows '\\\\host\\share' or a bare 'host/share'."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("\\\\"):
        return "smb://" + text[2:].replace("\\", "/")
    if "://" not in text:
        return "smb://" + text.lstrip("/")
    scheme = text.split("://", 1)[0].lower()
    if scheme == "ssh":
        text = "sftp" + text[3:]
    return text


class ConnectServerDialog(Adw.Dialog):
    """'Connect to Server', with the list of recently used servers."""
    __gtype_name__ = "W11ConnectServerDialog"

    def __init__(self, window, on_connect):
        super().__init__(title="Connect to Server", content_width=460)
        self.window = window
        self.on_connect = on_connect
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=18,
                      margin_end=18, margin_top=6, margin_bottom=18)
        intro = Gtk.Label(label="Type the address of a shared folder or server.", xalign=0, wrap=True)
        box.append(intro)
        self.entry = Gtk.Entry(placeholder_text="smb://server/share", activates_default=True,
                               hexpand=True)
        self.entry.connect("changed", lambda e: self.connect_btn.set_sensitive(bool(e.get_text().strip())))
        self.entry.connect("activate", lambda e: self._connect())
        box.append(self.entry)
        examples = Gtk.Label(xalign=0, wrap=True)
        examples.set_markup(
            "<small>Examples: <tt>smb://nas/photos</tt>, <tt>\\\\PC-NAME\\Share</tt>, "
            "<tt>sftp://user@host</tt>, <tt>ftp://ftp.example.com</tt>, <tt>davs://host/dav</tt></small>")
        examples.add_css_class("w11-dim")
        box.append(examples)

        self.recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6)
        box.append(self.recent_box)
        self._fill_recent()

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda b: self.close())
        self.connect_btn = Gtk.Button(label="Connect", sensitive=False)
        self.connect_btn.add_css_class("suggested-action")
        self.connect_btn.connect("clicked", lambda b: self._connect())
        buttons.append(cancel)
        buttons.append(self.connect_btn)
        box.append(buttons)
        toolbar.set_content(box)
        self.set_child(toolbar)
        self.set_default_widget(self.connect_btn)
        self.set_focus(self.entry)

    def _fill_recent(self):
        child = self.recent_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.recent_box.remove(child)
            child = nxt
        servers = self.window.settings.get("servers", [])
        if not servers:
            return
        heading = Gtk.Label(label="Recent servers", xalign=0)
        heading.add_css_class("heading")
        self.recent_box.append(heading)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        for addr in servers:
            row = Adw.ActionRow(title=addr, activatable=True)
            row.add_prefix(Gtk.Image(icon_name="w11-network", pixel_size=16))
            remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove from list",
                                valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda b, a=addr: self._remove(a))
            row.add_suffix(remove)
            row.connect("activated", lambda r, a=addr: (self.entry.set_text(a), self._connect()))
            lb.append(row)
        self.recent_box.append(lb)

    def _remove(self, addr):
        servers = [s for s in self.window.settings.get("servers", []) if s != addr]
        self.window.settings["servers"] = servers
        self._fill_recent()

    def _connect(self):
        addr = normalise_server_address(self.entry.get_text())
        if not addr:
            return
        self.close()
        self.on_connect(addr)
