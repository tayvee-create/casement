"""Browse inside archives (ZIP, 7z, RAR, TAR, ISO…) as folders, via GVfs's archive backend.

The backend is read-only. It mounts `archive://<escaped file URI>/`, but reports the
mount root with the file URI escaped twice, so we always work from the mount root it
reports rather than the URI we built.
"""
from gi.repository import Gio, GLib

BROWSABLE_TYPES = {
    "application/zip",
    "application/x-7z-compressed",
    "application/vnd.rar", "application/x-rar", "application/x-rar-compressed",
    "application/x-tar", "application/x-compressed-tar", "application/x-bzip-compressed-tar",
    "application/x-bzip2-compressed-tar", "application/x-xz-compressed-tar",
    "application/x-zstd-compressed-tar", "application/x-lzma-compressed-tar",
    "application/x-lz4-compressed-tar",
    "application/x-cd-image", "application/x-iso9660-image",
    "application/vnd.ms-cab-compressed", "application/x-cpio",
}

MOUNT_WAIT_TRIES = 20      # the mount can reach our volume monitor a moment after mounting
MOUNT_WAIT_MS = 100


def is_browsable(content_type):
    # Exact matches only: .docx, .jar, .apk etc. are ZIPs underneath but shouldn't open as folders.
    return content_type in BROWSABLE_TYPES


def is_archive_loc(loc):
    return isinstance(loc, Gio.File) and loc.get_uri_scheme() == "archive"


def _archive_uri(gfile):
    return "archive://" + GLib.Uri.escape_string(gfile.get_uri(), None, False) + "/"


def _decode_host(uri):
    """The archive file's URI from an archive:// URI (escaped once or twice)."""
    host = uri[len("archive://"):].split("/", 1)[0]
    for _ in range(4):
        if "://" in host:
            return host
        host = GLib.Uri.unescape_string(host, None) or host
    return None


def archive_source(loc):
    """The archive file (Gio.File) that an archive:// location lives in."""
    if not is_archive_loc(loc):
        return None
    uri = _decode_host(loc.get_uri())
    return Gio.File.new_for_uri(uri) if uri else None


def archive_root(loc):
    """The root of the archive mount containing `loc`."""
    if not is_archive_loc(loc):
        return None
    try:
        return loc.find_enclosing_mount(None).get_root()
    except GLib.Error:
        return None


def find_mount(archive_file):
    uri = archive_file.get_uri()
    for mount in Gio.VolumeMonitor.get().get_mounts():
        root = mount.get_root()
        if root.get_uri_scheme() == "archive" and _decode_host(root.get_uri()) == uri:
            return mount
    return None


def open_archive(archive_file, callback):
    """Mount the archive if needed; callback(root Gio.File or None, error message or None)."""
    mount = find_mount(archive_file)
    if mount is not None:
        callback(mount.get_root(), None)
        return

    def wait_for_mount(tries):
        mount = find_mount(archive_file)
        if mount is not None:
            callback(mount.get_root(), None)
            return False
        if tries <= 0:
            callback(None, "The archive was opened but couldn't be shown.")
            return False
        GLib.timeout_add(MOUNT_WAIT_MS, wait_for_mount, tries - 1)
        return False

    def done(f, res):
        try:
            f.mount_enclosing_volume_finish(res)
        except GLib.Error as e:
            if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                callback(None, e.message)
                return
        wait_for_mount(MOUNT_WAIT_TRIES)

    target = Gio.File.new_for_uri(_archive_uri(archive_file))
    target.mount_enclosing_volume(Gio.MountMountFlags.NONE, None, None, done)


def archive_mounts():
    return [m for m in Gio.VolumeMonitor.get().get_mounts()
            if m.get_root().get_uri_scheme() == "archive"]
