#!/usr/bin/env python3
"""
SoloCUPS Printer Center
A simple, zero-headache front end for CUPS, written with PyQt6.

It talks to CUPS through its standard command-line tools (lpstat, lpadmin,
lpinfo, lpoptions, lp, cancel, cupsctl, ipptool), so there is nothing to
install besides PyQt6 itself.

Arch / CachyOS setup:
    sudo pacman -S python-pyqt6 cups
    sudo systemctl enable --now cups
Optional, for finding Wi-Fi printers and driverless USB printers:
    sudo pacman -S avahi nss-mdns ipp-usb
    sudo systemctl enable --now avahi-daemon
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

from PyQt6.QtCore import (
    QFileSystemWatcher, QObject, QPointF, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QImage, QImageReader, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap,
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QStyledItemDelegate, QVBoxLayout,
    QWidget,
)

# --- CUPS layer -------------------------------------------------------------

AUTH_ERRORS = re.compile(
    r"forbidden|not.authorized|unauthorized|(permission|access) denied|insufficient permissions|authentication",
    re.I,
)


def run(cmd, admin=False, timeout=60):
    """Run a command. If it needs admin rights, retry once through pkexec."""
    def go(c):
        return subprocess.run(c, capture_output=True, text=True, timeout=timeout)

    result = go(cmd)
    if admin and result.returncode != 0 and AUTH_ERRORS.search(result.stderr or ""):
        pkexec = shutil.which("pkexec")
        exe = shutil.which(cmd[0])
        if pkexec and exe:
            result = go([pkexec, exe] + cmd[1:])
    return result


def error_text(result):
    if isinstance(result, Exception):
        return str(result)
    return (result.stderr or result.stdout or "Something went wrong.").strip()


def parse_printers(p_out, d_out, v_out):
    uris = {}
    for line in v_out.splitlines():
        m = re.match(r"device for (.+?): (.+)$", line.strip())
        if m:
            uris[m.group(1)] = m.group(2)

    dm = re.search(r"destination:\s*(\S+)", d_out)
    default = dm.group(1) if dm else None

    printers = []
    for line in p_out.splitlines():
        m = re.match(r"printer (\S+) (.*)$", line)
        if not m:
            continue
        name, rest = m.groups()
        low = rest.lower()
        if "now printing" in low:
            state = "Printing"
        elif "disabled" in low:
            state = "Paused"
        else:
            state = "Ready"
        printers.append({
            "name": name,
            "state": state,
            "uri": uris.get(name, ""),
            "default": name == default,
        })
    return printers


def get_printers():
    try:
        sched = run(["lpstat", "-r"])
    except FileNotFoundError:
        return {"error": "CUPS isn't installed.\nInstall it with: sudo pacman -S cups",
                "printers": []}
    if "not running" in sched.stdout.lower() or "not responding" in sched.stderr.lower():
        return {"error": "The CUPS service isn't running.\n"
                         "Turn it on with the switch at the top right.",
                "printers": []}
    p = run(["lpstat", "-p"]).stdout
    d = run(["lpstat", "-d"]).stdout
    v = run(["lpstat", "-v"]).stdout
    printers = parse_printers(p, d, v)
    for pr in printers:
        pr.update(get_supplies(pr["name"]))
    return {"error": None, "printers": printers}


JOB_RE = re.compile(r"^(\S+)-(\d+)\s+(\S+)\s+(\d+)\s+(.*)$")


def parse_jobs(out):
    """Parse `lpstat -o` output. With -l, indented lines describe the job
    above them; an Alerts line mentioning "hold" means the job is on hold."""
    jobs = []
    for line in out.splitlines():
        if line[:1].isspace():
            if jobs and "alerts:" in line.lower() and "hold" in line.lower():
                jobs[-1]["held"] = True
            continue
        m = JOB_RE.match(line.strip())
        if m:
            printer, num, user, size, date = m.groups()
            jobs.append({
                "id": f"{printer}-{num}",
                "printer": printer,
                "user": user,
                "kb": max(1, int(size) // 1024),
                "date": date.strip(),
                "held": False,
            })
    return jobs


def parse_keep_files(out):
    """True if cupsd keeps finished documents (needed to reprint them)."""
    for line in out.splitlines():
        key, _, val = line.partition("=")
        if key.strip() == "PreserveJobFiles":
            return val.strip().lower() not in ("no", "0", "false", "off")
    return False


def get_queue():
    """Active jobs, the last few finished jobs, and the keep-documents setting."""
    try:
        active = parse_jobs(run(["lpstat", "-W", "not-completed", "-l", "-o"]).stdout)
        done = parse_jobs(run(["lpstat", "-W", "completed", "-o"]).stdout)
        keep = parse_keep_files(run(["cupsctl"]).stdout)
    except FileNotFoundError:
        return {"jobs": [], "history": [], "keep": False}
    return {"jobs": active, "history": done[::-1][:10], "keep": keep}


# --- Service on/off ---

def cupsctl_value(out, key):
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def cups_state():
    """Is CUPS running, and is its web interface switched on?"""
    on = cups_active()
    web = False
    if on:
        value = cupsctl_value(run(["cupsctl"], timeout=8).stdout, "WebInterface")
        web = (value or "").lower() in ("yes", "true", "on", "1")
    return {"on": on, "web": web}


def cups_active():
    for unit in ("cups.service", "cups.socket"):
        if run(["systemctl", "is-active", unit], timeout=5).stdout.strip() == "active":
            return True
    return False


def set_cups_service(on):
    # The socket and path units restart the service on demand, so they have
    # to go along with it or "off" would not stay off.
    if on:
        cmd = ["systemctl", "enable", "--now", "cups.socket", "cups.service"]
    else:
        cmd = ["systemctl", "disable", "--now", "cups.socket", "cups.path", "cups.service"]
    return run(cmd, admin=True, timeout=120)


# --- Supplies (ink / toner) and alerts ---

ALERT_TEXT = {
    "toner-low": "Toner low", "toner-empty": "Out of toner",
    "marker-supply-low": "Supply low", "marker-supply-empty": "Out of supply",
    "media-low": "Paper low", "media-empty": "Out of paper",
    "media-jam": "Paper jam", "cover-open": "Cover open", "door-open": "Door open",
    "offline": "Printer offline", "connecting-to-device": "Connecting…",
}


def parse_supplies(text):
    """Pull ink/toner levels and warnings out of `ipptool -tv` output."""
    vals = {}
    for line in text.splitlines():
        m = re.match(r"\s*(marker-[a-z-]+|printer-state-reasons)\s+\(\S+\)\s+=\s*(.*)$", line)
        if m:
            vals[m.group(1)] = [x.strip() for x in m.group(2).split(",")]

    markers = []
    names = vals.get("marker-names", [])
    levels = vals.get("marker-levels", [])
    colors = vals.get("marker-colors", [])
    types = vals.get("marker-types", [])
    for i, name in enumerate(names):
        try:
            level = int(levels[i])
        except (IndexError, ValueError):
            continue
        if not 0 <= level <= 100 or "waste" in (types[i] if i < len(types) else ""):
            continue  # -1/-2/-3 mean unknown, and waste bins aren't ink
        color = colors[i] if i < len(colors) and colors[i].startswith("#") else ""
        markers.append({"name": name, "level": level, "color": color})

    alerts = []
    for reason in vals.get("printer-state-reasons", []):
        key = re.sub(r"-(warning|report|error)$", "", reason)
        if key in ALERT_TEXT and ALERT_TEXT[key] not in alerts:
            alerts.append(ALERT_TEXT[key])
    return {"markers": markers, "alerts": alerts}


_supply_cache = {}


def get_supplies(name):
    """Cached for 30s so the 4-second refresh doesn't keep asking the printer."""
    hit = _supply_cache.get(name)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    data = {"markers": [], "alerts": []}
    try:
        r = run(["ipptool", "-tv", f"ipp://localhost/printers/{name}",
                 "get-printer-attributes.test"], timeout=8)
        if r.returncode == 0:
            data = parse_supplies(r.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    _supply_cache[name] = (time.monotonic(), data)
    return data


# --- Per-printer options ---

OPTION_LABELS = {
    "PageSize": "Paper size", "media": "Paper size", "cupsPrintQuality": "Print quality",
    "print-quality": "Print quality", "ColorModel": "Color mode",
    "print-color-mode": "Color mode", "Duplex": "Two-sided printing",
    "sides": "Two-sided printing", "InputSlot": "Paper source",
    "MediaType": "Paper type", "OutputBin": "Output tray",
}


def pretty_value(v):
    v = re.sub(r"^Com\.(hp\.?)?", "", v).replace("_", " ")
    v = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", v)
    return v[:1].upper() + v[1:]


def parse_options(out):
    """Parse `lpoptions -p NAME -l`: 'Key/Label: a *b c' (* marks the default)."""
    options = []
    for line in out.splitlines():
        m = re.match(r"([^/:\s]+)(?:/([^:]*))?:\s+(.*)$", line)
        if not m:
            continue
        key, label, rest = m.groups()
        values, current = [], None
        for tok in rest.split():
            star = tok.startswith("*")
            tok = tok.lstrip("*")
            if tok.startswith("Custom"):
                continue
            values.append(tok)
            if star:
                current = tok
        if len(values) >= 2:
            options.append({
                "key": key,
                "label": OPTION_LABELS.get(key) or label or key,
                "values": values,
                "current": current or values[0],
            })
    return options


def get_options(name):
    return parse_options(run(["lpoptions", "-p", name, "-l"]).stdout)


def parse_devices(out):
    records, cur = [], None
    for line in out.splitlines():
        if line.startswith("Device:"):
            cur = {}
            records.append(cur)
            m = re.search(r"uri = (.*)", line)
            cur["uri"] = m.group(1).strip() if m else ""
        elif cur is not None and "=" in line:
            key, _, val = line.partition("=")
            cur[key.strip()] = val.strip()

    devices, seen = [], set()
    for d in records:
        uri = d.get("uri", "")
        # Skip the generic backend placeholders ("socket", "lpd", ...)
        if "://" not in uri or uri in seen or d.get("class") == "file":
            continue
        scheme = uri.split("://", 1)[0]
        if scheme in ("cups-brf", "file"):
            continue
        seen.add(uri)
        kind = "USB" if scheme == "usb" else "Network"
        info = d.get("info") or d.get("make-and-model") or uri
        devices.append({
            "uri": uri,
            "info": info,
            "make": d.get("make-and-model") or info,
            "kind": kind,
        })
    devices.sort(key=lambda x: (x["kind"] != "USB", x["info"].lower()))
    return devices


def scan_devices():
    r = run(["lpinfo", "-l", "-v"], admin=True, timeout=120)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError(error_text(r))
    return parse_devices(r.stdout)


LOG_CMD = ["journalctl", "-u", "cups.service", "-n", "300", "--no-pager", "-o", "short"]
PROBLEM_RE = re.compile(
    r"\b(error|errors|fail|failed|failure|warn|warning|denied|unable|cannot|"
    r"critical|timed out|stopped|disabled)\b", re.I)
PAGE_RANGE_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")


def read_cups_log():
    """Recent CUPS log lines. Falls back to an admin prompt if the journal
    isn't readable by this user."""
    r = run(LOG_CMD)
    text = r.stdout.strip()
    unreadable = r.returncode != 0 or not text or "not seeing messages" in (text + r.stderr)
    if unreadable and shutil.which("pkexec"):
        r2 = run(["pkexec"] + LOG_CMD)
        if r2.returncode == 0 and r2.stdout.strip():
            return r2.stdout.strip()
    return text or error_text(r)


def collect_diagnostics():
    parts = ["SoloCUPS diagnostics"]
    try:
        parts.append("CUPS version: " + run(["cups-config", "--version"]).stdout.strip())
    except OSError:
        pass
    try:
        parts.append("\n--- lpstat -t ---\n" + run(["lpstat", "-t"]).stdout.strip())
    except OSError:
        pass
    parts.append("\n--- CUPS log (last 100 lines) ---\n"
                 + "\n".join(read_cups_log().splitlines()[-100:]))
    return "\n".join(parts)


def connection_kind(uri):
    """A plain-English connection type for a CUPS device address."""
    scheme = uri.split(":", 1)[0].lower()
    if scheme == "usb":
        return "USB printer"
    if scheme in ("ipp", "ipps", "http", "https", "dnssd", "socket", "lpd",
                  "implicitclass"):
        return "Network printer"
    return "Local printer" if uri else "Connection unknown"


def sanitize_name(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:40]


def test_page_path():
    for p in ("/usr/share/cups/data/testprint",
              "/usr/share/cups/data/default-testpage.pdf"):
        if os.path.exists(p):
            return p
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="solocups-test-")
    with os.fdopen(fd, "w") as f:
        f.write("SoloCUPS Printer Center\n\nIf you can read this, your printer works.\n")
    return path


