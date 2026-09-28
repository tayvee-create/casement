"""The file list: Details (column view) and icon/tile views (grid view)."""
import os
import threading
import time

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from .items import (ATTRS, FileItem, folder_sizer, format_date, format_size, format_size_kb,
                    is_trash, thumbnailer, translate_point)

SEARCH_DELAY_MS = 350
SEARCH_MAX_RESULTS = 10000

VIEW_MODES = {
    "xlarge": {"label": "Extra large icons", "kind": "grid", "icon": 192, "width": 212},
    "large": {"label": "Large icons", "kind": "grid", "icon": 96, "width": 124},
    "medium": {"label": "Medium icons", "kind": "grid", "icon": 48, "width": 96},
    "small": {"label": "Small icons", "kind": "row", "icon": 16, "width": 220},
    "details": {"label": "Details", "kind": "details", "icon": 16},
    "tiles": {"label": "Tiles", "kind": "tile", "icon": 48, "width": 290},
}
VIEW_ORDER = ["xlarge", "large", "medium", "small", "details", "tiles"]
ZOOM_ORDER = ["details", "small", "tiles", "medium", "large", "xlarge"]

SORT_KEYS = {"name": "Name", "modified": "Date modified", "type": "Type", "size": "Size"}


def _cmp(a, b):
    return (a > b) - (a < b)


class ItemBox(Gtk.Box):
    """Row/tile widget; knows which item and position it shows."""
    __gtype_name__ = "W11ItemBox"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.item = None
        self.pos = -1
        self.binder = None
        self.is_name = False  # shows the item name (can be renamed in place)


