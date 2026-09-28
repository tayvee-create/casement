"""File items, locations, formatting helpers, icons and thumbnails."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene  # noqa: E402

from .settings import special_dir_paths

ATTRS = ",".join([
    "standard::name", "standard::display-name", "standard::edit-name",
    "standard::type", "standard::size", "standard::allocated-size",
    "standard::icon", "standard::content-type", "standard::fast-content-type",
    "standard::is-hidden", "standard::is-backup", "standard::is-symlink",
    "standard::target-uri", "time::modified", "time::created", "time::access",
    "thumbnail::path", "access::can-write", "access::can-rename",
    "access::can-delete", "access::can-trash", "trash::orig-path",
    "trash::deletion-date", "unix::mode", "owner::user", "owner::group",
])

# Special (non-file) locations.
HOME = "explorer:home"
THISPC = "explorer:thispc"

_special_cache = None


def special_dirs():
    global _special_cache
    if _special_cache is None:
        _special_cache = special_dir_paths()
    return _special_cache


# ---------------------------------------------------------------- locations

def is_special(loc):
    return isinstance(loc, str)


def loc_key(loc):
    return loc if is_special(loc) else loc.get_uri()


def loc_equal(a, b):
    if a is None or b is None:
        return a is b
    if is_special(a) or is_special(b):
        return a == b
    return a.equal(b)


def loc_from_key(key):
    if key in (HOME, THISPC):
        return key
    return Gio.File.new_for_uri(key)


def home_file():
    return Gio.File.new_for_path(GLib.get_home_dir())


def is_trash(loc):
    return not is_special(loc) and loc.get_uri_scheme() == "trash"


def mount_for(gfile):
    try:
        return gfile.find_enclosing_mount(None)
    except GLib.Error:
        return None


def location_title(loc):
    if loc == HOME:
        return "Home"
    if loc == THISPC:
        return "This PC"
    uri = loc.get_uri()
    if uri.startswith("archive:"):
        from .archives import archive_root, archive_source
        root = archive_root(loc)
        if root is None or root.equal(loc):
            src = archive_source(loc)
            return src.get_basename() if src else "Archive"
        return loc.get_basename()
    if uri.rstrip("/") == "trash:":
        return "Recycle Bin"
    if uri.rstrip("/") == "network:":
        return "Network"
    path = loc.get_path()
    if path == "/":
        return "Local Disk (/)"
    if path and os.path.normpath(path) == os.path.normpath(GLib.get_home_dir()):
        return GLib.get_user_name()
    mount = mount_for(loc)
    if mount and mount.get_root().equal(loc):
        return mount.get_name()
    try:
        info = loc.query_info("standard::display-name", Gio.FileQueryInfoFlags.NONE, None)
        return info.get_display_name()
    except GLib.Error:
        return loc.get_basename() or uri


def location_icon_name(loc):
    if loc == HOME:
        return "w11-home"
    if loc == THISPC:
        return "w11-thispc"
    uri = loc.get_uri()
    if uri.startswith("archive:"):
        return "w11-folder-zip"
    if uri.startswith("trash:"):
        return "w11-trash"
    if uri.startswith("network:"):
        return "w11-network"
    path = loc.get_path()
    if path == "/":
        return "w11-drive"
    if path:
        key = special_dirs().get(os.path.normpath(path))
        if key:
            return f"w11-{key}"
        if os.path.normpath(path) == os.path.normpath(GLib.get_home_dir()):
            return "w11-home"
    mount = mount_for(loc)
    if mount and mount.get_root().equal(loc):
        return "w11-drive-removable" if mount.can_eject() else "w11-drive"
    return "w11-folder"


def folder_icon_for(gfile):
    """Yellow Explorer folder, with a glyph for known folders."""
    path = gfile.get_path() if gfile else None
    if path:
        key = special_dirs().get(os.path.normpath(path))
        if key:
            return Gio.ThemedIcon.new(f"w11-folder-{key}")
    return Gio.ThemedIcon.new("w11-folder")


def translate_point(src, dest, x, y):
    """(x, y) in `src` coordinates -> `dest` coordinates, or None."""
    ok, point = src.compute_point(dest, Graphene.Point().init(x, y))
    return (point.x, point.y) if ok else None


# ---------------------------------------------------------------- formatting

def format_size(n):
    """'4.52 MB' style used in the status bar and properties."""
    if n < 1024:
        return f"{n} bytes"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        n /= 1024.0
        if n < 1024 or unit == "PB":
            if n < 10:
                return f"{n:.2f} {unit}"
            if n < 100:
                return f"{n:.1f} {unit}"
            return f"{n:.0f} {unit}"


def format_size_kb(n):
    """Details view shows sizes in whole KB, rounded up, e.g. '1,234 KB'."""
    kb = (n + 1023) // 1024
    return f"{kb:,} KB"


def format_bytes_exact(n):
    return f"{format_size(n)} ({n:,} bytes)"


def format_date(dt):
    if dt is None:
        return ""
    return dt.to_local().format("%d/%m/%Y %H:%M")


def format_date_long(dt):
    if dt is None:
        return ""
    return dt.to_local().format("%d %B %Y, %H:%M:%S")


def type_description(info):
    ft = info.get_file_type()
    if ft == Gio.FileType.DIRECTORY:
        return "File folder"
    ct = info.get_attribute_string("standard::content-type") or info.get_attribute_string(
        "standard::fast-content-type") or Gio.content_type_guess(info.get_name(), None)[0]
    if not ct:
        return "File"
    if ct == "application/x-desktop":
        return "Shortcut"
    if ct == "application/zip":
        return "Compressed (zipped) Folder"
    desc = Gio.content_type_get_description(ct)
    return desc[:1].upper() + desc[1:] if desc else "File"


# ---------------------------------------------------------------- FileItem

class FileItem(GObject.Object):
    __gtype_name__ = "W11FileItem"

    def __init__(self, gfile, info):
        super().__init__()
        self.file = gfile
        self.info = info
        self.dir_size = None  # filled in by FolderSizer for folders
        self._update_cache()

    def update(self, info):
        self.info = info
        self._update_cache()

    def _update_cache(self):
        info = self.info
        self.name = info.get_name()
        self.display_name = info.get_display_name() or self.name
        ft = info.get_file_type()
        self.is_dir = ft == Gio.FileType.DIRECTORY
        self.is_navigable = self.is_dir or ft in (Gio.FileType.MOUNTABLE, Gio.FileType.SHORTCUT)
        self.size = 0 if self.is_dir else info.get_size()
        dt = info.get_modification_date_time()
        self.mtime = dt.to_unix() if dt else 0
        # Some backends (e.g. archives) don't report a type, so fall back to the file name.
        self.content_type = (info.get_attribute_string("standard::content-type")
                             or info.get_attribute_string("standard::fast-content-type")
                             or Gio.content_type_guess(self.name, None)[0] or "")
        self.type_desc = type_description(info)
        self.collate_key = GLib.utf8_collate_key_for_filename(self.display_name.casefold(), -1)
        self.is_hidden = info.get_attribute_boolean("standard::is-hidden") or info.get_attribute_boolean("standard::is-backup") or self.name.startswith(".")

    @property
    def sort_size(self):
        if self.is_dir:
            return self.dir_size if self.dir_size is not None else -1
        return self.size

    @property
    def known_size(self):
        """Size in bytes, or None for a folder whose size isn't known yet."""
        return self.dir_size if self.is_dir else self.size

    @property
    def target(self):
        """Location to navigate to for mountables/shortcuts (network://)."""
        uri = self.info.get_attribute_string("standard::target-uri")
        return Gio.File.new_for_uri(uri) if uri else self.file

    def label(self, show_extensions=True):
        name = self.display_name
        if not show_extensions and not self.is_dir:
            stem, ext = os.path.splitext(name)
            if stem and ext:
                return stem
        return name

    @property
    def is_browsable_archive(self):
        from .archives import is_browsable
        return not self.is_dir and is_browsable(self.content_type)

    @property
    def gicon(self):
        if self.is_dir:
            return folder_icon_for(self.file)
        if self.content_type == "application/zip":
            return Gio.ThemedIcon.new("w11-folder-zip")
        icon = self.info.get_attribute_object("standard::icon")
        if icon is None and self.content_type:
            icon = Gio.content_type_get_icon(self.content_type)
        return icon or Gio.ThemedIcon.new("text-x-generic")

    @property
    def mtime_dt(self):
        return self.info.get_modification_date_time()

    @property
    def is_image(self):
        return self.content_type.startswith("image/")

    def attr_str(self, name):
        return self.info.get_attribute_as_string(name) if self.info.has_attribute(name) else None


