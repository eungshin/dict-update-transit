"""
hotkey_daemon.py — System-wide hotkey daemon for CLI Dictionary.

Architecture overview:
- Main thread: Tkinter mainloop + Win32 hotkey polling via root.after()
- Worker thread: lookup_word() calls, results pushed onto a queue.Queue
- WM_HOTKEY intercepted via PeekMessageW (ctypes) in after() callback
- SendInput (ctypes) injects Ctrl+C; clipboard read after CLIPBOARD_WAIT_MS
- Clipboard save/restore around capture to avoid clobbering user data

Public pure functions (no Win32/Tk dependencies, fully unit-testable):
    extract_word(text: str) -> str | None
    clamp_position(x, y, w, h, screen_w, screen_h) -> tuple[int, int]

Module constants consumed by both T01 (tests) and T02 (daemon):
    HOTKEY_ID, CLIPBOARD_WAIT_MS, POLL_INTERVAL_MS, POPUP_WIDTH, POPUP_MAX_HEIGHT
"""

from __future__ import annotations

# Load .env from project directory if present
try:
    from dotenv import load_dotenv as _load_dotenv
    from pathlib import Path as _Path
    _load_dotenv(_Path(__file__).parent / ".env")
except Exception:
    pass

import ctypes
import ctypes.wintypes
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

import win32clipboard
import win32con
import win32gui

from dictionary import format_definition, lookup_word
from ai_context import pick_definition, explain_phrase
from config import ensure_config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOTKEY_ID: int = 1           # Win32 RegisterHotKey identifier
CLIPBOARD_WAIT_MS: int = 100  # ms to wait after keybd_event before reading clipboard
POLL_INTERVAL_MS: int = 20   # ms between WM_HOTKEY polls in root.after()
POPUP_WIDTH: int = 420        # popup window width in pixels
POPUP_MAX_HEIGHT: int = 300   # popup window max height in pixels

# Win32 constants
WM_HOTKEY: int = 0x0312
PM_REMOVE: int = 0x0001
VK_D: int = 0x44

# ---------------------------------------------------------------------------
# ctypes structures for PeekMessageW and SendInput
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.wintypes.HWND),
        ("message", ctypes.wintypes.UINT),
        ("wParam", ctypes.wintypes.WPARAM),
        ("lParam", ctypes.wintypes.LPARAM),
        ("time", ctypes.wintypes.DWORD),
        ("pt", POINT),
    ]


# INPUT structures for SendInput
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("_input", _INPUT_UNION)]


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def extract_word(text: str) -> str | None:
    """Extract a word or phrase from raw clipboard text.

    Returns the stripped text if it contains at least one non-whitespace
    character and is not excessively long (max 60 chars after strip).
    Returns None when:
    - *text* is empty or whitespace-only after stripping
    - the stripped text is longer than 60 characters (likely a sentence, not a phrase)

    Single words, hyphenated words, apostrophes, and multi-word idioms/phrases
    are all accepted — 'well-known', "don't", 'kick the bucket' all pass.
    """
    if not isinstance(text, str):
        return None

    stripped = text.strip()

    if not stripped:
        return None

    # Reject if too long — likely a full sentence, not a word/phrase
    if len(stripped) > 60:
        return None

    # Normalize internal whitespace (tabs/newlines → single space)
    import re
    normalized = re.sub(r'\s+', ' ', stripped)

    return normalized


def clamp_position(
    x: int,
    y: int,
    w: int,
    h: int,
    screen_w: int,
    screen_h: int,
) -> tuple[int, int]:
    """Clamp a popup's top-left corner so it stays fully on-screen.

    Parameters
    ----------
    x, y:
        Proposed top-left corner of the popup (pixels).
    w, h:
        Width and height of the popup (pixels).
    screen_w, screen_h:
        Screen dimensions (pixels).

    Returns
    -------
    (clamped_x, clamped_y) — guaranteed to satisfy:
        0 <= clamped_x  and  clamped_x + w <= screen_w
        0 <= clamped_y  and  clamped_y + h <= screen_h
    (If the popup is wider/taller than the screen it is placed at 0.)
    """
    if x + w > screen_w:
        x = screen_w - w
    if y + h > screen_h:
        y = screen_h - h
    if x < 0:
        x = 0
    if y < 0:
        y = 0
    return (x, y)


# ---------------------------------------------------------------------------
# Font loader
# ---------------------------------------------------------------------------

def _load_pretendard() -> str:
    """Load Pretendard Variable font and return the family name to use in tkinter.

    Falls back to 'Segoe UI' if font file is missing or loading fails.
    """
    try:
        font_path = Path(__file__).parent / "fonts" / "public" / "variable" / "PretendardVariable.ttf"
        if font_path.exists():
            FR_PRIVATE = 0x10
            result = ctypes.windll.gdi32.AddFontResourceExW(str(font_path), FR_PRIVATE, None)
            if result > 0:
                return "Pretendard Variable"
    except Exception:
        pass
    return "Segoe UI"


_POPUP_FONT_FAMILY = _load_pretendard()