# --- Threading helper -------------------------------------------------------

class Bridge(QObject):
    """Carries results from worker threads back to the GUI thread."""
    done = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self.done.connect(self._dispatch)

    def _dispatch(self, callback, result):
        try:
            callback(result)
        except RuntimeError:
            pass  # the window that asked was replaced (theme change) while this ran


_bridge = None


def run_async(fn, callback):
    def work():
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - handed to the callback
            result = exc
        _bridge.done.emit(callback, result)

    threading.Thread(target=work, daemon=True).start()


# --- Themes -----------------------------------------------------------------
# SoloCUPS wears the Obscura look: sharp corners, hairline borders, warm ink on
# near-black (or the reverse), and the accent colour picked in System Settings.
# Every colour in the app comes from T, a table of roles that palette() builds
# from the tokens below.
#   bg / surface / card / field   are the backgrounds
#   primary, tonal, container     are the accent, its quiet variant and its badge
#   ok_*, bad_*, alert_*          are status chips and stay readable in any theme

APP_NAME = "SoloCUPS"
DEFAULT_ACCENT = "#ef3b47"   # the red of the SoloCUPS cup
DEFAULT_ACCENT2 = "#b81d2e"  # the deep end of the cup's gradient

TOKENS = {
    # Near-black with soft greys; the accent is the cup's red gradient (or a wallpaper colour).
    "dark": {"bg": "#0c0c0f", "raised": "#16161b", "field": "#101013", "ink": "#f3f3f5",
             "ok": "#5fd18b", "warn": "#f5b45c", "bad": "#ff8a80"},
    "light": {"bg": "#f1f0f0", "raised": "#ffffff", "field": "#f7f6f6", "ink": "#18181c",
              "ok": "#0d7a4a", "warn": "#a85a00", "bad": "#c0182a"},
}

THEMES = {
    "system": {"label": "Match system", "hint": "Follows Plasma's light or dark mode"},
    "default": {"label": "Default", "hint": "Black and grey with the cup's red gradient"},
    "light": {"label": "Light", "hint": "Soft grey and white with the same red"},
    "wallpaper": {"label": "Wallpaper", "hint": "Dark, with its colour taken from your wallpaper"},
}

def mix(a, b, t):
    """Blend colour a toward b: t=0 gives a, t=1 gives b."""
    a, b = QColor(a), QColor(b)
    return QColor(*(round(x + (y - x) * t) for x, y in
                    zip(a.getRgb()[:3], b.getRgb()[:3]))).name()


def contrast(a, b):
    def lum(c):
        def ch(v):
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b_ = QColor(c).getRgb()[:3]
        return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b_)
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def system_variant():
    scheme = QApplication.styleHints().colorScheme()
    return "light" if scheme == Qt.ColorScheme.Light else "dark"


# --- Wallpaper colours --------------------------------------------------------

APPLETS_RC = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                          "plasma-org.kde.plasma.desktop-appletsrc")
WALLPAPER_PLUGINS = ("org.kde.image", "org.obscura.diamond.wallpaper")


def wallpaper_file():
    """The picture on the first desktop that has one (read from Plasma's own config), or ''."""
    try:
        text = open(APPLETS_RC).read()
    except OSError:
        return ""
    plugin, images, group = {}, {}, None
    for line in text.splitlines():
        if line.startswith("["):
            group = re.fullmatch(r"\[Containments\]\[(\d+)\](?:\[Wallpaper\]\[([\w.]+)\]\[General\])?", line)
            continue
        if not group:
            continue
        cid, plug = group.group(1), group.group(2)
        if plug is None and line.startswith("wallpaperplugin="):
            plugin[cid] = line.split("=", 1)[1]
        elif plug and line.startswith("Image="):
            images[(cid, plug)] = line[6:]
    for cid in sorted(plugin, key=int):
        path = images.get((cid, plugin[cid]), "")
        if plugin[cid] in WALLPAPER_PLUGINS and path:
            path = path.removeprefix("file://").split("#")[0].rstrip("/")
            if os.path.isdir(path):  # an installed wallpaper package: use its largest picture
                pics = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(path, "contents", "images"))
                        for f in fs]
                path = max(pics, key=os.path.getsize, default="")
            if os.path.isfile(path):
                return path
    return ""


_wall_cache = {}


def wallpaper_colors():
    """(accent, deeper accent) from the wallpaper: the most colourful hue in it, lifted so it reads on
    black. Falls back to the cup's red for grey or missing wallpapers."""
    path = wallpaper_file()
    if not path:
        return DEFAULT_ACCENT, DEFAULT_ACCENT2
    key = (path, os.path.getmtime(path))
    if key in _wall_cache:
        return _wall_cache[key]
    result = (DEFAULT_ACCENT, DEFAULT_ACCENT2)
    reader = QImageReader(path)
    size = reader.size()
    if size.isValid():
        reader.setScaledSize(size.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio))
    img = reader.read()
    if not img.isNull():
        img = img.convertToFormat(QImage.Format.Format_RGB32)
        bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(24)]  # weight, sum of r, g, b
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                h, sat, val, _a = c.getHsvF()
                if h < 0 or sat < 0.25 or val < 0.22:
                    continue
                w = sat * val
                b = bins[int(h * 24) % 24]
                b[0] += w
                b[1] += c.redF() * w
                b[2] += c.greenF() * w
                b[3] += c.blueF() * w
        best = max(bins, key=lambda b: b[0])
        if best[0] > 0.5:
            base = QColor.fromRgbF(best[1] / best[0], best[2] / best[0], best[3] / best[0])
            h, sat, val, _a = base.getHsvF()
            sat = max(sat, 0.58)
            val = min(max(val, 0.72), 0.96)
            accent = QColor.fromHsvF(h, sat, val)
            deep = QColor.fromHsvF((h - 0.035) % 1.0, min(sat + 0.1, 1.0), val * 0.7)
            result = (accent.name(), deep.name())
    _wall_cache[key] = result
    return result


def palette(variant, accent, accent2, tint=0.0):
    k = TOKENS[variant]
    dark = variant == "dark"
    ink = k["ink"]
    bg = mix(k["bg"], accent, tint)
    raised = mix(k["raised"], accent, tint * 1.5)
    field = mix(k["field"], accent, tint)
    line = mix(bg, ink, 0.10)
    box = mix(raised, ink, 0.08)
    # Accent as text: lifted on dark, deepened on light, so it stays readable.
    accent_text = mix(accent, "#ffffff", 0.3) if dark else mix(accent, "#000000", 0.3)
    on_accent = "#ffffff" if contrast("#ffffff", accent) >= 3.3 else "#101014"
    return {
        "bg": bg, "border": line, "panel": bg, "ember": accent_text,
        "surface": raised, "card": raised, "card_border": box, "field": field,
        "primary": accent, "primary2": accent2,
        "primary_hover": mix(accent, "#ffffff", 0.14), "primary2_hover": mix(accent2, "#ffffff", 0.14),
        "primary_pressed": mix(accent, bg, 0.2), "primary2_pressed": mix(accent2, bg, 0.2),
        "on_primary": on_accent, "focus": accent, "accent_text": accent_text,
        "tonal": mix(bg, ink, 0.09), "tonal_hover": mix(bg, ink, 0.14),
        "tonal_pressed": mix(bg, ink, 0.2), "on_tonal": ink,
        "container": mix(raised, accent, 0.26 if dark else 0.16), "on_container": accent_text,
        "error": k["bad"], "text": ink, "text2": mix(bg, ink, 0.78), "muted": mix(bg, ink, 0.6),
        "placeholder": mix(bg, ink, 0.42), "outline": mix(bg, ink, 0.22), "outline_var": line,
        "tab_hover": mix(bg, ink, 0.06), "scroll_hover": mix(bg, ink, 0.45),
        "snack_bg": ink, "snack_fg": bg,
        "ok_fg": k["ok"], "ok_bg": mix(bg, k["ok"], 0.12),
        "bad_fg": k["warn"], "bad_bg": mix(bg, k["warn"], 0.12),
        "alert_fg": k["bad"], "alert_bg": mix(bg, k["bad"], 0.14),
        "disabled_icon": mix(bg, ink, 0.3),
    }


def resolve(name):
    """The colour table a theme setting stands for right now."""
    if name == "wallpaper":
        accent, accent2 = wallpaper_colors()
        return palette("dark", accent, accent2, tint=0.07)
    variant = system_variant() if name == "system" else name
    if variant == "default":
        variant = "dark"
    return palette(variant, DEFAULT_ACCENT, DEFAULT_ACCENT2)


# The active theme's colours. Updated in place by set_theme(), so any code
# that reads T["primary"] always sees the current value.
T = {}


def set_theme(name):
    if name not in THEMES:
        name = "system"
    T.clear()
    T.update(resolve(name))
    SETTINGS["theme"] = name


# --- Saved settings ---------------------------------------------------------

CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG_PATH = os.path.join(CONFIG_HOME, "solocups", "settings.json")
OLD_CONFIG_PATH = os.path.join(CONFIG_HOME, "solocup", "settings.json")  # from before the rename
DEFAULT_WEB_URL = "http://localhost:631"
SETTINGS = {"theme": "system", "refresh_secs": 4, "web_url": DEFAULT_WEB_URL}
REFRESH_CHOICES = [(2, "Every 2 seconds"), (4, "Every 4 seconds"),
                   (10, "Every 10 seconds"), (30, "Every 30 seconds")]


def load_settings():
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else OLD_CONFIG_PATH
    try:
        with open(path) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            theme = {"dark": "default"}.get(saved.get("theme"), saved.get("theme"))  # Diamond became Default
            if theme in THEMES:  # the old colour themes fall back to "system"
                SETTINGS["theme"] = theme
            if saved.get("refresh_secs") in [c[0] for c in REFRESH_CHOICES]:
                SETTINGS["refresh_secs"] = saved["refresh_secs"]
            if isinstance(saved.get("web_url"), str) and saved["web_url"].startswith(
                    ("http://", "https://")):
                SETTINGS["web_url"] = saved["web_url"]
    except (OSError, ValueError):
        pass


def save_settings():
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(SETTINGS, f, indent=2)
    except OSError:
        pass  # a read-only home just means the choice isn't remembered


# --- Stylesheet -------------------------------------------------------------
# @name@ is replaced with that theme colour; @name/31@ is the colour at an
# alpha of 31 out of 255.

QSS_TEMPLATE = """
* {
    color: @text@;
    font-family: "Inter", "Noto Sans", "Cantarell", sans-serif;
    font-size: 14px;
}
QFrame#root, QDialog, QMessageBox { background: @bg@; }
QFrame#shell { background: transparent; }
QFrame#header, QFrame#tabbar, QFrame#panel { background: @bg@; border: none; }
QFrame#card, QFrame#empty { background: transparent; border: none; }  /* Card paints these */

QLabel { background: transparent; }
QLabel#title { font-size: 24px; font-weight: 600; }
QLabel#subtitle { color: @muted@; }
QLabel#h2 { font-size: 19px; font-weight: 600; }
QLabel#cardTitle { font-size: 15px; font-weight: 600; }
QLabel#emptyTitle { font-size: 20px; font-weight: 600; }
QLabel#muted { color: @muted@; }
QLabel#eyebrow { color: @muted@; font-size: 11px; font-weight: 600; }
QLabel#snack { background: @snack_bg@; color: @snack_fg@; padding: 7px 14px; border-radius: 14px; font-size: 12px; }

/* Buttons: gradient accent (default), soft grey, text, danger, tab pill */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 @primary@, stop:1 @primary2@);
    color: @on_primary@; font-weight: 600;
    border: 2px solid transparent; border-radius: 18px; padding: 0 20px;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 @primary_hover@, stop:1 @primary2_hover@);
}
QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 @primary_pressed@, stop:1 @primary2_pressed@);
}
QPushButton:focus { border: 2px solid @text@; }
QPushButton:disabled { background: @text/31@; color: @text/97@; }

QPushButton#tonal {
    background: @tonal@; color: @text@; font-weight: 500; padding: 0 16px;
}
QPushButton#tonal:hover { background: @tonal_hover@; }
QPushButton#tonal:pressed { background: @tonal_pressed@; }
QPushButton#tonal:focus { border: 2px solid @primary@; }

QPushButton#text, QPushButton#danger {
    background: transparent; padding: 0 14px; font-weight: 500;
}
QPushButton#text { color: @accent_text@; }
QPushButton#text:hover { background: @primary/31@; }
QPushButton#text:pressed { background: @primary/56@; }
QPushButton#text:focus { border: 2px solid @primary@; }
QPushButton#danger { color: @error@; }
QPushButton#danger:hover { background: @error/31@; }
QPushButton#danger:pressed { background: @error/56@; }
QPushButton#danger:focus { border: 2px solid @error@; }

QPushButton#tonal:disabled, QPushButton#text:disabled, QPushButton#danger:disabled {
    background: transparent; color: @text/97@;
}

QPushButton#tab {
    background: transparent; color: @muted@; font-size: 13px; font-weight: 600;
    border: 2px solid transparent; border-radius: 17px; padding: 0 18px;
}
QPushButton#tab:hover { background: @tab_hover@; color: @text@; }
QPushButton#tab:checked { background: @container@; color: @text@; }
QPushButton#tab:focus { border: 2px solid @primary@; }

QMessageBox QPushButton { min-width: 72px; min-height: 34px; }

/* Fields */
QLineEdit, QComboBox {
    background: @field@; border: 1px solid @outline_var@; border-radius: 12px;
    padding: 0 14px; min-height: 40px;
}
QLineEdit {
    selection-background-color: @primary@; selection-color: @on_primary@;
    placeholder-text-color: @placeholder@;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid @primary@; }
QLineEdit:hover, QComboBox:hover { border-color: @muted@; }
QComboBox::drop-down { border: none; width: 36px; }
QComboBox::down-arrow { image: none; }
QComboBoxPrivateContainer { background: @card@; border: none; padding: 0; }
QComboBox QAbstractItemView {
    background: @card@; border: 1px solid @outline@; border-radius: 10px;
    selection-background-color: @container@; selection-color: @text@;
    outline: none; padding: 4px;
}
QComboBox QAbstractItemView::item { min-height: 30px; padding: 0 10px; border-radius: 8px; }
QComboBox QAbstractItemView::item:selected { background: @container@; color: @text@; }

QPlainTextEdit {
    background: @field@; border: 1px solid @outline_var@; border-radius: 12px;
    padding: 8px; selection-background-color: @primary@; selection-color: @on_primary@;
    font-family: "Geist Mono", monospace; font-size: 12px;
}

QListWidget {
    background: @field@; border: 1px solid @outline_var@; border-radius: 12px;
    padding: 4px; outline: none;
}
QListWidget::item { padding: 6px 12px; border-radius: 8px; }
QListWidget::item:hover:!selected { background: @tonal@; }
QListWidget::item:selected { background: @container@; color: @text@; }

QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 0; }
QScrollBar::handle:vertical {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 @primary@, stop:1 @primary2@);
    min-height: 36px; border-radius: 4px;
}
QScrollBar::handle:vertical:hover { background: @primary_hover@; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QToolTip { background: @snack_bg@; color: @snack_fg@; border: none; padding: 4px 8px; }
"""


def build_qss():
    def sub(m):
        color = QColor(T[m.group(1)])
        if m.group(2):
            return f"rgba({color.red()},{color.green()},{color.blue()},{m.group(2)})"
        return T[m.group(1)]
    return re.sub(r"@(\w+)(?:/(\d+))?@", sub, QSS_TEMPLATE)


# --- Icons ------------------------------------------------------------------
# Lucide icons (https://lucide.dev, ISC license), embedded so the app stays a
# single file. Each is the inside of a 24x24 outline SVG; icon() draws it
# in whatever colour a button needs.

ICONS = {
    "globe": (
        '<circle cx="12" cy="12" r="10" /> <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20" /> <path d="M2 12h20" />'),
    "circle-help": (
        '<circle cx="12" cy="12" r="10" /> <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /> <path d="M12 17h.01" />'),
    "minus": (
        '<path d="M5 12h14" />'),
    "chevron-down": (
        '<path d="m6 9 6 6 6-6" />'),
    "chevron-up": (
        '<path d="m18 15-6-6-6 6" />'),
    "copy": (
        '<rect width="14" height="14" x="8" y="8" rx="2" ry="2" /> <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />'),
    "check": (
        '<path d="M20 6 9 17l-5-5" />'),
    "circle-x": (
        '<circle cx="12" cy="12" r="10" /> <path d="m15 9-6 6" /> <path d="m9 9 6 6" />'),
    "file-text": (
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" /> <path d="M14 2v4a2 2 0 0 0 2 2h4" /> <path d="M10 9H8" /> <path d="M16 13H8" /> <path d="M16 17H8" />'),
    "file-up": (
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" /> <path d="M14 2v4a2 2 0 0 0 2 2h4" /> <path d="M12 12v6" /> <path d="m15 15-3-3-3 3" />'),
    "list-checks": (
        '<path d="m3 17 2 2 4-4" /> <path d="m3 7 2 2 4-4" /> <path d="M13 6h8" /> <path d="M13 12h8" /> <path d="M13 18h8" />'),
    "list": (
        '<path d="M3 12h.01" /> <path d="M3 18h.01" /> <path d="M3 6h.01" /> <path d="M8 12h13" /> <path d="M8 18h13" /> <path d="M8 6h13" />'),
    "pause": (
        '<rect x="14" y="4" width="4" height="16" rx="1" /> <rect x="6" y="4" width="4" height="16" rx="1" />'),
    "play": (
        '<polygon points="6 3 20 12 6 21 6 3" />'),
    "plus": (
        '<path d="M5 12h14" /> <path d="M12 5v14" />'),
    "power": (
        '<path d="M12 2v10" /> <path d="M18.4 6.6a9 9 0 1 1-12.77.04" />'),
    "printer": (
        '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2" /> <path d="M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6" /> <rect x="6" y="14" width="12" height="8" rx="1" />'),
    "refresh-cw": (
        '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" /> <path d="M21 3v5h-5" /> <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" /> <path d="M8 16H3v5" />'),
    "rotate-ccw": (
        '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /> <path d="M3 3v5h5" />'),
    "settings-2": (
        '<path d="M20 7h-9" /> <path d="M14 17H5" /> <circle cx="17" cy="17" r="3" /> <circle cx="7" cy="7" r="3" />'),
    "sliders-horizontal": (
        '<line x1="21" x2="14" y1="4" y2="4" /> <line x1="10" x2="3" y1="4" y2="4" /> <line x1="21" x2="12" y1="12" y2="12" /> <line x1="8" x2="3" y1="12" y2="12" /> <line x1="21" x2="16" y1="20" y2="20" /> <line x1="12" x2="3" y1="20" y2="20" /> <line x1="14" x2="14" y1="2" y2="6" /> <line x1="8" x2="8" y1="10" y2="14" /> <line x1="16" x2="16" y1="18" y2="22" />'),
    "star": (
        '<path d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z" />'),
    "trash-2": (
        '<path d="M3 6h18" /> <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /> <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" /> <line x1="10" x2="10" y1="11" y2="17" /> <line x1="14" x2="14" y1="11" y2="17" />'),
    "x": (
        '<path d="M18 6 6 18" /> <path d="m6 6 12 12" />'),
}


def icon(name, color, size=18, filled=False):
    fill = color if filled else "none"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="{fill}" '
           f'stroke="{color}" stroke-width="2" stroke-linecap="round" '
           f'stroke-linejoin="round">{ICONS[name]}</svg>').encode()
    renderer = QSvgRenderer(svg)
    scale = 2  # render at 2x so it stays sharp on HiDPI screens
    pix = QPixmap(size * scale, size * scale)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    renderer.render(p)
    p.end()
    pix.setDevicePixelRatio(scale)
    return pix


def icon_colors(kind):
    """Icon colour for a button style: (normal, checked, disabled)."""
    normal = {None: T["on_primary"], "tonal": T["on_tonal"], "text": T["accent_text"],
              "danger": T["error"], "tab": T["muted"]}[kind]
    checked = T["text"] if kind == "tab" else normal
    return normal, checked, T["disabled_icon"]


# --- Small widgets ----------------------------------------------------------

def normalize_web_url(text):
    """Turn what someone typed into a web address. Empty means this computer."""
    text = text.strip().rstrip("/")
    if not text:
        return DEFAULT_WEB_URL
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    return text if urlparse(text).hostname else DEFAULT_WEB_URL


def is_local_url(url):
    return urlparse(url).hostname in ("localhost", "127.0.0.1", "::1")


def icon_button(icon_name, tooltip, on_click, size=40):
    """A square, icon-only outlined button."""
    b = button("", "tonal", on_click, size, icon_name)
    b.setFixedWidth(size)
    b.setStyleSheet("padding: 0;")
    b.setToolTip(tooltip)
    b.setAccessibleName(tooltip)
    return b


def clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w:
            w.setParent(None)
            w.deleteLater()
        elif item.layout():
            clear(item.layout())  # nested rows would otherwise leak their widgets


def button(text, kind=None, on_click=None, height=40, icon_name=None):
    """kind: None (filled), 'tonal', 'text', 'danger' or 'tab'."""
    b = QPushButton(f"  {text}" if icon_name and text else text)  # spaces give the icon breathing room
    if icon_name:
        normal, checked, disabled = icon_colors(kind)
        ic = QIcon()
        ic.addPixmap(icon(icon_name, normal), QIcon.Mode.Normal, QIcon.State.Off)
        ic.addPixmap(icon(icon_name, checked), QIcon.Mode.Normal, QIcon.State.On)
        ic.addPixmap(icon(icon_name, disabled), QIcon.Mode.Disabled)
        b.setIcon(ic)
        b.setIconSize(QSize(18, 18))
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    # Show the focus ring for keyboard users only, not after every mouse click.
    b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    b.setFixedHeight(height)
    if kind:
        b.setObjectName(kind)
    if on_click:
        b.clicked.connect(on_click)
    return b


def chip_style(fg, bg):
    """A small pill tag: tinted fill, no outline."""
    return (f"color: {fg}; background: {bg}; border-radius: 10px; padding: 3px 10px; "
            "font-size: 11px; font-weight: 600;")


def chip(text, fg, bg):
    label = QLabel(text)
    label.setStyleSheet(chip_style(fg, bg))
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    return label


RADIUS = 16


class Card(QFrame):
    """A soft rounded card: raised fill with a faint outline (QSS can't fill and outline this cleanly)."""

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(T["card_border"]), 1))
        p.setBrush(QColor(T["card"]))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        super().paintEvent(event)


