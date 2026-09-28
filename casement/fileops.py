"""File operations run on worker threads, plus clipboard and naming helpers."""
import errno
import fcntl
import os
import shutil
import stat
import threading
import time
import zipfile

from gi.repository import Gdk, Gio, GLib, GObject

ARCHIVE_TYPES = {
    "application/zip", "application/x-tar", "application/x-compressed-tar",
    "application/x-bzip-compressed-tar", "application/x-xz-compressed-tar",
    "application/gzip", "application/x-gzip",
}


# ---------------------------------------------------------------- naming

def split_ext(name, is_dir=False):
    if is_dir:
        return name, ""
    for double in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if name.lower().endswith(double) and len(name) > len(double):
            return name[:-len(double)], name[-len(double):]
    stem, ext = os.path.splitext(name)
    if not stem:
        return name, ""
    return stem, ext


def _exists(parent, name, taken):
    return name in taken or parent.get_child(name).query_exists(None)


def unique_name(parent, name, is_dir=False, style="number", taken=()):
    """Explorer-style unique names.

    style='number' -> 'name (2).ext'
    style='copy'   -> 'name - Copy.ext', 'name - Copy (2).ext'
    """
    if style == "number" and not _exists(parent, name, taken):
        return name
    stem, ext = split_ext(name, is_dir)
    if style == "copy":
        cand = f"{stem} - Copy{ext}"
        if not _exists(parent, cand, taken):
            return cand
        stem = f"{stem} - Copy"
    n = 2
    while True:
        cand = f"{stem} ({n}){ext}"
        if not _exists(parent, cand, taken):
            return cand
        n += 1


# ---------------------------------------------------------------- clipboard

COPIED_FILES = "x-special/gnome-copied-files"


def clipboard_set_files(clipboard, files, cut=False):
    uris = [f.get_uri() for f in files]
    gnome = ("cut" if cut else "copy") + "\n" + "\n".join(uris)
    uri_list = "".join(u + "\r\n" for u in uris)
    text = "\n".join(f.get_path() or f.get_uri() for f in files)
    providers = [
        Gdk.ContentProvider.new_for_bytes(COPIED_FILES, GLib.Bytes.new(gnome.encode())),
        Gdk.ContentProvider.new_for_bytes("text/uri-list", GLib.Bytes.new(uri_list.encode())),
        Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new(text.encode())),
    ]
    clipboard.set_content(Gdk.ContentProvider.new_union(providers))


def clipboard_has_files(clipboard):
    formats = clipboard.get_formats()
    return (formats.contain_mime_type(COPIED_FILES) or formats.contain_mime_type("text/uri-list")
            or formats.contain_gtype(Gdk.FileList))


def clipboard_read_files(clipboard, callback):
    """callback(list_of_Gio.File, is_cut) on the main loop."""
    formats = clipboard.get_formats()
    for mime in (COPIED_FILES, "text/uri-list"):
        if formats.contain_mime_type(mime):
            clipboard.read_async([mime], GLib.PRIORITY_DEFAULT, None, _on_clip_stream, callback)
            return
    if formats.contain_gtype(Gdk.FileList):
        def done(cb, res):
            try:
                value = cb.read_value_finish(res)
                callback(list(value.get_files()), False)
            except GLib.Error:
                callback([], False)
        clipboard.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, done)
        return
    callback([], False)


def _on_clip_stream(clipboard, res, callback):
    try:
        stream, mime = clipboard.read_finish(res)
    except GLib.Error:
        callback([], False)
        return
    out = Gio.MemoryOutputStream.new_resizable()

    def spliced(o, r):
        try:
            o.splice_finish(r)
        except GLib.Error:
            callback([], False)
            return
        data = o.steal_as_bytes().get_data().decode("utf-8", "replace")
        lines = [ln.strip() for ln in data.replace("\r", "").split("\n") if ln.strip()]
        cut = False
        if mime == COPIED_FILES and lines:
            cut = lines[0] == "cut"
            lines = lines[1:]
        files = [Gio.File.new_for_uri(u) for u in lines if not u.startswith("#")]
        callback(files, cut)

    out.splice_async(stream, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE |
                     Gio.OutputStreamSpliceFlags.CLOSE_TARGET, GLib.PRIORITY_DEFAULT, None, spliced)