# ---------------------------------------------------------------- thumbnails

class Thumbnailer:
    """Loads image thumbnails on worker threads with an LRU-ish cache."""

    MAX_BYTES = 40 * 1024 * 1024

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=3)
        self._cache = {}
        self._pending = {}
        self._lock = threading.Lock()

    def can_thumbnail(self, item):
        if item.is_dir:
            return False
        if item.info.get_attribute_byte_string("thumbnail::path"):
            return True
        return item.is_image and item.file.get_path() is not None and item.size <= self.MAX_BYTES

    def request(self, item, size, callback):
        """callback(texture) is called on the main loop (maybe immediately)."""
        key = (item.file.get_uri(), item.mtime, size)
        tex = self._cache.get(key)
        if tex is not None:
            callback(tex)
            return
        waiters = self._pending.get(key)
        if waiters is not None:
            waiters.append(callback)
            return
        self._pending[key] = [callback]
        thumb_path = item.info.get_attribute_byte_string("thumbnail::path")
        src = item.file.get_path()
        # Pre-generated thumbnails are small (<=256px); for big views prefer the source.
        use_path = thumb_path if thumb_path and (size <= 128 or not item.is_image) else src
        self._pool.submit(self._load, key, use_path or thumb_path, size)

    def _load(self, key, path, size):
        tex = None
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
            pb = pb.apply_embedded_orientation() or pb
            ok, buf = pb.save_to_bufferv("png", [], [])
            if ok:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf))
        except Exception:
            tex = None
        GLib.idle_add(self._done, key, tex)

    def _done(self, key, tex):
        waiters = self._pending.pop(key, [])
        if tex is not None:
            if len(self._cache) > 600:
                self._cache.clear()
            self._cache[key] = tex
            for cb in waiters:
                cb(tex)
        return False