def gradient(rect, horizontal=True):
    g = QLinearGradient(rect.topLeft(), rect.topRight() if horizontal else rect.bottomLeft())
    g.setColorAt(0, QColor(T["primary"]))
    g.setColorAt(1, QColor(T["primary2"]))
    return g


def eyebrow(text):
    """A spaced-out mono caption, the Obscura way of labelling a section."""
    label = QLabel(text.upper())
    label.setObjectName("eyebrow")
    font = label.font()
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
    label.setFont(font)
    return label


def default_badge():
    """Where "Make default" was: a solid star that just says Default."""
    b = button("Default", "text", height=36)
    b.setIcon(QIcon(icon("star", T["accent_text"], filled=True)))
    b.setIconSize(QSize(18, 18))
    b.setText("  Default")
    b.setCursor(Qt.CursorShape.ArrowCursor)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setStyleSheet("QPushButton:hover, QPushButton:pressed { background: transparent; }")
    b.setToolTip("This is the printer your apps use unless you pick another")
    return b


class IconBadge(QWidget):
    """A round tile with a small printer drawn inside it."""

    def __init__(self, size=48, bg=None, fg=None):
        super().__init__()
        self.setFixedSize(size, size)
        self.bg = QColor(bg or T["container"])
        self.fg = QColor(fg or T["on_container"])

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.bg)
        p.drawEllipse(self.rect())

        # Draw the icon on a 24-unit grid, centred at half the badge size.
        p.translate(self.width() * 0.25, self.height() * 0.25)
        scale = self.width() * 0.5 / 24
        p.scale(scale, scale)

        pen = QPen(self.fg)
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(3, 8, 18, 9), 2, 2)          # body
        p.drawRoundedRect(QRectF(7, 3, 10, 5), 1, 1)          # paper in
        p.setBrush(self.bg)                                   # paper out hides
        p.drawRoundedRect(QRectF(7, 13, 10, 8), 1, 1)         # the body edge
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.fg)
        p.drawEllipse(QRectF(16.8, 10.3, 2.4, 2.4))           # status light


class AppMark(QWidget):
    """The SoloCUPS cup, drawn in the live accent colour."""

    SVG = ("""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512"><defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#26262d"/><stop offset="1" stop-color="#15151a"/></linearGradient><linearGradient id="r" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#d62839"/><stop offset=".5" stop-color="#ef3b47"/><stop offset="1" stop-color="#b81d2e"/></linearGradient></defs><rect width="512" height="512" rx="112" fill="url(#bg)"/><path d="M132 176 L380 176 L346 436 Q344 452 328 452 L184 452 Q168 452 166 436 Z" fill="url(#r)"/><rect x="112" y="146" width="288" height="44" rx="14" fill="#ff6b6b"/><rect x="150" y="232" width="212" height="34" rx="17" fill="#2a0b0e"/><rect x="166" y="244" width="180" height="8" rx="4" fill="#5a1a20"/><rect x="162" y="314" width="188" height="74" rx="14" fill="#a8161f"/><circle cx="204" cy="351" r="14" fill="#3dff9a"/><circle cx="204" cy="351" r="22" fill="#3dff9a" opacity=".2"/><rect x="248" y="338" width="78" height="26" rx="13" fill="#ffd0d0"/></svg>""")

    def __init__(self, size=48):
        super().__init__()
        self.setFixedSize(size, size)
        self.renderer = QSvgRenderer(self.SVG.encode())

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.renderer.render(p, QRectF(self.rect()))


class Dropdown(QComboBox):
    """A combo box with a visible chevron so it reads as a menu."""

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # The default delegate ignores ::item rules in the stylesheet.
        self.setItemDelegate(QStyledItemDelegate(self))

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(T["text2"] if self.isEnabled() else T["outline"]), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        p.setPen(pen)
        cx, cy = self.width() - 24, self.height() / 2
        p.drawPolyline([QPointF(cx - 5, cy - 2.5), QPointF(cx, cy + 2.5),
                        QPointF(cx + 5, cy - 2.5)])


class Switch(QAbstractButton):
    """Pill on/off switch. Emits clicked(bool) only for user clicks,
    so setChecked() from a refresh never triggers an action."""

    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(46, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        on = self.isChecked()
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(gradient(track))
        else:
            p.setPen(QPen(QColor(T["outline"]), 1))
            p.setBrush(QColor(T["field"]))
        p.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T["on_primary"] if on else T["muted"]))
        knob = 18
        x = self.width() - knob - 4 if on else 4
        p.drawEllipse(QRectF(x, (self.height() - knob) / 2, knob, knob))
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(T["text"]), 2))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)


class SupplyBar(QWidget):
    """A thin progress bar for one ink or toner level."""

    def __init__(self, level, color):
        super().__init__()
        self.level = level
        c = QColor(color) if color else QColor(T["primary"])
        dark_theme = QColor(T["card"]).lightnessF() < 0.5
        if dark_theme and c.lightnessF() < 0.3:  # black ink would vanish on a dark card
            c = QColor(T["text2"])
        elif not dark_theme and c.lightnessF() > 0.8:  # and white/yellow on a light one
            c = QColor(T["muted"])
        self.fill = QColor(T["error"]) if level <= 10 else c
        self.setFixedHeight(6)
        self.setMinimumWidth(80)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T["tonal_pressed"]))
        r = self.height() / 2
        p.drawRoundedRect(QRectF(self.rect()), r, r)
        w = int(self.width() * self.level / 100)
        if w > 0:
            p.setBrush(self.fill)
            p.drawRoundedRect(QRectF(0, 0, max(w, self.height()), self.height()), r, r)


class ElidedLabel(QLabel):
    """A one-line label that shortens itself with an ellipsis instead of
    forcing the window wider."""

    def __init__(self, text=""):
        super().__init__(text)
        self._full = text
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        shown = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, max(self.width(), 20))
        self.setText(shown)


class EmptyCard(Card):
    """Friendly message card used for empty and error states."""

    def __init__(self, title, body, button_text=None, on_click=None):
        super().__init__()
        self.setObjectName("empty")
        v = QVBoxLayout(self)
        v.setContentsMargins(32, 44, 32, 44)
        v.setSpacing(8)
        v.addWidget(IconBadge(72), alignment=Qt.AlignmentFlag.AlignCenter)
        v.addSpacing(10)
        t = QLabel(title)
        t.setObjectName("emptyTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        b = QLabel(body)
        b.setObjectName("muted")
        b.setAlignment(Qt.AlignmentFlag.AlignCenter)
        b.setWordWrap(True)
        v.addWidget(t)
        v.addWidget(b)
        if button_text:
            v.addSpacing(20)
            btn = button(button_text, on_click=on_click, icon_name="plus")
            btn.setFixedWidth(170)
            v.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)