class FolderView(Gtk.Box):
    __gtype_name__ = "W11FolderView"
    __gsignals__ = {
        # item, open_in_new_tab
        "item-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "contents-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # widget, x, y, on_item
        "context-menu": (GObject.SignalFlags.RUN_FIRST, None, (object, float, float, bool)),
        # list of Gio.File, destination Gio.File, Gdk.DragAction
        "files-dropped": (GObject.SignalFlags.RUN_FIRST, None, (object, object, int)),
        "zoom": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "loaded": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # emitted when entering or leaving search results
        "search-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # item, new name (from renaming in place)
        "rename-requested": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
    }

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.add_css_class("w11-folderview")
        self.location = None
        self.location_info = None
        self.cancellable = None
        self.monitor = None
        self.loading = False
        self.error = None
        self.search_text = ""
        self.show_hidden = False
        self.view_mode = "details"
        self.pending_select = set()
        self._bound = []
        self._type_buf = ""
        self._type_timeout = 0
        self._drop_hl = None
        self.search_mode = False
        self._search_timeout = 0
        self._search_cancel = None
        self._size_cancel = threading.Event()
        self._size_resort_id = 0
        self._editing = None  # (box, entry, item, focus controller) while renaming
        self._pending_rename = None
        self._slow_click_id = 0

        self.store = Gio.ListStore(item_type=FileItem)
        self.filter = Gtk.CustomFilter.new(self._filter_func)
        self.filter_model = Gtk.FilterListModel(model=self.store, filter=self.filter)

        self._build_columnview()
        self.dirs_sorter = Gtk.CustomSorter.new(self._dirs_first)
        self.sorter = Gtk.MultiSorter()
        self.sorter.append(self.dirs_sorter)
        self.sorter.append(self.cv.get_sorter())
        self.sort_model = Gtk.SortListModel(model=self.filter_model, sorter=self.sorter)
        self.selection = Gtk.MultiSelection(model=self.sort_model)
        self.cv.set_model(self.selection)
        self.cv.sort_by_column(self.col_name, Gtk.SortType.ASCENDING)
        self.cv.get_sorter().connect("changed", self._on_sort_changed)
        self.selection.connect("selection-changed", lambda *a: self.emit("selection-changed"))
        self.sort_model.connect("items-changed", self._on_items_changed)

        self.gv = Gtk.GridView(model=self.selection, max_columns=200, min_columns=1)
        self.gv.set_enable_rubberband(True)
        self.gv.add_css_class("w11-grid")
        self.gv.connect("activate", self._on_activate)

        self.cv_scroll = Gtk.ScrolledWindow(child=self.cv, vexpand=True, hexpand=True)
        self.gv_scroll = Gtk.ScrolledWindow(child=self.gv, vexpand=True, hexpand=True,
                                            hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.stack.add_named(self.cv_scroll, "details")
        self.stack.add_named(self.gv_scroll, "grid")

        self.placeholder = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                                     margin_top=36, wrap=True, visible=False,
                                     can_target=False)
        self.placeholder.add_css_class("w11-placeholder")
        overlay = Gtk.Overlay(child=self.stack)
        overlay.add_overlay(self.placeholder)
        self.append(overlay)

        for view in (self.cv, self.gv):
            click = Gtk.GestureClick(button=0)
            click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            click.connect("pressed", self._on_click, view)
            view.add_controller(click)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drop.set_preload(True)
        drop.connect("enter", self._on_drop_motion)
        drop.connect("motion", self._on_drop_motion)
        drop.connect("leave", lambda *a: self._set_drop_highlight(None))
        drop.connect("drop", self._on_drop)
        self.stack.add_controller(drop)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        self.set_view_mode("details")

    # ------------------------------------------------------------ building

    def _build_columnview(self):
        self.cv = Gtk.ColumnView(show_row_separators=False, show_column_separators=False,
                                 reorderable=False, hexpand=True)
        self.cv.set_enable_rubberband(True)
        self.cv.add_css_class("w11-details")
        self.cv.connect("activate", self._on_activate)

        name_sorter = Gtk.CustomSorter.new(lambda a, b, *_: _cmp(a.collate_key, b.collate_key))
        date_sorter = Gtk.CustomSorter.new(lambda a, b, *_: _cmp(a.mtime, b.mtime))
        type_sorter = Gtk.CustomSorter.new(lambda a, b, *_: _cmp(a.type_desc, b.type_desc))
        size_sorter = Gtk.CustomSorter.new(lambda a, b, *_: _cmp(a.sort_size, b.sort_size))
        orig_sorter = Gtk.CustomSorter.new(
            lambda a, b, *_: _cmp(a.attr_str("trash::orig-path") or "", b.attr_str("trash::orig-path") or ""))
        del_sorter = Gtk.CustomSorter.new(
            lambda a, b, *_: _cmp(a.attr_str("trash::deletion-date") or "",
                                  b.attr_str("trash::deletion-date") or ""))

        self.col_name = self._add_column("Name", self._bind_name, name_sorter, 340, expand=True,
                                         with_icon=True)
        folder_sorter = Gtk.CustomSorter.new(
            lambda a, b, *_: _cmp(self._folder_text(a).casefold(), self._folder_text(b).casefold()))
        self.col_folder = self._add_column("Folder", self._bind_folder, folder_sorter, 260)
        self.col_orig = self._add_column("Original location", self._bind_orig, orig_sorter, 220)
        self.col_deleted = self._add_column("Date deleted", self._bind_deleted, del_sorter, 150)
        self.col_date = self._add_column("Date modified", self._bind_date, date_sorter, 150)
        self.col_type = self._add_column("Type", self._bind_type, type_sorter, 150)
        self.col_size = self._add_column("Size", self._bind_size, size_sorter, 100, right=True)
        self.col_orig.set_visible(False)
        self.col_deleted.set_visible(False)
        self.col_folder.set_visible(False)
        self.size_sorter = size_sorter
        self.sort_columns = {"name": self.col_name, "modified": self.col_date,
                             "type": self.col_type, "size": self.col_size}

    def _add_column(self, title, binder, sorter, width, expand=False, with_icon=False, right=False):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_cell, with_icon, right)
        factory.connect("bind", self._bind, binder)
        factory.connect("unbind", self._unbind)
        col = Gtk.ColumnViewColumn(title=title, factory=factory, resizable=True, expand=expand)
        col.set_fixed_width(width)
        col.set_sorter(sorter)
        self.cv.append_column(col)
        return col

    def _setup_cell(self, factory, list_item, with_icon, right):
        box = ItemBox(spacing=8)
        if with_icon:
            box.img = Gtk.Image(pixel_size=16)
            box.append(box.img)
        box.lbl = Gtk.Label(xalign=1 if right else 0, hexpand=True,
                            ellipsize=Pango.EllipsizeMode.END)
        if not with_icon:
            box.lbl.add_css_class("w11-dim")
        box.is_name = with_icon
        box.append(box.lbl)
        self._attach_drag(box)
        list_item.set_child(box)

    def _bind(self, factory, list_item, binder):
        box = list_item.get_child()
        box.item = list_item.get_item()
        box.pos = list_item.get_position()
        box.binder = binder
        self._bound.append(box)
        self._apply(box)

    def _unbind(self, factory, list_item):
        box = list_item.get_child()
        if self._editing is not None and self._editing[0] is box:
            self._finish_edit(commit=True, refocus=False)
        if box in self._bound:
            self._bound.remove(box)
        box.item = None

    def _apply(self, box):
        item = box.item
        if item is None:
            return
        box.binder(box, item)
        if item.file.get_uri() in self.window.cut_uris:
            box.add_css_class("w11-cut")
        else:
            box.remove_css_class("w11-cut")

    def rebind(self):
        for box in list(self._bound):
            self._apply(box)

    def _bind_name(self, box, item):
        box.img.set_from_gicon(item.gicon)
        box.lbl.set_label(item.label(self.window.show_extensions))

    def _bind_date(self, box, item):
        box.lbl.set_label(format_date(item.mtime_dt))

    def _bind_type(self, box, item):
        box.lbl.set_label(item.type_desc)

    def _bind_size(self, box, item):
        size = item.known_size
        box.lbl.set_label("" if size is None else format_size_kb(size))

    def _folder_text(self, item):
        parent = item.file.get_parent()
        if parent is None:
            return ""
        return parent.get_path() or parent.get_uri()

    def _bind_folder(self, box, item):
        # Trim long paths from the start so the nearest folders stay visible.
        box.lbl.set_ellipsize(Pango.EllipsizeMode.START)
        box.lbl.set_label(self._folder_text(item))
        box.set_tooltip_text(self._folder_text(item))

    def _bind_orig(self, box, item):
        p = item.attr_str("trash::orig-path") or ""
        box.lbl.set_label(os.path.dirname(p))

    def _bind_deleted(self, box, item):
        d = item.attr_str("trash::deletion-date") or ""
        try:
            dt = GLib.DateTime.new_from_iso8601(d, GLib.TimeZone.new_local())
            box.lbl.set_label(format_date(dt) if dt else d)
        except Exception:
            box.lbl.set_label(d)

    # grid (icons / tiles)

    def _grid_factory(self, spec):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_grid, spec)
        factory.connect("bind", self._bind, lambda box, item: self._bind_grid(box, item, spec))
        factory.connect("unbind", self._unbind)
        return factory

    def _setup_grid(self, factory, list_item, spec):
        kind, size = spec["kind"], spec["icon"]
        if kind == "grid":
            box = ItemBox(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_size_request(spec["width"], -1)
            holder = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
            holder.set_size_request(size, size)
        else:
            box = ItemBox(spacing=10 if kind == "tile" else 6)
            box.set_size_request(spec["width"], -1)
            holder = Gtk.Box(valign=Gtk.Align.CENTER)
        box.img = Gtk.Image(pixel_size=size, halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
                            hexpand=True, vexpand=True)
        box.pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, visible=False,
                              halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
        box.pic.set_size_request(size, size)
        box.pic.add_css_class("w11-thumb")
        holder.append(box.img)
        holder.append(box.pic)
        box.append(holder)
        if kind == "grid":
            box.lbl = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=2,
                                ellipsize=Pango.EllipsizeMode.END, justify=Gtk.Justification.CENTER,
                                max_width_chars=max(8, spec["width"] // 8))
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_insert_hyphens_new(False))
            box.lbl.set_attributes(attrs)
            box.append(box.lbl)
        elif kind == "row":
            box.lbl = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
            box.append(box.lbl)
        else:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
            box.lbl = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            box.sub1 = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            box.sub2 = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            box.sub1.add_css_class("w11-dim")
            box.sub2.add_css_class("w11-dim")
            col.append(box.lbl)
            col.append(box.sub1)
            col.append(box.sub2)
            box.append(col)
        box.is_name = True
        self._attach_drag(box)
        list_item.set_child(box)

    def _bind_grid(self, box, item, spec):
        box.lbl.set_label(item.label(self.window.show_extensions))
        size = item.known_size
        box.set_tooltip_text(f"{item.display_name}\nType: {item.type_desc}"
                             + ("" if size is None else f"\nSize: {format_size(size)}")
                             + f"\nDate modified: {format_date(item.mtime_dt)}")
        if spec["kind"] == "tile":
            box.sub1.set_label(item.type_desc)
            box.sub2.set_label("" if size is None else format_size(size))
        box.img.set_from_gicon(item.gicon)
        box.img.set_visible(True)
        box.pic.set_visible(False)
        if spec["icon"] >= 48 and thumbnailer.can_thumbnail(item):
            def done(tex, box=box, item=item):
                if box.item is item:
                    box.pic.set_paintable(tex)
                    box.pic.set_visible(True)
                    box.img.set_visible(False)
            thumbnailer.request(item, max(spec["icon"], 96), done)

    # ------------------------------------------------------------ view modes

    def set_view_mode(self, mode):
        if mode not in VIEW_MODES:
            mode = "details"
        self.view_mode = mode
        spec = VIEW_MODES[mode]
        if spec["kind"] == "details":
            self.stack.set_visible_child_name("details")
        else:
            self.gv.set_factory(self._grid_factory(spec))
            for k in ("grid", "row", "tile"):
                self.gv.remove_css_class(f"w11-{k}")
            self.gv.add_css_class(f"w11-{spec['kind']}")
            self.stack.set_visible_child_name("grid")

    @property
    def current_view(self):
        return self.cv if self.view_mode == "details" else self.gv

    def focus_view(self):
        self.current_view.grab_focus()

    # ------------------------------------------------------------ sorting

    def _dirs_first(self, a, b, *_):
        if a.is_dir == b.is_dir:
            return 0
        r = -1 if a.is_dir else 1
        sorter = self.cv.get_sorter()
        if sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING:
            r = -r
        return r

    def _on_sort_changed(self, *args):
        self.dirs_sorter.changed(Gtk.SorterChange.DIFFERENT)

    def get_sort(self):
        sorter = self.cv.get_sorter()
        col = sorter.get_primary_sort_column()
        key = next((k for k, c in self.sort_columns.items() if c == col), "name")
        return key, sorter.get_primary_sort_order()

    def sort_by(self, key=None, order=None):
        cur_key, cur_order = self.get_sort()
        key = key or cur_key
        order = cur_order if order is None else order
        self.cv.sort_by_column(self.sort_columns[key], order)

    # ------------------------------------------------------------ filtering

    def _filter_func(self, item, *_):
        if not self.show_hidden and item.is_hidden:
            return False
        if self.search_text and self.search_text not in item.display_name.casefold():
            return False
        return True

    def set_show_hidden(self, show):
        if show != self.show_hidden:
            self.show_hidden = show
            self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def set_search(self, text):
        """Filter the current folder at once, then search subfolders after a pause."""
        text = text.strip().casefold()
        if text == self.search_text:
            return
        self.search_text = text
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        if self._search_timeout:
            GLib.source_remove(self._search_timeout)
            self._search_timeout = 0
        if not text:
            if self.search_mode:
                self._exit_search()
        else:
            self._search_timeout = GLib.timeout_add(SEARCH_DELAY_MS, self._start_search)
        self._update_placeholder()

    def search_now(self):
        if self._search_timeout:
            GLib.source_remove(self._search_timeout)
            self._start_search()

    def _cancel_search(self):
        if self._search_cancel is not None:
            self._search_cancel.set()
            self._search_cancel = None

    def _start_search(self):
        self._search_timeout = 0
        if self.location is None or not self.search_text:
            return False
        self._cancel_search()
        self._size_cancel.set()
        self._size_cancel = threading.Event()
        cancel = threading.Event()
        self._search_cancel = cancel
        if not self.search_mode:
            self.search_mode = True
            self.col_folder.set_visible(True)
            self.emit("search-changed")
        self.error = None
        self.loading = True
        self.store.remove_all()
        self._update_placeholder()
        threading.Thread(target=self._search_worker,
                         args=(self.location, self.search_text, self.show_hidden, cancel),
                         daemon=True).start()
        return False

    def _search_worker(self, root, term, show_hidden, cancel):
        batch = []
        state = {"last": time.monotonic(), "count": 0}

        def found(f, info):
            batch.append((f, info))
            state["count"] += 1
            if time.monotonic() - state["last"] > 0.15:
                GLib.idle_add(self._add_search_results, list(batch), cancel)
                batch.clear()
                state["last"] = time.monotonic()
            return state["count"] < SEARCH_MAX_RESULTS

        try:
            path = root.get_path()
            if path:
                self._search_local(path, term, show_hidden, cancel, found)
            else:
                self._search_gio(root, term, show_hidden, cancel, found)
        finally:
            if batch:
                GLib.idle_add(self._add_search_results, list(batch), cancel)
            GLib.idle_add(self._search_done, cancel)

    def _search_local(self, path, term, show_hidden, cancel, found):
        try:
            dev = os.lstat(path).st_dev
        except OSError:
            return
        stack = [path]
        while stack and not cancel.is_set():
            try:
                with os.scandir(stack.pop()) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                if cancel.is_set():
                    return
                name = entry.name
                if not show_hidden and name.startswith("."):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if is_dir and entry.stat(follow_symlinks=False).st_dev == dev:
                        stack.append(entry.path)
                except OSError:
                    is_dir = False
                if term in name.casefold():
                    f = Gio.File.new_for_path(entry.path)
                    try:
                        info = f.query_info(ATTRS, Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
                    except GLib.Error:
                        continue
                    if not found(f, info):
                        return

    def _search_gio(self, root, term, show_hidden, cancel, found):
        stack = [root]
        while stack and not cancel.is_set():
            folder = stack.pop()
            try:
                enum = folder.enumerate_children(ATTRS, Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
            except GLib.Error:
                continue
            try:
                while not cancel.is_set():
                    info = enum.next_file(None)
                    if info is None:
                        break
                    name = info.get_name()
                    if not show_hidden and (name.startswith(".") or info.get_attribute_boolean("standard::is-hidden")):
                        continue
                    child = enum.get_child(info)
                    if info.get_file_type() == Gio.FileType.DIRECTORY:
                        stack.append(child)
                    if term in (info.get_display_name() or name).casefold():
                        if not found(child, info):
                            return
            except GLib.Error:
                pass
            finally:
                enum.close(None)

    def _add_search_results(self, results, cancel):
        if cancel.is_set():
            return False
        items = [FileItem(f, info) for f, info in results]
        self.store.splice(self.store.get_n_items(), 0, items)
        self._request_sizes(items)
        return False

    def _search_done(self, cancel):
        if cancel is self._search_cancel:
            self.loading = False
            self._update_placeholder()
            self.emit("loaded")
        return False

    def _exit_search(self):
        self._cancel_search()
        if self.location is not None:
            self.load(self.location)

    # ------------------------------------------------------------ folder sizes

    def _request_sizes(self, items):
        if not self.window.settings.get("folder_sizes", True):
            return
        cancel = self._size_cancel
        for item in items:
            # Only real local folders: sizing inside archives or network shares is slow.
            local = item.is_dir and item.file.get_uri_scheme() == "file"
            path = item.file.get_path() if local else None
            if path:
                folder_sizer.request(path, cancel, lambda size, item=item: self._got_size(item, size))

    def request_all_sizes(self):
        self._request_sizes([self.store.get_item(i) for i in range(self.store.get_n_items())])

    def clear_sizes(self):
        self._size_cancel.set()
        self._size_cancel = threading.Event()
        for i in range(self.store.get_n_items()):
            self.store.get_item(i).dir_size = None
        self.rebind()

    def _got_size(self, item, size):
        item.dir_size = size
        for box in self._bound:
            if box.item is item:
                self._apply(box)
        if self.selection.is_selected(self._position_of(item)):
            self.emit("selection-changed")
        if self.get_sort()[0] == "size" and not self._size_resort_id:
            self._size_resort_id = GLib.timeout_add(300, self._resort_sizes)

    def _resort_sizes(self):
        self._size_resort_id = 0
        self.size_sorter.changed(Gtk.SorterChange.DIFFERENT)
        return False

    def _position_of(self, item):
        for i in range(self.sort_model.get_n_items()):
            if self.sort_model.get_item(i) is item:
                return i
        return Gtk.INVALID_LIST_POSITION

    # ------------------------------------------------------------ loading

    def load(self, gfile):
        if self._editing is not None:
            self._finish_edit(commit=False, refocus=False)
        self._cancel_slow_click()
        self._cancel_search()
        if self._search_timeout:
            GLib.source_remove(self._search_timeout)
            self._search_timeout = 0
        self._size_cancel.set()
        self._size_cancel = threading.Event()
        was_search = self.search_mode
        self.search_mode = False
        self.col_folder.set_visible(False)
        if was_search:
            self.emit("search-changed")
        if self.cancellable:
            self.cancellable.cancel()
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None
        self.cancellable = Gio.Cancellable()
        self.location = gfile
        self.location_info = None
        self.error = None
        self.loading = True
        self.search_text = ""
        self.store.remove_all()
        trash = is_trash(gfile)
        self.col_orig.set_visible(trash)
        self.col_deleted.set_visible(trash)
        self._update_placeholder()
        gfile.query_info_async("standard::display-name,access::can-write,standard::type",
                               Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT,
                               self.cancellable, self._on_location_info, self.cancellable)
        gfile.enumerate_children_async(ATTRS, Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT,
                                       self.cancellable, self._on_enumerate, self.cancellable)
        try:
            self.monitor = gfile.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.monitor.connect("changed", self._on_monitor)
        except GLib.Error:
            self.monitor = None

    def reload(self):
        if self.location is None:
            return
        if self.location.get_path():
            folder_sizer.invalidate(self.location.get_path())
        if self.search_mode:
            self._start_search()
            return
        names = {i.name for i in self.selected_items()}
        self.load(self.location)
        self.pending_select = names

    def _on_location_info(self, src, res, cancellable):
        try:
            info = src.query_info_finish(res)
        except GLib.Error:
            return
        if not cancellable.is_cancelled():
            self.location_info = info
            self.emit("contents-changed")

    def _after_mount(self, src, ok):
        if self.location is None or not src.equal(self.location):
            return
        if ok:
            self.load(src)
            return
        self.loading = False
        self.error = f"Couldn't connect to {src.get_uri()}"
        self._update_placeholder()
        self.emit("loaded")

    @property
    def can_write(self):
        if self.location_info is None:
            return self.location is not None and self.location.get_path() is not None
        if not self.location_info.has_attribute("access::can-write"):
            return True
        return self.location_info.get_attribute_boolean("access::can-write")

    def _on_enumerate(self, src, res, cancellable):
        try:
            enum = src.enumerate_children_finish(res)
        except GLib.Error as e:
            if cancellable.is_cancelled():
                return
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_MOUNTED) \
                    and not getattr(self, "_mount_tried", None) == src.get_uri():
                # A server location that isn't connected yet: connect, then try again.
                self._mount_tried = src.get_uri()
                self.placeholder.set_label("Connecting...")
                self.window.mount_enclosing(src, lambda ok: self._after_mount(src, ok))
                return
            self.loading = False
            self.error = e.message
            self._update_placeholder()
            self.emit("loaded")
            return
        self._mount_tried = None
        enum.next_files_async(256, GLib.PRIORITY_DEFAULT, cancellable, self._on_next_files,
                              cancellable)

    def _on_next_files(self, enum, res, cancellable):
        try:
            infos = enum.next_files_finish(res)
        except GLib.Error as e:
            if cancellable.is_cancelled():
                return
            infos = []
            self.error = e.message
        if cancellable.is_cancelled():
            return
        if not infos:
            enum.close_async(GLib.PRIORITY_DEFAULT, None, None)
            self.loading = False
            self._update_placeholder()
            self.emit("loaded")
            self._select_pending()
            return
        items = [FileItem(enum.get_child(i), i) for i in infos]
        self.store.splice(self.store.get_n_items(), 0, items)
        self._request_sizes(items)
        self._select_pending()
        enum.next_files_async(256, GLib.PRIORITY_DEFAULT, cancellable, self._on_next_files,
                              cancellable)

    def _find(self, name):
        for i in range(self.store.get_n_items()):
            it = self.store.get_item(i)
            if it.name == name:
                return i, it
        return -1, None

    def _on_monitor(self, monitor, f, other, event):
        if self.search_mode:
            return
        E = Gio.FileMonitorEvent
        if event in (E.DELETED, E.MOVED_OUT):
            self._remove_file(f)
        elif event == E.RENAMED:
            self._remove_file(f)
            if other is not None:
                self._add_or_refresh(other)
        elif event in (E.CREATED, E.MOVED_IN, E.CHANGES_DONE_HINT, E.ATTRIBUTE_CHANGED):
            self._add_or_refresh(f)

    def _remove_file(self, f):
        pos, _ = self._find(f.get_basename())
        if pos >= 0:
            self.store.remove(pos)

    def _add_or_refresh(self, f):
        parent = f.get_parent()
        if parent is None or self.location is None or not parent.equal(self.location):
            return
        cancellable = self.cancellable
        f.query_info_async(ATTRS, Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT,
                           cancellable, self._on_refresh_info, cancellable)

    def _on_refresh_info(self, f, res, cancellable):
        try:
            info = f.query_info_finish(res)
        except GLib.Error:
            return
        if cancellable.is_cancelled():
            return
        pos, item = self._find(info.get_name())
        if item is not None:
            item.update(info)
            if item.is_dir and f.get_path():
                folder_sizer.invalidate(f.get_path())
                self._request_sizes([item])
            for box in self._bound:
                if box.item is item:
                    self._apply(box)
            self.emit("selection-changed")
        else:
            new = FileItem(f, info)
            self.store.append(new)
            self._request_sizes([new])
            self._select_pending()

    def _on_items_changed(self, model, pos, removed, added):
        self._update_placeholder()
        self.emit("contents-changed")

    def _update_placeholder(self):
        n = self.sort_model.get_n_items()
        text = None
        if self.error:
            text = self.error
        elif n == 0 and self.loading:
            text = "Searching..." if self.search_mode else "Working on it..."
        elif n == 0 and self.search_text:
            text = "No items match your search."
        elif n == 0 and not self.loading:
            text = "This folder is empty."
        self.placeholder.set_label(text or "")
        self.placeholder.set_visible(bool(text))

    # ------------------------------------------------------------ selection

    def items(self):
        return [self.sort_model.get_item(i) for i in range(self.sort_model.get_n_items())]

    def n_items(self):
        return self.sort_model.get_n_items()

    def selected_items(self):
        bs = self.selection.get_selection()
        return [self.sort_model.get_item(bs.get_nth(i)) for i in range(bs.get_size())]

    def select_all(self):
        self.selection.select_all()

    def unselect_all(self):
        self.selection.unselect_all()

    def invert_selection(self):
        n = self.sort_model.get_n_items()
        sel = Gtk.Bitset.new_range(0, n)
        sel.subtract(self.selection.get_selection())
        mask = Gtk.Bitset.new_range(0, n)
        self.selection.set_selection(sel, mask)

    def select_names(self, names, scroll=True):
        names = set(names)
        first = -1
        sel = Gtk.Bitset.new_empty()
        for i in range(self.sort_model.get_n_items()):
            if self.sort_model.get_item(i).name in names:
                sel.add(i)
                if first < 0:
                    first = i
        n = self.sort_model.get_n_items()
        self.selection.set_selection(sel, Gtk.Bitset.new_range(0, n))
        if scroll and first >= 0:
            self.scroll_to(first, select=False)
        return first >= 0

    def select_later(self, names):
        """Select these names once they appear (after create/paste)."""
        self.pending_select = set(names)
        self._select_pending()

    def _select_pending(self):
        self._check_pending_rename()
        if not self.pending_select:
            return
        present = {self.sort_model.get_item(i).name for i in range(self.sort_model.get_n_items())}
        if self.pending_select & present:
            want = self.pending_select
            self.select_names(want)
            if want <= present:
                self.pending_select = set()

    def scroll_to(self, pos, select=True):
        flags = Gtk.ListScrollFlags.FOCUS
        if select:
            flags |= Gtk.ListScrollFlags.SELECT
        if self.view_mode == "details":
            self.cv.scroll_to(pos, None, flags, None)
        else:
            self.gv.scroll_to(pos, flags, None)

    # ------------------------------------------------------------ events

    def _item_at(self, view, x, y):
        w = view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and w is not view:
            if isinstance(w, ItemBox) and w.item is not None:
                return w
            # Clicking anywhere on a details row counts as the row.
            if w.get_css_name() == "row" and view is self.cv:
                child = self._first_itembox(w)
                if child is not None:
                    return child
            w = w.get_parent()
        return None

    def _first_itembox(self, w):
        c = w.get_first_child()
        while c is not None:
            if isinstance(c, ItemBox) and c.item is not None:
                return c
            found = self._first_itembox(c)
            if found is not None:
                return found
            c = c.get_next_sibling()
        return None

    def _on_activate(self, view, pos):
        item = self.sort_model.get_item(pos)
        if item is not None:
            self.emit("item-activated", item, False)

    def _on_click(self, gesture, n_press, x, y, view):
        button = gesture.get_current_button()
        box = self._item_at(view, x, y)
        if os.environ.get("W11_DEBUG"):
            print(f"click button={button} at=({x:.0f},{y:.0f}) view={view.get_css_name()} "
                  f"item={box.item.name if box else None}", flush=True)
        state = gesture.get_current_event_state()
        if button == Gdk.BUTTON_SECONDARY:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            view.grab_focus()
            if box is not None:
                if not self.selection.is_selected(box.pos):
                    self.selection.select_item(box.pos, True)
                self.emit("context-menu", view, x, y, True)
            else:
                self.selection.unselect_all()
                self.emit("context-menu", view, x, y, False)
        elif button == Gdk.BUTTON_MIDDLE:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            if box is not None and box.item.is_navigable:
                self.emit("item-activated", box.item, True)
        elif button == Gdk.BUTTON_PRIMARY and box is None:
            self._cancel_slow_click()
            if not state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                self.selection.unselect_all()
            view.grab_focus()
        elif button == Gdk.BUTTON_PRIMARY:
            self._cancel_slow_click()
            # Explorer: a second, slow click on the name of the selected item renames it.
            mods = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
            picked = view.pick(x, y, Gtk.PickFlags.DEFAULT)
            if (n_press == 1 and not state & mods and box.is_name and picked is box.lbl
                    and self.selection.is_selected(box.pos)
                    and self.selection.get_selection().get_size() == 1):
                item = box.item
                self._slow_click_id = GLib.timeout_add(600, self._slow_click_rename, item)

    # ------------------------------------------------------------ rename in place

    def _cancel_slow_click(self):
        if self._slow_click_id:
            GLib.source_remove(self._slow_click_id)
            self._slow_click_id = 0

    def _slow_click_rename(self, item):
        self._slow_click_id = 0
        sel = self.selected_items()
        if len(sel) == 1 and sel[0] is item and self._editing is None:
            self.start_rename(item)
        return False

    def _name_box_for(self, item):
        for box in self._bound:
            if box.item is item and box.is_name and box.get_mapped():
                return box
        return None

    def start_rename(self, item, tries=0):
        """Turn the item's name into an edit box. Returns False if it can't."""
        self._cancel_slow_click()
        pos = self._position_of(item)
        if pos == Gtk.INVALID_LIST_POSITION:
            return False
        box = self._name_box_for(item)
        if box is None:
            if tries == 0:
                self.scroll_to(pos, select=True)
            if tries < 20:
                GLib.timeout_add(50, lambda: self.start_rename(item, tries + 1) and False)
            return True
        self._begin_edit(box)
        return True

    def rename_when_visible(self, name):
        """Start renaming `name` as soon as it shows up (after New folder etc.)."""
        self._pending_rename = name
        self.select_later([name])
        self._check_pending_rename()

    def _check_pending_rename(self):
        if not self._pending_rename:
            return
        for i in range(self.sort_model.get_n_items()):
            item = self.sort_model.get_item(i)
            if item.name == self._pending_rename:
                self._pending_rename = None
                GLib.idle_add(lambda: self.start_rename(item) and False)
                return

    def _begin_edit(self, box):
        if self._editing is not None:
            self._finish_edit(commit=True, refocus=False)
        item = box.item
        entry = Gtk.Entry(text=item.display_name, hexpand=True)
        entry.add_css_class("w11-rename")
        if self.view_mode != "details" and VIEW_MODES[self.view_mode]["kind"] == "grid":
            entry.set_alignment(0.5)
            entry.set_width_chars(1)
        box.lbl.set_visible(False)
        box.insert_child_after(entry, box.lbl)
        focus = Gtk.EventControllerFocus()
        self._editing = (box, entry, item, focus)
        entry.connect("activate", lambda e: self._finish_edit(commit=True))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_edit_key)
        entry.add_controller(keys)
        focus.connect("leave", lambda *a: GLib.idle_add(self._on_edit_focus_left, entry))
        entry.add_controller(focus)
        entry.grab_focus()
        stem = os.path.splitext(item.display_name)[0] if not item.is_dir else item.display_name
        if not stem:
            stem = item.display_name
        GLib.idle_add(lambda: entry.select_region(0, len(stem)) and False)

    def _on_edit_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self._finish_edit(commit=False)
            return True
        return False

    def _on_edit_focus_left(self, entry):
        if self._editing is not None and self._editing[1] is entry \
                and not self._editing[3].props.contains_focus:
            self._finish_edit(commit=True, refocus=False)
        return False

    @property
    def is_editing(self):
        return self._editing is not None

    def _finish_edit(self, commit, refocus=True):
        if self._editing is None:
            return
        box, entry, item, _ = self._editing
        self._editing = None
        new = entry.get_text().strip()
        if entry.get_parent() is box:
            box.remove(entry)
        box.lbl.set_visible(True)
        if refocus:
            self.focus_view()
        if commit and new and new != item.display_name:
            self.emit("rename-requested", item, new)

    def _on_key(self, ctrl, keyval, keycode, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK |
                    Gdk.ModifierType.SUPER_MASK):
            return False
        ch = Gdk.keyval_to_unicode(keyval)
        if not ch:
            return False
        ch = chr(ch)
        if not ch.isprintable() or (ch == " " and not self._type_buf):
            return False
        self._type_buf += ch.casefold()
        if self._type_timeout:
            GLib.source_remove(self._type_timeout)
        self._type_timeout = GLib.timeout_add(1000, self._clear_typeahead)
        show_ext = self.window.show_extensions
        for i in range(self.sort_model.get_n_items()):
            if self.sort_model.get_item(i).label(show_ext).casefold().startswith(self._type_buf):
                self.scroll_to(i, select=True)
                break
        return True

    def _clear_typeahead(self):
        self._type_buf = ""
        self._type_timeout = 0
        return False

    def _on_scroll(self, ctrl, dx, dy):
        state = ctrl.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            self.emit("zoom", -1 if dy > 0 else 1)
            return True
        return False

    # ------------------------------------------------------------ drag & drop

    def _attach_drag(self, box):
        src = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        src.connect("prepare", self._on_drag_prepare, box)
        src.connect("drag-begin", self._on_drag_begin, box)
        box.add_controller(src)

    def _on_drag_prepare(self, source, x, y, box):
        if box.item is None:
            return None
        if not self.selection.is_selected(box.pos):
            self.selection.select_item(box.pos, True)
        files = [i.file for i in self.selected_items()]
        if not files:
            return None
        flist = Gdk.FileList.new_from_array(files)
        return Gdk.ContentProvider.new_for_value(GObject.Value(Gdk.FileList, flist))

    def _on_drag_begin(self, source, drag, box):
        if box.item is None:
            return
        theme = Gtk.IconTheme.get_for_display(self.get_display())
        paintable = theme.lookup_by_gicon(box.item.gicon, 48, self.get_scale_factor(),
                                          Gtk.TextDirection.NONE, 0)
        source.set_icon(paintable, 24, 24)

    def _drop_dest(self, x, y):
        view = self.current_view
        point = translate_point(self.stack, view, x, y)
        box = self._item_at(view, *point) if point else None
        if box is not None and box.item.is_dir:
            return box.item.file, box
        return self.location, None

    def _set_drop_highlight(self, box):
        if self._drop_hl is not None:
            self._drop_hl.remove_css_class("w11-drop-target")
        self._drop_hl = box
        if box is not None:
            box.add_css_class("w11-drop-target")

    def _drop_action(self, target, files, dest):
        if dest is None or not files or dest.get_uri_scheme() == "archive":
            return 0  # archives are read-only
        state = target.get_current_event_state()
        for f in files:
            if f.equal(dest) or dest.has_prefix(f):
                return 0
        if state & Gdk.ModifierType.CONTROL_MASK:
            return Gdk.DragAction.COPY
        if state & Gdk.ModifierType.SHIFT_MASK:
            action = Gdk.DragAction.MOVE
        else:
            action = Gdk.DragAction.MOVE if _same_device(files[0], dest) else Gdk.DragAction.COPY
        if action == Gdk.DragAction.MOVE:
            parent = files[0].get_parent()
            if parent is not None and parent.equal(dest) and all(
                    f.get_parent() and f.get_parent().equal(dest) for f in files):
                return 0
        return action

    def _on_drop_motion(self, target, x, y):
        value = target.get_value()
        dest, box = self._drop_dest(x, y)
        self._set_drop_highlight(box)
        if value is None:
            return Gdk.DragAction.COPY
        return self._drop_action(target, value.get_files(), dest)

    def _on_drop(self, target, value, x, y):
        self._set_drop_highlight(None)
        files = value.get_files()
        dest, _ = self._drop_dest(x, y)
        action = self._drop_action(target, files, dest)
        if not action:
            return False
        self.emit("files-dropped", files, dest, int(action))
        return True


def _same_device(a, b):
    pa, pb = a.get_path(), b.get_path()
    if pa and pb:
        try:
            return os.stat(pa).st_dev == os.stat(pb).st_dev
        except OSError:
            return False
    return a.get_uri_scheme() == b.get_uri_scheme() and a.get_uri_scheme() == "trash"