# ---------------------------------------------------------------- jobs

class Job(GObject.Object):
    __gtype_name__ = "W11Job"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, title):
        super().__init__()
        self.title = title
        self.detail = ""
        self.fraction = -1.0  # < 0 means indeterminate
        self.cancellable = Gio.Cancellable()
        self.errors = []
        self.result = None
        self._last_emit = 0.0
        # Details for the progress dialog.
        self.kind = "other"          # "copy", "move" or "other"
        self.source_name = ""
        self.dest_name = ""
        self.bytes_total = 0
        self.bytes_done = 0
        self.items_total = 0
        self.items_done = 0
        self.started = time.monotonic()
        self._running = threading.Event()
        self._running.set()
        self.cancel_requested = False

    @property
    def paused(self):
        return not self._running.is_set()

    def pause(self):
        self._running.clear()
        self.emit("changed")

    def resume(self):
        self._running.set()
        self.emit("changed")

    def cancel(self):
        self.cancel_requested = True
        self.cancellable.cancel()
        self._running.set()

    def wait_if_paused(self):
        """Called from the worker thread; blocks while paused."""
        while not self._running.wait(0.2):
            if self.cancellable.is_cancelled():
                return

    def progress(self, fraction=None, detail=None, force=False):
        """Called from the worker thread."""
        if fraction is not None:
            self.fraction = max(0.0, min(1.0, fraction))
        if detail is not None:
            self.detail = detail
        now = time.monotonic()
        if force or now - self._last_emit > 0.08:
            self._last_emit = now
            GLib.idle_add(self._emit_changed)

    def _emit_changed(self):
        self.emit("changed")
        return False

    @property
    def cancelled(self):
        return self.cancellable.is_cancelled()


class JobManager(GObject.Object):
    __gtype_name__ = "W11JobManager"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.jobs = []

    def run(self, title, func, on_done=None, **details):
        job = Job(title)
        for key, value in details.items():
            setattr(job, key, value)
        job.connect("changed", lambda *a: self.emit("changed"))
        self.jobs.append(job)
        self.emit("changed")

        def worker():
            try:
                job.result = func(job)
            except GLib.Error as e:
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    job.errors.append(e.message)
            except Exception as e:  # noqa: BLE001 - surface anything to the user
                job.errors.append(str(e))
            GLib.idle_add(finish)

        def finish():
            self.jobs.remove(job)
            self.emit("changed")
            if on_done:
                on_done(job)
            return False

        threading.Thread(target=worker, daemon=True).start()
        return job


# ---------------------------------------------------------------- workers

NOFOLLOW = Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS


def _is_dir(f, cancellable=None):
    try:
        return f.query_file_type(NOFOLLOW, cancellable) == Gio.FileType.DIRECTORY
    except GLib.Error:
        return False


def _children(f, cancellable):
    enum = f.enumerate_children("standard::name,standard::type,standard::size", NOFOLLOW, cancellable)
    try:
        while True:
            info = enum.next_file(cancellable)
            if info is None:
                break
            yield enum.get_child(info), info
    finally:
        enum.close(None)


def measure(files, cancellable=None):
    """Return (total_bytes, n_files, n_dirs)."""
    total = nfiles = ndirs = 0
    stack = list(files)
    while stack:
        f = stack.pop()
        if cancellable and cancellable.is_cancelled():
            break
        path = f.get_path()
        if path and os.path.isdir(path) and not os.path.islink(path):
            ndirs += 1
            try:
                for root, dirs, fnames in os.walk(path):
                    ndirs += len(dirs)
                    for n in fnames:
                        try:
                            total += os.lstat(os.path.join(root, n)).st_size
                        except OSError:
                            pass
                        nfiles += 1
                    if cancellable and cancellable.is_cancelled():
                        break
            except OSError:
                pass
            continue
        try:
            info = f.query_info("standard::type,standard::size", NOFOLLOW, cancellable)
        except GLib.Error:
            continue
        if info.get_file_type() == Gio.FileType.DIRECTORY:
            ndirs += 1
            try:
                stack.extend(c for c, _ in _children(f, cancellable))
            except GLib.Error:
                pass
        else:
            nfiles += 1
            total += info.get_size()
    return total, nfiles, ndirs


class _Cancelled(Exception):
    pass


