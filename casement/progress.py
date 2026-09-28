"""Explorer-style progress window for file operations (speed graph, time left, pause)."""
import time
from collections import deque

import gi

gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .items import format_size

SAMPLE_MS = 500
HISTORY = 120          # graph points (one per sample)
SPEED_WINDOW = 4.0     # seconds of samples used for the speed figure


def format_remaining(seconds):
    if seconds is None:
        return "Calculating..."
    seconds = int(seconds)
    if seconds < 5:
        return "A few seconds"
    if seconds < 60:
        return f"About {seconds} seconds"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"About {minutes} minute{'s' if minutes != 1 else ''}"
    hours, minutes = divmod(minutes, 60)
    text = f"About {hours} hour{'s' if hours != 1 else ''}"
    if minutes:
        text += f" {minutes} minute{'s' if minutes != 1 else ''}"
    return text


def _rgba(r, g, b, a):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


class SpeedGraph(Gtk.Widget):
    """Filled area chart of transfer speed, like Explorer's green graph.

    Drawn with GtkSnapshot/GskPath, so it needs no cairo bindings."""
    __gtype_name__ = "W11SpeedGraph"

    FILL = _rgba(0.02, 0.69, 0.15, 0.45)
    LINE = _rgba(0.02, 0.55, 0.12, 1.0)
    DONE = _rgba(0.02, 0.69, 0.15, 0.10)
    BORDER = _rgba(0.5, 0.5, 0.5, 0.35)
    MARK = _rgba(0.4, 0.4, 0.4, 0.8)

    def __init__(self):
        super().__init__(hexpand=True)
        self.set_size_request(-1, 72)
        self.values = deque(maxlen=HISTORY)
        self.fraction = 0.0

    def push(self, speed, fraction):
        self.values.append(max(speed, 0.0))
        self.fraction = fraction
        self.queue_draw()

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        snapshot.append_color(self.DONE, Graphene.Rect().init(0, 0, width * self.fraction, height))
        border = Gsk.PathBuilder.new()
        border.add_rect(Graphene.Rect().init(0.5, 0.5, width - 1, height - 1))
        snapshot.append_stroke(border.to_path(), Gsk.Stroke.new(1), self.BORDER)
        values = list(self.values)
        if len(values) < 2:
            return
        top = max(values) * 1.2 or 1.0
        span = max(width * self.fraction, width * 0.05)
        step = span / (len(values) - 1)

        def y(v):
            return height - (v / top) * (height - 4)

        area = Gsk.PathBuilder.new()
        area.move_to(0, height)
        for i, v in enumerate(values):
            area.line_to(i * step, y(v))
        area.line_to((len(values) - 1) * step, height)
        area.close()
        snapshot.append_fill(area.to_path(), Gsk.FillRule.WINDING, self.FILL)

        line = Gsk.PathBuilder.new()
        line.move_to(0, y(values[0]))
        for i, v in enumerate(values[1:], start=1):
            line.line_to(i * step, y(v))
        snapshot.append_stroke(line.to_path(), Gsk.Stroke.new(1.5), self.LINE)

        mark = Gsk.PathBuilder.new()
        mark.move_to(0, y(values[-1]))
        mark.line_to(width, y(values[-1]))
        snapshot.append_stroke(mark.to_path(), Gsk.Stroke.new(1), self.MARK)


