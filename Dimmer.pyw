"""
Dimmer (pyw) - screen dimmer with tray support.

Requires:  pip install pystray pillow

 - Close (X)       -> closes the app
 - Hide to tray    -> hides the window to the tray
 - Minimize (_)    -> goes to taskbar normally
 - Restore         -> tray icon, Ctrl+Alt+D, or launch Dimmer again
 - The Dimmer window is always above the dim layer, so it is never dimmed.
 - Logos: change TRAY_ICON_STYLE / WINDOW_ICON_STYLE (1-6) in the icons section below.
 - Slider look: change SLIDER_STYLE (1-6) in the slider section.
 - Updates: set GITHUB_REPO and raise VERSION for every new release (see the steps I sent).
 - Auto-hide taskbar keeps working (dim layer leaves a 2px strip on the taskbar edge).
"""
import os
import sys
import json
import queue
import socket
import ctypes
import threading
import hashlib
import subprocess
import tempfile
import webbrowser
import urllib.error
import urllib.request
import http.client
import math
import tkinter as tk
from tkinter import messagebox
from ctypes import wintypes

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
import pystray

APP = "Dimmer"
PORT = 48653
MAX_ALPHA = 1.0     # 100% = fully black
EDGE_STRIP = 2
DEFAULT_LEVEL = 35          # dimming % every time Dimmer starts
DEFAULTS_VERSION = 4        # bump to re-apply the defaults above one time

# ---- updates (see the GitHub steps) -------------------------------------
VERSION = "1.0.5"                       # raise this number before building each new release
GITHUB_REPO = "socialidentity00/Dimmer-by-hon3st"   # your GitHub  username/repository
UPDATE_ASSET = "Dimmer.exe"             # the file you attach to each GitHub release

# ---------------------------------------------------------------- Win32 setup
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Dimmer.App")
except Exception:
    pass

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uCallbackMessage", wintypes.UINT), ("uEdge", wintypes.UINT),
                ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                       MonitorEnumProc, wintypes.LPARAM]
shell32.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(APPBARDATA)]
shell32.SHAppBarMessage.restype = ctypes.c_size_t


def get_monitors():
    result = []

    def cb(hmon, hdc, lprc, lparam):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        m, w = mi.rcMonitor, mi.rcWork
        result.append({
            "mon": (m.left, m.top, m.right, m.bottom),
            "work": (w.left, w.top, w.right, w.bottom),
            "primary": bool(mi.dwFlags & 1),
        })
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    result.sort(key=lambda r: (not r["primary"], r["mon"][0], r["mon"][1]))
    return result


def taskbar_info():
    """returns (autohide, edge) ; edge: 0 left, 1 top, 2 right, 3 bottom"""
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(abd)
    state = shell32.SHAppBarMessage(0x4, ctypes.byref(abd))      # ABM_GETSTATE
    autohide = bool(state & 1)
    edge = 3
    if shell32.SHAppBarMessage(0x5, ctypes.byref(abd)):          # ABM_GETTASKBARPOS
        edge = abd.uEdge
    return autohide, edge


user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
HWND_TOPMOST = wintypes.HWND(-1)


def lift_taskbars():
    """put the Windows taskbar(s) above the dim layer, so an auto-hide taskbar that
    slides in is already bright (otherwise it stays dim until you click it)"""
    hwnds = []
    main = user32.FindWindowW("Shell_TrayWnd", None)
    if main:
        hwnds.append(main)
    h = None
    for _ in range(8):
        h = user32.FindWindowExW(None, h, "Shell_SecondaryTrayWnd", None)
        if not h:
            break
        hwnds.append(h)
    for hw in hwnds:
        user32.SetWindowPos(hw, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


def fullscreen_busy():
    """True while a fullscreen app/game/presentation is running (leave the taskbar alone then)"""
    try:
        state = ctypes.c_int(0)
        shell32.SHQueryUserNotificationState(ctypes.byref(state))
        return state.value in (2, 3, 4)
    except Exception:
        return False


def overlay_rect(mon, dim_taskbar, autohide, edge):
    l, t, r, b = mon["mon"] if (dim_taskbar or autohide) else mon["work"]
    if autohide:  # leave the taskbar edge free so the taskbar can pop up
        if edge == 0:
            l += EDGE_STRIP
        elif edge == 1:
            t += EDGE_STRIP
        elif edge == 2:
            r -= EDGE_STRIP
        else:
            b -= EDGE_STRIP
    return l, t, r, b


# ------------------------------------------------------------------- icons
# Pick your logos here (see the numbers below):
#   1 Classic Sun   2 Crescent Moon   3 Half Circle
#   4 Mixer Sliders 5 Sunset          6 Dimmer Dial
TRAY_ICON_STYLE = 2       # icon in the system tray
WINDOW_ICON_STYLE = 2     # icon in the window title bar + taskbar

AMBER = (255, 190, 90, 255)
CHARCOAL = (32, 32, 36, 255)
GREY = (70, 70, 76, 255)
WHITE = (255, 255, 255, 255)


def _round_mask(s):
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=255)
    return mask


def _base(s, top, bottom=None):
    """rounded square, flat colour or top-to-bottom gradient"""
    bottom = bottom or top
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / max(1, s - 1)
        gd.line((0, y, s, y), fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(4)))
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(grad, (0, 0), _round_mask(s))
    return img