class _Progress:
    def __init__(self, job, total, verb):
        self.job = job
        self.total = max(total, 1)
        self.done = 0
        self.verb = verb
        job.bytes_total = total

    def check(self):
        """Honour Pause and Cancel between files and chunks (worker thread).
        Called once per file, so it only reads plain attributes."""
        job = self.job
        if not job._running.is_set():
            job.wait_if_paused()
        if job.cancel_requested:
            raise _Cancelled()

    def partial(self, name, current):
        """Progress within one large file."""
        self.job.bytes_done = self.done + current
        self.job.progress((self.done + current) / self.total, f"{self.verb} {name}")

    def file_cb(self, name):
        """Progress callback for GIO copies."""
        base = self.done
        job = self.job

        def cb(current, total, *_):
            job.bytes_done = base + current
            job.progress((base + current) / self.total, f"{self.verb} {name}")
            job.wait_if_paused()  # pausing mid-file simply blocks the copy
        return cb

    def advance(self, n, items=1, name=None):
        self.done += n
        self.job.bytes_done = self.done
        self.job.items_done += items
        self.job.progress(self.done / self.total, f"{self.verb} {name}" if name else None)


# ---------------------------------------------------------------- local fast path
#
# Local-to-local copies skip GIO: GIO's per-file overhead (extra stat calls, attribute
# queries, a slower directory walk) made 20,000 small files take 3.6x as long as `cp`.
# Here each file costs just open/open/copy/chmod/utime, and the data is moved by the
# kernel: reflink clone where the filesystem supports it (Btrfs, XFS), else
# copy_file_range, else sendfile, else a plain read/write loop.

FICLONE = 0x40049409          # ioctl: instant copy-on-write clone (Btrfs, XFS…)
LARGE_FILE = 32 << 20         # files above this are copied in chunks (progress, pause, cancel)
CHUNK = 16 << 20
_FALLBACK_ERRNOS = {errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP,
                    errno.ENOTSUP, errno.EBADF, errno.ETXTBSY}


def is_local(f):
    return f.get_uri_scheme() == "file" and f.get_path() is not None


def _current_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