class ScrollPage(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self.v = QVBoxLayout(body)
        self.v.setContentsMargins(14, 14, 14, 14)
        self.v.setSpacing(12)
        self.v.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(body)
        self._last = None

    def unchanged(self, data):
        """True if data matches the last render (avoids needless rebuilds)."""
        key = repr(data)
        if key == self._last:
            return True
        self._last = key
        return False


class PrinterCard(Card):
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        paths = [x for x in paths if os.path.isfile(x)]
        if paths:
            event.acceptProposedAction()
            self.app.print_file(self.name, paths)

    @staticmethod
    def state_colors(state):
        """(text colour, chip background) for a printer state."""
        return {
            "Ready": (T["ok_fg"], T["ok_bg"]),
            "Printing": (T["on_container"], T["container"]),
            "Paused": (T["bad_fg"], T["bad_bg"]),
        }.get(state, (T["text2"], T["tonal"]))

    def __init__(self, p, app):
        super().__init__()
        self.setObjectName("card")
        self.setAcceptDrops(True)  # drop files here to print them
        self.app = app
        name = self.name = p["name"]

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 14)
        v.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(IconBadge(48))

        info = QVBoxLayout()
        info.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel(name.replace("_", " "))
        title.setObjectName("cardTitle")
        row.addWidget(title)
        fg, bg = self.state_colors(p["state"])
        row.addWidget(chip(p["state"], fg, bg))
        for text in p.get("alerts", []):
            row.addWidget(chip(text, T["alert_fg"], T["alert_bg"]))
        row.addStretch()
        info.addLayout(row)
        kind = QLabel(connection_kind(p["uri"]))
        kind.setObjectName("muted")
        kind.setToolTip("Open Options to see the connection address")
        info.addWidget(kind)
        top.addLayout(info, 1)
        v.addLayout(top)

        for m in p.get("markers", []):
            sr = QHBoxLayout()
            sr.setSpacing(10)
            sr.addSpacing(64)
            full = pretty_value(m["name"])
            name_lbl = QLabel()
            name_lbl.setObjectName("muted")
            name_lbl.setFixedWidth(170)
            name_lbl.setToolTip(full)
            name_lbl.setText(name_lbl.fontMetrics().elidedText(
                full, Qt.TextElideMode.ElideRight, 170))
            sr.addWidget(name_lbl)
            sr.addWidget(SupplyBar(m["level"], m["color"]), 1)
            pct = QLabel(f"{m['level']}%")
            pct.setObjectName("muted")
            pct.setFixedWidth(40)
            pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            sr.addWidget(pct)
            v.addLayout(sr)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addSpacing(64)  # lines the buttons up with the text above
        actions.addWidget(button("Test page", "tonal", lambda: app.print_test(name), 36, "file-text"))
        actions.addWidget(button("Print file", "tonal", lambda: app.print_file(name), 36, "file-up"))
        actions.addWidget(button("Options", "tonal", lambda: app.edit_options(name, p["uri"]), 36,
                                 "sliders-horizontal"))
        if p["default"]:
            actions.addWidget(default_badge())
        else:
            actions.addWidget(button("Make default", "text",
                                     lambda: app.make_default(name), 36, "star"))
        actions.addStretch()
        actions.addWidget(button("Remove", "danger", lambda: app.remove_printer(name), 36, "trash-2"))
        v.addLayout(actions)


# --- Pages ------------------------------------------------------------------

class PrintersPage(ScrollPage):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def refresh(self):
        run_async(get_printers, self._render)

    def _render(self, res):
        if isinstance(res, Exception):
            res = {"error": str(res), "printers": []}
        if self.unchanged(res):
            return
        clear(self.v)
        if res["error"]:
            self.v.addWidget(EmptyCard("Can't reach CUPS", res["error"]))
        elif not res["printers"]:
            self.v.addWidget(EmptyCard(
                "No Printers Added Yet",
                "Click the 'Add Printer' tab above to automatically scan "
                "for your USB or Wi-Fi printer.",
                "Add Printer", self.app.go_add))
        else:
            for p in res["printers"]:
                self.v.addWidget(PrinterCard(p, self.app))


class QueuePage(ScrollPage):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def refresh(self):
        run_async(get_queue, self._render)

    def _render(self, q):
        if isinstance(q, Exception):
            q = {"jobs": [], "history": [], "keep": False}
        if self.unchanged(q):
            return
        clear(self.v)
        jobs = q["jobs"]
        if not jobs:
            self.v.addWidget(EmptyCard(
                "Nothing is printing",
                "Documents you send to a printer will show up here until they finish."))
        for j in jobs:
            self.v.addWidget(self._job_card(j))
        if jobs:
            self.v.addWidget(button("Cancel all jobs", "tonal", self.app.cancel_all, 36, "circle-x"),
                             alignment=Qt.AlignmentFlag.AlignRight)
        self._history(q["history"], q["keep"])

    def _job_card(self, j):
        card = Card()
        card.setObjectName("card")
        h = QHBoxLayout(card)
        h.setContentsMargins(20, 14, 16, 14)
        h.setSpacing(16)
        h.addWidget(IconBadge(44, T["tonal"], T["on_tonal"]))
        col = QVBoxLayout()
        col.setSpacing(2)
        number = j["id"].rsplit("-", 1)[1]
        row = QHBoxLayout()
        row.setSpacing(8)
        name = QLabel(f"{j['printer'].replace('_', ' ')}, job {number}")
        name.setObjectName("cardTitle")
        row.addWidget(name)
        if j["held"]:
            row.addWidget(chip("On hold", T["alert_fg"], T["alert_bg"]))
        row.addStretch()
        col.addLayout(row)
        meta = QLabel(f"{j['user']}, {j['kb']} KB, {j['date']}")
        meta.setObjectName("muted")
        col.addWidget(meta)
        h.addLayout(col, 1)
        if j["held"]:
            h.addWidget(button("Release", "tonal",
                               lambda _=False, jid=j["id"]: self.app.release_job(jid), 36,
                               "play"))
        else:
            h.addWidget(button("Hold", "text",
                               lambda _=False, jid=j["id"]: self.app.hold_job(jid), 36,
                               "pause"))
        h.addWidget(button("Cancel", "danger",
                           lambda _=False, jid=j["id"]: self.app.cancel_job(jid), 36, "x"))
        return card

    def _history(self, history, keep):
        self.v.addSpacing(10)
        head = QHBoxLayout()
        title = QLabel("Recent jobs")
        title.setObjectName("h2")
        head.addWidget(title, 1)
        keep_lbl = QLabel("Keep documents for reprinting")
        keep_lbl.setObjectName("muted")
        head.addWidget(keep_lbl)
        sw = Switch()
        sw.setChecked(keep)
        sw.setToolTip("When off, CUPS deletes a document once it has printed, "
                      "so Reprint can't work.")
        sw.clicked.connect(self.app.set_keep_documents)
        head.addWidget(sw)
        self.v.addLayout(head)

        if not history:
            note = QLabel("Finished jobs will be listed here.")
            note.setObjectName("muted")
            self.v.addWidget(note)
            return
        for j in history:
            card = Card()
            card.setObjectName("card")
            h = QHBoxLayout(card)
            h.setContentsMargins(20, 10, 16, 10)
            h.setSpacing(12)
            col = QVBoxLayout()
            col.setSpacing(0)
            number = j["id"].rsplit("-", 1)[1]
            t = QLabel(f"{j['printer'].replace('_', ' ')}, job {number}")
            t.setObjectName("cardTitle")
            col.addWidget(t)
            meta = QLabel(f"{j['user']}, {j['kb']} KB, {j['date']}")
            meta.setObjectName("muted")
            col.addWidget(meta)
            h.addLayout(col, 1)
            h.addWidget(button("Reprint", "tonal",
                               lambda _=False, jid=j["id"]: self.app.reprint_job(jid), 32,
                               "rotate-ccw"))
            self.v.addWidget(card)


class OptionsDialog(QDialog):
    """Default settings for one printer, built from whatever its driver offers,
    plus a collapsible card with the printer's connection address."""

    def __init__(self, parent, name, options, uri=""):
        super().__init__(parent)
        self.setWindowTitle("Printer options")
        self.options = options
        self.uri = uri
        self.combos = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 20)
        v.setSpacing(12)
        heading = QLabel(f"{name.replace('_', ' ')} defaults")
        heading.setObjectName("h2")
        v.addWidget(heading)
        sub = QLabel("These apply to everything you print on this printer."
                     if options else "This printer doesn't offer any settings to change.")
        sub.setObjectName("muted")
        v.addWidget(sub)

        page = ScrollPage()
        for o in options:
            lbl = QLabel(o["label"])
            lbl.setObjectName("muted")
            combo = Dropdown()
            for val in o["values"]:
                combo.addItem(pretty_value(val), val)
            combo.setCurrentIndex(o["values"].index(o["current"]))
            self.combos[o["key"]] = combo
            page.v.addWidget(lbl)
            page.v.addWidget(combo)
        if options:
            v.addWidget(page, 1)
        else:
            v.addStretch(1)

        # Connection details: closed by default, opens as a small card.
        self.details_btn = button("Connection details", "text", self._toggle_details, 36,
                                  "chevron-down")
        v.addWidget(self.details_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        self.details = Card()
        self.details.setObjectName("card")
        dv = QVBoxLayout(self.details)
        dv.setContentsMargins(16, 12, 12, 12)
        dv.setSpacing(6)
        kind = QLabel(f"{connection_kind(uri)}  \u2022  address")
        kind.setObjectName("muted")
        dv.addWidget(kind)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.uri_label = QLabel(uri or "No address on file")
        self.uri_label.setWordWrap(True)
        self.uri_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.uri_label, 1)
        self.copy_btn = button("Copy", "tonal", self._copy, 32, "copy")
        self.copy_btn.setEnabled(bool(uri))
        row.addWidget(self.copy_btn)
        dv.addLayout(row)
        self.details.hide()
        v.addWidget(self.details)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("Cancel" if options else "Close", "text", self.reject))
        if options:
            row.addWidget(button("Save", on_click=self.accept, icon_name="check"))
        v.addLayout(row)

        # Open tall enough to show every option without scrolling, within the screen.
        want = (page.widget().sizeHint().height() if options else 40) + 220
        self._cap = int(QApplication.primaryScreen().availableGeometry().height() * 0.9) \
            if QApplication.primaryScreen() else want + 200
        self.resize(480, min(want, self._cap))

    def _toggle_details(self):
        opening = self.details.isHidden()
        self.details.setVisible(opening)
        shown = "chevron-up" if opening else "chevron-down"
        normal, _, disabled = icon_colors("text")
        ic = QIcon()
        ic.addPixmap(icon(shown, normal), QIcon.Mode.Normal)
        ic.addPixmap(icon(shown, disabled), QIcon.Mode.Disabled)
        self.details_btn.setIcon(ic)
        # Grow or shrink the window by the card's height so nothing gets squeezed.
        delta = self.details.sizeHint().height() + 12
        h = self.height() + (delta if opening else -delta)
        self.resize(self.width(), max(200, min(h, self._cap)))

    def _copy(self):
        QApplication.clipboard().setText(self.uri)
        self.copy_btn.setText("  Copied")

    def changes(self):
        return {o["key"]: self.combos[o["key"]].currentData()
                for o in self.options
                if self.combos[o["key"]].currentData() != o["current"]}


class Stepper(QWidget):
    """A number with - and + buttons, for small counts like copies."""

    def __init__(self, value=1, low=1, high=99):
        super().__init__()
        self.low, self.high, self._value = low, high, value
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        self.minus = button("", "tonal", lambda: self.step(-1), 40, "minus")
        self.plus = button("", "tonal", lambda: self.step(1), 40, "plus")
        for b in (self.minus, self.plus):
            b.setFixedWidth(40)
            b.setStyleSheet("padding: 0;")
        self.label = QLabel(str(value))
        self.label.setObjectName("cardTitle")
        self.label.setFixedWidth(36)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self.minus)
        h.addWidget(self.label)
        h.addWidget(self.plus)
        h.addStretch()
        self._sync()

    def step(self, d):
        self._value = max(self.low, min(self.high, self._value + d))
        self._sync()

    def _sync(self):
        self.label.setText(str(self._value))
        self.minus.setEnabled(self._value > self.low)
        self.plus.setEnabled(self._value < self.high)

    def value(self):
        return self._value


