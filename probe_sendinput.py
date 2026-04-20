"""Diagnostic: launch Notepad, send Ctrl+A + Ctrl+C via SendInput, verify clipboard.

If clipboard ends up with the typed text, SendInput works on a normal target.
Combined with the daemon log showing 0/4 against 홈택스 Chrome tab, this
isolates the failure to the security plugin attached to that tab.
"""
import ctypes
import ctypes.wintypes
import subprocess
import sys
import time

import win32clipboard

user32 = ctypes.windll.user32

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0
VK_CONTROL = 0x11
VK_A = 0x41
VK_C = 0x43


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


user32.SendInput.argtypes = [ctypes.wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.wintypes.UINT
user32.MapVirtualKeyW.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.UINT]
user32.MapVirtualKeyW.restype = ctypes.wintypes.UINT
user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
user32.GetForegroundWindow.restype = ctypes.wintypes.HWND


def make_key(vk: int, key_up: bool = False) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_KEYUP if key_up else 0
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp._input.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return inp


def send_combo(vk: int) -> int:
    events = (INPUT * 4)(
        make_key(VK_CONTROL),
        make_key(vk),
        make_key(vk, key_up=True),
        make_key(VK_CONTROL, key_up=True),
    )
    return user32.SendInput(4, events, ctypes.sizeof(INPUT))


def find_notepad_hwnd() -> int:
    result = [0]

    def cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value in ("Notepad", "ApplicationFrameWindow"):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            if "Notepad" in title.value or "메모장" in title.value:
                result[0] = hwnd
                return False
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return result[0]


def read_clipboard() -> str | None:
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


def set_clipboard(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def main():
    print("Step 1: spawn Notepad")
    proc = subprocess.Popen(["notepad.exe"])
    time.sleep(1.5)

    hwnd = find_notepad_hwnd()
    print(f"Step 2: located Notepad hwnd={hwnd}")
    if not hwnd:
        print("FAIL: could not locate Notepad window")
        proc.terminate()
        sys.exit(1)

    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    fg = user32.GetForegroundWindow()
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(fg, cls, 256)
    print(f"Step 3: foreground hwnd={fg} class={cls.value!r}")

    print("Step 4: seed clipboard with sentinel 'PROBE_SENTINEL'")
    set_clipboard("PROBE_SENTINEL")

    print("Step 5: paste sentinel into Notepad via SendInput Ctrl+V")
    sent_v = send_combo(0x56)  # VK_V
    print(f"  SendInput Ctrl+V returned: {sent_v}/4")
    time.sleep(0.4)

    print("Step 6: clear clipboard")
    set_clipboard("")
    time.sleep(0.1)

    print("Step 7: select all via SendInput Ctrl+A")
    sent_a = send_combo(VK_A)
    print(f"  SendInput Ctrl+A returned: {sent_a}/4")
    time.sleep(0.2)

    print("Step 8: copy via SendInput Ctrl+C")
    sent_c = send_combo(VK_C)
    print(f"  SendInput Ctrl+C returned: {sent_c}/4")
    time.sleep(0.4)

    print("Step 9: read clipboard")
    captured = read_clipboard()
    print(f"  Clipboard contains: {captured!r}")

    print("Step 10: cleanup — close Notepad")
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    time.sleep(0.3)
    try:
        proc.terminate()
    except Exception:
        pass

    print()
    print("=" * 60)
    if captured == "PROBE_SENTINEL":
        print("RESULT: SendInput WORKS on Notepad")
        print("Conclusion: SendInput is functional system-wide right now.")
        print("The 0/4 result against 홈택스 Chrome tab is caused by a")
        print("security plugin attached to that page.")
    elif sent_c == 0 or sent_a == 0:
        print("RESULT: SendInput is BLOCKED system-wide right now")
        print("A keyboard hook driver is currently swallowing synthetic input")
        print("for ALL targets, not just Chrome.")
    else:
        print(f"RESULT: SendInput delivered {sent_a}+{sent_c}/8 events but")
        print(f"clipboard does not contain the sentinel ({captured!r}).")
        print("Likely race condition or Notepad focus loss.")


if __name__ == "__main__":
    main()
