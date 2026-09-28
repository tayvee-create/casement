# Casement

A GNOME file manager inspired by the Windows 11 File Explorer. The name comes from
the casement, a window that opens on hinges.
Written in Python with GTK 4 and libadwaita. There is nothing to compile.

## Run

```sh
./casement.sh             # opens Home
./casement.sh ~/Pictures  # opens a folder
```

Install for your user (adds an app-grid entry and `~/.local/bin/casement`):

```sh
./install.sh
xdg-mime default io.github.tayvee_create.Casement.desktop inode/directory   # optional: open folders with it
```

Requirements: Python 3, PyGObject, GTK ≥ 4.12, libadwaita ≥ 1.5. Ubuntu/GNOME ships all of these.

## Windows 11 features it reproduces

- **Tabs in the title bar**, with a `+` button and square window controls (the close button turns red on hover).
  Ctrl+T, Ctrl+W, Ctrl+Tab, Ctrl+1…9. Middle-click closes a tab.
- **Address bar** with breadcrumbs. Each `›` chevron opens a list of subfolders.
  Click the empty space (or press Ctrl+L / Alt+D / F4) to type a path.
  Typing `Home`, `This PC` or `Recycle Bin` works too. There is also a recent-locations dropdown.
- **Back / Forward / Up / Refresh** buttons. The mouse back/forward buttons and Backspace also work.
  Going up selects the folder you came from.
- **Search box** ("Search Downloads"):
  - Typing filters the current folder instantly. After a short pause it also searches every
    subfolder in the background, as Windows 11 does.
  - Results show a **Folder** column, and the right-click menu gains **Open file location**.
  - Press Enter to search straight away, or Escape to clear the search.
  - Searching from Home or This PC searches your home folder.
- **Command bar**:
  - New ▾ (Folder, Text Document, and anything in `~/Templates`)
  - Cut, Copy, Paste, Rename, Share, Delete
  - Sort ▾ and View ▾, with Compact view and the Show submenu (navigation / details / preview panes,
    file name extensions, hidden items)
  - `…` menu
  - Details toggle
  - Recycle Bin tools and "Extract all", which appear only when they apply
- **Navigation pane** with a folder tree: Home, pinned Quick access folders, This PC (drives, with eject),
  Network, Recycle Bin. Every folder has a › arrow that expands its subfolders (Right/Left arrow keys
  work too). The arrow disappears on folders with nothing inside, and expanded folders are remembered.
  The current item gets the blue pill indicator.
- **Home page**: Quick access tiles and Recent files. **This PC**: folders plus drives with usage bars
  that turn red when nearly full.
- **Views**: Extra large / Large / Medium / Small icons, Details, Tiles. The chosen view is remembered per folder.
  Ctrl+Shift+1…7 and Ctrl+mouse wheel switch views. Image thumbnails appear in the icon views.
- **Details columns**: Name, Date modified, Type, Size (in KB). **Folders show their size too**, worked
  out in the background; turn this off in View ▸ Show ▸ Folder sizes. The Recycle Bin also shows Original location
  and Date deleted. Sorting uses natural order. Folders come first (last when sorting descending).
- **Windows 11 context menu**: a row of icons at the top (Cut / Copy / Rename / Share / Delete), then Open,
  Open with, Pin to Quick access, Compress to ZIP, Extract All, Copy as path, Open in Terminal, Properties.
- **File operations** run in the background. Copies and moves that take more than a second open the
  Windows-style **progress window**: percentage, a green speed graph, time remaining, items remaining,
  speed, and **Pause/Resume** and Cancel buttons. "More details" on the status strip reopens it.
  - **Fast copy engine**: local copies bypass GIO and let the kernel move the data (instant
    reflink clones on Btrfs/XFS, then `copy_file_range`, `sendfile`, plain read/write), keeping
    modified times, permissions and `user.*` attributes. It runs at `cp` speed: 20,000 small files
    in ~1.8s vs `cp`'s ~1.6s, where the difference is the upfront scan behind the progress window.
    Same-disk moves are plain renames. Network and archive copies go through GIO.
  - One unreadable file doesn't stop a copy; it's listed at the end. Cancelling never leaves
    a half-copied file behind.
  - Replace / Skip / Keep both when names clash.
  - Copies made in the same folder are named "name - Copy.ext".
  - Delete moves items to the Recycle Bin, with an Undo toast. Shift+Delete deletes permanently after asking.
  - **Ctrl+Z undo** covers rename, move, copy, new items and delete.
  - **Rename in place**: F2, or a slow second click on a selected item's name, turns the name into an
    edit box with the part before the extension selected. Enter saves, Escape cancels, clicking away saves.
    It warns if you change the extension.
  - New folder / New file are created as "New folder" etc. and go straight into rename mode.
  - Cut items appear faded.
- **Drag and drop** onto folders, the navigation pane, the Recycle Bin and tabs.
  Drag in the empty area to select a group of files.
- **Details pane** (Alt+Shift+P) and **Preview pane** (Alt+P) for images, text, video and audio.
- **Properties dialog** (Alt+Enter):
  - General tab: size and size on disk worked out live, contents count, dates, "Opens with",
    and a Read-only box
  - Security tab: owner, group and permissions
- **Open archives like folders**: double-click a ZIP, 7z, RAR, TAR (.gz/.bz2/.xz/.zst) or ISO file to
  browse inside it, as in Windows. ZIPs show as "Compressed (zipped) Folder" with a zipped-folder icon.
  Inside an archive you can open, copy and drag files out; the contents are read-only. **Extract all**
  unpacks the whole archive next to it, and archives are closed automatically once you leave them.
  "Open with…" still offers Archive Manager and other apps.
- **Connect to server** (… menu, This PC, or right-click Network): SMB, SFTP, FTP, WebDAV, AFP and NFS.
  Windows-style `\\server\share` paths work too. Recent servers are remembered and shown as
  **Network locations** on This PC. Passwords are asked for by GNOME's own sign-in prompt.
- **Light and dark** themes follow the GNOME setting.
- The clipboard works with Nautilus and other GNOME apps.

## Layout

| File | Purpose |
|---|---|
| `casement/app.py` | Application, shortcuts, styling, undo stack |
| `casement/window.py` | Window, tab strip, toolbars, actions, file operations UI |
| `casement/tab.py` | Per-tab location and history |
| `casement/folderview.py` | Details and icon views, sorting, filtering, drag & drop, live folder monitoring |
| `casement/sidebar.py` | Navigation pane |
| `casement/addressbar.py` | Breadcrumb / text address bar |
| `casement/homepage.py` | Home and This PC pages |
| `casement/detailspane.py` | Details / Preview pane |
| `casement/dialogs.py` | Rename, conflict, confirm and Properties dialogs |
| `casement/fileops.py` | Threaded copy/move/trash/zip/restore (with pause), clipboard helpers |
| `casement/progress.py` | Progress window with speed graph |
| `casement/archives.py` | Browsing inside archives (via GNOME's GVfs archive backend) |
| `casement/style.css` | The Windows 11 look |
| `tools/make_icons.py` | Generates the Fluent-style folder and place icons |
| `tools/dev-broadway.sh` | Runs a separate test copy in a browser (GTK Broadway) at http://localhost:8085 |

Settings live in `~/.config/casement/settings.json`.

## Not done yet

- The "List" and "Content" views, and Group by.
- Adding files into a ZIP (archives are read-only when browsed), and creating 7z archives.
- Dragging tabs to reorder them.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).

Not affiliated with or endorsed by Microsoft. Windows and File Explorer are trademarks of Microsoft Corporation; they're mentioned here only to describe what the app is inspired by.