# ---------------------------------------------------------------------------
# PopupWindow
# ---------------------------------------------------------------------------


class PopupWindow:
    """Modern dark-mode definition popup positioned near the cursor.

    Design: Dark Mode OLED style — deep black bg, high contrast text,
    color-coded typography for word/phonetic/POS/definitions/examples.
    """

    # ── Design tokens (OLED dark, ui-ux-pro-max compliant)
    _BG       = "#0f1219"   # deep dark
    _BG_CARD  = "#161b26"   # card surface
    _BORDER   = "#1e293b"   # subtle border
    _FG       = "#e2e8f0"   # primary text (slate-200)
    _FG_MUTED = "#64748b"   # muted text (slate-500)
    _WORD     = "#f1f5f9"   # word heading (slate-100, bold)
    _PHONETIC = "#38bdf8"   # phonetic (sky-400)
    _POS      = "#22c55e"   # part of speech (green-500)
    _NUM      = "#a78bfa"   # definition number (violet-400)
    _EXAMPLE  = "#94a3b8"   # example text (slate-400)
    _DIVIDER  = "#1e293b"   # section divider

    def __init__(self, root: tk.Tk, result: dict | None, x: int, y: int,
                 font_family: str = "Pretendard Variable",
                 font_size: int = 11) -> None:
        self.root = root
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.97)
        self.top.configure(bg=self._BG)

        # Shadow frame (outer border glow)
        shadow = tk.Frame(self.top, bg=self._BORDER, padx=1, pady=1)
        shadow.pack(fill=tk.BOTH, expand=True)

        inner = tk.Frame(shadow, bg=self._BG, padx=14, pady=10)
        inner.pack(fill=tk.BOTH, expand=True)

        # Font setup
        actual_family = font_family if font_family in tkfont.families() else _POPUP_FONT_FAMILY
        body_font = tkfont.Font(family=actual_family, size=font_size)
        word_font = tkfont.Font(family=actual_family, size=font_size + 4, weight="bold")
        phonetic_font = tkfont.Font(family=actual_family, size=font_size + 1)
        pos_font = tkfont.Font(family=actual_family, size=font_size, weight="bold")
        num_font = tkfont.Font(family=actual_family, size=font_size)
        example_font = tkfont.Font(family=actual_family, size=font_size - 1, slant="italic")

        # Scrollable text widget with tag-based styling
        txt = tk.Text(
            inner, wrap=tk.WORD, font=body_font,
            bg=self._BG, fg=self._FG,
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
            width=52, spacing1=2, spacing3=2,
            cursor="arrow", padx=2, pady=2,
        )

        # Define text tags for color coding
        txt.tag_configure("word", font=word_font, foreground=self._WORD,
                          spacing3=4)
        txt.tag_configure("phonetic", font=phonetic_font,
                          foreground=self._PHONETIC, spacing3=6)
        txt.tag_configure("pos", font=pos_font, foreground=self._POS,
                          spacing1=8, spacing3=2)
        txt.tag_configure("divider", foreground=self._DIVIDER,
                          font=body_font, spacing1=4, spacing3=4)
        txt.tag_configure("num", font=num_font, foreground=self._NUM)
        txt.tag_configure("defn", font=body_font, foreground=self._FG,
                          lmargin1=20, lmargin2=20)
        txt.tag_configure("example", font=example_font,
                          foreground=self._EXAMPLE,
                          lmargin1=28, lmargin2=28, spacing1=1, spacing3=3)
        txt.tag_configure("notfound", font=body_font,
                          foreground=self._FG_MUTED)

        # Populate with rich formatting
        if result is None:
            txt.insert(tk.END, "Word not found.", "notfound")
        else:
            word = result.get("word", "")
            phonetic = result.get("phonetic")

            txt.insert(tk.END, word, "word")
            if phonetic:
                txt.insert(tk.END, f"  {phonetic}", "phonetic")
            txt.insert(tk.END, "\n")

            for mi, meaning in enumerate(result.get("meanings", [])):
                pos = meaning.get("part_of_speech", "")
                if mi > 0:
                    txt.insert(tk.END, "\u2500" * 36 + "\n", "divider")
                txt.insert(tk.END, f"{pos}\n", "pos")

                for di, defn in enumerate(meaning.get("definitions", []), 1):
                    definition_text = defn.get("definition", "")
                    txt.insert(tk.END, f" {di}. ", "num")
                    txt.insert(tk.END, f"{definition_text}\n", "defn")

                    example = defn.get("example")
                    if example:
                        txt.insert(tk.END, f'  "{example}"\n', "example")

        txt.configure(state=tk.DISABLED)

        # Compute height
        line_count = int(txt.index(tk.END).split(".")[0])
        line_h = body_font.metrics("linespace")
        raw_h = line_count * line_h + 28
        popup_h = max(80, min(raw_h, POPUP_MAX_HEIGHT))

        if raw_h > POPUP_MAX_HEIGHT:
            scrollbar = tk.Scrollbar(inner, command=txt.yview,
                                     bg=self._BG, troughcolor=self._BG_CARD,
                                     highlightthickness=0, borderwidth=0)
            txt.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 0))

        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Position with screen-edge clamping
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        cx, cy = clamp_position(x, y, POPUP_WIDTH, popup_h, screen_w, screen_h)
        self.top.geometry(f"{POPUP_WIDTH}x{popup_h}+{cx}+{cy}")

        # Focus + bindings
        self.top.focus_force()
        self.top.bind("<Escape>", lambda _e: self.dismiss())
        self.top.bind("<FocusOut>", self._on_focus_out)
        txt.bind("<Escape>", lambda _e: self.dismiss())

        logger.info("Popup shown at (%d, %d)", cx, cy)

    def _on_focus_out(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if event.widget is self.top:
            self.dismiss()

    def dismiss(self) -> None:
        if self.top is not None:
            logger.info("Popup dismissed")
            self.top.destroy()
            self.top = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# HotkeyDaemon
# ---------------------------------------------------------------------------


class HotkeyDaemon:
    """System-wide Ctrl+Shift+D hotkey daemon."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()  # hidden root window
        self._queue: queue.Queue[dict | None] = queue.Queue()
        self._popup: PopupWindow | None = None
        self._saved_clipboard: str | None = None
        self._config = ensure_config()
        self._tray = None
        self._paused = False
        logger.info("Config loaded: pronunciation=%s ai_enabled=%s",
                    self._config.get("pronunciation"), self._config.get("ai_enabled"))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._hotkey_queue: queue.Queue[bool] = queue.Queue()
        self._stop_event = threading.Event()
        self._hotkey_ready = threading.Event()

        listener = threading.Thread(target=self._hotkey_listener, daemon=True)
        listener.start()

        # Wait for listener thread to register hotkey before continuing
        self._hotkey_ready.wait(timeout=3.0)

        try:
            self._start_tray()
            self.root.after(POLL_INTERVAL_MS, self._poll)
            self.root.mainloop()
        finally:
            self._stop_event.set()
            if self._tray is not None:
                try:
                    self._tray.stop()
                except Exception:
                    pass
            logger.info("Daemon stopped.")

    def _hotkey_listener(self) -> None:
        """Dedicated thread: registers hotkey and blocks on GetMessage."""
        win32gui.RegisterHotKey(
            None,
            HOTKEY_ID,
            win32con.MOD_CONTROL | win32con.MOD_SHIFT,
            VK_D,
        )
        err = ctypes.GetLastError()
        if err != 0:
            logger.error("Failed to register hotkey (error=%d, already in use?)", err)
            self._hotkey_ready.set()
            self.root.after(0, self.root.quit)
            return

        logger.info("Hotkey Ctrl+Shift+D registered (id=%d). Listening…", HOTKEY_ID)
        self._hotkey_ready.set()

        msg = MSG()
        while not self._stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY)
            if ret <= 0:
                break
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                logger.info("Hotkey triggered")
                self._hotkey_queue.put(True)

        win32gui.UnregisterHotKey(None, HOTKEY_ID)
        logger.info("Hotkey unregistered.")

    # ------------------------------------------------------------------
    # System tray icon
    # ------------------------------------------------------------------

    def _make_tray_image(self, paused: bool = False) -> "Image.Image":
        from PIL import Image, ImageDraw, ImageFont
        color = (80, 80, 80) if paused else (30, 30, 70)
        img = Image.new("RGB", (64, 64), color=color)
        draw = ImageDraw.Draw(img)
        try:
            fnt = ImageFont.truetype("arial.ttf", 40)
        except Exception:
            fnt = ImageFont.load_default()
        letter = "❚❚" if paused else "D"
        text_color = (160, 160, 160) if paused else (100, 200, 255)
        try:
            bbox = draw.textbbox((0, 0), letter, font=fnt)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(((64 - tw) // 2, (64 - th) // 2 - 2), letter, fill=text_color, font=fnt)
        except Exception:
            draw.text((16, 12), "D", fill=text_color, font=fnt)
        return img

    def _start_tray(self) -> None:
        try:
            import pystray
            from setup_autostart import create_shortcut, is_installed, remove_shortcut

            def _status_label(item):
                return "⏸ Paused" if self._paused else "▶ Running"

            def _pause_label(item):
                return "Resume" if self._paused else "Pause"

            def _toggle_pause(icon, item):
                self._paused = not self._paused
                icon.icon = self._make_tray_image(self._paused)
                icon.title = "Dict Tool (paused)" if self._paused else "Dict Tool"
                logger.info("Daemon %s", "paused" if self._paused else "resumed")

            def _toggle_autostart(icon, item):
                if is_installed():
                    remove_shortcut()
                    logger.info("Auto-start disabled via tray")
                else:
                    create_shortcut()
                    logger.info("Auto-start enabled via tray")

            def _autostart_label(item):
                return "Disable auto-start" if is_installed() else "Enable auto-start"

            def _open_settings(icon, item):
                self.root.after(0, self._show_settings)

            menu = pystray.Menu(
                pystray.MenuItem(_status_label, None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(_pause_label, _toggle_pause),
                pystray.MenuItem("Settings…", _open_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(_autostart_label, _toggle_autostart),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit_from_tray),
            )
            icon = pystray.Icon(
                "dict-tool",
                self._make_tray_image(False),
                "Dict Tool",
                menu,
            )
            self._tray = icon

            t = threading.Thread(target=icon.run, daemon=True)
            t.start()
            logger.info("System tray icon started")
        except Exception as exc:
            logger.warning("Could not start tray icon: %s", exc)

    def _quit_from_tray(self, icon, item) -> None:  # type: ignore[type-arg]
        logger.info("Quit requested from tray")
        self.root.after(0, self.root.quit)

    # ------------------------------------------------------------------
    # Settings window
    # ------------------------------------------------------------------

    def _show_settings(self) -> None:
        """Open a modern CustomTkinter settings dialog on the main thread."""
        import customtkinter as ctk
        import os as _os

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")

        GEMINI_VOICES = ["Kore", "Puck", "Charon", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr"]
        VOICE_DESC = {
            "Kore": "Female, warm", "Puck": "Male, friendly",
            "Charon": "Male, deep", "Fenrir": "Male, strong",
            "Aoede": "Female, bright", "Leda": "Female, calm",
            "Orus": "Male, clear", "Zephyr": "Female, soft",
        }
        FONT_OPTIONS = ["Pretendard Variable", "Segoe UI", "Consolas", "Arial", "Malgun Gothic"]

        # ── Design tokens
        BG_DARK   = "#0f1219"
        CARD_BG   = "#161b26"
        CARD_BDR  = "#1e293b"
        ACCENT    = "#22C55E"
        ACCENT2   = "#16A34A"
        ACCENT_DIM = "#134e2a"
        BLUE      = "#38BDF8"
        MUTED     = "#64748b"
        FG        = "#e2e8f0"
        WARN      = "#f59e0b"

        win = ctk.CTkToplevel(self.root)
        win.title("Dict Tool — Settings")
        win.geometry("480x760")
        win.resizable(False, True)
        win.attributes("-topmost", True)
        win.configure(fg_color=BG_DARK)
        win.grab_set()

        # ────────────────────────────────────────────
        # Title bar
        # ────────────────────────────────────────────
        title_frame = ctk.CTkFrame(win, height=56, corner_radius=0,
                                   fg_color=CARD_BG, border_width=0)
        title_frame.pack(fill="x")
        title_frame.pack_propagate(False)

        title_left = ctk.CTkFrame(title_frame, fg_color="transparent")
        title_left.pack(side="left", padx=20, fill="y")
        ctk.CTkLabel(title_left, text="Settings",
                     font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=FG).pack(side="left", pady=14)
        ctk.CTkLabel(title_left, text="  Dict Tool",
                     font=ctk.CTkFont(size=12),
                     text_color=MUTED).pack(side="left", pady=14)

        # ────────────────────────────────────────────
        # Scrollable body
        # ────────────────────────────────────────────
        body = ctk.CTkScrollableFrame(win, fg_color="transparent",
                                      scrollbar_button_color=CARD_BDR,
                                      scrollbar_button_hover_color=MUTED)
        body.pack(fill="both", expand=True, padx=0, pady=0)

        # ── Helpers
        def card(parent):
            c = ctk.CTkFrame(parent, fg_color=CARD_BG, corner_radius=12,
                             border_width=1, border_color=CARD_BDR)
            c.pack(fill="x", padx=16, pady=(0, 12))
            return c

        def section_header(parent, icon, text):
            hdr = ctk.CTkFrame(parent, fg_color="transparent")
            hdr.pack(fill="x", padx=16, pady=(18, 6))
            ctk.CTkLabel(hdr, text=f"{icon}  {text}",
                         font=ctk.CTkFont(size=12, weight="bold"),
                         text_color=BLUE).pack(side="left")

        def labeled_row(parent, label_text, padx=16, pady=6):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=padx, pady=pady)
            ctk.CTkLabel(row, text=label_text, anchor="w",
                         font=ctk.CTkFont(size=13),
                         text_color=FG).pack(side="left")
            return row

        def sub_label(parent, text, padx=16):
            ctk.CTkLabel(parent, text=text,
                         font=ctk.CTkFont(size=10),
                         text_color=MUTED, anchor="w").pack(
                             fill="x", padx=padx, pady=(0, 8))

        # ────────────────────────────────────────────
        # Section 1: Features
        # ────────────────────────────────────────────
        section_header(body, "\u25ce", "FEATURES")
        feat_card = card(body)

        pron_row = labeled_row(feat_card, "발음 읽기 (TTS)")
        pronunciation_var = ctk.StringVar(
            value="on" if self._config.get("pronunciation", True) else "off")
        ctk.CTkSwitch(pron_row, text="", variable=pronunciation_var,
                      onvalue="on", offvalue="off",
                      button_color=ACCENT, button_hover_color=ACCENT2,
                      progress_color=ACCENT_DIM,
                      width=46).pack(side="right")

        ai_row = labeled_row(feat_card, "AI 문맥 의미 선택")
        ai_var = ctk.StringVar(
            value="on" if self._config.get("ai_enabled", True) else "off")
        ctk.CTkSwitch(ai_row, text="", variable=ai_var,
                      onvalue="on", offvalue="off",
                      button_color=ACCENT, button_hover_color=ACCENT2,
                      progress_color=ACCENT_DIM,
                      width=46).pack(side="right")

        # ────────────────────────────────────────────
        # Section 2: TTS Engine
        # ────────────────────────────────────────────
        section_header(body, "\u266b", "TTS ENGINE")
        tts_card = card(body)

        engine_row = labeled_row(tts_card, "엔진")
        engine_var = ctk.StringVar(value=self._config.get("tts_engine", "pyttsx3"))

        engine_seg = ctk.CTkSegmentedButton(
            engine_row, values=["pyttsx3", "gemini"],
            variable=engine_var,
            font=ctk.CTkFont(size=12),
            selected_color=ACCENT, selected_hover_color=ACCENT2,
            unselected_color=CARD_BDR, unselected_hover_color="#2d3748",
            width=200)
        engine_seg.pack(side="right")

        # Conditional panels (packed inside tts_card)
        pyttsx3_frame = ctk.CTkFrame(tts_card, fg_color="transparent")
        gemini_frame = ctk.CTkFrame(tts_card, fg_color="transparent")

        def _on_engine_change(value):
            if value == "pyttsx3":
                gemini_frame.pack_forget()
                pyttsx3_frame.pack(fill="x", padx=4, pady=(4, 8))
            else:
                pyttsx3_frame.pack_forget()
                gemini_frame.pack(fill="x", padx=4, pady=(4, 8))

        engine_seg.configure(command=_on_engine_change)

        # ── pyttsx3 sub-panel
        spd_var = tk.IntVar(value=int(self._config.get("tts_rate", 150)))
        vol_var = tk.IntVar(value=int(self._config.get("tts_volume", 100)))

        spd_row = ctk.CTkFrame(pyttsx3_frame, fg_color="transparent")
        spd_row.pack(fill="x", padx=12, pady=(8, 4))
        ctk.CTkLabel(spd_row, text="속도", width=50, anchor="w",
                     font=ctk.CTkFont(size=12), text_color=FG).pack(side="left")
        spd_val_label = ctk.CTkLabel(spd_row, text=str(spd_var.get()), width=36,
                                     font=ctk.CTkFont(size=12, weight="bold"),
                                     text_color=ACCENT)
        spd_val_label.pack(side="right")
        spd_slider = ctk.CTkSlider(
            spd_row, from_=80, to=250, number_of_steps=34,
            button_color=ACCENT, button_hover_color=ACCENT2,
            progress_color=ACCENT_DIM,
            command=lambda v: (spd_var.set(int(v)),
                               spd_val_label.configure(text=str(int(v)))))
        spd_slider.set(spd_var.get())
        spd_slider.pack(side="left", fill="x", expand=True, padx=(8, 8))

        vol_row_f = ctk.CTkFrame(pyttsx3_frame, fg_color="transparent")
        vol_row_f.pack(fill="x", padx=12, pady=(4, 8))
        ctk.CTkLabel(vol_row_f, text="볼륨", width=50, anchor="w",
                     font=ctk.CTkFont(size=12), text_color=FG).pack(side="left")
        vol_val_label = ctk.CTkLabel(vol_row_f, text=str(vol_var.get()), width=36,
                                     font=ctk.CTkFont(size=12, weight="bold"),
                                     text_color=ACCENT)
        vol_val_label.pack(side="right")
        vol_slider = ctk.CTkSlider(
            vol_row_f, from_=0, to=100, number_of_steps=20,
            button_color=ACCENT, button_hover_color=ACCENT2,
            progress_color=ACCENT_DIM,
            command=lambda v: (vol_var.set(int(v)),
                               vol_val_label.configure(text=str(int(v)))))
        vol_slider.set(vol_var.get())
        vol_slider.pack(side="left", fill="x", expand=True, padx=(8, 8))

        # ── Gemini sub-panel
        voice_var = ctk.StringVar(value=self._config.get("gemini_voice_name", "Kore"))

        voice_row = ctk.CTkFrame(gemini_frame, fg_color="transparent")
        voice_row.pack(fill="x", padx=12, pady=(8, 2))
        ctk.CTkLabel(voice_row, text="음성", width=50, anchor="w",
                     font=ctk.CTkFont(size=12), text_color=FG).pack(side="left")

        voice_desc_label = ctk.CTkLabel(
            voice_row, text=VOICE_DESC.get(voice_var.get(), ""),
            font=ctk.CTkFont(size=10), text_color=MUTED, width=90)
        voice_desc_label.pack(side="right", padx=(0, 8))

        def _on_voice_change(val):
            voice_desc_label.configure(text=VOICE_DESC.get(val, ""))

        ctk.CTkOptionMenu(
            voice_row, values=GEMINI_VOICES, variable=voice_var,
            font=ctk.CTkFont(size=12), command=_on_voice_change,
            button_color=ACCENT, button_hover_color=ACCENT2,
            fg_color=CARD_BDR, dropdown_fg_color=CARD_BG,
            dropdown_hover_color=CARD_BDR,
            width=140).pack(side="right")

        # Preview button
        preview_row = ctk.CTkFrame(gemini_frame, fg_color="transparent")
        preview_row.pack(fill="x", padx=12, pady=(2, 4))

        def _preview_voice():
            preview_btn.configure(text="Playing...", state="disabled")
            win.update_idletasks()
            import threading
            def _play():
                try:
                    self._config["gemini_voice_name"] = voice_var.get()
                    self._speak_gemini("dictionary")
                except Exception:
                    pass
                win.after(0, lambda: preview_btn.configure(
                    text="Preview", state="normal"))
            threading.Thread(target=_play, daemon=True).start()

        preview_btn = ctk.CTkButton(
            preview_row, text="Preview", command=_preview_voice,
            font=ctk.CTkFont(size=11),
            fg_color=CARD_BDR, hover_color="#2d3748",
            text_color=FG, corner_radius=6,
            width=80, height=28)
        preview_btn.pack(side="left")

        # API key status
        has_key = bool(_os.environ.get("GEMINI_API_KEY", "").strip())
        key_color = ACCENT if has_key else WARN
        key_text = "API Key: Connected" if has_key else "GEMINI_API_KEY not set in .env"
        ctk.CTkLabel(gemini_frame, text=key_text,
                     font=ctk.CTkFont(size=10), text_color=key_color
                     ).pack(padx=12, pady=(0, 8), anchor="w")

        _on_engine_change(engine_var.get())

        # ────────────────────────────────────────────
        # Section 3: AI 설명 방식
        # ────────────────────────────────────────────
        section_header(body, "\u2726", "AI EXPLANATION")
        ai_card = card(body)

        lang_row = labeled_row(ai_card, "언어")
        lang_var = ctk.StringVar(value=self._config.get("ai_language", "ko"))
        lang_map = {"한국어": "ko", "English": "en", "한영 혼용": "mixed"}
        lang_reverse = {v: k for k, v in lang_map.items()}
        lang_display = ctk.StringVar(value=lang_reverse.get(lang_var.get(), "한국어"))

        def _on_lang(val):
            lang_var.set(lang_map.get(val, "ko"))

        ctk.CTkSegmentedButton(
            lang_row, values=["한국어", "English", "한영 혼용"],
            variable=lang_display, command=_on_lang,
            font=ctk.CTkFont(size=11),
            selected_color=ACCENT, selected_hover_color=ACCENT2,
            unselected_color=CARD_BDR, unselected_hover_color="#2d3748",
            width=250).pack(side="right")

        style_row = labeled_row(ai_card, "스타일")
        style_var = ctk.StringVar(value=self._config.get("ai_style", "detailed"))
        style_map = {"상세": "detailed", "간결": "concise"}
        style_reverse = {v: k for k, v in style_map.items()}
        style_display = ctk.StringVar(value=style_reverse.get(style_var.get(), "상세"))

        def _on_style(val):
            style_var.set(style_map.get(val, "detailed"))

        ctk.CTkSegmentedButton(
            style_row, values=["상세", "간결"],
            variable=style_display, command=_on_style,
            font=ctk.CTkFont(size=11),
            selected_color=ACCENT, selected_hover_color=ACCENT2,
            unselected_color=CARD_BDR, unselected_hover_color="#2d3748",
            width=140).pack(side="right")

        # 추가 지시
        ctk.CTkLabel(ai_card, text="추가 지시", anchor="w",
                     font=ctk.CTkFont(size=12),
                     text_color=MUTED).pack(fill="x", padx=16, pady=(8, 2))
        custom_entry = ctk.CTkTextbox(
            ai_card, height=70, corner_radius=8,
            font=ctk.CTkFont(size=12),
            fg_color=CARD_BDR, border_width=0,
            text_color=FG)
        custom_entry.pack(fill="x", padx=16, pady=(0, 4))
        saved_prompt = self._config.get("ai_custom_prompt", "")
        if saved_prompt:
            custom_entry.insert("0.0", saved_prompt)
        sub_label(ai_card, "e.g. 어원도 함께 설명해줘 / 반드시 예문 3개 포함해")

        # ────────────────────────────────────────────
        # Section 4: 팝업 폰트
        # ────────────────────────────────────────────
        section_header(body, "\u2766", "POPUP FONT")
        font_card = card(body)

        font_row = labeled_row(font_card, "폰트")
        font_var = ctk.StringVar(value=self._config.get("popup_font", "Pretendard Variable"))
        ctk.CTkOptionMenu(
            font_row, values=FONT_OPTIONS, variable=font_var,
            font=ctk.CTkFont(size=12),
            button_color=ACCENT, button_hover_color=ACCENT2,
            fg_color=CARD_BDR, dropdown_fg_color=CARD_BG,
            dropdown_hover_color=CARD_BDR,
            width=200).pack(side="right")

        size_row = labeled_row(font_card, "크기")
        size_var = tk.IntVar(value=int(self._config.get("popup_font_size", 11)))
        size_val_label = ctk.CTkLabel(
            size_row, text=f"{size_var.get()} pt", width=44,
            font=ctk.CTkFont(size=12, weight="bold"), text_color=ACCENT)
        size_val_label.pack(side="right")
        size_slider = ctk.CTkSlider(
            size_row, from_=8, to=20, number_of_steps=12,
            button_color=ACCENT, button_hover_color=ACCENT2,
            progress_color=ACCENT_DIM,
            command=lambda v: (size_var.set(int(v)),
                               size_val_label.configure(text=f"{int(v)} pt")))
        size_slider.set(size_var.get())
        size_slider.pack(side="left", fill="x", expand=True, padx=(0, 8))

        # Bottom spacer
        ctk.CTkFrame(body, height=8, fg_color="transparent").pack()

        # ────────────────────────────────────────────
        # Bottom button bar
        # ────────────────────────────────────────────
        btn_bar = ctk.CTkFrame(win, height=64, corner_radius=0,
                               fg_color=CARD_BG, border_width=0)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        def _save():
            self._config["pronunciation"]    = pronunciation_var.get() == "on"
            self._config["ai_enabled"]       = ai_var.get() == "on"
            self._config["tts_engine"]       = engine_var.get()
            self._config["gemini_voice_name"] = voice_var.get()
            self._config["ai_language"]      = lang_var.get()
            self._config["ai_style"]         = style_var.get()
            self._config["ai_custom_prompt"] = custom_entry.get("0.0", "end-1c").strip()
            self._config["popup_font"]       = font_var.get()
            self._config["popup_font_size"]  = size_var.get()
            self._config["tts_rate"]         = spd_var.get()
            self._config["tts_volume"]       = vol_var.get()
            from config import save_config
            save_config(self._config)
            logger.info("Settings saved — engine=%s voice=%s",
                        self._config["tts_engine"], self._config["gemini_voice_name"])
            save_btn.configure(text="  Saved!", fg_color=ACCENT2)
            win.after(600, win.destroy)

        save_btn = ctk.CTkButton(
            btn_bar, text="Save", command=_save,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT2,
            corner_radius=10, width=130, height=40)
        save_btn.pack(side="right", padx=20, pady=12)

        ctk.CTkButton(
            btn_bar, text="Cancel", command=win.destroy,
            font=ctk.CTkFont(size=13),
            fg_color="transparent", hover_color=CARD_BDR,
            text_color=MUTED, corner_radius=10,
            border_width=1, border_color=CARD_BDR,
            width=90, height=40).pack(side="right", pady=12)

        win.focus_force()

    # ------------------------------------------------------------------
    # Poll loop (runs on main thread via root.after)
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        # Drain hotkey signals from listener thread
        try:
            while True:
                self._hotkey_queue.get_nowait()
                self._on_hotkey()
        except queue.Empty:
            pass

        # Drain lookup results from worker thread
        try:
            while True:
                result = self._queue.get_nowait()
                self._show_popup(result)
        except queue.Empty:
            pass

        self.root.after(POLL_INTERVAL_MS, self._poll)

    # ------------------------------------------------------------------
    # Hotkey handler
    # ------------------------------------------------------------------

    def _on_hotkey(self) -> None:
        if self._paused:
            logger.info("Hotkey triggered but daemon is paused — skipping")
            return
        if self._popup is not None:
            self._popup.dismiss()
            self._popup = None

        self._saved_clipboard = self._save_clipboard()
        self._clear_clipboard()
        self._inject_ctrl_c()
        self.root.after(CLIPBOARD_WAIT_MS, self._read_clipboard_and_lookup)

    def _read_clipboard_and_lookup(self) -> None:
        text = self._read_clipboard()
        if text is None:
            logger.info("Clipboard empty after capture — skipping")
            self._restore_clipboard(self._saved_clipboard)
            return

        word = extract_word(text)
        if word is None:
            logger.info("Clipboard text not a single word (%r) — skipping", text[:40])
            self._restore_clipboard(self._saved_clipboard)
            return

        logger.info("Looking up: %r", word)
        # Keep raw clipboard text as sentence context (may be multi-word)
        sentence = text if text != word else word
        self._restore_clipboard(self._saved_clipboard)

        # Background lookup — result goes onto queue
        def _lookup() -> None:
            try:
                result = lookup_word(word)
                if result and result.get("meanings") and self._config.get("ai_enabled", True):
                    idx = pick_definition(word, sentence, result["meanings"])
                    if idx is not None and idx != 0:
                        meanings = result["meanings"]
                        reordered = [meanings[idx]] + meanings[:idx] + meanings[idx + 1:]
                        result = dict(result, meanings=reordered)
                        logger.info("AI reordered meanings: index %d promoted to front", idx)
                    self._queue.put(result)
                elif result is None:
                    # Dictionary has no entry — ask AI directly
                    if self._config.get("ai_enabled", True):
                        logger.info("No dictionary result for %r — asking AI", word)
                        explanation = explain_phrase(
                            word,
                            sentence,
                            language=self._config.get("ai_language", "ko"),
                            style=self._config.get("ai_style", "detailed"),
                            custom_suffix=self._config.get("ai_custom_prompt", ""),
                        )
                        if explanation:
                            # Wrap AI response in a pseudo-result dict for display
                            ai_result = {
                                "word": word,
                                "phonetic": None,
                                "meanings": [{
                                    "part_of_speech": "AI explanation",
                                    "definitions": [{"definition": explanation, "example": None}],
                                }],
                            }
                            self._queue.put(ai_result)
                        else:
                            self._queue.put(None)
                    else:
                        self._queue.put(None)
                else:
                    self._queue.put(result)
            except Exception as exc:
                logger.error("lookup failed: %s", exc)
                self._queue.put(None)

        t = threading.Thread(target=_lookup, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Popup display
    # ------------------------------------------------------------------

    def _show_popup(self, result: dict | None) -> None:
        word = result.get("word", "") if result else ""

        # Cursor position for popup placement (offset slightly below cursor)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        x, y = pt.x + 10, pt.y + 20

        self._popup = PopupWindow(
            self.root, result, x, y,
            font_family=self._config.get("popup_font", "Pretendard Variable"),
            font_size=int(self._config.get("popup_font_size", 11)),
        )

        # TTS pronunciation (background thread, never crashes daemon)
        if word and self._config.get("pronunciation", True):
            t = threading.Thread(target=self._speak, args=(word,), daemon=True)
            t.start()

    def _speak(self, word: str) -> None:
        engine = self._config.get("tts_engine", "pyttsx3")
        if engine == "gemini":
            self._speak_gemini(word)
        else:
            self._speak_pyttsx3(word)

    def _speak_pyttsx3(self, word: str) -> None:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", int(self._config.get("tts_rate", 150)))
            engine.setProperty("volume", self._config.get("tts_volume", 100) / 100.0)
            engine.say(word)
            engine.runAndWait()
            engine.stop()
            logger.info("TTS spoke (pyttsx3): %r", word)
        except Exception as exc:
            logger.warning("TTS pyttsx3 failed: %s", exc)

    def _speak_gemini(self, word: str) -> None:
        try:
            import os
            import tempfile
            import wave
            import winsound
            from google import genai
            from google.genai import types

            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not api_key:
                logger.warning("GEMINI_API_KEY not set — falling back to pyttsx3")
                self._speak_pyttsx3(word)
                return

            voice_name = self._config.get("gemini_voice_name", "Kore")
            client = genai.Client(api_key=api_key)

            response = client.models.generate_content(
                model="gemini-2.5-flash-preview-tts",
                contents=f"Say clearly: {word}",
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice_name,
                            )
                        )
                    ),
                ),
            )

            pcm_data = response.candidates[0].content.parts[0].inline_data.data
            tmp_path = os.path.join(tempfile.gettempdir(), "dict_gemini_tts.wav")
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)   # 16-bit PCM
                wf.setframerate(24000)
                wf.writeframes(pcm_data)

            winsound.PlaySound(tmp_path, winsound.SND_FILENAME)
            os.unlink(tmp_path)
            logger.info("TTS spoke (gemini/%s): %r", voice_name, word)
        except Exception as exc:
            logger.warning("TTS gemini failed: %s — falling back to pyttsx3", exc)
            self._speak_pyttsx3(word)

    # ------------------------------------------------------------------
    # Clipboard helpers
    # ------------------------------------------------------------------

    def _save_clipboard(self) -> str | None:
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            logger.warning("Could not save clipboard: %s", exc)
        return None

    def _restore_clipboard(self, text: str | None) -> None:
        if text is None:
            return
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            logger.warning("Could not restore clipboard: %s", exc)

    def _read_clipboard(self) -> str | None:
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            logger.warning("Could not read clipboard: %s", exc)
        return None

    def _clear_clipboard(self) -> None:
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            logger.warning("Could not clear clipboard: %s", exc)

    # ------------------------------------------------------------------
    # Inject Ctrl+C via keybd_event
    # ------------------------------------------------------------------

    def _inject_ctrl_c(self) -> None:
        import win32api
        import win32con as _wc
        try:
            win32api.keybd_event(_wc.VK_CONTROL, 0, 0, 0)           # Ctrl down
            win32api.keybd_event(0x43, 0, 0, 0)                      # C down
            win32api.keybd_event(0x43, 0, _wc.KEYEVENTF_KEYUP, 0)   # C up
            win32api.keybd_event(_wc.VK_CONTROL, 0, _wc.KEYEVENTF_KEYUP, 0)  # Ctrl up
            logger.info("Ctrl+C injected via keybd_event")
        except Exception as exc:
            logger.warning("keybd_event failed: %s", exc)


# ---------------------------------------------------------------------------
# Console script entry point
# ---------------------------------------------------------------------------


def main() -> None:
    HotkeyDaemon().run()


if __name__ == "__main__":
    main()
