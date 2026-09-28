"""Persistent settings stored as JSON in ~/.config/casement."""
import json
import os

from gi.repository import GLib

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "casement")
# Where settings lived before the app was renamed; read once if there are no new ones yet.
LEGACY_CONFIG_PATH = os.path.join(GLib.get_user_config_dir(), "w11explorer", "settings.json")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")

SPECIAL_DIRS = [
    ("desktop", GLib.UserDirectory.DIRECTORY_DESKTOP),
    ("downloads", GLib.UserDirectory.DIRECTORY_DOWNLOAD),
    ("documents", GLib.UserDirectory.DIRECTORY_DOCUMENTS),
    ("pictures", GLib.UserDirectory.DIRECTORY_PICTURES),
    ("music", GLib.UserDirectory.DIRECTORY_MUSIC),
    ("videos", GLib.UserDirectory.DIRECTORY_VIDEOS),
]


def special_dir_paths():
    """Map of absolute path -> key ('desktop', 'downloads', ...)."""
    home = GLib.get_home_dir()
    out = {}
    for key, d in SPECIAL_DIRS:
        p = GLib.get_user_special_dir(d)
        if p and os.path.normpath(p) != os.path.normpath(home):
            out[os.path.normpath(p)] = key
    return out


def _default_pins():
    return [p for p in special_dir_paths() if os.path.isdir(p)]


DEFAULTS = {
    "view_mode": "details",
    "folder_views": {},
    "show_hidden": False,
    "show_extensions": True,
    "show_nav_pane": True,
    "right_pane": "none",
    "compact": False,
    "folder_sizes": True,
    "pins": None,
    "window_size": [1200, 760],
    "maximized": False,
    "sidebar_width": 230,
    "home_sections": {"quick": True, "recent": True},
}


class Settings:
    def __init__(self):
        self._data = dict(DEFAULTS)
        self._save_id = 0
        path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else LEGACY_CONFIG_PATH
        try:
            with open(path) as f:
                self._data.update(json.load(f))
        except (OSError, ValueError):
            pass
        if path == LEGACY_CONFIG_PATH and os.path.exists(path):
            self.save_now()   # carry pins, views and servers over to the new location
        if self._data.get("pins") is None:
            self._data["pins"] = _default_pins()

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value
        self.save_soon()

    def get(self, key, default=None):
        return self._data.get(key, default)

    def save_soon(self):
        if not self._save_id:
            self._save_id = GLib.timeout_add(500, self._save)

    def _save(self):
        self._save_id = 0
        self.save_now()
        return False

    def save_now(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except OSError as e:
            print("casement: could not save settings:", e)

    # Per-folder view memory, like Explorer.
    def view_for(self, uri):
        return self._data["folder_views"].get(uri, self._data["view_mode"])

    def set_view_for(self, uri, mode):
        self._data["folder_views"][uri] = mode
        self._data["view_mode"] = mode
        self.save_soon()