class PrintDialog(QDialog):
    """Everything you might want to set for one print job, in one place."""

    def __init__(self, parent, printer, options, paths):
        super().__init__(parent)
        self.setWindowTitle("Print a file")
        self.printer = printer
        self.paths = list(paths)
        self.opt_boxes = []  # (lp option name, dropdown)
        keys = {o["key"]: o for o in options}

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 20)
        v.setSpacing(10)
        heading = QLabel(f"Print on {printer.replace('_', ' ')}")
        heading.setObjectName("h2")
        v.addWidget(heading)

        # File picker
        box = Card()
        box.setObjectName("card")
        fh = QHBoxLayout(box)
        fh.setContentsMargins(16, 8, 8, 8)
        self.file_label = ElidedLabel("")
        fh.addWidget(self.file_label, 1)
        fh.addWidget(button("Browse", "tonal", self.browse, 36, "file-up"))
        v.addWidget(box)
        self._show_files()

        # Copies and pages
        top = QGridLayout()
        top.setHorizontalSpacing(16)
        top.addLayout(self._field("Copies", Stepper()), 0, 0, Qt.AlignmentFlag.AlignTop)
        self.copies = top.itemAtPosition(0, 0).layout().itemAt(1).widget()
        self.pages = QLineEdit()
        self.pages.setPlaceholderText("All pages, or like 1-3, 5")
        top.addLayout(self._field("Pages", self.pages), 0, 1, Qt.AlignmentFlag.AlignTop)
        v.addLayout(top)

        # Dropdowns: only ask about things this printer can actually do.
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        cells = []
        paper = next((keys[k] for k in ("PageSize", "media") if k in keys), None)
        if paper:
            cells.append(("Paper size", "media",
                          [(pretty_value(x), x) for x in paper["values"]]))
        cells.append(("Orientation", "orientation-requested",
                      [("Portrait", "3"), ("Landscape", "4")]))
        if any(k in keys for k in ("ColorModel", "print-color-mode", "ColorMode")):
            cells.append(("Color", "print-color-mode",
                          [("Color", "color"), ("Black & white", "monochrome")]))
        if any(k in keys for k in ("Duplex", "sides")):
            cells.append(("Two-sided", "sides",
                          [("One side only", "one-sided"),
                           ("Both sides, flip like a book", "two-sided-long-edge"),
                           ("Both sides, flip like a notepad", "two-sided-short-edge")]))
        for i, (label, opt, choices) in enumerate(cells):
            dd = Dropdown()
            dd.addItem("Printer default", None)
            for text, data in choices:
                dd.addItem(text, data)
            self.opt_boxes.append((opt, dd))
            grid.addLayout(self._field(label, dd), i // 2, i % 2)
        v.addLayout(grid)

        fit_row = QHBoxLayout()
        fit_lbl = QLabel("Shrink to fit the page")
        fit_row.addWidget(fit_lbl, 1)
        self.fit = Switch()
        fit_row.addWidget(self.fit)
        v.addLayout(fit_row)

        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet(f"color: {T['error']};")
        v.addWidget(self.msg)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("Cancel", "text", self.reject))
        row.addWidget(button("Print", on_click=self.accept, icon_name="file-text"))
        v.addLayout(row)
        self.setMinimumWidth(520)

    @staticmethod
    def _field(label, widget):
        col = QVBoxLayout()
        col.setSpacing(4)
        lbl = QLabel(label)
        lbl.setObjectName("muted")
        col.addWidget(lbl)
        col.addWidget(widget)
        return col

    def _show_files(self):
        if not self.paths:
            text = "No file chosen yet"
        elif len(self.paths) == 1:
            text = os.path.basename(self.paths[0])
        else:
            text = f"{len(self.paths)} files"
        self.file_label._full = text
        self.file_label.setToolTip("\n".join(self.paths))
        self.file_label.setText(text)

    def browse(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choose files to print")
        if files:
            self.paths = files
            self.msg.setText("")
            self._show_files()

    def accept(self):
        pages = self.pages.text().replace(" ", "")
        if not self.paths:
            self.msg.setText("Choose a file to print first.")
        elif pages and not PAGE_RANGE_RE.match(pages):
            self.msg.setText("Pages should look like 1-3, 5 or 2,4,6.")
        else:
            super().accept()

    def build_args(self):
        args = ["lp", "-d", self.printer, "-n", str(self.copies.value())]
        pages = self.pages.text().replace(" ", "")
        if pages:
            args += ["-P", pages]
        for opt, dd in self.opt_boxes:
            if dd.currentData():
                args += ["-o", f"{opt}={dd.currentData()}"]
        if self.fit.isChecked():
            args += ["-o", "fit-to-page"]
        return args + self.paths


class LogDialog(QDialog):
    """Recent CUPS log, with a problems-only filter and a diagnostics copy."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("CUPS log")
        self.resize(780, 560)
        self.lines = []

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 20)
        v.setSpacing(10)
        heading = QLabel("CUPS log")
        heading.setObjectName("h2")
        v.addWidget(heading)
        sub = QLabel("What the print service has been reporting. Newest messages are at the bottom.")
        sub.setObjectName("muted")
        v.addWidget(sub)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.filter = Dropdown()
        self.filter.addItem("Everything", False)
        self.filter.addItem("Problems only", True)
        self.filter.setFixedWidth(200)
        self.filter.activated.connect(lambda _: self._show())
        bar.addWidget(self.filter)
        bar.addStretch()
        bar.addWidget(button("Refresh", "tonal", self.load, 36, "refresh-cw"))
        self.copy_btn = button("Copy diagnostics", "tonal", self.copy_diagnostics, 36, "copy")
        bar.addWidget(self.copy_btn)
        v.addLayout(bar)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        v.addWidget(self.view, 1)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("Close", on_click=self.accept))
        v.addLayout(row)
        self.load()

    def load(self):
        self.view.setPlainText("Loading…")
        run_async(read_cups_log, self._loaded)

    def _loaded(self, text):
        self.lines = [str(text)] if isinstance(text, Exception) else text.splitlines()
        self._show()

    def _show(self):
        only_problems = self.filter.currentData()
        shown = [l for l in self.lines if not only_problems or PROBLEM_RE.search(l)]
        if not shown:
            self.view.setPlainText("No problems found." if only_problems and self.lines
                                   else "No log entries yet.")
            return
        self.view.setPlainText("\n".join(shown))
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def copy_diagnostics(self):
        self.copy_btn.setEnabled(False)

        def done(text):
            self.copy_btn.setEnabled(True)
            if not isinstance(text, Exception):
                QApplication.clipboard().setText(text)
                self.copy_btn.setText("  Copied")
        run_async(collect_diagnostics, done)


class ThemeCard(QAbstractButton):
    """A clickable preview of one theme, drawn in that theme's own colours.
    "Match system" shows dark and light side by side."""

    def __init__(self, key, selected):
        super().__init__()
        self.key = key
        self.selected = selected
        self.label = THEMES[key]["label"]
        keys = ("default", "light") if key == "system" else (key,)
        self.looks = [{k: QColor(v) for k, v in resolve(name).items()} for name in keys]
        self.setFixedHeight(128)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(THEMES[key]["hint"])
        self.setAccessibleName(f"{self.label} theme")

    @staticmethod
    def _mock(p, c, r):
        """A tiny window: header, a card with a gradient button and a soft one."""
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c["bg"])
        p.drawRect(r)
        x, w = r.x() + 12, r.width() - 24
        g = QLinearGradient(x, 0, x + 34, 0)
        g.setColorAt(0, c["primary"])
        g.setColorAt(1, c["primary2"])
        p.setBrush(g)
        p.drawEllipse(QRectF(x, r.y() + 11, 11, 11))
        p.setBrush(c["text2"])
        p.drawRoundedRect(QRectF(x + 17, r.y() + 14, w * 0.35, 5), 2.5, 2.5)
        p.setPen(QPen(c["card_border"], 1))
        p.setBrush(c["card"])
        p.drawRoundedRect(QRectF(x + 0.5, r.y() + 34.5, w, 34), 8, 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(QRectF(x + 8, r.y() + 46, 38, 11), 5.5, 5.5)
        p.setBrush(c["tonal"])
        p.drawRoundedRect(QRectF(x + 52, r.y() + 46, 30, 11), 5.5, 5.5)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(r, 14, 14)
        p.setClipPath(clip)
        mock = QRectF(r.x(), r.y(), r.width(), r.height() - 38)
        part = mock.width() / len(self.looks)
        for i, c in enumerate(self.looks):
            p.save()
            p.setClipRect(QRectF(mock.x() + i * part, mock.y(), part, mock.height()), Qt.ClipOperation.IntersectClip)
            self._mock(p, c, mock)
            p.restore()

        foot = QRectF(r.x(), mock.bottom(), r.width(), r.bottom() - mock.bottom())
        p.fillRect(foot, QColor(T["card"]))
        p.setPen(QColor(T["text"]))
        font = p.font()
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.drawText(foot.adjusted(14, 0, -40, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.label)
        if self.selected:
            box = QRectF(foot.right() - 30, foot.center().y() - 9, 18, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(gradient(box))
            p.drawEllipse(box)
            p.drawPixmap(box.adjusted(2, 2, -2, -2).toRect(), icon("check", T["on_primary"], 14))

        p.setClipping(False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.selected:
            p.setPen(QPen(QColor(T["primary"]), 2))
        else:
            p.setPen(QPen(QColor(T["text"] if self.hasFocus() else T["border"]), 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 14, 14)


class SettingsPage(ScrollPage):
    def __init__(self, app):
        super().__init__()
        self.app = app

        title = QLabel("Appearance")
        title.setObjectName("h2")
        hint = QLabel("Default is black and grey with the cup's red. Wallpaper takes its colour "
                      "from your desktop picture and follows it when it changes.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        self.v.addWidget(title)
        self.v.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(12)
        for i, key in enumerate(THEMES):
            card = ThemeCard(key, key == SETTINGS["theme"])
            card.clicked.connect(lambda _=False, k=key: self.app.change_theme(k))
            grid.addWidget(card, 0, i)
        self.v.addLayout(grid)
        self.v.addSpacing(14)

        title2 = QLabel("Printer status")
        title2.setObjectName("h2")
        hint2 = QLabel("How often SoloCUPS checks your printers and print queue.")
        hint2.setObjectName("muted")
        self.v.addWidget(title2)
        self.v.addWidget(hint2)
        self.refresh_box = Dropdown()
        self.refresh_box.setFixedWidth(240)
        for secs, text in REFRESH_CHOICES:
            self.refresh_box.addItem(text, secs)
        self.refresh_box.setCurrentIndex(
            [c[0] for c in REFRESH_CHOICES].index(SETTINGS["refresh_secs"]))
        self.refresh_box.activated.connect(
            lambda i: self.app.set_refresh(self.refresh_box.itemData(i)))
        self.v.addWidget(self.refresh_box)
        self.v.addSpacing(14)

        title_w = QLabel("Web interface")
        title_w.setObjectName("h2")
        hint_w = QLabel("CUPS has its own web page for advanced settings. "
                        "The globe and ? buttons at the top open it.")
        hint_w.setObjectName("muted")
        hint_w.setWordWrap(True)
        self.v.addWidget(title_w)
        self.v.addWidget(hint_w)
        web_row = QHBoxLayout()
        web_lbl = QLabel("Web interface is on")
        web_row.addWidget(web_lbl, 1)
        self.web_switch = Switch()
        self.web_switch.clicked.connect(self.app.set_web_interface)
        web_row.addWidget(self.web_switch)
        self.v.addLayout(web_row)
        addr_lbl = QLabel("Address (leave empty for this computer)")
        addr_lbl.setObjectName("muted")
        self.v.addWidget(addr_lbl)
        self.web_url = QLineEdit()
        self.web_url.setPlaceholderText(DEFAULT_WEB_URL)
        if SETTINGS["web_url"] != DEFAULT_WEB_URL:
            self.web_url.setText(SETTINGS["web_url"])
        self.web_url.editingFinished.connect(self._web_url_edited)
        self.v.addWidget(self.web_url)
        self.v.addSpacing(14)

        title3 = QLabel("Troubleshooting")
        title3.setObjectName("h2")
        hint3 = QLabel("Something not printing? See what CUPS has been reporting.")
        hint3.setObjectName("muted")
        self.v.addWidget(title3)
        self.v.addWidget(hint3)
        self.v.addWidget(button("View CUPS log", "tonal", self.app.show_log, 40, "file-text"),
                         alignment=Qt.AlignmentFlag.AlignLeft)

    def _web_url_edited(self):
        url = self.app.set_web_url(self.web_url.text())
        self.web_url.setText("" if url == DEFAULT_WEB_URL else url)

    def refresh(self):
        # Mirror what CUPS is really doing; it can only be changed while CUPS runs.
        self.web_switch.setChecked(self.app.web_on)
        self.web_switch.setEnabled(bool(self.app.cups_on))


class DriverDialog(QDialog):
    """Fallback for printers that don't support driverless setup."""

    def __init__(self, parent, hint):
        super().__init__(parent)
        self.setWindowTitle("Choose a driver")
        self.resize(580, 500)
        self.choice = None
        self.drivers = []

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 20)
        v.setSpacing(12)
        heading = QLabel("Choose a driver")
        heading.setObjectName("h2")
        v.addWidget(heading)
        sub = QLabel("Search for your printer's make and model.")
        sub.setObjectName("muted")
        v.addWidget(sub)
        self.filter = QLineEdit(hint)
        self.filter.textChanged.connect(self.apply_filter)
        v.addWidget(self.filter)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        v.addWidget(self.list, 1)
        self.status = QLabel("Loading drivers…")
        self.status.setObjectName("muted")
        v.addWidget(self.status)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("Cancel", "text", self.reject))
        row.addWidget(button("Use this driver", on_click=self.accept, icon_name="check"))
        v.addLayout(row)

        run_async(lambda: run(["lpinfo", "-m"], admin=True, timeout=180), self._loaded)

    def _loaded(self, r):
        if isinstance(r, Exception) or (r.returncode != 0 and not r.stdout):
            self.status.setText(error_text(r))
            return
        for line in r.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                self.drivers.append((parts[0], parts[1]))
        self.apply_filter()

    def apply_filter(self):
        words = self.filter.text().lower().split()
        self.list.clear()
        shown = 0
        for model, desc in self.drivers:
            if all(w in desc.lower() for w in words):
                item = QListWidgetItem(desc)
                item.setData(Qt.ItemDataRole.UserRole, model)
                self.list.addItem(item)
                shown += 1
                if shown >= 300:
                    break
        if self.drivers:
            self.status.setText(f"Showing {shown} of {len(self.drivers)} drivers")

    def accept(self):
        item = self.list.currentItem()
        if not item:
            return
        self.choice = item.data(Qt.ItemDataRole.UserRole)
        super().accept()