class _LocalCopier:
    def __init__(self, job, prog):
        self.job = job
        self.prog = prog
        self.umask = _current_umask()
        # Remember which kernel shortcuts work so we stop trying ones that don't.
        self.can_clone = True
        self.can_cfr = True
        self.can_sendfile = True

    # -- data

    def _copy_data(self, fin, fout, size, name):
        if size > 0 and self.can_clone:
            try:
                fcntl.ioctl(fout, FICLONE, fin)
                return
            except OSError:
                self.can_clone = False
        copied = 0
        chunk = CHUNK if size > LARGE_FILE else max(size, 1)
        while self.can_cfr:
            try:
                n = os.copy_file_range(fin, fout, chunk)
            except OSError as e:
                if e.errno in _FALLBACK_ERRNOS and copied == 0:
                    self.can_cfr = False
                    break
                raise
            if n == 0:
                return self._finish_tail(fin, fout, copied, size, name)
            copied += n
            if size > LARGE_FILE:
                self.prog.partial(name, copied)
                self.prog.check()
            elif copied >= size > 0:
                return  # small file done in one call; skip the extra end-of-file call
        while self.can_sendfile:
            try:
                n = os.sendfile(fout, fin, None, chunk)
            except OSError as e:
                if e.errno in _FALLBACK_ERRNOS and copied == 0:
                    self.can_sendfile = False
                    break
                raise
            if n == 0:
                return self._finish_tail(fin, fout, copied, size, name)
            copied += n
            if size > LARGE_FILE:
                self.prog.partial(name, copied)
                self.prog.check()
        self._read_write(fin, fout, copied, size, name)

    def _finish_tail(self, fin, fout, copied, size, name):
        # Some files (e.g. under /proc) report a size but copy_file_range returns 0;
        # finish anything left with plain reads.
        if copied < size or size == 0:
            self._read_write(fin, fout, copied, size, name)

    def _read_write(self, fin, fout, copied, size, name):
        while True:
            buf = os.read(fin, 1 << 20)
            if not buf:
                return
            os.write(fout, buf)
            copied += len(buf)
            if size > LARGE_FILE:
                self.prog.partial(name, copied)
                self.prog.check()

    # -- one file

    def copy_file(self, src, dst, st, overwrite, name):
        if overwrite and os.path.lexists(dst) and not os.path.isdir(dst):
            os.unlink(dst)
        mode = stat.S_IMODE(st.st_mode)
        create_mode = mode | 0o200          # we need to write it ourselves
        fin = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            fout = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, create_mode)
            try:
                try:
                    self._copy_data(fin, fout, st.st_size, name)
                    if create_mode & ~self.umask != mode:
                        os.fchmod(fout, mode)   # only when open() couldn't give the exact mode
                    os.utime(fout, ns=(st.st_atime_ns, st.st_mtime_ns))
                    self._copy_xattrs(fin, fout)
                finally:
                    os.close(fout)
            except BaseException:
                # Never leave a half-copied file behind (cancel, disk full, read error…).
                try:
                    os.unlink(dst)
                except OSError:
                    pass
                raise
        finally:
            os.close(fin)

    @staticmethod
    def _copy_xattrs(fin, fout):
        # Keep user attributes such as the "downloaded from" URL browsers record.
        try:
            names = os.listxattr(fin)
        except OSError:
            return
        for attr in names:
            if attr.startswith("user."):
                try:
                    os.setxattr(fout, attr, os.getxattr(fin, attr))
                except OSError:
                    pass

    # -- trees

    def copy_item(self, src, dst, overwrite):
        """Copy a file, symlink or whole folder. Per-file errors are recorded and skipped."""
        st = os.lstat(src)
        if not stat.S_ISDIR(st.st_mode):
            self._copy_entry(src, dst, st, overwrite)
            return
        # Iterative walk: no recursion limit on deep trees. Folder times are set
        # after their contents, or writing the contents would change them.
        stack = [(src, dst, st)]
        finished_dirs = []
        while stack:
            s_dir, d_dir, d_st = stack.pop()
            self.prog.check()
            try:
                os.mkdir(d_dir, stat.S_IMODE(d_st.st_mode) | 0o700)
            except FileExistsError:
                if not os.path.isdir(d_dir):
                    raise
            try:
                with os.scandir(s_dir) as it:
                    entries = list(it)
            except OSError as e:
                self.job.errors.append(f"{s_dir}: {e.strerror}")
                continue
            prefix = d_dir + "/"
            for entry in entries:
                s_path, d_path = entry.path, prefix + entry.name
                try:
                    e_st = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(e_st.st_mode):
                        stack.append((s_path, d_path, e_st))
                    else:
                        self._copy_entry(s_path, d_path, e_st, overwrite, entry.name)
                except _Cancelled:
                    raise
                except OSError as e:
                    self.job.errors.append(f"{s_path}: {e.strerror}")
            finished_dirs.append((d_dir, d_st))
        for d_dir, d_st in reversed(finished_dirs):
            try:
                os.chmod(d_dir, stat.S_IMODE(d_st.st_mode))
                os.utime(d_dir, ns=(d_st.st_atime_ns, d_st.st_mtime_ns))
            except OSError:
                pass

    def _copy_entry(self, src, dst, st, overwrite, name=None):
        self.prog.check()
        name = name or os.path.basename(src)
        if stat.S_ISREG(st.st_mode):
            self.copy_file(src, dst, st, overwrite, name)
            self.prog.advance(st.st_size, name=name)
        elif stat.S_ISLNK(st.st_mode):
            if overwrite and os.path.lexists(dst):
                os.unlink(dst)
            os.symlink(os.readlink(src), dst)
            self.prog.advance(st.st_size, name=name)  # measure() counts the link's own size
        else:
            # Pipes, sockets and devices can't be copied as files (Explorer skips them too).
            self.job.errors.append(f"{name}: special file skipped")


# ---------------------------------------------------------------- GIO path (network, archives…)