def _icon_sun(s):
    img = _base(s, CHARCOAL)
    d = ImageDraw.Draw(img)
    c, r = s / 2, s * 0.17
    d.ellipse((c - r, c - r, c + r, c + r), fill=AMBER)
    for k in range(8):
        a = math.radians(k * 45)
        d.line((c + math.cos(a) * s * 0.27, c + math.sin(a) * s * 0.27,
                c + math.cos(a) * s * 0.40, c + math.sin(a) * s * 0.40),
               fill=AMBER, width=max(2, s // 16))
    return img


def _icon_moon(s):
    img = _base(s, (22, 28, 54, 255), (46, 56, 98, 255))
    moon = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(moon)
    r = s * 0.30
    cx, cy = s * 0.46, s * 0.52
    md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    r2 = s * 0.25
    cx2, cy2 = s * 0.60, s * 0.42
    md.ellipse((cx2 - r2, cy2 - r2, cx2 + r2, cy2 + r2), fill=0)
    pale = (245, 236, 205, 255)
    img.paste(Image.new("RGBA", (s, s), pale), (0, 0), moon)
    d = ImageDraw.Draw(img)
    for x, y, rr in ((0.74, 0.27, 0.030), (0.82, 0.52, 0.022), (0.85, 0.74, 0.026)):
        d.ellipse((s * (x - rr), s * (y - rr), s * (x + rr), s * (y + rr)), fill=pale)
    return img


def _icon_half(s):
    img = _base(s, CHARCOAL)
    d = ImageDraw.Draw(img)
    c, r = s / 2, s * 0.30
    box = (c - r, c - r, c + r, c + r)
    d.ellipse(box, outline=AMBER, width=max(2, s // 14))
    d.pieslice(box, 90, 270, fill=AMBER)
    return img


def _icon_sliders(s):
    img = _base(s, CHARCOAL)
    d = ImageDraw.Draw(img)
    w = max(3, s // 11)
    x0, x1 = s * 0.20, s * 0.80
    for y, v in ((0.30, 0.65), (0.50, 0.35), (0.70, 0.55)):
        yy = s * y
        xk = x0 + (x1 - x0) * v
        d.line((x0, yy, x1, yy), fill=GREY, width=w)
        d.line((x0, yy, xk, yy), fill=AMBER, width=w)
        for xe, col in ((x0, AMBER), (x1, GREY)):
            d.ellipse((xe - w / 2, yy - w / 2, xe + w / 2, yy + w / 2), fill=col)
        kr = s * 0.08
        d.ellipse((xk - kr, yy - kr, xk + kr, yy + kr), fill=WHITE)
    return img


def _icon_sunset(s):
    img = _base(s, (52, 30, 74, 255), (236, 124, 72, 255))
    d = ImageDraw.Draw(img)
    c, hy, r = s / 2, s * 0.60, s * 0.24
    d.ellipse((c - r, hy - r, c + r, hy + r), fill=(255, 214, 120, 255))
    d.rectangle((0, hy, s, s), fill=(28, 22, 40, 255))
    img.putalpha(ImageChops.multiply(img.getchannel("A"), _round_mask(s)))
    d = ImageDraw.Draw(img)
    lw = max(2, s // 28)
    for k, frac in enumerate((0.95, 0.70, 0.45)):
        y = hy + s * (0.07 + 0.075 * k)
        d.line((c - r * frac, y, c + r * frac, y), fill=(255, 190, 90, 255), width=lw)
    return img


def _icon_dial(s):
    img = _base(s, CHARCOAL)
    d = ImageDraw.Draw(img)
    c, R, w = s / 2, s * 0.34, max(3, s // 12)
    box = (c - R, c - R, c + R, c + R)
    end = 135 + 270 * 0.40
    d.arc(box, 135, 45, fill=GREY, width=w)
    d.arc(box, 135, end, fill=AMBER, width=w)
    kr = s * 0.21
    d.ellipse((c - kr, c - kr, c + kr, c + kr), fill=(58, 58, 64, 255))
    a = math.radians(end)
    d.line((c + math.cos(a) * kr * 0.30, c + math.sin(a) * kr * 0.30,
            c + math.cos(a) * kr * 0.92, c + math.sin(a) * kr * 0.92),
           fill=WHITE, width=max(2, s // 20))
    return img


ICON_NAMES = {1: "Classic Sun", 2: "Crescent Moon", 3: "Half Circle",
              4: "Mixer Sliders", 5: "Sunset", 6: "Dimmer Dial"}
_ICONS = {1: _icon_sun, 2: _icon_moon, 3: _icon_half,
          4: _icon_sliders, 5: _icon_sunset, 6: _icon_dial}


def make_icon(size=64, style=1):
    """draws big, then shrinks, so edges stay smooth at small sizes"""
    ss = 4
    big = _ICONS.get(style, _icon_sun)(size * ss)
    return big.resize((size, size), Image.LANCZOS)


# ------------------------------------------------------------------- helpers
def settings_path():
    folder = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "Dimmer.json")


def startup_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    py = sys.executable
    if py.lower().endswith("python.exe"):
        py = py[:-10] + "pythonw.exe"
    return f'"{py}" "{os.path.abspath(__file__)}"'


def set_startup(enable):
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE)
    try:
        if enable:
            winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


def single_instance(q):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
    except OSError:
        try:
            c = socket.create_connection(("127.0.0.1", PORT), 1)
            c.sendall(b"show")
            c.close()
        except Exception:
            pass
        return None
    s.listen(5)

    def loop():
        while True:
            try:
                c, _ = s.accept()
                c.recv(16)
                c.close()
                q.put("show")
            except Exception:
                break

    threading.Thread(target=loop, daemon=True).start()
    return s


def hotkey_thread(q):
    # Ctrl + Alt + D  (MOD_ALT | MOD_CONTROL | MOD_NOREPEAT)
    if not user32.RegisterHotKey(None, 1, 0x1 | 0x2 | 0x4000, 0x44):
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            q.put("hotkey")


# ------------------------------------------------------------------- updater
def version_tuple(text):
    """'v1.2.10' -> (1, 2, 10) so versions compare properly"""
    nums = []
    for part in str(text).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


class UpdateError(Exception):
    """kind: 'network' (internet problem) | 'disk' (can't save the file) | 'github' (anything else)"""

    def __init__(self, kind, text):
        super().__init__(text)
        self.kind = kind
        self.text = text


def classify_error(e):
    if isinstance(e, UpdateError):
        return e
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return UpdateError("github", "GitHub couldn't find a published release for Dimmer.")
        if e.code in (403, 429):
            return UpdateError("github", "GitHub is limiting requests right now. Try again in a little while.")
        return UpdateError("github", f"GitHub answered with an error (code {e.code}).")
    if isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                      http.client.HTTPException)):
        return UpdateError("network", "Weak or no internet connection.")
    if isinstance(e, PermissionError):
        return UpdateError("disk", "Dimmer can't save the update next to Dimmer.exe. "
                                   "Move Dimmer.exe to Documents or Desktop and try again.")
    if isinstance(e, OSError):
        return UpdateError("disk", "Dimmer couldn't save the update file on this PC.")
    if isinstance(e, (ValueError, KeyError, TypeError)):
        return UpdateError("github", "GitHub sent back something Dimmer couldn't read.")
    return UpdateError("github", "Something unexpected went wrong while updating.")


def _http_get(url, timeout=15, api=False):
    headers = {"User-Agent": "Dimmer-updater"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def fetch_latest_release():
    with _http_get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", api=True) as r:
        return json.load(r)


def find_asset(release, name):
    for a in release.get("assets", []):
        if a.get("name") == name:
            return a
    return None


def expected_sha256(release, asset):
    """checksum from 'Dimmer.exe.sha256' on the release, else GitHub's own digest, else None"""
    side = find_asset(release, UPDATE_ASSET + ".sha256")
    if side:
        with _http_get(side["browser_download_url"]) as r:
            return r.read(512).decode("ascii", "ignore").split()[0].lower()
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        return digest[7:].lower()
    return None


def download_asset(asset, dest, progress):
    url = asset["browser_download_url"]
    if not url.lower().startswith(f"https://github.com/{GITHUB_REPO}/".lower()):
        raise RuntimeError("Unexpected download address")
    h = hashlib.sha256()
    with _http_get(url, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total:
                progress(int(done * 100 / total))
    return h.hexdigest()


UPDATE_BAT = r"""@echo off
set tries=0
:retry
ping 127.0.0.1 -n 2 >nul
move /y "NEWFILE" "TARGET" >nul 2>&1
if not errorlevel 1 goto done
set /a tries+=1
if %tries% lss 30 goto retry
exit /b 1
:done
start "" "TARGET"
(goto) 2>nul & del "%~f0"
"""


# ----------------------------------------------------------- shortcut fixer
# Points old Dimmer shortcuts (desktop, Start menu, taskbar pins that targeted
# Dimmer.pyw or an old Dimmer.exe) at the current Dimmer.exe, with the moon icon.
SHORTCUT_PS = r"""param([string]$Exe)
$sh = New-Object -ComObject WScript.Shell
$dirs = @(
  [Environment]::GetFolderPath('Desktop'),
  [Environment]::GetFolderPath('CommonDesktopDirectory'),
  [Environment]::GetFolderPath('Programs'),
  [Environment]::GetFolderPath('CommonPrograms'),
  (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch'),
  (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'),
  (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\StartMenu')
)
$fixed = 0
foreach ($d in $dirs) {
  if (-not $d -or -not (Test-Path $d)) { continue }
  Get-ChildItem -Path $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
    try {
      $l = $sh.CreateShortcut($_.FullName)
      $t = [string]$l.TargetPath
      $a = [string]$l.Arguments
      $leaf = [IO.Path]::GetFileName($t)
      $old = ($a -match 'Dimmer\.pyw') -or ($t -match 'Dimmer\.pyw') -or (($leaf -ieq 'Dimmer.exe') -and ($t -ne $Exe))
      if ($old) {
        $l.TargetPath = $Exe
        $l.Arguments = ''
        $l.WorkingDirectory = [IO.Path]::GetDirectoryName($Exe)
        $l.IconLocation = "$Exe,0"
        $l.Save()
        $fixed++
      }
    } catch {}
  }
}
Write-Output $fixed
"""


def fix_shortcuts(exe):
    """returns how many shortcuts were pointed at the current exe"""
    ps = os.path.join(tempfile.gettempdir(), "dimmer_shortcuts.ps1")
    with open(ps, "w", encoding="utf-8-sig") as f:
        f.write(SHORTCUT_PS)
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", ps, "-Exe", exe],
                       capture_output=True, text=True, timeout=90, creationflags=0x08000000)
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except Exception:
        return 0


# ------------------------------------------------------------------- overlay
class Overlay:
    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.configure(bg="black")
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.0)
        self.win.update_idletasks()
        hwnd = user32.GetAncestor(self.win.winfo_id(), 2)
        ex = user32.GetWindowLongW(hwnd, -20)
        # layered | click-through | toolwindow (no taskbar) | no-activate
        user32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)
        self.win.withdraw()
        self.geom = None
        self.alpha = None

    def update(self, rect, alpha, visible):
        """returns True if something visible changed"""
        changed = False
        if not visible or alpha <= 0:
            if self.win.state() != "withdrawn":
                self.win.withdraw()
                changed = True
            return changed
        l, t, r, b = rect
        geom = f"{r - l}x{b - t}+{l}+{t}"
        if geom != self.geom:
            self.win.geometry(geom)
            self.geom = geom
            changed = True
        if alpha != self.alpha:
            self.win.attributes("-alpha", alpha)
            self.alpha = alpha
            changed = True
        if self.win.state() == "withdrawn":
            self.win.deiconify()
            self.win.attributes("-topmost", True)
            changed = True
        return changed

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# ----------------------------------------------------------------------- app
BG = "#1e1e1e"
FG = "#e6e6e6"
ACC = "#d9a066"
GREEN = "#2e8b45"          # "Update now" / "Restart now" button
GREEN_HOVER = "#37a653"


# ------------------------------------------------------------ slider looks
# Pick the slider design here (see Dimmer_slider_options.png):
#   1 Pill   2 Glow   3 Volume Bar   4 Ring   5 Switch   6 Sun
SLIDER_STYLE = 1
SLIDER_NAMES = {1: "Pill", 2: "Glow", 3: "Volume Bar", 4: "Ring", 5: "Switch", 6: "Sun"}
_S_BG = (30, 30, 30)
_S_TRACK = (58, 58, 62)
_S_ACC = (217, 160, 102)
_S_WHITE = (255, 255, 255)
_S_SUN = (255, 200, 110)


def slider_image(w, h, x0, x1, v, style):
    """anti-aliased slider (track + knob) as a w x h image; v is 0-100"""
    ss = 4
    W, H = w * ss, h * ss
    img = Image.new("RGB", (W, H), _S_BG)
    d = ImageDraw.Draw(img)
    cy = H / 2
    X0, X1 = x0 * ss, x1 * ss
    X = X0 + (X1 - X0) * v / 100.0

    def bar(xa, xb, t, col):
        if xb - xa >= 1:
            d.rounded_rectangle((xa, cy - t / 2, xb, cy + t / 2), radius=t / 2, fill=col)

    def dot(cx, r, col):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)

    if style == 1:        # Pill: white pill handle with grip lines
        bar(X0, X1, 6 * ss, _S_TRACK)
        bar(X0, X, 6 * ss, _S_ACC)
        kw, kh = 30 * ss, 20 * ss
        d.rounded_rectangle((X - kw / 2, cy - kh / 2, X + kw / 2, cy + kh / 2),
                            radius=kh / 2, fill=_S_WHITE)
        for dx in (-4, 0, 4):
            d.line((X + dx * ss, cy - 4 * ss, X + dx * ss, cy + 4 * ss), fill=(185, 185, 185), width=ss)
    elif style == 2:      # Glow: soft amber halo around a round knob
        bar(X0, X1, 6 * ss, _S_TRACK)
        bar(X0, X, 6 * ss, _S_ACC)
        glow = Image.new("L", (W, H), 0)
        r = 12 * ss
        ImageDraw.Draw(glow).ellipse((X - r, cy - r, X + r, cy + r), fill=190)
        glow = glow.filter(ImageFilter.GaussianBlur(3 * ss))
        img.paste(Image.new("RGB", (W, H), _S_ACC), (0, 0), glow)
        d = ImageDraw.Draw(img)
        dot(X, 11 * ss, _S_ACC)
        dot(X, 7 * ss, _S_WHITE)
    elif style == 3:      # Volume bar: chunky bar with a thin handle
        bar(X0, X1, 14 * ss, _S_TRACK)
        bar(X0, X, 14 * ss, _S_ACC)
        d.rounded_rectangle((X - 3 * ss, cy - 14 * ss, X + 3 * ss, cy + 14 * ss),
                            radius=3 * ss, fill=_S_WHITE)
    elif style == 4:      # Ring: amber ring with dark centre
        bar(X0, X1, 4 * ss, _S_TRACK)
        bar(X0, X, 4 * ss, _S_ACC)
        dot(X, 13 * ss, _S_ACC)
        dot(X, 8 * ss, (36, 36, 40))
        dot(X, 3 * ss, _S_ACC)
    elif style == 5:      # Switch: pill track with the knob inside it
        bar(X0 - 10 * ss, X1 + 10 * ss, 22 * ss, _S_TRACK)
        bar(X0 - 10 * ss, X + 10 * ss * v / 100.0, 22 * ss, _S_ACC)
        dot(X, 9 * ss, _S_WHITE)
    else:                 # 6 Sun: a little sun as the handle
        bar(X0, X1, 6 * ss, _S_TRACK)
        bar(X0, X, 6 * ss, _S_ACC)
        for k in range(8):
            a = math.radians(k * 45)
            d.line((X + math.cos(a) * 11 * ss, cy + math.sin(a) * 11 * ss,
                    X + math.cos(a) * 15 * ss, cy + math.sin(a) * 15 * ss),
                   fill=_S_SUN, width=int(2.5 * ss))
        dot(X, 8 * ss, _S_SUN)
    return img.resize((w, h), Image.LANCZOS)


class Slider(tk.Canvas):
    """0-100 slider, drawn smoothly as an image (look is set by SLIDER_STYLE)"""
    PAD = 16
    LABEL_W = 70
    HEIGHT = 36

    def __init__(self, parent, var, command):
        super().__init__(parent, width=330, height=self.HEIGHT, bg=BG, highlightthickness=0,
                         bd=0, cursor="hand2")
        self.var = var
        self.command = command
        self._photo = None
        self.bind("<Button-1>", self.drag)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<Configure>", lambda e: self.draw())
        self.var.trace_add("write", lambda *a: self.draw())
        self.draw()

    def span(self):
        w = max(self.winfo_width(), 120)
        return self.PAD, w - self.LABEL_W

    def draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 120)
        x0, x1 = self.span()
        v = max(0, min(100, self.var.get()))
        self._photo = ImageTk.PhotoImage(slider_image(w, self.HEIGHT, x0, x1, v, SLIDER_STYLE))
        self.create_image(0, 0, image=self._photo, anchor="nw")
        self.create_text(x1 + self.LABEL_W - 8, self.HEIGHT // 2, text=f"{v}%", fill=FG,
                         anchor="e", font=("Segoe UI", 11))

    def drag(self, e):
        x0, x1 = self.span()
        v = int(round((e.x - x0) / float(x1 - x0) * 100))
        v = max(0, min(100, v))
        self.var.set(v)
        self.command(v)


def flat_button(parent, text, command, bg="#333333", hover="#444444", fg=None, padx=14):
    b = tk.Button(parent, text=text, command=command, bg=bg, fg=fg or FG, relief="flat", bd=0,
                  activebackground=hover, activeforeground=fg or FG, padx=padx, pady=6,
                  cursor="hand2", font=("Segoe UI", 10))
    b.bind("<Enter>", lambda e: b.config(bg=hover))
    b.bind("<Leave>", lambda e: b.config(bg=bg))
    return b


class DimmerApp:
    def __init__(self):
        self.q = queue.Queue()
        self.lock = single_instance(self.q)
        if self.lock is None:
            sys.exit(0)

        self.cfg = self.load()
        if self.cfg.get("defaults_version") != DEFAULTS_VERSION:
            # one-time reset so the new defaults (40 %, taskbar dimmed) take effect
            self.cfg = {"startup": self.cfg.get("startup", False),
                        "defaults_version": DEFAULTS_VERSION}
        self.root = tk.Tk()
        self.root.title(APP)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.icon_imgs = [ImageTk.PhotoImage(make_icon(n, WINDOW_ICON_STYLE)) for n in (64, 48, 32, 16)]
        self.root.iconphoto(True, *self.icon_imgs)
        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

        self.master_on = tk.BooleanVar(value=self.cfg.get("master_on", True))
        self.master_level = tk.IntVar(value=DEFAULT_LEVEL)   # always starts at the default, never remembered
        self.dim_taskbar = tk.BooleanVar(value=True)    # always ticked on start, never remembered
        self.keep_bright = tk.BooleanVar(value=True)    # always ticked on start, never remembered
        self.startup = tk.BooleanVar(value=self.cfg.get("startup", False))

        self.mon_key = None
        self.monitors = []
        self.overlays = []
        self.scr_on = []
        self.scr_level = []
        self.tray = None
        self.hidden = False
        self.pending_release = None
        self.busy = False            # True while an update is downloading
        self.auto_tries = 0
        self.popup = None
        self.ready_file = None      # downloaded update waiting for a restart
        self.master_flag = self.master_on.get()   # thread-safe mirror for the tray

        self.build_ui()
        self.refresh_monitors(force=True)
        self.lift_ui()

        threading.Thread(target=hotkey_thread, args=(self.q,), daemon=True).start()
        self.start_tray()
        self.poll()
        self.tick()
        self.root.after(1500, lambda: self.check_updates(silent=True))   # auto-check on every start
        self.root.after(500, self.taskbar_guard)
        self.clean_old_update()
        self.root.after(8000, self.fix_shortcuts_once)

    # -------------------------------------------------------------- settings
    def load(self):
        try:
            with open(settings_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self):
        self.cfg.update({
            "master_on": self.master_on.get(),
            "defaults_version": DEFAULTS_VERSION,
            "startup": self.startup.get(),
            "screens": {str(i): {"on": self.scr_on[i].get()}
                        for i in range(len(self.scr_on))},
        })
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except Exception:
            pass

    # -------------------------------------------------------------------- UI
    def chk(self, parent, text, var, cmd):
        return tk.Checkbutton(parent, text=text, variable=var, command=cmd, bg=BG, fg=FG,
                              selectcolor="#333333", activebackground=BG, activeforeground=FG,
                              highlightthickness=0, bd=0, anchor="w", font=("Segoe UI", 10))

    def build_ui(self):
        pad = {"padx": 12}
        tk.Label(self.root, text="Dimmer", bg=BG, fg=ACC,
                 font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(10, 0), **pad)

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", pady=(6, 0), **pad)
        self.chk(top, "All screens", self.master_on, self.on_master_toggle).pack(anchor="w")
        Slider(top, self.master_level, self.on_master_slide).pack(fill="x")

        self.screen_frame = tk.Frame(self.root, bg=BG)
        self.screen_frame.pack(fill="x", pady=(6, 0), **pad)

        opts = tk.Frame(self.root, bg=BG)
        opts.pack(fill="x", pady=(6, 0), **pad)
        self.chk(opts, "Dim taskbar too", self.dim_taskbar, self.on_change).pack(anchor="w")
        self.chk(opts, "Dimmer app stays undimmed", self.keep_bright, self.on_bright).pack(anchor="w")
        self.chk(opts, "Start with Windows", self.startup, self.on_startup).pack(anchor="w")

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", pady=(8, 4), **pad)
        for text, cmd in (("Hide to tray", self.hide_to_tray), ("Exit", self.exit_app)):
            tk.Button(btns, text=text, command=cmd, bg="#333333", fg=FG, relief="flat",
                      activebackground="#444444", activeforeground=FG,
                      padx=10).pack(side="left", padx=(0, 6))

        upd = tk.Frame(self.root, bg=BG)
        upd.pack(fill="x", pady=(0, 4), **pad)
        self.upd_btn = tk.Button(upd, text="Check for updates", command=self.on_update_click,
                                 bg="#333333", fg=FG, relief="flat", activebackground="#444444",
                                 activeforeground=FG, padx=10)
        self.upd_btn.pack(side="left")
        self.upd_text = tk.StringVar(value=f"Version {VERSION}")
        tk.Label(upd, textvariable=self.upd_text, bg=BG, fg="#888888", font=("Segoe UI", 9),
                 wraplength=200, justify="left").pack(side="left", padx=(8, 0))

        tk.Label(self.root, text="Ctrl+Alt+D or relaunch = show window again",
                 bg=BG, fg="#888888", font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 10), **pad)

    def build_screens(self):
        for w in self.screen_frame.winfo_children():
            w.destroy()
        saved = self.cfg.get("screens", {})
        old_levels = [v.get() for v in self.scr_level]   # keep this session's levels if screens change
        self.scr_on, self.scr_level = [], []
        for i, mon in enumerate(self.monitors):
            s = saved.get(str(i), {})
            on = tk.BooleanVar(value=s.get("on", True))
            lvl = tk.IntVar(value=old_levels[i] if i < len(old_levels) else DEFAULT_LEVEL)
            self.scr_on.append(on)
            self.scr_level.append(lvl)
            l, t, r, b = mon["mon"]
            row = tk.Frame(self.screen_frame, bg=BG)
            row.pack(fill="x", pady=(2, 0))
            self.chk(row, f"Screen {i + 1}  ({r - l}x{b - t})", on, self.on_change).pack(anchor="w")
            Slider(row, lvl, lambda v: self.on_change()).pack(fill="x")

    # ---------------------------------------------------------------- events
    def on_master_toggle(self):
        self.master_flag = self.master_on.get()
        self.on_change()

    def on_master_slide(self, v):
        for lvl in self.scr_level:
            lvl.set(int(float(v)))
        self.on_change()

    def on_change(self):
        self.apply()
        self.save()

    def on_startup(self):
        try:
            set_startup(self.startup.get())
        except Exception:
            self.startup.set(False)
        self.save()

    # ---------------------------------------------------------------- dimming
    def refresh_monitors(self, force=False):
        mons = get_monitors()
        key = tuple((m["mon"], m["work"]) for m in mons)
        if key == self.mon_key and not force:
            return
        self.mon_key = key
        self.monitors = mons
        for o in self.overlays:
            o.destroy()
        self.overlays = [Overlay(self.root) for _ in mons]
        self.build_screens()
        self.apply()

    def apply(self):
        if len(self.overlays) != len(self.monitors) or len(self.scr_on) != len(self.monitors):
            return
        autohide, edge = taskbar_info()
        changed = False
        for i, mon in enumerate(self.monitors):
            on = self.master_on.get() and self.scr_on[i].get()
            alpha = round(self.scr_level[i].get() / 100.0 * MAX_ALPHA, 3)
            rect = overlay_rect(mon, self.dim_taskbar.get(), autohide, edge)
            if self.overlays[i].update(rect, alpha, on):
                changed = True
        if changed:
            self.lift_ui()

    def lift_ui(self):
        """ticked: Dimmer window sits above the dim layer. unticked: dim layer covers it too"""
        if self.hidden:
            return
        if self.keep_bright.get():
            self.root.attributes("-topmost", True)
            self.root.lift()
        else:
            self.root.attributes("-topmost", False)   # the dim layer is topmost, so it wins

    def on_bright(self):
        self.lift_ui()
        self.on_change()

    def tick(self):
        try:
            self.refresh_monitors()
            self.apply()
        finally:
            self.root.after(1000, self.tick)

    # --------------------------------------------------------------- updates
    def set_update_btn(self, text, mode):
        """mode: normal (grey) | green | busy (greyed out, can't be clicked)"""
        bg, fg, hover = {"normal": ("#333333", FG, "#444444"),
                         "green": (GREEN, "#ffffff", GREEN_HOVER),
                         "busy": ("#2b2b2b", "#8a8a8a", "#2b2b2b")}[mode]
        self.upd_btn.config(text=text, bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                            disabledforeground=fg, state="disabled" if mode == "busy" else "normal")

    def on_update_click(self):
        if self.ready_file:
            self.install_update(self.ready_file)      # "Restart now"
        elif self.pending_release:
            self.start_update()
        else:
            self.check_updates()

    def check_updates(self, silent=False):
        if self.busy or self.ready_file or (silent and self.pending_release):
            return
        if GITHUB_REPO.startswith("YOUR-"):
            if not silent:
                self.upd_text.set("Put your GitHub name in GITHUB_REPO first")
            return
        if not silent:
            self.set_update_btn("Checking...", "busy")
            self.upd_text.set("Checking for updates...")
        threading.Thread(target=self._check_worker, args=(silent,), daemon=True).start()

    def _check_worker(self, silent):
        try:
            rel = fetch_latest_release()
            tag = rel.get("tag_name", "")
            newer = version_tuple(tag) > version_tuple(VERSION)
            self.q.put(("update_info", rel if newer else None, tag, silent))
        except Exception as e:
            err = classify_error(e)
            self.q.put(("update_error", err.kind, err.text, silent))

    def on_update_info(self, rel, tag, silent):
        if self.busy or self.ready_file:
            return
        if rel:
            self.pending_release = rel
            self.set_update_btn("Update now", "green")
            self.upd_text.set(f"{tag} is available (you have v{VERSION})")
        else:
            self.pending_release = None
            self.set_update_btn("Check for updates", "normal")
            if not silent:
                self.upd_text.set(f"You're up to date (v{VERSION})")

    def on_update_error(self, kind, text, silent):
        if silent:                        # the quiet check at startup: say nothing, maybe retry
            if kind == "network" and self.auto_tries < 3:
                self.auto_tries += 1
                self.root.after(30000, lambda: self.check_updates(silent=True))
            return
        self.busy = False
        if self.pending_release:
            self.set_update_btn("Update now", "green")
        else:
            self.set_update_btn("Check for updates", "normal")
        if kind == "network":
            self.upd_text.set("Weak or no internet. Check your connection and try again.")
        elif kind == "disk":
            self.upd_text.set(text)
        else:
            self.upd_text.set("Couldn't update automatically.")
            self.show_manual_popup(text)

    def show_manual_popup(self, reason):
        """nice window with the GitHub link, a copy button and an open-in-browser button"""
        url = f"https://github.com/{GITHUB_REPO}/releases/latest"
        self.show()
        if self.popup is not None:
            try:
                self.popup.destroy()
            except Exception:
                pass
        win = self.popup = tk.Toplevel(self.root)
        win.title("Dimmer - update problem")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        try:
            win.iconphoto(False, *self.icon_imgs)
        except Exception:
            pass
        pad = {"padx": 24}
        tk.Label(win, text="Couldn't update automatically", bg=BG, fg=ACC,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(22, 6), **pad)
        tk.Label(win, text=reason, bg=BG, fg=FG, font=("Segoe UI", 10), wraplength=420,
                 justify="left").pack(anchor="w", **pad)
        tk.Label(win, text="You can download the newest Dimmer.exe yourself from GitHub:", bg=BG,
                 fg="#aaaaaa", font=("Segoe UI", 10), wraplength=420,
                 justify="left").pack(anchor="w", pady=(16, 6), **pad)
        box = tk.Frame(win, bg="#2a2a2a", highlightthickness=1, highlightbackground="#444444")
        box.pack(fill="x", **pad)
        entry = tk.Entry(box, relief="flat", bd=0, bg="#2a2a2a", fg=FG, readonlybackground="#2a2a2a",
                         insertbackground=FG, highlightthickness=0, font=("Consolas", 10))
        entry.insert(0, url)
        entry.config(state="readonly")
        entry.pack(fill="x", padx=10, pady=9)
        row = tk.Frame(win, bg=BG)
        row.pack(fill="x", pady=(16, 6), **pad)

        def copy():
            win.clipboard_clear()
            win.clipboard_append(url)
            copy_btn.config(text="Copied!")
            win.after(1600, lambda: copy_btn.winfo_exists() and copy_btn.config(text="Copy link"))

        copy_btn = flat_button(row, "Copy link", copy)
        copy_btn.pack(side="left")
        flat_button(row, "Open in browser", lambda: webbrowser.open(url), bg=GREEN, hover=GREEN_HOVER,
                    fg="#ffffff").pack(side="left", padx=(8, 0))
        flat_button(row, "Close", win.destroy).pack(side="right")
        tk.Label(win, text="After downloading, close Dimmer and replace your old Dimmer.exe with the new one.",
                 bg=BG, fg="#888888", font=("Segoe UI", 9), wraplength=420,
                 justify="left").pack(anchor="w", pady=(8, 22), **pad)
        win.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_y() + 40
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.focus_force()

    def start_update(self):
        rel = self.pending_release
        if not getattr(sys, "frozen", False):
            # running as a .pyw script: just open the download page
            webbrowser.open(rel.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases")
            self.upd_text.set("Opened the download page")
            return
        asset = find_asset(rel, UPDATE_ASSET)
        if asset is None:
            self.on_update_error("github", f"The newest release doesn't contain {UPDATE_ASSET}.", False)
            return
        self.busy = True
        self.set_update_btn("Downloading...", "busy")      # changes at once, no flicker
        self.upd_text.set("Downloading... 0%")
        threading.Thread(target=self._download_worker, args=(rel, asset), daemon=True).start()

    def _download_worker(self, rel, asset):
        new = os.path.join(os.path.dirname(sys.executable), "Dimmer_update.exe")
        try:
            want = expected_sha256(rel, asset)
            got = download_asset(asset, new, lambda p: self.q.put(("update_progress", p)))
            with open(new, "rb") as f:
                is_exe = f.read(2) == b"MZ"
            if want and got != want:
                raise UpdateError("github", "The downloaded file didn't match its checksum, so the "
                                            "update was cancelled to keep you safe.")
            if not is_exe:
                raise UpdateError("github", "The downloaded file doesn't look like the Dimmer program.")
            self.q.put(("update_ready", new))
        except Exception as e:
            try:
                os.remove(new)
            except OSError:
                pass
            err = classify_error(e)
            self.q.put(("update_error", err.kind, err.text, False))

    def on_update_ready(self, new_file):
        self.busy = False
        self.ready_file = new_file
        self.set_update_btn("Restart now", "green")
        self.upd_text.set("Update downloaded. Restart Dimmer to finish the update.")
        self.show()      # bring the window up so the Restart button is seen

    def taskbar_guard(self):
        """auto-hide taskbar + 'Dim taskbar too' unticked: keep the taskbar above the dim layer"""
        try:
            if self.master_on.get() and not self.dim_taskbar.get() and not fullscreen_busy():
                autohide, _ = taskbar_info()
                if autohide:
                    lift_taskbars()
        except Exception:
            pass
        finally:
            self.root.after(250, self.taskbar_guard)

    def clean_old_update(self):
        if getattr(sys, "frozen", False):
            try:
                os.remove(os.path.join(os.path.dirname(sys.executable), "Dimmer_update.exe"))
            except OSError:
                pass

    def fix_shortcuts_once(self):
        """once per version: re-point old Dimmer shortcuts/pins at this Dimmer.exe"""
        if not getattr(sys, "frozen", False):
            return
        key = f"{sys.executable}|{VERSION}"
        if self.cfg.get("shortcuts_checked") == key:
            return

        def work():
            try:
                fix_shortcuts(sys.executable)
                self.q.put(("shortcuts_done", key))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def install_update(self, new_file):
        """a small helper swaps the files after Dimmer closes, then starts the new one"""
        target = sys.executable
        bat = os.path.join(tempfile.gettempdir(), "dimmer_update.bat")
        with open(bat, "w", encoding="utf-8") as f:
            f.write(UPDATE_BAT.replace("NEWFILE", new_file).replace("TARGET", target))
        env = dict(os.environ)
        env.pop("_MEIPASS2", None)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen(["cmd", "/c", bat], env=env, close_fds=True,
                         creationflags=0x08000000 | 0x00000008)   # no window, detached
        self.exit_app()

    # ------------------------------------------------------- window / tray
    def start_tray(self):
        if self.tray is not None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda i, it: self.q.put("show"), default=True),
            pystray.MenuItem("Dimmer on", lambda i, it: self.q.put("toggle"),
                             checked=lambda it: self.master_flag),
            pystray.MenuItem("Exit", lambda i, it: self.q.put("exit")),
        )
        self.tray = pystray.Icon(APP, make_icon(64, TRAY_ICON_STYLE), APP, menu)
        self.tray.run_detached()

    def stop_tray(self):
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None

    def show(self):
        self.hidden = False
        self.start_tray()
        self.root.deiconify()
        self.root.state("normal")
        self.lift_ui()

    def hide_to_tray(self):
        self.start_tray()
        self.root.withdraw()
        self.hidden = True

    def exit_app(self):
        self.save()
        self.stop_tray()
        for o in self.overlays:
            o.destroy()
        self.root.destroy()

    def poll(self):
        try:
            while True:
                cmd = self.q.get_nowait()
                if cmd == "show":
                    self.show()
                elif cmd == "hotkey":
                    if self.hidden:
                        self.show()
                    else:
                        self.hide_to_tray()
                elif cmd == "toggle":
                    self.master_on.set(not self.master_on.get())
                    self.on_master_toggle()
                elif cmd == "exit":
                    self.exit_app()
                    return
                elif isinstance(cmd, tuple):
                    kind = cmd[0]
                    if kind == "update_info":
                        self.on_update_info(*cmd[1:])
                    elif kind == "update_progress":
                        if self.busy:
                            self.upd_text.set(f"Downloading... {cmd[1]}%")
                    elif kind == "update_error":
                        self.on_update_error(*cmd[1:])
                    elif kind == "update_ready":
                        self.on_update_ready(cmd[1])
                    elif kind == "shortcuts_done":
                        self.cfg["shortcuts_checked"] = cmd[1]
                        self.save()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    DimmerApp().run()