thumbnailer = Thumbnailer()


# ---------------------------------------------------------------- folder sizes

def measure_tree(path, cancel=None):
    """Total size of files under path, without following symlinks or crossing
    onto other filesystems (so /proc and friends are skipped). None if cancelled."""
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return 0
    total = 0
    stack = [path]
    while stack:
        if cancel is not None and cancel.is_set():
            return None
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            if st.st_dev == dev:
                                stack.append(entry.path)
                        else:
                            total += st.st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


class FolderSizer:
    """Works out folder sizes on background threads, with a short-lived cache."""

    CACHE_SECONDS = 120

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._cache = {}

    def request(self, path, cancel, callback):
        """callback(size) on the main loop, unless `cancel` (threading.Event) is set."""
        hit = self._cache.get(path)
        if hit is not None and GLib.get_monotonic_time() - hit[1] < self.CACHE_SECONDS * 1e6:
            callback(hit[0])
            return
        self._pool.submit(self._work, path, cancel, callback)

    def _work(self, path, cancel, callback):
        if cancel.is_set():
            return
        size = measure_tree(path, cancel)
        if size is not None:
            GLib.idle_add(self._done, path, size, cancel, callback)

    def _done(self, path, size, cancel, callback):
        self._cache[path] = (size, GLib.get_monotonic_time())
        if not cancel.is_set():
            callback(size)
        return False

    def invalidate(self, prefix=None):
        if prefix is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache if k == prefix or k.startswith(prefix.rstrip("/") + "/")]:
            del self._cache[key]


folder_sizer = FolderSizer()