class AddPage(ScrollPage):
    added = pyqtSignal()

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.scanned = False
        self.make_hint = ""
        self.v.setSpacing(10)

        title = QLabel("Add a printer")
        title.setObjectName("h2")
        hint = QLabel("Turn your printer on and plug it in, or connect it to the "
                      "same Wi-Fi network as this computer.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        self.v.addWidget(title)
        self.v.addWidget(hint)
        self.v.addSpacing(4)

        self.list = QListWidget()
        self.list.setMinimumHeight(180)
        self.list.itemSelectionChanged.connect(self._picked)
        self.v.addWidget(self.list)

        row = QHBoxLayout()
        self.scan_label = QLabel("")
        self.scan_label.setObjectName("muted")
        row.addWidget(self.scan_label, 1)
        row.addWidget(button("Rescan", "tonal", self.scan, 36, "refresh-cw"))
        self.v.addLayout(row)
        self.v.addSpacing(6)

        self.manual = QLineEdit()
        self.manual.setPlaceholderText(
            "Not listed? Type an address, like ipp://192.168.1.50/ipp/print")
        self.manual.textEdited.connect(lambda _: self.list.clearSelection())
        self.v.addWidget(self.manual)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Printer name")
        self.v.addWidget(self.name)

        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.v.addWidget(self.msg)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.driver_btn = button("Choose driver manually", "tonal", self.choose_driver,
                                  icon_name="list-checks")
        self.driver_btn.hide()
        self.add_btn = button("Add printer", on_click=lambda: self.submit(), icon_name="plus")
        row2.addStretch()
        row2.addWidget(self.driver_btn)
        row2.addWidget(self.add_btn)
        self.v.addLayout(row2)

    def refresh(self):
        if not self.scanned:
            self.scan()

    def scan(self):
        self.scanned = True
        self.scan_label.setText("Looking for printers…")
        self.list.clear()
        run_async(scan_devices, self._scanned)

    def _scanned(self, res):
        if isinstance(res, Exception):
            self.scan_label.setText(f"Scan failed: {res}")
            return
        for d in res:
            kind = "USB printer" if d["kind"] == "USB" else "Network printer"
            item = QListWidgetItem(f"{d['info']}\n{kind}")
            item.setData(Qt.ItemDataRole.UserRole, d)
            item.setSizeHint(QSize(0, 58))
            self.list.addItem(item)
        if res:
            self.scan_label.setText(f"Found {len(res)}. Pick yours below.")
        else:
            self.scan_label.setText("No printers found. Type its address below, or rescan.")

    def _picked(self):
        item = self.list.currentItem()
        if not item:
            return
        d = item.data(Qt.ItemDataRole.UserRole)
        self.manual.clear()
        self.name.setText(sanitize_name(d["info"]))
        self.make_hint = d["make"]

    def _chosen_uri(self):
        typed = self.manual.text().strip()
        if typed:
            return typed
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole)["uri"] if item else ""

    def submit(self, driver="everywhere"):
        uri = self._chosen_uri()
        name = sanitize_name(self.name.text())
        if not uri:
            self.msg.setText("Pick a printer from the list, or type its address.")
            return
        if not name:
            self.msg.setText("Give the printer a name.")
            return
        self.add_btn.setEnabled(False)
        self.driver_btn.hide()
        self.msg.setText("Adding printer…")
        run_async(lambda: run(["lpadmin", "-p", name, "-E", "-v", uri, "-m", driver],
                              admin=True), self._finished)

    def _finished(self, r):
        self.add_btn.setEnabled(True)
        if not isinstance(r, Exception) and r.returncode == 0:
            self.msg.setText("")
            self.driver_btn.hide()
            self.manual.clear()
            self.name.clear()
            self.list.clearSelection()
            self.added.emit()
        else:
            self.msg.setText("This printer couldn't be set up automatically.\n"
                             + error_text(r))
            self.driver_btn.show()

    def choose_driver(self):
        dlg = DriverDialog(self.window(), self.make_hint)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.choice:
            self.submit(driver=dlg.choice)


# --- Main window ------------------------------------------------------------

