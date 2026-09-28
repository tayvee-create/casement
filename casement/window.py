"""Main window: tab strip, address bar, command bar, panes and actions."""
import os
import shutil
import threading
import time

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from . import archives, dialogs, fileops
from .addressbar import AddressBar
from .detailspane import RightPane
from .folderview import SORT_KEYS, VIEW_MODES, VIEW_ORDER, ZOOM_ORDER
from .items import (HOME, THISPC, format_size, home_file, is_special, is_trash, loc_equal,
                    loc_from_key, location_title)
from .sidebar import Sidebar
from .tab import Tab

APP_NAME = "Casement"

TERMINALS = [
    ("ptyxis", lambda d: ["ptyxis", "--new-window", "--working-directory", d]),
    ("kgx", lambda d: ["kgx", "--working-directory", d]),
    ("gnome-terminal", lambda d: ["gnome-terminal", f"--working-directory={d}"]),
    ("konsole", lambda d: ["konsole", "--workdir", d]),
    ("xfce4-terminal", lambda d: ["xfce4-terminal", f"--working-directory={d}"]),
    ("x-terminal-emulator", lambda d: ["x-terminal-emulator"]),
]


def _menu_item(label, action, target=None, icon=None):
    item = Gio.MenuItem.new(label, None)
    if target is None:
        item.set_detailed_action(action)
    else:
        item.set_action_and_target_value(action, GLib.Variant("s", target))
    if icon:
        item.set_attribute_value("verb-icon", GLib.Variant("s", icon))
    return item