class JobPanel(Gtk.Box):
    def __init__(self, job):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.job = job
        self.samples = deque()
        self.speed = 0.0
        self.transfer = job.kind in ("copy", "move")

        self.title = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.append(self.title)
        head = Gtk.Box(spacing=8)
        self.percent = Gtk.Label(xalign=0, hexpand=True)
        self.percent.add_css_class("title-3")
        head.append(self.percent)
        self.pause_btn = Gtk.Button(icon_name="media-playback-pause-symbolic", tooltip_text="Pause",
                                    valign=Gtk.Align.CENTER)
        self.pause_btn.add_css_class("flat")
        self.pause_btn.connect("clicked", self._toggle_pause)
        self.pause_btn.set_visible(self.transfer)
        cancel = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Cancel",
                            valign=Gtk.Align.CENTER)
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda b: job.cancel())
        head.append(self.pause_btn)
        head.append(cancel)
        self.append(head)

        if self.transfer:
            self.graph = SpeedGraph()
            self.append(self.graph)
            self.bar = None
        else:
            self.graph = None
            self.bar = Gtk.ProgressBar()
            self.append(self.bar)

        self.grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        self.rows = {}
        labels = ["Name", "Time remaining", "Items remaining", "Speed"] if self.transfer else ["Name"]
        for r, key in enumerate(labels):
            k = Gtk.Label(label=f"{key}:", xalign=0)
            k.add_css_class("w11-dim")
            v = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
            self.grid.attach(k, 0, r, 1, 1)
            self.grid.attach(v, 1, r, 1, 1)
            self.rows[key] = v
        self.append(self.grid)
        self.refresh()

    def _toggle_pause(self, *a):
        if self.job.paused:
            self.job.resume()
        else:
            self.job.pause()
        self.refresh()

    def sample(self):
        """Called every SAMPLE_MS; updates the speed figure and graph."""
        job = self.job
        now = time.monotonic()
        self.samples.append((now, job.bytes_done))
        while len(self.samples) > 2 and now - self.samples[0][0] > SPEED_WINDOW:
            self.samples.popleft()
        if job.paused:
            self.speed = 0.0
        elif len(self.samples) >= 2:
            (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
            self.speed = (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0
        if self.graph is not None:
            self.graph.push(self.speed, self.fraction)
        self.refresh()

    @property
    def fraction(self):
        job = self.job
        if job.bytes_total:
            return min(1.0, job.bytes_done / job.bytes_total)
        return max(job.fraction, 0.0)

    def refresh(self):
        job = self.job
        if self.transfer:
            verb = "Moving" if job.kind == "move" else "Copying"
            n = job.items_total
            what = f"{n:,} item{'s' if n != 1 else ''}" if n else "items"
            text = f"{verb} {what}"
            if job.source_name:
                text += f" from {job.source_name}"
            if job.dest_name:
                text += f" to {job.dest_name}"
            self.title.set_label(text)
        else:
            self.title.set_label(job.title)
        pct = int(self.fraction * 100)
        self.percent.set_label("Paused" if job.paused else f"{pct}% complete")
        self.pause_btn.set_icon_name("media-playback-start-symbolic" if job.paused
                                     else "media-playback-pause-symbolic")
        self.pause_btn.set_tooltip_text("Resume" if job.paused else "Pause")
        name = job.detail.split(" ", 1)[1] if " " in job.detail else job.detail
        self.rows["Name"].set_label(name)
        if self.bar is not None:
            if job.fraction < 0:
                self.bar.pulse()
            else:
                self.bar.set_fraction(job.fraction)
        if not self.transfer:
            return
        remaining_bytes = max(job.bytes_total - job.bytes_done, 0)
        remaining_items = max(job.items_total - job.items_done, 0)
        eta = None
        if job.paused:
            self.rows["Time remaining"].set_label("Paused")
        else:
            if self.speed > 0 and time.monotonic() - job.started > 1.5:
                eta = remaining_bytes / self.speed
            self.rows["Time remaining"].set_label(format_remaining(eta))
        self.rows["Items remaining"].set_label(f"{remaining_items:,} ({format_size(remaining_bytes)})")
        self.rows["Speed"].set_label(f"{format_size(int(self.speed))}/s" if self.speed else "—")


class ProgressWindow(Adw.Window):
    """One window listing every running operation, like Explorer's."""
    __gtype_name__ = "W11ProgressWindow"

    def __init__(self, app):
        # Not registered with the application, so a hidden progress window never
        # keeps the app running after the last Explorer window closes.
        super().__init__(default_width=480, resizable=False, hide_on_close=True)
        self.add_css_class("w11")
        self.app = app
        self.panels = {}
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_start=20,
                           margin_end=20, margin_top=8, margin_bottom=20)
        toolbar.set_content(self.box)
        self.set_content(toolbar)
        self._timer = 0
        app.jobs.connect("changed", lambda *a: self.sync())

    def sync(self):
        jobs = list(self.app.jobs.jobs)
        for job in list(self.panels):
            if job not in jobs:
                panel = self.panels.pop(job)
                sep = getattr(panel, "separator", None)
                if sep is not None:
                    self.box.remove(sep)
                self.box.remove(panel)
        for job in jobs:
            if job not in self.panels:
                if self.panels:
                    sep = Gtk.Separator()
                    self.box.append(sep)
                else:
                    sep = None
                panel = JobPanel(job)
                panel.separator = sep
                self.panels[job] = panel
                self.box.append(panel)
            else:
                self.panels[job].refresh()
        if not jobs:
            self.set_visible(False)
            self._stop_timer()
            return
        self._update_title()

    def _update_title(self):
        panels = list(self.panels.values())
        if len(panels) == 1:
            self.set_title(panels[0].percent.get_label())
        else:
            self.set_title(f"{len(panels)} operations running")

    def show_for(self, parent):
        if not self.panels:
            self.sync()
        if not self.panels:
            return
        if parent is not None:
            self.set_transient_for(parent)
        self.present()
        if not self._timer:
            self._timer = GLib.timeout_add(SAMPLE_MS, self._tick)

    def _tick(self):
        if not self.get_visible():
            self._timer = 0
            return False
        for panel in self.panels.values():
            panel.sample()
        self._update_title()
        return True

    def _stop_timer(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