class MainWindow(QFrame):
    def __init__(self, start_tab=0):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle(APP_NAME)
        # Tall enough that the Add Printer form fits without scrolling,
        # but never taller than the screen.
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry().height() if screen else 800
        self.resize(920, min(780, avail - 60))
        self.setMinimumSize(760, 520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        shell = QFrame()
        shell.setObjectName("shell")
        outer.addWidget(shell)

        v = QVBoxLayout(shell)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Header
        header = QFrame()
        header.setObjectName("header")
        hh = QHBoxLayout(header)
        hh.setContentsMargins(24, 18, 24, 18)
        hh.setSpacing(16)
        hh.addWidget(AppMark(52))
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(eyebrow("Printer center"))
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        text.addWidget(title)
        hh.addLayout(text, 1)

        self.web_on = False
        self.web_btn = icon_button("globe", "Open the CUPS web interface", self.open_web)
        self.help_btn = icon_button("circle-help", "Open CUPS help", self.open_help)
        hh.addWidget(self.web_btn)
        hh.addWidget(self.help_btn)
        hh.addSpacing(8)

        self.cups_on = None      # None until the first check finishes
        self.cups_busy = False
        self.cups_chip = QLabel("")
        self.cups_chip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        hh.addWidget(self.cups_chip, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.cups_switch = Switch()
        self.cups_switch.setToolTip("Turn the CUPS print service on or off")
        self.cups_switch.clicked.connect(self.toggle_cups)
        hh.addWidget(self.cups_switch)
        self._paint_cups()
        v.addWidget(header)

        # Tabs: soft pills, the current one filled
        tab_bar = QFrame()
        tab_bar.setObjectName("tabbar")
        tabs = QHBoxLayout(tab_bar)
        tabs.setContentsMargins(24, 0, 24, 8)
        tabs.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.tab_buttons = []
        tab_defs = ("My Printers", "Print Queue", "Add Printer", "Settings")
        for i, label in enumerate(tab_defs):
            b = button(label, "tab", height=36)
            b.setCheckable(True)
            self.group.addButton(b, i)
            self.tab_buttons.append(b)
            tabs.addWidget(b)
        tabs.addStretch()
        self.status = QLabel("")
        self.status.setObjectName("snack")
        self.status.hide()
        tabs.addWidget(self.status)
        v.addWidget(tab_bar)

        # Pages
        panel = QFrame()
        panel.setObjectName("panel")
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(10, 10, 10, 10)
        self.stack = QStackedWidget()
        self.printers_page = PrintersPage(self)
        self.queue_page = QueuePage(self)
        self.add_page = AddPage(self)
        self.add_page.added.connect(self._printer_added)
        self.settings_page = SettingsPage(self)
        for page in (self.printers_page, self.queue_page, self.add_page,
                     self.settings_page):
            self.stack.addWidget(page)
        pv.addWidget(self.stack)
        v.addWidget(panel, 1)

        self.group.idClicked.connect(self.show_tab)
        self.tab_buttons[start_tab].setChecked(True)
        self.show_tab(start_tab)

        # Keep printer state and the queue fresh.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(SETTINGS["refresh_secs"] * 1000)

        self.snack_timer = QTimer(self)
        self.snack_timer.setSingleShot(True)
        self.snack_timer.timeout.connect(self.status.hide)
        self.refresh_cups()

    # CUPS service switch
    @staticmethod
    def cups_chip_style(state):
        """(text, text colour, chip background) for the header status chip."""
        return {
            "on": ("CUPS is on", T["ok_fg"], T["ok_bg"]),
            "off": ("CUPS is off", T["bad_fg"], T["bad_bg"]),
            "busy": ("Working…", T["on_container"], T["container"]),
            "unknown": ("Checking…", T["text2"], T["tonal"]),
        }[state]

    def _paint_cups(self, busy_text=None):
        if self.cups_busy:
            state = "busy"
        elif self.cups_on is None:
            state = "unknown"
        else:
            state = "on" if self.cups_on else "off"
        text, fg, bg = self.cups_chip_style(state)
        self.cups_chip.setText(busy_text or text)
        self.cups_chip.setText(self.cups_chip.text().upper())
        self.cups_chip.setStyleSheet(chip_style(fg, bg) + "padding: 6px 10px;")
        self.cups_switch.setChecked(bool(self.cups_on))
        self.cups_switch.setEnabled(state in ("on", "off"))
        self._paint_web_buttons()

    def refresh_cups(self):
        run_async(cups_state, self._cups_checked)

    def _cups_checked(self, res):
        if self.cups_busy:
            return
        if isinstance(res, Exception):
            res = {"on": False, "web": False}
        changed = res["on"] != self.cups_on or res["web"] != self.web_on
        pages_changed = res["on"] != self.cups_on
        self.cups_on, self.web_on = res["on"], res["web"]
        self._paint_cups()
        if changed:
            self.settings_page.refresh()
        if pages_changed:
            self._refresh_pages()

    # Web interface and help
    def web_url(self, path=""):
        return SETTINGS["web_url"].rstrip("/") + path

    def _paint_web_buttons(self):
        remote = not is_local_url(SETTINGS["web_url"])
        ready = remote or (bool(self.cups_on) and self.web_on)
        if ready:
            tips = ("Open the CUPS web interface", "Open CUPS help")
        elif self.cups_on:
            tips = ("The web interface is off. Turn it on in Settings.",) * 2
        else:
            tips = ("Turn CUPS on to use the web interface.",) * 2
        for btn, tip in zip((self.web_btn, self.help_btn), tips):
            btn.setEnabled(ready)
            btn.setToolTip(tip)

    def open_web(self):
        self._open(self.web_url())

    def open_help(self):
        self._open(self.web_url("/help/"))

    def _open(self, url):
        if not QDesktopServices.openUrl(QUrl(url)):
            self.say("Couldn't open your browser.")

    def set_web_url(self, text):
        SETTINGS["web_url"] = normalize_web_url(text)
        save_settings()
        self._paint_web_buttons()
        return SETTINGS["web_url"]

    def set_web_interface(self, on):
        def done(r):
            if isinstance(r, Exception) or r.returncode != 0:
                QMessageBox.warning(self, APP_NAME, error_text(r))
            else:
                self.say("Web interface is on." if on else "Web interface is off.")
            self.refresh_cups()
        run_async(lambda: run(["cupsctl", "WebInterface=" + ("yes" if on else "no")],
                              admin=True), done)

    def toggle_cups(self, want_on):
        self.cups_busy = True
        self._paint_cups("Starting…" if want_on else "Stopping…")

        def done(r):
            self.cups_busy = False
            if isinstance(r, Exception) or r.returncode != 0:
                QMessageBox.warning(self, APP_NAME, error_text(r))
            else:
                self.say("CUPS is on." if want_on else "CUPS is off.")
            self.cups_on = None
            self._paint_cups()
            self.refresh_cups()
        run_async(lambda: set_cups_service(want_on), done)

    def _refresh_pages(self):
        for page in (self.printers_page, self.queue_page):
            page._last = None
        if self.stack.currentIndex() in (0, 1):
            self.stack.currentWidget().refresh()

    # navigation
    def show_tab(self, i):
        self.stack.setCurrentIndex(i)
        self.tab_buttons[i].setChecked(True)
        self.stack.currentWidget().refresh()

    def change_theme(self, key):
        if key == SETTINGS["theme"]:
            return
        # Wait for the click to finish before replacing the window it came from.
        QTimer.singleShot(0, lambda: switch_theme(key, self))

    def set_refresh(self, secs):
        SETTINGS["refresh_secs"] = secs
        save_settings()
        self.timer.start(secs * 1000)

    def go_add(self):
        self.show_tab(2)

    def _tick(self):
        self.refresh_cups()
        if self.stack.currentIndex() in (0, 1):
            self.stack.currentWidget().refresh()

    def _printer_added(self):
        self.say("Printer added.")
        self.printers_page._last = None
        self.show_tab(0)

    def say(self, text):
        # Elide long messages so the tab bar never pushes them out of view.
        self.status.setToolTip(text)
        self.status.setText(self.status.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideMiddle, 360))
        self.status.show()
        self.snack_timer.start(5000)

    # actions
    def do(self, cmd, ok, admin=False):
        def done(r):
            if isinstance(r, Exception) or r.returncode != 0:
                QMessageBox.warning(self, APP_NAME, error_text(r))
            else:
                self.say(ok)
            for page in (self.printers_page, self.queue_page):
                page._last = None
            self.stack.currentWidget().refresh()
        run_async(cmd if callable(cmd) else lambda: run(cmd, admin=admin), done)

    def print_test(self, name):
        self.do(["lp", "-d", name, test_page_path()], f"Test page sent to {name}.")

    def print_file(self, name, paths=None):
        """Open the print dialog; paths come from a drag-and-drop, if any."""
        def loaded(opts):
            if isinstance(opts, Exception):
                opts = []
            dlg = PrintDialog(self, name, opts, paths or [])
            if dlg.exec() == QDialog.DialogCode.Accepted:
                n = len(dlg.paths)
                what = os.path.basename(dlg.paths[0]) if n == 1 else f"{n} files"
                self.do(dlg.build_args(), f"Sent {what} to {name}.")
        run_async(lambda: get_options(name), loaded)

    def show_log(self):
        LogDialog(self).exec()

    def make_default(self, name):
        def both():
            # Your own default (~/.cups/lpoptions) beats the system one, and
            # a stale one there hides it, so set both.
            r = run(["lpoptions", "-d", name])
            return r if r.returncode != 0 else run(["lpadmin", "-d", name], admin=True)
        self.do(both, f"{name.replace('_', ' ')} is now the default.")

    def remove_printer(self, name):
        answer = QMessageBox.question(
            self, "Remove printer", f"Remove {name} from this computer?")
        if answer == QMessageBox.StandardButton.Yes:
            self.do(["lpadmin", "-x", name], f"Removed {name}.", admin=True)

    def cancel_job(self, job_id):
        self.do(["cancel", job_id], "Job cancelled.", admin=True)

    def cancel_all(self):
        self.do(["cancel", "-a"], "All jobs cancelled.", admin=True)

    def hold_job(self, job_id):
        self.do(["lp", "-i", job_id, "-H", "hold"], "Job on hold.")

    def release_job(self, job_id):
        self.do(["lp", "-i", job_id, "-H", "resume"], "Job released.")

    def reprint_job(self, job_id):
        def done(r):
            if isinstance(r, Exception) or r.returncode != 0:
                QMessageBox.warning(
                    self, APP_NAME,
                    error_text(r) + "\n\nCUPS may not have kept this document. "
                    "Turn on \"Keep documents for reprinting\" to reprint future jobs.")
            else:
                self.say("Reprinting.")
            self._refresh_pages()
        run_async(lambda: run(["lp", "-i", job_id, "-H", "restart"]), done)

    def set_keep_documents(self, on):
        self.do(["cupsctl", "PreserveJobFiles=1d" if on else "PreserveJobFiles=No"],
                "Documents will be kept for a day." if on else "Documents won't be kept.",
                admin=True)

    def edit_options(self, name, uri=""):
        def loaded(opts):
            if isinstance(opts, Exception):
                opts = []
            dlg = OptionsDialog(self, name, opts, uri)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            changes = dlg.changes()
            if not changes:
                return
            cmd = ["lpadmin", "-p", name]
            for key, val in changes.items():
                cmd += ["-o", f"{key}={val}"]
            self.do(cmd, f"Saved settings for {name}.", admin=True)
        run_async(lambda: get_options(name), loaded)


_window = None


def switch_theme(key, old):
    """Apply a theme by rebuilding the window, so every icon and painted
    widget picks up the new colours."""
    global _window
    set_theme(key)
    save_settings()
    QApplication.instance().setStyleSheet(build_qss())
    new = MainWindow(start_tab=old.stack.currentIndex())
    new.resize(old.size())
    new.move(old.pos())
    new.show()
    old.hide()
    old.deleteLater()
    _window = new


def follow_system():
    """Rebuild when Plasma's light/dark mode or accent colour has changed."""
    if resolve(SETTINGS["theme"]) == T:
        return
    if QApplication.activeModalWidget():
        # Rebuilding would delete the window a dialog belongs to; try again later.
        _follow_timer.start(1000)
        return
    switch_theme(SETTINGS["theme"], _window)


def watch_system_look(app):
    """Plasma announces light/dark switches through Qt, and rewrites its desktop config when the
    wallpaper changes. Both land in follow_system(), batched."""
    global _follow_timer, _watcher
    _follow_timer = QTimer()
    _follow_timer.setSingleShot(True)
    _follow_timer.timeout.connect(follow_system)
    app.styleHints().colorSchemeChanged.connect(lambda _: _follow_timer.start(300))

    _watcher = QFileSystemWatcher([APPLETS_RC] if os.path.exists(APPLETS_RC) else [])

    def changed(path):
        # KDE replaces the file instead of editing it, which drops the watch.
        if path not in _watcher.files() and os.path.exists(path):
            _watcher.addPath(path)
        _follow_timer.start(1500)  # the wallpaper plugin may still be writing
    _watcher.fileChanged.connect(changed)


def app_icon():
    here = os.path.join(os.path.dirname(os.path.realpath(__file__)), "icons", "solocups.svg")
    return QIcon.fromTheme("solocups", QIcon(here))


def main():
    global _bridge, _window
    app = QApplication(sys.argv)
    # The stylesheet and our own painting draw every widget, so use Qt's plain
    # base style. Desktop styles like Kvantum get in the way: its combo_menu
    # option turns dropdown lists into see-through menus.
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("solocups")
    app.setWindowIcon(app_icon())
    load_settings()
    set_theme(SETTINGS["theme"])
    app.setStyleSheet(build_qss())
    _bridge = Bridge()
    _window = MainWindow()
    _window.show()
    watch_system_look(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