def copy_tree(src, dst, overwrite, job, prog, info=None):
    """GIO copy for non-local locations. `info` (from the parent's listing) saves a
    round trip per file, which matters over SMB/SFTP."""
    c = job.cancellable
    if info is None:
        info = src.query_info("standard::type,standard::size", NOFOLLOW, c)
    if info.get_file_type() == Gio.FileType.DIRECTORY:
        try:
            dst.make_directory(c)
        except GLib.Error as e:
            if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
                raise
        for child, child_info in _children(src, c):
            copy_tree(child, dst.get_child(child_info.get_name()), overwrite, job, prog, child_info)
        return
    flags = Gio.FileCopyFlags.NOFOLLOW_SYMLINKS | Gio.FileCopyFlags.ALL_METADATA
    if overwrite:
        flags |= Gio.FileCopyFlags.OVERWRITE
    job.wait_if_paused()
    src.copy(dst, flags, c, prog.file_cb(src.get_basename()), None)
    prog.advance(info.get_size())


def is_empty(f):
    """An empty folder or a zero-byte file."""
    try:
        info = f.query_info("standard::type,standard::size", NOFOLLOW, None)
        if info.get_file_type() == Gio.FileType.DIRECTORY:
            enum = f.enumerate_children("standard::name", NOFOLLOW, None)
            empty = enum.next_file(None) is None
            enum.close(None)
            return empty
        return info.get_size() == 0
    except GLib.Error:
        return False


def delete_tree(f, cancellable):
    if is_local(f):
        path = f.get_path()
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)   # fd-based and much faster than walking through GIO
        else:
            os.unlink(path)
        return
    if _is_dir(f, cancellable):
        for child, _ in list(_children(f, cancellable)):
            delete_tree(child, cancellable)
    f.delete(cancellable)


def _io_error(e):
    return e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)


def _try_rename(src, dst, overwrite, cancellable):
    """Same-disk move: a rename, no copying. Returns False if a copy is needed."""
    flags = Gio.FileCopyFlags.NOFOLLOW_SYMLINKS | Gio.FileCopyFlags.ALL_METADATA \
        | Gio.FileCopyFlags.NO_FALLBACK_FOR_MOVE
    if overwrite:
        flags |= Gio.FileCopyFlags.OVERWRITE
    try:
        src.move(dst, flags, cancellable, None, None)
        return True
    except GLib.Error as e:
        if _io_error(e):
            raise
        needs_copy = (Gio.IOErrorEnum.WOULD_RECURSE, Gio.IOErrorEnum.WOULD_MERGE,
                      Gio.IOErrorEnum.NOT_SUPPORTED)
        if any(e.matches(Gio.io_error_quark(), code) for code in needs_copy) or \
                (overwrite and e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS)):
            return False
        raise


def transfer(job, pairs, move):
    """pairs: list of (src, dst, overwrite). Returns list of (src, dst) done."""
    c = job.cancellable
    verb = "Moving" if move else "Copying"
    done = []
    job.items_total = len(pairs)

    # Moves within a disk are renames: do them first, without scanning anything.
    to_copy = []
    if move:
        for src, dst, overwrite in pairs:
            if c.is_cancelled():
                return done
            job.wait_if_paused()
            try:
                if _try_rename(src, dst, overwrite, c):
                    done.append((src, dst))
                    job.items_done += 1
                    job.progress(len(done) / len(pairs), f"{verb} {src.get_basename()}")
                else:
                    to_copy.append((src, dst, overwrite))
            except GLib.Error as e:
                if _io_error(e):
                    return done
                job.errors.append(f"{src.get_basename()}: {e.message}")
    else:
        to_copy = list(pairs)
    if not to_copy:
        job.progress(1.0, force=True)
        return done

    # Only what really has to be copied is measured (for progress and time left).
    total, nfiles, _ = measure([s for s, _, _ in to_copy], c)
    job.items_total = len(done) + max(nfiles, len(to_copy))
    prog = _Progress(job, total, verb)
    local = _LocalCopier(job, prog)
    for src, dst, overwrite in to_copy:
        errors_before = len(job.errors)
        try:
            prog.check()
            if is_local(src) and is_local(dst):
                local.copy_item(src.get_path(), dst.get_path(), overwrite)
            else:
                copy_tree(src, dst, overwrite, job, prog)
            if move and len(job.errors) == errors_before:
                delete_tree(src, c)   # only once everything arrived safely
            done.append((src, dst))
        except _Cancelled:
            break
        except GLib.Error as e:
            if _io_error(e):
                break
            job.errors.append(f"{src.get_basename()}: {e.message}")
        except OSError as e:
            job.errors.append(f"{src.get_basename()}: {e.strerror or e}")
    job.progress(1.0, force=True)
    return done