class ExplorerWindow(Adw.ApplicationWindow):
    __gtype_name__ = "W11ExplorerWindow"

    def __init__(self, app, location=None):
        super().__init__(application=app)
        self.app = app
        self.settings = app.settings
        self.jobs = app.jobs
        self.tabs = []
        self.current = None
        self.recent_locations = []
        self.add_css_class("w11")

        w, h = self.settings["window_size"]
        self.set_default_size(w, h)
        if self.settings["maximized"]:
            self.maximize()
        self.set_icon_name("io.github.tayvee_create.Casement")

        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *a: self._sync_dark())
        self._sync_dark()

        self._build_actions()
        self._build_ui()
        self._apply_compact()

        self.clipboard = self.get_clipboard()
        self.clipboard.connect("changed", self._on_clipboard_changed)
        self.jobs.connect("changed", self._on_jobs_changed)
        self.connect("close-request", self._on_close)

        self.new_tab(location or HOME)

    def _sync_dark(self):
        if Adw.StyleManager.get_default().get_dark():
            self.add_css_class("w11-dark")
        else:
            self.remove_css_class("w11-dark")

    # ================================================================ properties

    @property
    def show_extensions(self):
        return self.settings["show_extensions"]

    @property
    def cut_uris(self):
        return self.app.cut_uris

    @property
    def view(self):
        return self.current.view if self.current else None

    def selected_items(self):
        if self.current and self.current.is_folder:
            return self.view.selected_items()
        return []

    def selected_files(self):
        return [i.file for i in self.selected_items()]

    # ================================================================ UI

    def _build_ui(self):
        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.FLAT,
                                  bottom_bar_style=Adw.ToolbarStyle.FLAT)
        toolbar.add_top_bar(self._build_tabstrip())
        toolbar.add_top_bar(self._build_navbar())
        toolbar.add_top_bar(self._build_cmdbar())

        self.sidebar = Sidebar(self)
        self.sidebar.connect("navigate", lambda s, loc, nt: self.open_location(loc, nt))
        self.sidebar.connect("files-dropped", lambda s, files, dest, action: self._on_drop(files, dest, action))

        self.tab_stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.tab_stack.add_css_class("w11-content")
        self.right_pane = RightPane(self)
        self.right_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        main = Gtk.Box()
        main.append(self.tab_stack)
        main.append(self.right_sep)
        main.append(self.right_pane)

        self.paned = Gtk.Paned(start_child=self.sidebar, end_child=main,
                               shrink_start_child=False, resize_start_child=False,
                               position=self.settings["sidebar_width"])
        self.paned.add_css_class("w11-paned")
        self.sidebar.set_size_request(150, -1)
        self.toasts.set_child(self.paned)
        toolbar.set_content(self.toasts)

        # Keyboard shortcuts that must not steal keys from text entries only apply
        # while the file area has focus.
        local = Gtk.ShortcutController()
        local.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        for trigger, action in (("Delete", "win.delete"), ("KP_Delete", "win.delete"),
                                ("<Shift>Delete", "win.delete-permanent"), ("<Control>d", "win.delete"),
                                ("<Control>c", "win.copy"), ("<Control>x", "win.cut"),
                                ("<Control>v", "win.paste"), ("<Control>a", "win.select-all"),
                                ("<Control>z", "win.undo"), ("BackSpace", "win.back"),
                                ("<Control>Return", "win.open-selected-new-tab"),
                                ("Menu", "win.context-menu"), ("<Shift>F10", "win.context-menu")):
            local.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                                Gtk.NamedAction.new(action)))
        self.tab_stack.add_controller(local)

        # Mouse back/forward buttons.
        nav_click = Gtk.GestureClick(button=0)
        nav_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def on_nav_click(gesture, n, x, y):
            button = gesture.get_current_button()
            if button == 8:
                self.activate_action("win.back", None)
            elif button == 9:
                self.activate_action("win.forward", None)
        nav_click.connect("pressed", on_nav_click)
        self.add_controller(nav_click)

        toolbar.add_bottom_bar(self._build_statusbar())
        self.set_content(toolbar)
        self.sidebar.set_visible(self.settings["show_nav_pane"])
        self._sync_right_pane()

    # ------------------------------------------------------------ tab strip

    def _build_tabstrip(self):
        handle = Gtk.WindowHandle()
        bar = Gtk.Box()
        bar.add_css_class("w11-tabstrip")
        self.tab_box = Gtk.Box(spacing=0, valign=Gtk.Align.END)
        new_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New tab (Ctrl+T)",
                             valign=Gtk.Align.CENTER)
        new_btn.add_css_class("flat")
        new_btn.add_css_class("w11-newtab")
        new_btn.set_action_name("win.new-tab")
        inner = Gtk.Box(spacing=4)
        inner.append(self.tab_box)
        inner.append(new_btn)
        scroller = Gtk.ScrolledWindow(child=inner, hexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                      vscrollbar_policy=Gtk.PolicyType.NEVER)
        bar.append(scroller)
        controls = Gtk.WindowControls(side=Gtk.PackType.END, valign=Gtk.Align.START,
                                      decoration_layout=":minimize,maximize,close")
        bar.append(controls)
        handle.set_child(bar)
        return handle

    def _make_tab_widget(self, tab):
        box = Gtk.Box(spacing=8)
        box.add_css_class("w11-tab")
        tab.icon = Gtk.Image(pixel_size=16)
        tab.label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                              width_chars=14, max_width_chars=22)
        close = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                           tooltip_text="Close tab (Ctrl+W)", can_focus=False)
        close.add_css_class("flat")
        close.add_css_class("w11-tab-close")
        close.connect("clicked", lambda b: self.close_tab(tab))
        box.append(tab.icon)
        box.append(tab.label)
        box.append(close)
        click = Gtk.GestureClick(button=0)

        def pressed(g, n, x, y):
            if g.get_current_button() == Gdk.BUTTON_MIDDLE:
                self.close_tab(tab)
            elif g.get_current_button() == Gdk.BUTTON_PRIMARY:
                self.switch_tab(tab)
        click.connect("pressed", pressed)
        box.add_controller(click)
        # Drop files on a tab to switch to it (like Explorer).
        motion = Gtk.DropControllerMotion()
        motion.connect("enter", lambda *a: GLib.timeout_add(500, lambda: (
            self.switch_tab(tab) if motion.is_pointer() else None) and False))
        box.add_controller(motion)
        tab.widget = box
        return box

    def _tab_title(self, tab):
        if tab.is_folder and tab.view.search_mode:
            return f"Search Results in {tab.title}"
        return tab.title

    def _update_tab_widget(self, tab):
        tab.icon.set_from_icon_name("system-search-symbolic" if tab.is_folder and tab.view.search_mode
                                    else tab.icon_name)
        title = self._tab_title(tab)
        tab.label.set_label(title)
        tab.widget.set_tooltip_text(title)
        if tab is self.current:
            self.set_title(f"{title} - {APP_NAME}")

    # ------------------------------------------------------------ nav bar

    def _nav_button(self, icon, action, tooltip):
        b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, action_name=action)
        b.add_css_class("flat")
        b.add_css_class("w11-navbtn")
        return b

    def _build_navbar(self):
        bar = Gtk.Box(spacing=4)
        bar.add_css_class("w11-navbar")
        bar.append(self._nav_button("go-previous-symbolic", "win.back", "Back (Alt+Left Arrow)"))
        bar.append(self._nav_button("go-next-symbolic", "win.forward", "Forward (Alt+Right Arrow)"))
        bar.append(self._nav_button("go-up-symbolic", "win.up", "Up (Alt+Up Arrow)"))
        bar.append(self._nav_button("view-refresh-symbolic", "win.refresh", "Refresh (F5)"))
        self.address = AddressBar(self)
        self.address.set_margin_start(4)
        self.address.connect("navigate", lambda a, loc: self.open_location(loc))
        self.address.connect("navigate-text", lambda a, text: self.navigate_text(text))
        bar.append(self.address)
        self.search = Gtk.SearchEntry(placeholder_text="Search", width_chars=24)
        self.search.add_css_class("w11-search")
        self.search.connect("search-changed", self._on_search_changed)
        self.search.connect("stop-search", lambda e: (e.set_text(""), self.focus_content()))
        self.search.connect("activate", self._on_search_activate)
        bar.append(self.search)
        return bar

    # ------------------------------------------------------------ command bar

    def _cmd_button(self, icon, action, tooltip):
        b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, action_name=action)
        b.add_css_class("flat")
        b.add_css_class("w11-cmd")
        return b

    def _cmd_menu_button(self, icon, label, tooltip, model=None, popup_func=None):
        b = Gtk.MenuButton(tooltip_text=tooltip, always_show_arrow=True)
        b.add_css_class("flat")
        b.add_css_class("w11-cmd")
        child = Gtk.Box(spacing=6)
        img = Gtk.Image(icon_name=icon)
        child.append(img)
        if label:
            child.append(Gtk.Label(label=label))
        b.set_child(child)
        if model is not None:
            b.set_menu_model(model)
        if popup_func is not None:
            b.set_create_popup_func(popup_func)
        return b, img

    def _build_cmdbar(self):
        bar = Gtk.Box(spacing=2)
        bar.add_css_class("w11-cmdbar")
        new_btn, new_img = self._cmd_menu_button("list-add-symbolic", "New", "Create a new item",
                                                 popup_func=self._build_new_menu)
        new_img.add_css_class("w11-accent-icon")
        bar.append(new_btn)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        bar.append(self._cmd_button("edit-cut-symbolic", "win.cut", "Cut (Ctrl+X)"))
        bar.append(self._cmd_button("edit-copy-symbolic", "win.copy", "Copy (Ctrl+C)"))
        bar.append(self._cmd_button("edit-paste-symbolic", "win.paste", "Paste (Ctrl+V)"))
        bar.append(self._cmd_button("document-edit-symbolic", "win.rename", "Rename (F2)"))
        bar.append(self._cmd_button("send-to-symbolic", "win.share", "Share"))
        bar.append(self._cmd_button("user-trash-symbolic", "win.delete", "Delete (Delete)"))
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        sort_btn, _ = self._cmd_menu_button("view-sort-descending-symbolic", "Sort",
                                            "Sort and group options", model=self._sort_menu())
        bar.append(sort_btn)
        view_btn, _ = self._cmd_menu_button("view-grid-symbolic", "View", "Layout and view options",
                                            model=self._view_menu())
        bar.append(view_btn)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # Recycle Bin tools, shown only there.
        self.trash_tools = Gtk.Box(spacing=2)
        for icon, label, action in (("user-trash-symbolic", "Empty Recycle Bin", "win.empty-trash"),
                                    ("edit-undo-symbolic", "Restore all items", "win.restore-all"),
                                    ("edit-undo-symbolic", "Restore the selected items", "win.restore")):
            b = Gtk.Button(action_name=action)
            c = Gtk.Box(spacing=6)
            c.append(Gtk.Image(icon_name=icon))
            c.append(Gtk.Label(label=label))
            b.set_child(c)
            b.add_css_class("flat")
            b.add_css_class("w11-cmd")
            self.trash_tools.append(b)
        self.trash_tools.set_visible(False)
        bar.append(self.trash_tools)
        # Archive tools, shown when an archive is selected.
        self.extract_btn = Gtk.Button(action_name="win.extract")
        c = Gtk.Box(spacing=6)
        c.append(Gtk.Image(icon_name="package-x-generic-symbolic"))
        c.append(Gtk.Label(label="Extract all"))
        self.extract_btn.set_child(c)
        self.extract_btn.add_css_class("flat")
        self.extract_btn.add_css_class("w11-cmd")
        self.extract_btn.set_visible(False)
        bar.append(self.extract_btn)

        more_btn = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="See more",
                                  menu_model=self._more_menu())
        more_btn.add_css_class("flat")
        more_btn.add_css_class("w11-cmd")
        bar.append(more_btn)
        bar.append(Gtk.Box(hexpand=True))
        details = Gtk.ToggleButton(tooltip_text="Details pane (Alt+Shift+P)")
        c = Gtk.Box(spacing=6)
        c.append(Gtk.Image(icon_name="sidebar-show-right-symbolic"))
        c.append(Gtk.Label(label="Details"))
        details.set_child(c)
        details.add_css_class("flat")
        details.add_css_class("w11-cmd")
        details.set_action_name("win.right-pane")
        details.set_action_target_value(GLib.Variant("s", "details"))
        bar.append(details)
        return bar

    def _build_new_menu(self, btn):
        btn.set_menu_model(self._new_menu_model())

    def _new_menu_model(self):
        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append_item(_menu_item("Folder", "win.new-folder"))
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append_item(_menu_item("Text Document", "win.new-file", "text"))
        s2.append_item(_menu_item("Empty File", "win.new-file", "empty"))
        tdir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_TEMPLATES)
        if tdir and os.path.isdir(tdir):
            try:
                names = sorted(n for n in os.listdir(tdir) if not n.startswith("."))
            except OSError:
                names = []
            for n in names[:30]:
                full = os.path.join(tdir, n)
                if os.path.isfile(full):
                    s2.append_item(_menu_item(os.path.splitext(n)[0], "win.new-file", full))
        menu.append_section(None, s2)
        return menu

    def _sort_menu(self):
        menu = Gio.Menu()
        s1 = Gio.Menu()
        for key, label in SORT_KEYS.items():
            s1.append_item(_menu_item(label, "win.sort-by", key))
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append_item(_menu_item("Ascending", "win.sort-dir", "asc"))
        s2.append_item(_menu_item("Descending", "win.sort-dir", "desc"))
        menu.append_section(None, s2)
        return menu

    def _view_menu(self):
        menu = Gio.Menu()
        s1 = Gio.Menu()
        for mode in VIEW_ORDER:
            s1.append_item(_menu_item(VIEW_MODES[mode]["label"], "win.view-mode", mode))
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append_item(_menu_item("Compact view", "win.compact"))
        menu.append_section(None, s2)
        show = Gio.Menu()
        show.append_item(_menu_item("Navigation pane", "win.show-nav"))
        show.append_item(_menu_item("Details pane", "win.right-pane", "details"))
        show.append_item(_menu_item("Preview pane", "win.right-pane", "preview"))
        show.append_item(_menu_item("File name extensions", "win.show-extensions"))
        show.append_item(_menu_item("Hidden items", "win.show-hidden"))
        show.append_item(_menu_item("Folder sizes", "win.show-folder-sizes"))
        s3 = Gio.Menu()
        s3.append_submenu("Show", show)
        menu.append_section(None, s3)
        return menu

    def _more_menu(self):
        menu = Gio.Menu()
        s0 = Gio.Menu()
        s0.append_item(_menu_item("Undo", "win.undo"))
        menu.append_section(None, s0)
        s1 = Gio.Menu()
        s1.append_item(_menu_item("Pin to Quick access", "win.pin-selected"))
        s1.append_item(_menu_item("Copy as path", "win.copy-path"))
        s1.append_item(_menu_item("Compress to ZIP file", "win.compress"))
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append_item(_menu_item("Select all", "win.select-all"))
        s2.append_item(_menu_item("Select none", "win.select-none"))
        s2.append_item(_menu_item("Invert selection", "win.invert-selection"))
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append_item(_menu_item("Open in Terminal", "win.terminal"))
        s3.append_item(_menu_item("Connect to server…", "win.connect-server"))
        s3.append_item(_menu_item("Properties", "win.properties"))
        menu.append_section(None, s3)
        s4 = Gio.Menu()
        s4.append_item(_menu_item("Keyboard shortcuts", "app.shortcuts"))
        s4.append_item(_menu_item("About Casement", "app.about"))
        menu.append_section(None, s4)
        return menu

    # ------------------------------------------------------------ status bar

    def _build_statusbar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.jobs_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        jb = Gtk.Box(spacing=12)
        jb.add_css_class("w11-jobs")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.job_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.job_title.add_css_class("heading")
        self.job_detail = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.job_detail.add_css_class("w11-dim")
        self.job_bar = Gtk.ProgressBar()
        col.append(self.job_title)
        col.append(self.job_bar)
        col.append(self.job_detail)
        jb.append(col)
        cancel = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Cancel",
                            valign=Gtk.Align.CENTER)
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda b: [j.cancel() for j in self.jobs.jobs])
        details = Gtk.Button(label="More details", valign=Gtk.Align.CENTER)
        details.add_css_class("flat")
        details.connect("clicked", lambda b: self.app.show_progress(self))
        jb.append(details)
        jb.append(cancel)
        self.jobs_revealer.set_child(jb)
        box.append(self.jobs_revealer)

        bar = Gtk.Box(spacing=16)
        bar.add_css_class("w11-status")
        self.status_count = Gtk.Label(xalign=0)
        self.status_sel = Gtk.Label(xalign=0)
        bar.append(self.status_count)
        bar.append(self.status_sel)
        bar.append(Gtk.Box(hexpand=True))
        for mode, icon, tip in (("details", "view-list-symbolic",
                                 "Displays information about each item in the window."),
                                ("large", "view-grid-symbolic", "Display items by using large thumbnails.")):
            t = Gtk.ToggleButton(icon_name=icon, tooltip_text=tip, action_name="win.view-mode",
                                 action_target=GLib.Variant("s", mode))
            t.add_css_class("flat")
            t.add_css_class("w11-status-btn")
            bar.append(t)
        box.append(bar)
        return box

    def _on_jobs_changed(self, mgr):
        jobs = mgr.jobs
        if not jobs:
            self.jobs_revealer.set_reveal_child(False)
            return
        # Tiny operations flash by; only show the bar after a moment.
        job = jobs[-1]
        if not self.jobs_revealer.get_reveal_child():
            GLib.timeout_add(350, lambda: (self.jobs_revealer.set_reveal_child(bool(self.jobs.jobs))
                                           and False))
        self.job_title.set_label(job.title if len(jobs) == 1 else f"{len(jobs)} operations running")
        self.job_detail.set_label(job.detail)
        if job.fraction < 0:
            self.job_bar.pulse()
        else:
            self.job_bar.set_fraction(job.fraction)

    # ================================================================ actions

    def _add_action(self, name, callback, param=None, state=None):
        vtype = GLib.VariantType.new(param) if param else None
        if state is None:
            action = Gio.SimpleAction.new(name, vtype)
            action.connect("activate", lambda a, p: callback(p.unpack() if p is not None else None))
        else:
            action = Gio.SimpleAction.new_stateful(name, vtype, state)
            action.connect("change-state", lambda a, v: callback(v.unpack()))
        self.add_action(action)
        return action

    def _build_actions(self):
        A = self._add_action
        A("back", lambda _: self.current.go_back())
        A("forward", lambda _: self.current.go_forward())
        A("up", lambda _: self.current.go_up())
        A("refresh", lambda _: self.current.refresh())
        A("new-tab", lambda _: self.new_tab(HOME))
        A("close-tab", lambda _: self.close_tab(self.current))
        A("new-window", lambda _: self.app.new_window(HOME))
        A("next-tab", lambda _: self._cycle_tab(1))
        A("prev-tab", lambda _: self._cycle_tab(-1))
        A("goto-tab", lambda n: self._goto_tab(n), "i")
        A("focus-address", lambda _: self.address.edit())
        A("focus-search", lambda _: (self.search.grab_focus(), self.search.select_region(0, -1)))
        A("cut", lambda _: self.copy_selection(cut=True))
        A("copy", lambda _: self.copy_selection(cut=False))
        A("paste", lambda _: self.paste())
        A("rename", lambda _: self.rename_selected())
        A("delete", lambda _: self.delete_selected(permanent=False))
        A("delete-permanent", lambda _: self.delete_selected(permanent=True))
        A("share", lambda _: self.share_selected())
        A("new-folder", lambda _: self.new_folder())
        A("new-file", lambda kind: self.new_file(kind), "s")
        A("select-all", lambda _: self.view.select_all() if self.current.is_folder else None)
        A("select-none", lambda _: self.view.unselect_all() if self.current.is_folder else None)
        A("invert-selection", lambda _: self.view.invert_selection() if self.current.is_folder else None)
        A("undo", lambda _: self.undo())
        A("copy-path", lambda _: self.copy_paths())
        A("open-selected", lambda _: self.activate_items(self.selected_items()))
        A("open-selected-new-tab", lambda _: self.activate_items(self.selected_items(), new_tab=True))
        A("open-with", lambda _: self.open_with(self.selected_files()))
        A("pin-selected", lambda _: self.pin_selected())
        A("properties", lambda _: self.show_properties())
        A("terminal", lambda _: self.open_terminal(self._terminal_dir()))
        A("compress", lambda _: self.compress_selected())
        A("extract", lambda _: self.extract_selected())
        A("restore", lambda _: self.restore_items(self.selected_items()))
        A("restore-all", lambda _: self.restore_items(self.view.items()))
        A("empty-trash", lambda _: self.empty_trash())
        A("context-menu", lambda _: self._keyboard_context_menu())
        A("connect-server", lambda _: self.show_connect_server())
        A("connect-to", lambda addr: self.connect_to_server(addr), "s")

        A("open-location", lambda key: self.open_location(loc_from_key(key)), "s")
        A("open-new-tab", lambda key: self.open_location(loc_from_key(key), True), "s")
        A("open-new-window", lambda key: self.app.new_window(loc_from_key(key)), "s")
        A("toggle-pin", lambda key: self.toggle_pin(loc_from_key(key)), "s")
        A("terminal-at", lambda key: self.open_terminal(Gio.File.new_for_uri(key).get_path()), "s")
        A("properties-at", lambda key: self.show_properties([Gio.File.new_for_uri(key)]), "s")
        A("eject-at", lambda key: self._eject_uri(key), "s")
        A("open-uri", lambda uri: self.launch_files([Gio.File.new_for_uri(uri)]), "s")
        A("open-file-location", lambda uri: self._open_file_location(uri), "s")
        A("remove-recent", lambda uri: self._remove_recent(uri), "s")

        s = self.settings
        A("view-mode", self._set_view_mode, "s", GLib.Variant("s", "details"))
        A("sort-by", lambda key: self._set_sort(key=key), "s", GLib.Variant("s", "name"))
        A("sort-dir", lambda d: self._set_sort(order=d), "s", GLib.Variant("s", "asc"))
        A("show-hidden", self._set_show_hidden, None, GLib.Variant("b", s["show_hidden"]))
        A("show-extensions", self._set_show_extensions, None, GLib.Variant("b", s["show_extensions"]))
        A("show-nav", self._set_show_nav, None, GLib.Variant("b", s["show_nav_pane"]))
        A("compact", self._set_compact, None, GLib.Variant("b", s["compact"]))
        A("show-folder-sizes", self._set_folder_sizes, None,
          GLib.Variant("b", s.get("folder_sizes", True)))
        rp = Gio.SimpleAction.new_stateful("right-pane", GLib.VariantType.new("s"),
                                           GLib.Variant("s", s["right_pane"]))
        rp.connect("activate", self._on_right_pane)
        self.add_action(rp)

    def _set_enabled(self, name, enabled):
        action = self.lookup_action(name)
        if action is not None:
            action.set_enabled(bool(enabled))

    def update_actions(self):
        tab = self.current
        if tab is None:
            return
        folder = tab.is_folder
        items = self.selected_items()
        n = len(items)
        in_trash = folder and is_trash(tab.loc)
        in_archive = folder and archives.is_archive_loc(tab.loc)
        writable = folder and self.view.can_write and not in_trash and not in_archive
        self._set_enabled("back", tab.can_go_back())
        self._set_enabled("forward", tab.can_go_forward())
        self._set_enabled("up", tab.up_location() is not None)
        self._set_enabled("close-tab", True)
        self._set_enabled("cut", n and not in_trash and not in_archive)
        self._set_enabled("copy", n)
        self._set_enabled("paste", writable and fileops.clipboard_has_files(self.clipboard))
        self._set_enabled("rename", n == 1 and not in_trash and not in_archive
                          and self._can_rename(items[0]))
        self._set_enabled("delete", n and not in_archive)
        self._set_enabled("delete-permanent", n and not in_archive)
        self._set_enabled("share", n and shutil.which("xdg-email") and all(
            not i.is_dir and i.file.get_path() for i in items))
        self._set_enabled("new-folder", writable)
        self._set_enabled("new-file", writable)
        self._set_enabled("select-all", folder)
        self._set_enabled("select-none", folder and n)
        self._set_enabled("invert-selection", folder)
        self._set_enabled("undo", bool(self.app.undo_stack))
        self._set_enabled("copy-path", n)
        self._set_enabled("open-selected", n)
        self._set_enabled("open-with", n == 1 and not items[0].is_dir)
        self._set_enabled("pin-selected", (n == 1 and items[0].is_dir and items[0].file.get_path())
                          or (n == 0 and folder and tab.loc.get_path()))
        self._set_enabled("properties", folder or n)
        self._set_enabled("terminal", self._terminal_dir() is not None)
        self._set_enabled("compress", n and writable and all(i.file.get_path() for i in items))
        archive = n == 1 and items[0].content_type in fileops.ARCHIVE_TYPES and items[0].file.get_path()
        # Inside an archive, "Extract all" extracts the whole archive, as in Explorer.
        whole = in_archive and self._current_archive_file() is not None
        self._set_enabled("extract", (archive and writable) or whole)
        self.extract_btn.set_visible(bool(archive) or whole)
        self._set_enabled("restore", in_trash and n)
        self._set_enabled("restore-all", in_trash and self.view.n_items())
        self._set_enabled("empty-trash", True)
        self.trash_tools.set_visible(bool(in_trash))
        self._set_enabled("view-mode", folder)
        self._set_enabled("sort-by", folder)
        self._set_enabled("sort-dir", folder)

    def _can_rename(self, item):
        info = item.info
        if info.has_attribute("access::can-rename"):
            return info.get_attribute_boolean("access::can-rename")
        return True

    # ================================================================ tabs

    def new_tab(self, loc, switch=True):
        tab = Tab(self)
        tab.connect("location-changed", self._on_tab_location_changed)
        v = tab.view
        v.connect("selection-changed", lambda *a: self._on_view_selection(tab))
        v.connect("contents-changed", lambda *a: self._on_view_selection(tab))
        v.connect("item-activated", lambda v, item, nt: self._on_item_activated(tab, item, nt))
        v.connect("context-menu", self._on_context_menu)
        v.connect("files-dropped", lambda v, files, dest, action: self._on_drop(files, dest, action))
        v.connect("zoom", self._on_zoom)
        v.connect("loaded", lambda *a: self._on_view_loaded(tab))
        v.connect("search-changed", lambda *a: self._update_tab_widget(tab))
        v.connect("rename-requested", self._on_rename_requested)
        self.tabs.append(tab)
        self.tab_box.append(self._make_tab_widget(tab))
        self.tab_stack.add_child(tab.stack)
        tab.navigate(loc, record=False)
        if switch or self.current is None:
            self.switch_tab(tab)
        return tab

    def close_tab(self, tab):
        if len(self.tabs) == 1:
            self.close()
            return
        idx = self.tabs.index(tab)
        self.tabs.remove(tab)
        if tab.view.cancellable:
            tab.view.cancellable.cancel()
        if tab.view.monitor:
            tab.view.monitor.cancel()
        self.tab_box.remove(tab.widget)
        self.tab_stack.remove(tab.stack)
        self._schedule_archive_cleanup()
        if tab is self.current:
            self.current = None
            self.switch_tab(self.tabs[min(idx, len(self.tabs) - 1)])

    def switch_tab(self, tab):
        if tab is self.current:
            return
        self.current = tab
        for t in self.tabs:
            if t is tab:
                t.widget.add_css_class("active")
            else:
                t.widget.remove_css_class("active")
        self.tab_stack.set_visible_child(tab.stack)
        self._sync_ui()

    def _cycle_tab(self, delta):
        i = self.tabs.index(self.current)
        self.switch_tab(self.tabs[(i + delta) % len(self.tabs)])

    def _goto_tab(self, n):
        if n == 9 or n > len(self.tabs):
            self.switch_tab(self.tabs[-1])
        else:
            self.switch_tab(self.tabs[n - 1])

    # ================================================================ navigation

    def open_location(self, loc, new_tab=False, tab=None):
        if loc is None:
            return
        if new_tab:
            self.new_tab(loc, switch=False)
            return
        tab = tab or self.current
        if tab is not self.current:
            self.switch_tab(tab)
        tab.navigate(loc)

    def navigate_text(self, text):
        low = text.strip().lower()
        specials = {"home": HOME, "this pc": THISPC, "recycle bin": Gio.File.new_for_uri("trash:///"),
                    "network": Gio.File.new_for_uri("network:///")}
        if low in specials:
            self.open_location(specials[low])
            return
        if text.startswith("~"):
            text = os.path.expanduser(text)
        base = self.current.loc if self.current.is_folder else None
        if "://" in text or text.startswith("/") or base is None:
            f = Gio.File.new_for_commandline_arg(text)
        else:
            f = base.resolve_relative_path(text)

        def on_info(src, res):
            try:
                info = src.query_info_finish(res)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_MOUNTED):
                    self.mount_enclosing(src, lambda ok: ok and self.open_location(src))
                    return
                dialogs.show_error(self, APP_NAME,
                                   f"Can't find '{text}'. Check the spelling and try again.")
                return
            if info.get_file_type() == Gio.FileType.DIRECTORY or info.get_file_type() in (
                    Gio.FileType.MOUNTABLE, Gio.FileType.SHORTCUT):
                self.open_location(src)
            else:
                self.launch_files([src])
        f.query_info_async("standard::type", Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT,
                           None, on_info)

    def _schedule_archive_cleanup(self):
        GLib.timeout_add(1500, lambda: self.app.cleanup_archives() and False)

    def _on_tab_location_changed(self, tab):
        self._schedule_archive_cleanup()
        self._update_tab_widget(tab)
        if tab.loc is not None:
            self.recent_locations.append(tab.loc)
            del self.recent_locations[:-50]
        if tab is self.current:
            self._sync_ui()

    def _on_item_activated(self, tab, item, new_tab):
        items = self.view.selected_items() if tab is self.current else []
        if item not in items:
            items = [item]
        self.activate_items(items, new_tab)

    def activate_items(self, items, new_tab=False):
        dirs = [i for i in items if i.is_navigable]
        zips = [i for i in items if i.is_browsable_archive]
        files = [i.file for i in items if not i.is_navigable and not i.is_browsable_archive]
        if dirs:
            if new_tab:
                for d in dirs:
                    self.new_tab(d.target, switch=False)
            else:
                self.open_location(dirs[0].target)
                for d in dirs[1:]:
                    self.new_tab(d.target, switch=False)
        # Archives open like folders, as in Explorer ("Open with…" still offers other apps).
        for i, item in enumerate(zips):
            self.open_archive(item.file, new_tab=new_tab or bool(dirs) or i > 0)
        if files:
            self.launch_files(files)

    def open_archive(self, archive_file, new_tab=False):
        tab = self.current

        def opened(root, error):
            if root is None:
                dialogs.show_error(self, "Can't open archive",
                                   f"{archive_file.get_basename()}: {error}")
                return
            self.app.opened_archives.add(root.get_uri())
            if new_tab or tab not in self.tabs:
                self.new_tab(root, switch=not new_tab)
            else:
                self.open_location(root, tab=tab)
        archives.open_archive(archive_file, opened)

    def launch_files(self, files):
        for f in files:
            launcher = Gtk.FileLauncher.new(f)

            def done(l, res, f=f):
                try:
                    l.launch_finish(res)
                    Gtk.RecentManager.get_default().add_item(f.get_uri())
                except GLib.Error as e:
                    if not e.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                        self.toast(f"Couldn't open {f.get_basename()}: {e.message}")
            launcher.launch(self, None, done)

    def open_with(self, files):
        for f in files:
            launcher = Gtk.FileLauncher.new(f)
            launcher.set_always_ask(True)
            launcher.launch(self, None, lambda l, r: self._ignore_dismiss(l.launch_finish, r))

    def _ignore_dismiss(self, finish, res):
        try:
            finish(res)
        except GLib.Error:
            pass

    def focus_content(self):
        if self.current and self.current.is_folder:
            self.view.focus_view()

    # ================================================================ syncing

    def _sync_ui(self):
        tab = self.current
        if tab is None or tab.loc is None:
            return
        self.set_title(f"{self._tab_title(tab)} - {APP_NAME}")
        self.address.set_location(tab.loc)
        self.search.handler_block_by_func(self._on_search_changed)
        self.search.set_text(tab.view.search_text if tab.is_folder else "")
        self.search.handler_unblock_by_func(self._on_search_changed)
        self.search.set_placeholder_text(f"Search {tab.title}")
        self.sidebar.select_location(tab.loc)
        if tab.is_folder:
            self.lookup_action("view-mode").set_state(GLib.Variant("s", tab.view.view_mode))
            self._sync_sort_state()
            GLib.idle_add(lambda: self.focus_content() and False)
        self._update_status()
        self.update_actions()
        self._update_right_pane()

    def _sync_sort_state(self):
        key, order = self.view.get_sort()
        self.lookup_action("sort-by").set_state(GLib.Variant("s", key))
        self.lookup_action("sort-dir").set_state(
            GLib.Variant("s", "desc" if order == Gtk.SortType.DESCENDING else "asc"))

    def _on_view_selection(self, tab):
        if tab is self.current:
            self._update_status()
            self.update_actions()
            self._update_right_pane()
            self._sync_sort_state()

    def _on_view_loaded(self, tab):
        if tab is self.current:
            self._update_status()
            self.update_actions()

    def _update_status(self):
        tab = self.current
        if not tab.is_folder:
            if tab.loc == HOME:
                n = len(self.settings["pins"])
            else:
                n = len(self.sidebar.drives()) + len(self.settings["pins"]) + 1
            self.status_count.set_label(f"{n} item{'s' if n != 1 else ''}")
            self.status_sel.set_label("")
            return
        n = self.view.n_items()
        self.status_count.set_label(f"{n:,} item{'s' if n != 1 else ''}")
        sel = self.view.selected_items()
        if sel:
            txt = f"{len(sel):,} item{'s' if len(sel) != 1 else ''} selected"
            sizes = [i.known_size for i in sel if i.known_size is not None]
            if sizes and (len(sizes) == len(sel) or any(not i.is_dir for i in sel)):
                txt += f"  {format_size(sum(sizes))}"
            self.status_sel.set_label(txt)
        else:
            self.status_sel.set_label("")

    def _update_right_pane(self):
        if self.settings["right_pane"] == "none" or self.current is None:
            return
        tab = self.current
        if tab.is_folder:
            self.right_pane.update(tab.loc, self.view.selected_items(), self.view.n_items())
        else:
            self.right_pane.update(tab.loc, [])

    def _sync_right_pane(self):
        mode = self.settings["right_pane"]
        visible = mode != "none"
        self.right_pane.set_visible(visible)
        self.right_sep.set_visible(visible)
        self.right_pane.set_mode(mode)
        if visible:
            self._update_right_pane()

    # ================================================================ view state

    def _set_view_mode(self, mode):
        self.lookup_action("view-mode").set_state(GLib.Variant("s", mode))
        if self.current and self.current.is_folder:
            self.view.set_view_mode(mode)
            self.settings.set_view_for(self.current.loc.get_uri(), mode)
            self.focus_content()

    def _on_zoom(self, view, delta):
        cur = view.view_mode
        i = ZOOM_ORDER.index(cur) if cur in ZOOM_ORDER else 0
        i = max(0, min(len(ZOOM_ORDER) - 1, i + delta))
        self._set_view_mode(ZOOM_ORDER[i])

    def _set_sort(self, key=None, order=None):
        if not (self.current and self.current.is_folder):
            return
        o = None
        if order is not None:
            o = Gtk.SortType.DESCENDING if order == "desc" else Gtk.SortType.ASCENDING
        self.view.sort_by(key, o)
        self._sync_sort_state()

    def _set_show_hidden(self, value):
        self.lookup_action("show-hidden").set_state(GLib.Variant("b", value))
        self.settings["show_hidden"] = value
        for tab in self.app.all_tabs():
            tab.view.set_show_hidden(value)

    def _set_show_extensions(self, value):
        self.lookup_action("show-extensions").set_state(GLib.Variant("b", value))
        self.settings["show_extensions"] = value
        for tab in self.app.all_tabs():
            tab.view.rebind()

    def _set_show_nav(self, value):
        self.lookup_action("show-nav").set_state(GLib.Variant("b", value))
        self.settings["show_nav_pane"] = value
        self.sidebar.set_visible(value)

    def _set_folder_sizes(self, value):
        self.lookup_action("show-folder-sizes").set_state(GLib.Variant("b", value))
        self.settings["folder_sizes"] = value
        for tab in self.app.all_tabs():
            if value:
                tab.view.request_all_sizes()
            else:
                tab.view.clear_sizes()

    def _set_compact(self, value):
        self.lookup_action("compact").set_state(GLib.Variant("b", value))
        self.settings["compact"] = value
        self._apply_compact()

    def _apply_compact(self):
        if self.settings["compact"]:
            self.add_css_class("w11-compact")
        else:
            self.remove_css_class("w11-compact")

    def _on_right_pane(self, action, param):
        target = param.unpack()
        new = "none" if action.get_state().unpack() == target else target
        action.set_state(GLib.Variant("s", new))
        self.settings["right_pane"] = new
        self._sync_right_pane()

    def _on_search_changed(self, entry):
        if self.current is None:
            return
        text = entry.get_text()
        if not self.current.is_folder:
            if not text.strip():
                return
            # Searching from Home or This PC searches your home folder.
            self.current.navigate(home_file())
            self.search.handler_block_by_func(self._on_search_changed)
            self.search.set_text(text)
            self.search.set_position(-1)
            self.search.handler_unblock_by_func(self._on_search_changed)
        self.view.set_search(text)
        self._update_status()

    def _on_search_activate(self, entry):
        if self.current and self.current.is_folder:
            self.view.set_search(entry.get_text())
            self.view.search_now()
        self.focus_content()

    # ================================================================ context menus

    def _on_context_menu(self, view, widget, x, y, on_item):
        if os.environ.get("W11_DEBUG"):
            print(f"context-menu on_item={on_item} parent={widget.get_css_name()}", flush=True)
        menu = self._item_menu() if on_item else self._background_menu()
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.add_css_class("w11-context")
        pop.set_parent(widget)
        pop.set_has_arrow(False)
        pop.set_halign(Gtk.Align.START)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _keyboard_context_menu(self):
        if not (self.current and self.current.is_folder):
            return
        view = self.view.current_view
        on_item = bool(self.view.selected_items())
        self._on_context_menu(self.view, view, 40, 40, on_item)

    def _item_menu(self):
        items = self.selected_items()
        in_trash = is_trash(self.current.loc)
        menu = Gio.Menu()
        icons = Gio.Menu()
        if not in_trash:
            icons.append_item(_menu_item("Cut", "win.cut", icon="edit-cut-symbolic"))
        icons.append_item(_menu_item("Copy", "win.copy", icon="edit-copy-symbolic"))
        if not in_trash:
            icons.append_item(_menu_item("Rename", "win.rename", icon="document-edit-symbolic"))
            icons.append_item(_menu_item("Share", "win.share", icon="send-to-symbolic"))
        icons.append_item(_menu_item("Delete", "win.delete", icon="user-trash-symbolic"))
        sec = Gio.MenuItem.new_section(None, icons)
        sec.set_attribute_value("display-hint", GLib.Variant("s", "horizontal-buttons"))
        menu.append_item(sec)

        s1 = Gio.Menu()
        if in_trash:
            s1.append_item(_menu_item("Restore", "win.restore"))
        else:
            s1.append_item(_menu_item("Open", "win.open-selected"))
            if any(i.is_navigable for i in items):
                s1.append_item(_menu_item("Open in new tab", "win.open-selected-new-tab"))
                if len(items) == 1:
                    s1.append_item(_menu_item("Open in new window", "win.open-new-window",
                                              items[0].target.get_uri()))
            if len(items) == 1 and not items[0].is_dir:
                s1.append_item(_menu_item("Open with...", "win.open-with"))
            if len(items) == 1 and self.view.search_mode:
                s1.append_item(_menu_item("Open file location", "win.open-file-location",
                                          items[0].file.get_uri()))
        menu.append_section(None, s1)

        s2 = Gio.Menu()
        if not in_trash:
            if len(items) == 1 and items[0].is_dir and items[0].file.get_path():
                pinned = items[0].file.get_path() in self.settings["pins"]
                s2.append_item(_menu_item("Unpin from Quick access" if pinned else "Pin to Quick access",
                                          "win.pin-selected"))
            s2.append_item(_menu_item("Compress to ZIP file", "win.compress"))
            if len(items) == 1 and items[0].content_type in fileops.ARCHIVE_TYPES:
                s2.append_item(_menu_item("Extract All...", "win.extract"))
            s2.append_item(_menu_item("Copy as path", "win.copy-path"))
            if len(items) == 1 and items[0].is_dir and items[0].file.get_path():
                s2.append_item(_menu_item("Open in Terminal", "win.terminal-at", items[0].file.get_uri()))
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append_item(_menu_item("Properties", "win.properties"))
        menu.append_section(None, s3)
        return menu

    def _background_menu(self):
        menu = Gio.Menu()
        s1 = Gio.Menu()
        view = Gio.Menu()
        for mode in VIEW_ORDER:
            view.append_item(_menu_item(VIEW_MODES[mode]["label"], "win.view-mode", mode))
        s1.append_submenu("View", view)
        s1.append_submenu("Sort by", self._sort_menu())
        menu.append_section(None, s1)
        s2 = Gio.Menu()
        s2.append_item(_menu_item("Refresh", "win.refresh"))
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append_item(_menu_item("Paste", "win.paste"))
        s3.append_item(_menu_item("Undo", "win.undo"))
        menu.append_section(None, s3)
        s4 = Gio.Menu()
        s4.append_item(_menu_item("New folder", "win.new-folder"))
        s4.append_submenu("New", self._new_menu_model())
        if self.current.loc.get_path():
            s4.append_item(_menu_item("Open in Terminal", "win.terminal"))
        if is_trash(self.current.loc):
            s4.append_item(_menu_item("Empty Recycle Bin", "win.empty-trash"))
        if self.current.loc.get_uri_scheme() == "network":
            s4.append_item(_menu_item("Connect to server…", "win.connect-server"))
        menu.append_section(None, s4)
        s5 = Gio.Menu()
        s5.append_item(_menu_item("Properties", "win.properties"))
        menu.append_section(None, s5)
        return menu

    # ================================================================ clipboard

    def copy_selection(self, cut):
        files = self.selected_files()
        if not files:
            return
        fileops.clipboard_set_files(self.clipboard, files, cut)
        self.app.set_cut_uris({f.get_uri() for f in files} if cut else set())

    def _on_clipboard_changed(self, clipboard):
        if not clipboard.is_local() and self.app.cut_uris:
            self.app.set_cut_uris(set())
        self.update_actions()

    def paste(self, dest=None):
        dest = dest or (self.current.loc if self.current.is_folder else None)
        if dest is None:
            return

        def got(files, cut):
            if files:
                self.transfer(files, dest, move=cut, from_clipboard=cut)
        fileops.clipboard_read_files(self.clipboard, got)

    def copy_paths(self):
        files = self.selected_files()
        if not files:
            return
        text = "\n".join(f'"{f.get_path() or f.get_uri()}"' for f in files)
        self.clipboard.set(text)
        self.toast(f"Copied {'path' if len(files) == 1 else f'{len(files)} paths'}")

    # ================================================================ operations

    def _on_drop(self, files, dest, action):
        if dest is None:
            return
        if is_trash(dest):
            self.trash_files(list(files))
            return
        self.transfer(list(files), dest, move=action == Gdk.DragAction.MOVE)

    def _view_for(self, dest):
        if self.current and self.current.is_folder and loc_equal(self.current.loc, dest):
            return self.view
        return None

    def transfer(self, files, dest, move, from_clipboard=False):
        for src in files:
            if src.equal(dest) or dest.has_prefix(src):
                dialogs.show_error(self, "Interrupted Action",
                                   "The destination folder is a subfolder of the source folder.")
                return

        # Checking for name clashes touches the disk once or twice per item. That's
        # instant locally but a network round trip each on SMB/SFTP, so it runs on a
        # worker thread to keep the window responsive.
        def plan():
            pairs, conflicts, taken = [], [], set()
            for src in files:
                name = src.get_basename()
                parent = src.get_parent()
                is_dir = fileops._is_dir(src)
                if parent is not None and parent.equal(dest):
                    if move:
                        continue
                    new = fileops.unique_name(dest, name, is_dir, "copy", taken)
                    taken.add(new)
                    pairs.append([src, dest.get_child(new), False, is_dir])
                    continue
                dst = dest.get_child(name)
                if name in taken or dst.query_exists(None):
                    conflicts.append(len(pairs))
                taken.add(name)
                pairs.append([src, dst, False, is_dir])
            GLib.idle_add(lambda: self._start_transfer(pairs, conflicts, dest, move, from_clipboard)
                          and False)
        threading.Thread(target=plan, daemon=True).start()

    def _start_transfer(self, pairs, conflicts, dest, move, from_clipboard):
        if not pairs:
            return
        conflicts = set(conflicts)   # looked up once per item below

        def go(policy):
            if conflicts and policy is None:
                return
            final = []
            used = set(p[1].get_basename() for i, p in enumerate(pairs) if i not in conflicts)
            for i, (s, d, o, is_dir) in enumerate(pairs):
                if i in conflicts:
                    if policy == "skip":
                        continue
                    if policy == "replace":
                        o = True
                    elif policy == "keep":
                        new = fileops.unique_name(dest, d.get_basename(), is_dir, "number", used)
                        used.add(new)
                        d = dest.get_child(new)
                final.append((s, d, o))
            if not final:
                return
            verb = "Moving" if move else "Copying"
            n = len(final)
            title = f"{verb} {n} item{'s' if n != 1 else ''} to {location_title(dest)}"

            def done(job):
                pairs_done = job.result or []
                if pairs_done:
                    if move:
                        self.app.push_undo({"type": "move", "pairs": pairs_done})
                    else:
                        self.app.push_undo({"type": "copy", "files": [d for _, d in pairs_done]})
                    v = self._view_for(dest)
                    if v is not None:
                        v.select_later([d.get_basename() for _, d in pairs_done])
                if from_clipboard and move:
                    self.clipboard.set_content(None)
                    self.app.set_cut_uris(set())
                self._report(job)
            parent = final[0][0].get_parent()
            job = self.jobs.run(title, lambda job: fileops.transfer(job, final, move), done,
                                kind="move" if move else "copy",
                                source_name=location_title(parent) if parent else "",
                                dest_name=location_title(dest))
            self._show_progress_later(job)

        if conflicts:
            dialogs.ask_conflict(self, len(conflicts), move, go)
        else:
            go(None)

    def _refresh_search(self):
        for tab in self.tabs:
            if tab.is_folder and tab.view.search_mode:
                tab.view.reload()

    def _show_progress_later(self, job):
        """Open the progress window if the operation is still going after a second."""
        def check():
            if job in self.jobs.jobs:
                self.app.show_progress(self)
            return False
        GLib.timeout_add(1000, check)

    def _report(self, job):
        self._refresh_search()
        if job.errors:
            body = "\n".join(job.errors[:8])
            if len(job.errors) > 8:
                body += f"\n…and {len(job.errors) - 8} more"
            dialogs.show_error(self, "Some items couldn't be processed", body)
        self.update_actions()

    def delete_selected(self, permanent=False):
        items = self.selected_items()
        if not items:
            return
        files = [i.file for i in items]
        if permanent or is_trash(self.current.loc):
            if len(files) == 1:
                body = f"Are you sure you want to permanently delete this {'folder' if items[0].is_dir else 'file'}?\n\n{items[0].display_name}"
            else:
                body = f"Are you sure you want to permanently delete these {len(files)} items?"
            heading = "Delete Folder" if len(files) == 1 and items[0].is_dir else (
                "Delete File" if len(files) == 1 else "Delete Multiple Items")
            dialogs.confirm(self, heading, body, "Yes", lambda: self._delete_permanently(files))
        else:
            self.trash_files(files)

    def _delete_permanently(self, files):
        n = len(files)
        self.jobs.run(f"Deleting {n} item{'s' if n != 1 else ''}",
                      lambda job: fileops.delete(job, files), self._report)

    def trash_files(self, files):
        start = time.time()
        paths = [f.get_path() for f in files if f.get_path()]

        def done(job):
            unsupported = job.result or []
            trashed = [p for p in paths if p not in {f.get_path() for f in unsupported}]
            if trashed and not job.errors:
                self.app.push_undo({"type": "trash", "paths": trashed, "time": start})
                n = len(trashed)
                self.toast(f"{n} item{'s' if n != 1 else ''} moved to the Recycle Bin",
                           "Undo", "win.undo")
            self._report(job)
            if unsupported:
                n = len(unsupported)
                dialogs.confirm(
                    self, "Delete Multiple Items" if n > 1 else "Delete File",
                    f"{'These items are' if n > 1 else 'This item is'} too big to recycle or the "
                    f"location has no Recycle Bin. Do you want to permanently delete "
                    f"{'them' if n > 1 else 'it'}?", "Yes", lambda: self._delete_permanently(unsupported))
        n = len(files)
        self.jobs.run(f"Moving {n} item{'s' if n != 1 else ''} to the Recycle Bin",
                      lambda job: fileops.trash(job, files), done)

    def rename_selected(self):
        items = self.selected_items()
        if len(items) != 1:
            return
        item = items[0]
        if not self.view.start_rename(item):
            dialogs.ask_rename(self, item.display_name, item.is_dir,
                               lambda new: self.rename_file(item.file, new, item.is_dir))

    def _on_rename_requested(self, view, item, new_name):
        if "/" in new_name or new_name in (".", ".."):
            dialogs.show_error(self, "Rename", "A file name can't contain the character /")
            return
        self.rename_file(item.file, new_name, item.is_dir)

    def rename_file(self, f, new_name, is_dir=None, push_undo=True):
        old = f.get_basename()
        if is_dir is None:
            is_dir = fileops._is_dir(f)
        old_ext = fileops.split_ext(old, is_dir)[1].lower()
        new_ext = fileops.split_ext(new_name, is_dir)[1].lower()
        if not is_dir and old_ext != new_ext and self.show_extensions:
            dialogs.confirm(self, "Rename",
                            "If you change a file name extension, the file might become unusable.\n\n"
                            "Are you sure you want to change it?", "Yes",
                            lambda: self._do_rename(f, old, new_name, push_undo), destructive=False)
        else:
            self._do_rename(f, old, new_name, push_undo)

    def _do_rename(self, f, old, new_name, push_undo=True):
        parent = f.get_parent()
        if parent is not None and new_name.casefold() != old.casefold() and \
                parent.get_child(new_name).query_exists(None):
            dialogs.show_error(self, "Rename",
                               f"There is already a file with the same name ({new_name}) in this location.")
            return
        try:
            new_file = f.set_display_name(new_name, None)
        except GLib.Error as e:
            dialogs.show_error(self, "Rename", e.message)
            return
        stack = self.app.undo_stack
        if stack and stack[-1]["type"] == "create" and len(stack[-1]["files"]) == 1 \
                and stack[-1]["files"][0].equal(f):
            # Naming something just created: undo should remove it, not un-rename it.
            stack[-1]["files"] = [new_file]
        elif push_undo:
            self.app.push_undo({"type": "rename", "file": new_file, "old": old})
        self._refresh_search()
        v = self._view_for(parent)
        if v is not None:
            v.select_later([new_file.get_basename()])

    def new_folder(self):
        if not self.current.is_folder:
            return
        dest = self.current.loc
        name = fileops.unique_name(dest, "New folder", True)
        f = dest.get_child(name)
        try:
            f.make_directory(None)
        except GLib.Error as e:
            dialogs.show_error(self, "New folder", e.message)
            return
        self.app.push_undo({"type": "create", "files": [f]})
        self._rename_new(f, name, True)

    def _rename_new(self, f, name, is_dir):
        """Explorer puts a newly created item straight into rename mode."""
        if self.view.search_mode:
            dialogs.ask_rename(self, name, is_dir, lambda new: self.rename_file(f, new, is_dir),
                               title="Name the new folder" if is_dir else "Rename")
        else:
            self.view.rename_when_visible(name)

    def new_file(self, kind):
        if not self.current.is_folder:
            return
        dest = self.current.loc
        if kind == "text":
            base, src = "New Text Document.txt", None
        elif kind == "empty":
            base, src = "New File", None
        else:
            base, src = os.path.basename(kind), Gio.File.new_for_path(kind)
        name = fileops.unique_name(dest, base, False)
        f = dest.get_child(name)
        try:
            if src is not None:
                src.copy(f, Gio.FileCopyFlags.NONE, None, None, None)
            else:
                f.create(Gio.FileCreateFlags.NONE, None).close(None)
        except GLib.Error as e:
            dialogs.show_error(self, "New", e.message)
            return
        self.app.push_undo({"type": "create", "files": [f]})
        self._rename_new(f, name, False)

    def compress_selected(self):
        items = self.selected_items()
        if not items:
            return
        dest = self.current.loc
        stem = fileops.split_ext(items[0].name, items[0].is_dir)[0]
        name = fileops.unique_name(dest, stem + ".zip", False)
        zip_path = os.path.join(dest.get_path(), name)
        files = [i.file for i in items]

        def done(job):
            if not job.errors:
                self.app.push_undo({"type": "create", "files": [Gio.File.new_for_path(zip_path)]})
                v = self._view_for(dest)
                if v is not None:
                    v.select_later([name])
            self._report(job)
        self.jobs.run(f"Compressing {len(files)} item{'s' if len(files) != 1 else ''}",
                      lambda job: fileops.compress(job, files, zip_path), done)

    def _current_archive_file(self):
        """The archive file the current tab is browsing inside (local archives only)."""
        if not (self.current and self.current.is_folder):
            return None
        root = archives.archive_root(self.current.loc)
        source = archives.archive_source(root) if root else None
        return source if source is not None and source.get_path() else None

    def extract_selected(self):
        items = self.selected_items()
        whole = self._current_archive_file()
        if whole is not None and not (len(items) == 1 and items[0].content_type in fileops.ARCHIVE_TYPES
                                      and items[0].file.get_uri_scheme() == "file"):
            # Browsing inside an archive: extract all of it next to the archive file.
            dest = whole.get_parent()
            archive = whole.get_path()
            display = whole.get_basename()
            stem = fileops.split_ext(display)[0]
        elif len(items) == 1:
            item = items[0]
            dest = self.current.loc
            archive = item.file.get_path()
            display = item.display_name
            stem = fileops.split_ext(item.name)[0]
        else:
            return
        name = fileops.unique_name(dest, stem, True)
        out = os.path.join(dest.get_path(), name)

        def done(job):
            if not job.errors:
                self.app.push_undo({"type": "create", "files": [Gio.File.new_for_path(out)]})
                v = self._view_for(dest)
                if v is not None:
                    v.select_later([name])
            self._report(job)
        self.jobs.run(f"Extracting {display}",
                      lambda job: fileops.extract(job, archive, out), done)
        if whole is not None and archive == whole.get_path():
            self.toast(f"Extracting to {name} next to {display}")

    def restore_items(self, items):
        pairs = [(i.file, i.attr_str("trash::orig-path")) for i in items if i.attr_str("trash::orig-path")]
        if not pairs:
            return
        self.jobs.run(f"Restoring {len(pairs)} item{'s' if len(pairs) != 1 else ''}",
                      lambda job: fileops.restore(job, pairs), self._report)

    def empty_trash(self):
        dialogs.confirm(self, "Empty Recycle Bin",
                        "Are you sure you want to permanently delete all of the items in the Recycle Bin?",
                        "Yes", lambda: self.jobs.run("Emptying Recycle Bin", fileops.empty_trash,
                                                     self._report))

    def share_selected(self):
        paths = [f.get_path() for f in self.selected_files() if f.get_path()]
        if not paths:
            return
        argv = ["xdg-email"]
        for p in paths:
            argv += ["--attach", p]
        try:
            Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            self.toast(f"Couldn't share: {e.message}")

    # ------------------------------------------------------------ undo

    def undo(self):
        entry = self.app.pop_undo()
        if entry is None:
            return
        kind = entry["type"]
        if kind == "rename":
            f = entry["file"]
            try:
                back = f.set_display_name(entry["old"], None)
                v = self._view_for(back.get_parent())
                if v is not None:
                    v.select_later([back.get_basename()])
            except GLib.Error as e:
                dialogs.show_error(self, "Undo", e.message)
        elif kind == "trash":
            paths, since = set(entry["paths"]), entry["time"]

            def work(job):
                return fileops.restore(job, fileops.find_in_trash(paths, since))
            self.jobs.run("Restoring items", work, self._report)
        elif kind == "move":
            pairs = [(dst, src, False) for src, dst in entry["pairs"]]
            job = self.jobs.run("Undoing move", lambda job: fileops.transfer(job, pairs, True),
                                self._report, kind="move")
            self._show_progress_later(job)
        elif kind in ("copy", "create"):
            files = entry["files"]

            def done(job):
                # Where there's no Recycle Bin, empty new items are just deleted;
                # anything with content needs a yes first.
                unsupported = job.result or []
                rest = []
                for f in unsupported:
                    if fileops.is_empty(f):
                        try:
                            f.delete(None)
                        except GLib.Error as e:
                            job.errors.append(f"{f.get_basename()}: {e.message}")
                    else:
                        rest.append(f)
                self._report(job)
                if rest:
                    n = len(rest)
                    dialogs.confirm(
                        self, "Undo",
                        f"This location has no Recycle Bin. Permanently delete "
                        f"{'these ' + str(n) + ' items' if n > 1 else rest[0].get_basename()}?",
                        "Yes", lambda: self._delete_permanently(rest))
            self.jobs.run("Undoing", lambda job: fileops.trash(job, files), done)
        self.update_actions()

    # ------------------------------------------------------------ pins

    def pin_selected(self):
        items = self.selected_items()
        if len(items) == 1 and items[0].is_dir:
            self.toggle_pin(items[0].file)
        elif not items and self.current.is_folder:
            self.toggle_pin(self.current.loc)

    def toggle_pin(self, loc):
        if is_special(loc) or not loc.get_path():
            return
        path = loc.get_path()
        pins = list(self.settings["pins"])
        if path in pins:
            pins.remove(path)
        else:
            pins.append(path)
        self.settings["pins"] = pins
        self.app.pins_changed()

    # ------------------------------------------------------------ properties

    def show_properties(self, files=None):
        if files is None:
            files = self.selected_files()
            if not files and self.current.is_folder:
                files = [self.current.loc]
        if files:
            dialogs.PropertiesDialog(self, files).present(self)

    # ------------------------------------------------------------ terminal

    def _terminal_dir(self):
        items = self.selected_items()
        if len(items) == 1 and items[0].is_dir and items[0].file.get_path():
            return items[0].file.get_path()
        if self.current and self.current.is_folder:
            return self.current.loc.get_path()
        return None

    def open_terminal(self, directory):
        if not directory:
            return
        for exe, argv in TERMINALS:
            if shutil.which(exe):
                launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
                launcher.set_cwd(directory)
                try:
                    launcher.spawnv(argv(directory))
                except GLib.Error as e:
                    self.toast(f"Couldn't open terminal: {e.message}")
                return
        self.toast("No terminal application found")

    # ------------------------------------------------------------ servers

    def show_connect_server(self):
        dialogs.ConnectServerDialog(self, self.connect_to_server).present(self)

    def connect_to_server(self, address):
        f = Gio.File.new_for_uri(address)

        def remember():
            servers = [a for a in self.settings.get("servers", []) if a != address]
            self.settings["servers"] = ([address] + servers)[:12]
            for w in self.app.get_windows():
                for tab in getattr(w, "tabs", []):
                    if tab.thispc is not None and tab.thispc.get_mapped():
                        tab.thispc.refresh()

        def mounted(ok):
            if ok:
                remember()
                self.open_location(f)
        self.toast(f"Connecting to {address}…")
        self.mount_enclosing(f, mounted)

    # ------------------------------------------------------------ mounts

    def mount_volume(self, volume, callback):
        op = Gtk.MountOperation.new(self)

        def done(vol, res):
            try:
                vol.mount_finish(res)
            except GLib.Error as e:
                self.toast(f"Couldn't mount {vol.get_name()}: {e.message}")
                return
            mount = vol.get_mount()
            if mount is not None:
                callback(mount.get_root())
        volume.mount(Gio.MountMountFlags.NONE, op, None, done)

    def mount_enclosing(self, gfile, callback):
        op = Gtk.MountOperation.new(self)

        def done(f, res):
            try:
                f.mount_enclosing_volume_finish(res)
                callback(True)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                    callback(True)
                    return
                self.toast(f"Couldn't connect: {e.message}")
                callback(False)
        gfile.mount_enclosing_volume(Gio.MountMountFlags.NONE, op, None, done)

    def eject_mount(self, mount):
        op = Gtk.MountOperation.new(self)
        name = mount.get_name()
        root = mount.get_root()

        def done(m, res):
            try:
                if m.can_eject():
                    m.eject_with_operation_finish(res)
                else:
                    m.unmount_with_operation_finish(res)
            except GLib.Error as e:
                self.toast(f"Couldn't eject {name}: {e.message}")
                return
            self.toast(f"It's now safe to remove {name}")
            for tab in self.tabs:
                if tab.is_folder and (tab.loc.equal(root) or tab.loc.has_prefix(root)):
                    tab.navigate(THISPC)
        if mount.can_eject():
            mount.eject_with_operation(Gio.MountUnmountFlags.NONE, op, None, done)
        else:
            mount.unmount_with_operation(Gio.MountUnmountFlags.NONE, op, None, done)

    def _eject_uri(self, uri):
        f = Gio.File.new_for_uri(uri)
        try:
            mount = f.find_enclosing_mount(None)
        except GLib.Error:
            return
        self.eject_mount(mount)

    # ------------------------------------------------------------ misc

    def _open_file_location(self, uri):
        f = Gio.File.new_for_uri(uri)
        parent = f.get_parent()
        if parent is None:
            return
        tab = self.current
        if tab.is_folder and tab.view.search_mode and loc_equal(parent, tab.loc):
            # Leave the search results and show the file in its own folder.
            self.search.set_text("")
            tab.view.set_search("")
            tab.view.select_later([f.get_basename()])
        else:
            tab.navigate(parent, select_name=f.get_basename())

    def _remove_recent(self, uri):
        try:
            Gtk.RecentManager.get_default().remove_item(uri)
        except GLib.Error:
            pass

    def toast(self, text, button=None, action=None):
        t = Adw.Toast(title=text, timeout=4)
        if button:
            t.set_button_label(button)
            t.set_action_name(action)
        self.toasts.add_toast(t)

    def _on_close(self, *args):
        if not self.is_maximized():
            w, h = self.get_default_size()
            self.settings["window_size"] = [w, h]
        self.settings["maximized"] = self.is_maximized()
        self.settings["sidebar_width"] = self.paned.get_position()
        self.settings.save_now()
        for tab in self.tabs:
            if tab.view.monitor:
                tab.view.monitor.cancel()
        return False
