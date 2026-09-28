"""Right-hand Details pane and Preview pane."""
from gi.repository import Gio, GLib, Gtk, Pango

from .items import format_date, format_size, location_icon_name, thumbnailer

TEXT_PREVIEW_BYTES = 64 * 1024


def _is_text(ct):
    return ct.startswith("text/") or Gio.content_type_is_a(ct, "text/plain") or ct in (
        "application/json", "application/xml", "application/x-shellscript",
        "application/javascript", "application/x-desktop", "application/toml",
        "application/x-yaml")


class RightPane(Gtk.Box):
    __gtype_name__ = "W11RightPane"

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.mode = "details"
        self.add_css_class("w11-rightpane")
        self.set_size_request(300, -1)
        self.set_hexpand(False)  # children with hexpand must not widen the pane
        self._token = 0
        self._video = None
        # The scroller does not propagate its natural width, so large previews
        # can't push the file list aside.
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_width=False)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=16,
                               margin_end=16, margin_top=16, margin_bottom=16)
        scroller.set_child(self.content)
        self.append(scroller)

    def set_mode(self, mode):
        self.mode = mode

    def _clear(self):
        self._token += 1
        if self._video is not None:
            stream = self._video.get_media_stream()
            if stream:
                stream.pause()
            self._video = None
        child = self.content.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.content.remove(child)
            child = nxt

    def _message(self, text):
        lbl = Gtk.Label(label=text, wrap=True, vexpand=True, valign=Gtk.Align.CENTER,
                        justify=Gtk.Justification.CENTER)
        lbl.add_css_class("w11-dim")
        self.content.append(lbl)

    def update(self, loc, items, folder_count=None):
        """items: selected FileItems; loc: current location."""
        self._clear()
        if self.mode == "preview":
            if len(items) == 1 and not items[0].is_dir:
                self._preview(items[0])
            else:
                self._message("Select a file to preview.")
            return
        if not items:
            if loc is None:
                self._message("Select a single file to get more information and share your content.")
                return
            img = Gtk.Image(icon_name=location_icon_name(loc) if isinstance(loc, str) else None,
                            pixel_size=96, margin_top=16)
            if not isinstance(loc, str):
                from .items import folder_icon_for, location_title
                img.set_from_gicon(folder_icon_for(loc))
                title = location_title(loc)
            else:
                from .items import location_title
                title = location_title(loc)
            self.content.append(img)
            self._title(title)
            if folder_count is not None:
                self._sub(f"{folder_count} item{'s' if folder_count != 1 else ''}")
            return
        if len(items) > 1:
            self.content.append(Gtk.Image(icon_name="edit-copy-symbolic", pixel_size=72, margin_top=24))
            self._title(f"{len(items)} items selected")
            total = sum(i.known_size for i in items if i.known_size is not None)
            if total:
                self._sub(f"Total size: {format_size(total)}")
            return
        item = items[0]
        img = Gtk.Image(gicon=item.gicon, pixel_size=128, margin_top=16)
        self.content.append(img)
        if thumbnailer.can_thumbnail(item):
            token = self._token

            def done(tex):
                if token == self._token:
                    pic = Gtk.Picture(paintable=tex, content_fit=Gtk.ContentFit.CONTAIN,
                                      can_shrink=True)
                    pic.add_css_class("w11-thumb")
                    self.content.insert_child_after(pic, img)
                    self.content.remove(img)
            thumbnailer.request(item, 256, done)
        self._title(item.display_name)
        self._sub(item.type_desc)
        grid = Gtk.Grid(column_spacing=12, row_spacing=6, margin_top=16)
        rows = []
        if item.known_size is not None:
            rows.append(("Size", format_size(item.known_size)))
        rows.append(("Date modified", format_date(item.mtime_dt)))
        created = item.info.get_creation_date_time() if hasattr(item.info, "get_creation_date_time") else None
        if created:
            rows.append(("Date created", format_date(created)))
        parent = item.file.get_parent()
        if parent is not None:
            rows.append(("Location", parent.get_path() or parent.get_uri()))
        orig = item.attr_str("trash::orig-path")
        if orig:
            rows.append(("Original location", orig))
        heading = Gtk.Label(label="Properties", xalign=0, margin_top=8)
        heading.add_css_class("heading")
        self.content.append(heading)
        for r, (k, v) in enumerate(rows):
            kl = Gtk.Label(label=k, xalign=0, valign=Gtk.Align.START)
            kl.add_css_class("w11-dim")
            vl = Gtk.Label(label=v, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                           selectable=True, hexpand=True)
            grid.attach(kl, 0, r, 1, 1)
            grid.attach(vl, 1, r, 1, 1)
        self.content.append(grid)

    def _title(self, text):
        lbl = Gtk.Label(label=text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                        justify=Gtk.Justification.CENTER, margin_top=8)
        lbl.add_css_class("title-4")
        self.content.append(lbl)

    def _sub(self, text):
        lbl = Gtk.Label(label=text, wrap=True)
        lbl.add_css_class("w11-dim")
        self.content.append(lbl)

    def _preview(self, item):
        ct = item.content_type
        if item.is_image and item.file.get_path():
            pic = Gtk.Picture.new_for_file(item.file)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_can_shrink(True)
            pic.set_vexpand(True)
            self.content.append(pic)
            return
        if ct.startswith("video/") or ct.startswith("audio/"):
            video = Gtk.Video.new_for_file(item.file)
            video.set_autoplay(False)
            video.set_vexpand(True)
            self._video = video
            self.content.append(video)
            return
        if _is_text(ct) and item.size <= 8 * 1024 * 1024:
            token = self._token
            view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                vexpand=True, cursor_visible=False)
            view.add_css_class("w11-textpreview")
            self.content.append(view)

            def loaded(f, res):
                try:
                    stream = f.read_finish(res)
                    data = stream.read_bytes(TEXT_PREVIEW_BYTES, None).get_data()
                    stream.close(None)
                except GLib.Error:
                    return
                if token == self._token:
                    view.get_buffer().set_text(data.decode("utf-8", "replace"))
            item.file.read_async(GLib.PRIORITY_DEFAULT, None, loaded)
            return
        self._message("No preview available.")