def trash(job, files):
    """Returns list of files that cannot be trashed (need permanent delete)."""
    unsupported = []
    for i, f in enumerate(files):
        if job.cancelled:
            break
        job.progress(i / max(len(files), 1), f"Deleting {f.get_basename()}")
        try:
            f.trash(job.cancellable)
        except GLib.Error as e:
            if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
                unsupported.append(f)
            elif not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                job.errors.append(f"{f.get_basename()}: {e.message}")
    return unsupported


def delete(job, files):
    for i, f in enumerate(files):
        if job.cancelled:
            break
        job.progress(i / max(len(files), 1), f"Deleting {f.get_basename()}")
        try:
            delete_tree(f, job.cancellable)
        except GLib.Error as e:
            if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                job.errors.append(f"{f.get_basename()}: {e.message}")


def compress(job, files, zip_path):
    paths = [f.get_path() for f in files if f.get_path()]
    base = os.path.dirname(paths[0])
    entries = []
    for p in paths:
        if os.path.isdir(p) and not os.path.islink(p):
            entries.append((p, os.path.relpath(p, base)))
            for root, dirs, names in os.walk(p):
                for d in dirs:
                    full = os.path.join(root, d)
                    entries.append((full, os.path.relpath(full, base)))
                for n in names:
                    full = os.path.join(root, n)
                    entries.append((full, os.path.relpath(full, base)))
        else:
            entries.append((p, os.path.relpath(p, base)))
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, (full, arc) in enumerate(entries):
                if job.cancelled:
                    raise GLib.Error.new_literal(Gio.io_error_quark(), "Cancelled",
                                                 Gio.IOErrorEnum.CANCELLED)
                job.progress(i / max(len(entries), 1), f"Compressing {arc}")
                zf.write(full, arc)
    except BaseException:
        try:
            os.unlink(zip_path)
        except OSError:
            pass
        raise


def extract(job, archive_path, dest_dir):
    job.progress(None, f"Extracting {os.path.basename(archive_path)}", force=True)
    os.makedirs(dest_dir, exist_ok=True)
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            for i, n in enumerate(names):
                if job.cancelled:
                    break
                job.progress(i / max(len(names), 1), f"Extracting {n}")
                zf.extract(n, dest_dir)
    else:
        shutil.unpack_archive(archive_path, dest_dir, filter="data")


def restore(job, items):
    """items: list of (trash Gio.File, original path)."""
    restored = []
    for trash_file, orig in items:
        if job.cancelled:
            break
        target = Gio.File.new_for_path(orig)
        parent = target.get_parent()
        try:
            if not parent.query_exists(None):
                parent.make_directory_with_parents(None)
            if target.query_exists(None):
                target = parent.get_child(unique_name(parent, target.get_basename(),
                                                      _is_dir(trash_file)))
            job.progress(None, f"Restoring {target.get_basename()}")
            trash_file.move(target, Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, job.cancellable, None, None)
            restored.append(target)
        except GLib.Error as e:
            job.errors.append(f"{target.get_basename()}: {e.message}")
    return restored


def find_in_trash(orig_paths, since):
    """Trash entries whose original path is in orig_paths, deleted after `since`."""
    out = []
    trash_root = Gio.File.new_for_uri("trash:///")
    try:
        enum = trash_root.enumerate_children("standard::name,trash::orig-path,trash::deletion-date",
                                             Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error:
        return out
    while True:
        info = enum.next_file(None)
        if info is None:
            break
        orig = info.get_attribute_byte_string("trash::orig-path")
        if orig not in orig_paths:
            continue
        date = info.get_deletion_date()
        if date is not None and date.to_unix() < since - 2:
            continue
        out.append((enum.get_child(info), orig))
    return out


def empty_trash(job):
    trash_root = Gio.File.new_for_uri("trash:///")
    children = [c for c, _ in _children(trash_root, job.cancellable)]
    for i, c in enumerate(children):
        if job.cancelled:
            break
        job.progress(i / max(len(children), 1), "Emptying Recycle Bin")
        try:
            c.delete(job.cancellable)
        except GLib.Error:
            try:
                delete_tree(c, job.cancellable)
            except GLib.Error as e:
                job.errors.append(e.message)
