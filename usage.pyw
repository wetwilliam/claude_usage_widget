"""Claude 用量小浮窗 — 待在螢幕角落，隨時看 Claude Code 的 5 小時 / 每週用量。

資料來源（做法參考 ccburn）：
  1. ~/.ccburn/history.db 最新一筆（ccburn 開著或 statusline 有掛 `ccburn collect` 時持續更新）
  2. 資料過舊時，自己呼叫 OAuth usage API（讀 ~/.claude/.credentials.json；429 時退避）

操作：
  拖曳        移動位置（會記住）
  單擊        打開 / 關閉詳細資訊
  右鍵        選單：重新整理、移到角落、資料來源、結束
設定存在 %APPDATA%\\ClaudeUsageWidget\\data.json
"""
import ctypes
import json
import os
import sqlite3
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ClaudeUsageWidget")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
DEFAULTS = {"corner": "br", "pos": None}
CORNERS = (("br", "右下角"), ("tr", "右上角"), ("bl", "左下角"), ("tl", "左上角"))

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
CCBURN_DB = Path.home() / ".ccburn" / "history.db"
API_URL = "https://api.anthropic.com/api/oauth/usage"
API_BETA = "oauth-2025-04-20"

DB_POLL = 5            # 秒：讀 DB 的間隔
STALE_AFTER = 300      # 秒：DB 資料超過這麼久就改打 API
API_EVERY = 180        # 秒：打 API 的最短間隔
API_BACKOFF = 600      # 秒：429 / 錯誤後的等待
SESSION_SEC, WEEK_SEC = 5 * 3600, 7 * 86400

FONT = "Microsoft JhengHei UI"
KEY = "#010203"
PILL_BG = "#2B2A33"
PILL_FG = "#FFFFFF"
PILL_SUB = "#B9B6C4"
RING_BG = "#E6E0F0"
COOL, OK, HOT, DEAD = "#3DB89A", "#E8A33D", "#E5533D", "#BDB7C9"
IDLE_ALPHA = 0.75
PANEL_BG = "#F7F5FB"
PINK_INK = "#2B2A33"
PANEL_SUB = "#8A8497"
BAR_BG = "#E3DEEE"


def load():
    d = dict(DEFAULTS)
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d.update(json.load(f))
    except Exception:
        pass
    return d


def save(d):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA_FILE)
    except Exception:
        pass


def work_area(root):
    try:
        class RECT(ctypes.Structure):
            _fields_ = [(n, ctypes.c_long) for n in ("l", "t", "r", "b")]
        r = RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)
        return r.l, r.t, r.r, r.b
    except Exception:
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def rrect(c, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


def parse_ts(v):
    """ISO 字串或 epoch 秒 → aware datetime；失敗回 None。"""
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=timezone.utc)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fmt_left(sec):
    sec = max(0, int(sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    return f"{h}h{m:02d}m" if h else f"{m}m"


# ---------- 資料 ----------
def read_db():
    """回傳 (資料時間, {key: (使用率0~1, resets_at)}) 或 None。"""
    if not CCBURN_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{CCBURN_DB.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT timestamp, five_hour_utilization, five_hour_resets_at,"
                " seven_day_all_utilization, seven_day_all_resets_at,"
                " seven_day_sonnet_utilization, seven_day_sonnet_resets_at,"
                " seven_day_opus_utilization, seven_day_opus_resets_at,"
                " monthly_utilization, monthly_resets_at"
                " FROM usage_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    ts = parse_ts(row[0])
    if ts is None:
        return None
    return ts, {"session": (row[1], parse_ts(row[2])), "week": (row[3], parse_ts(row[4])),
                "sonnet": (row[5], parse_ts(row[6])), "opus": (row[7], parse_ts(row[8])),
                "month": (row[9], parse_ts(row[10]))}


def read_history(since):
    """since 之後的 5 小時用量走勢 [(datetime, 0~1)]。"""
    try:
        con = sqlite3.connect(f"file:{CCBURN_DB.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute("SELECT timestamp, five_hour_utilization FROM usage_snapshots"
                               " WHERE timestamp >= ? AND five_hour_utilization IS NOT NULL"
                               " ORDER BY id", (since.isoformat(),)).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    out = [(parse_ts(t), u) for t, u in rows]
    return [(t, u) for t, u in out if t]


def read_api():
    """打 OAuth usage API。回傳 (資料時間, 同 read_db)；失敗丟例外。"""
    with open(CLAUDE_DIR / ".credentials.json", encoding="utf-8") as f:
        token = json.load(f)["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request(API_URL, headers={
        "Accept": "application/json", "Authorization": f"Bearer {token}",
        "anthropic-beta": API_BETA})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())

    def blk(key):
        b = data.get(key) or {}
        u = b.get("utilization")
        return (None if u is None else u / 100.0), parse_ts(b.get("resets_at"))

    snap = {"session": blk("five_hour"), "week": blk("seven_day"),
            "sonnet": blk("seven_day_sonnet"), "opus": blk("seven_day_opus"),
            "month": (None, None)}
    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("utilization") is not None:
        snap["month"] = (extra["utilization"] / 100.0, None)
    return datetime.now(timezone.utc), snap


def pace_color(util, resets, window):
    """用量 vs. 視窗已過比例：<0.9 涼、<1.15 正常、其餘過熱。"""
    if util is None:
        return DEAD
    if resets is None:
        return COOL if util < 0.6 else (OK if util < 0.85 else HOT)
    left = (resets - datetime.now(timezone.utc)).total_seconds()
    elapsed = min(1.0, max(0.02, 1 - left / window))
    ratio = util / elapsed
    return COOL if ratio < 0.9 else (OK if ratio < 1.15 else HOT)


def pace_info(util, resets, window):
    """→ (burn 倍率|None, 預估耗盡時間|None)。倍率 = 用量 / 視窗已過比例。"""
    if util is None or resets is None:
        return None, None
    now = datetime.now(timezone.utc)
    left = (resets - now).total_seconds()
    if left <= 0:
        return None, None
    elapsed = min(1.0, max(0.02, 1 - left / window))
    ratio = util / elapsed
    depl = None
    if util > 0.005 and ratio > 1:
        secs = elapsed * window * (1 - util) / util
        if secs < left:
            depl = now.timestamp() + secs
    return ratio, depl


class Widget:
    def __init__(self):
        self.d = load()
        self.root = root = tk.Tk()
        self.k = root.winfo_fpixels("1i") / 96
        self.T = self.Si(64)
        self.GAP = self.Si(6)
        self.PILL_W = self.Si(188)
        self.W, self.H = self.T + self.GAP + self.PILL_W, self.T

        root.title("Claude 用量")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.attributes("-alpha", IDLE_ALPHA)
        root.configure(bg=KEY)
        self.cv = tk.Canvas(root, width=self.W, height=self.H, bg=KEY, highlightthickness=0)
        self.cv.pack()
        self.f_big = tkfont.Font(family=FONT, size=11, weight="bold")
        self.f_small = tkfont.Font(family=FONT, size=9)
        self.f_ring = tkfont.Font(family=FONT, size=14, weight="bold")
        self.f_tiny = tkfont.Font(family=FONT, size=7)
        self.panel = None

        self.snap = None          # (資料時間, 資料)
        self.source = "—"
        self.error = ""
        self.fetching = False
        self.last_api = 0.0
        self.api_block_until = 0.0
        self.press = None
        self.dragging = False
        self.ticks = 0

        c = self.cv
        c.bind("<Enter>", lambda e: root.attributes("-alpha", 1.0))
        c.bind("<Leave>", lambda e: root.attributes("-alpha", IDLE_ALPHA))
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Button-3>", self.show_menu)

        self.place_initial()
        self.poll()
        self.tick()

    def S(self, v):
        return v * self.k

    def Si(self, v):
        return int(round(v * self.k))

    # ---------- 位置 ----------
    def place_initial(self):
        l, t, r, b = work_area(self.root)
        pos = self.d.get("pos")
        if pos and l - self.T < pos[0] < r and t - self.T < pos[1] < b:
            self.tx, self.ty = pos
        else:
            m = self.Si(16)
            cor = self.d.get("corner", "br")
            self.tx = r - self.T - m if "r" in cor else l + m
            # 右下角預設再往上避開番茄鐘
            self.ty = b - self.T - m - (self.T + m if cor == "br" else 0) if "b" in cor else t + m
        self.update_side()
        self.place()

    def update_side(self):
        l, t, r, b = work_area(self.root)
        self.side = "right" if self.tx + self.T / 2 > (l + r) / 2 else "left"

    def place(self):
        wx = self.tx - self.GAP - self.PILL_W if self.side == "right" else self.tx
        self.root.geometry(f"{self.W}x{self.H}+{int(wx)}+{int(self.ty)}")

    def set_corner(self, cor):
        self.d["corner"], self.d["pos"] = cor, None
        save(self.d)
        self.place_initial()
        self.draw()

    def on_press(self, e):
        self.press = (e.x_root, e.y_root, self.tx, self.ty)
        self.dragging = False

    def on_drag(self, e):
        if not self.press:
            return
        x0, y0, tx, ty = self.press
        if not self.dragging and abs(e.x_root - x0) + abs(e.y_root - y0) > self.Si(4):
            self.dragging = True
        if self.dragging:
            self.tx, self.ty = tx + e.x_root - x0, ty + e.y_root - y0
            self.place()

    def on_release(self, e):
        if self.dragging:
            self.dragging = False
            self.update_side()
            self.place()
            self.d["pos"] = [self.tx, self.ty]
            save(self.d)
            self.draw()
            return
        self.toggle_panel()

    # ---------- 取資料 ----------
    def poll(self, force=False):
        """先看 DB；太舊（或手動重新整理）就在背景打 API。"""
        got = read_db()
        now = datetime.now(timezone.utc)
        if got and not force:
            self.snap, self.source, self.error = got, "ccburn", ""
        fresh = got is not None and (now - got[0]).total_seconds() < STALE_AFTER
        if (force or not fresh) and not self.fetching:
            t = time.monotonic()
            if force or (t >= self.api_block_until and t - self.last_api >= API_EVERY):
                self.fetching = True
                self.last_api = t
                threading.Thread(target=self.fetch_api, args=(got,), daemon=True).start()
        elif got and force:
            self.snap, self.source = got, "ccburn"

    def fetch_api(self, fallback):
        try:
            snap = read_api()
            self.snap, self.source, self.error = snap, "API", ""
        except urllib.error.HTTPError as e:
            self.error = f"API {e.code}"
            self.api_block_until = time.monotonic() + API_BACKOFF
        except Exception as e:
            self.error = type(e).__name__
            self.api_block_until = time.monotonic() + API_BACKOFF
        if self.snap is None and fallback:
            self.snap = fallback
        self.fetching = False

    def tick(self):
        self.ticks += 1
        if self.ticks % DB_POLL == 0:
            self.poll()
        if self.ticks % 60 == 0:
            self.root.attributes("-topmost", True)
        self.draw()
        self.root.after(1000, self.tick)

    # ---------- 繪圖 ----------
    def metric(self, key, window):
        """→ (使用率|None, 剩餘秒|None, 顏色)；視窗已過期就視為歸零。"""
        if not self.snap:
            return None, None, DEAD
        util, resets = self.snap[1][key]
        if util is None:
            return None, None, DEAD
        left = None
        if resets is not None:
            left = (resets - datetime.now(timezone.utc)).total_seconds()
            if left <= 0:
                util, left = 0.0, None   # 重置時間已過，下一筆資料到之前先當 0
        return util, left, pace_color(util, resets if left else None, window)

    def draw(self):
        c, S = self.cv, self.S
        c.delete("all")
        su, sl, sc = self.metric("session", SESSION_SEC)
        wu, wl, wc = self.metric("week", WEEK_SEC)

        ox = self.W - self.T if self.side == "right" else 0
        cx, cy = ox + self.T / 2, self.T / 2
        R = S(27)
        c.create_oval(cx - R, cy - R, cx + R, cy + R, fill="#FFFFFF", outline=RING_BG, width=S(5))
        if su:
            c.create_arc(cx - R, cy - R, cx + R, cy + R, start=90,
                         extent=-359.9 * min(1.0, su), style="arc", outline=sc, width=S(5))
        c.create_text(cx, cy - S(3), text="--" if su is None else f"{round(su * 100)}%",
                      font=self.f_ring, fill="#2B2A33")
        c.create_text(cx, cy + S(12), text="5h 用量", font=self.f_tiny, fill="#6E6980")

        x0 = 0 if self.side == "right" else self.T + self.GAP
        x1 = x0 + self.PILL_W
        y0, y1 = cy - S(24), cy + S(24)
        rrect(c, x0, y0, x1, y1, S(14), fill=PILL_BG, outline="")
        pad = S(12)
        self.draw_row(x0 + pad, x1 - pad, y0 + S(15), "5h", su, sl, sc)
        self.draw_row(x0 + pad, x1 - pad, y1 - S(15), "7d", wu, wl, wc)

        status = self.status_text()
        if status:
            c.create_text(x1 - pad, y1 + S(1), anchor="e", text=status,
                          font=self.f_small, fill=HOT if self.error else PILL_SUB)

    def draw_row(self, xa, xb, y, label, util, left, color):
        c, S = self.cv, self.S
        c.create_text(xa, y, anchor="w", text=label, font=self.f_small, fill=PILL_SUB)
        bx0 = xa + S(20)
        bx1 = bx0 + S(62)
        c.create_line(bx0, y, bx1, y, fill="#4A4856", width=S(5), capstyle="round")
        if util:
            c.create_line(bx0, y, bx0 + (bx1 - bx0) * min(1.0, util), y, fill=color,
                          width=S(5), capstyle="round")
        txt = "--" if util is None else f"{round(util * 100)}%"
        c.create_text(bx1 + S(8), y, anchor="w", text=txt, font=self.f_small, fill=PILL_FG)
        if left is not None:
            c.create_text(xb, y, anchor="e", text=fmt_left(left), font=self.f_small, fill=PILL_SUB)

    def status_text(self):
        """只在有問題時才顯示（錯誤 / 資料過舊 / 還沒有資料）。"""
        if self.snap is None:
            return "查詢中…" if self.fetching else (self.error or "尚無資料")
        age = (datetime.now(timezone.utc) - self.snap[0]).total_seconds()
        if self.error:
            return self.error
        if age > 2 * STALE_AFTER:
            return f"資料 {fmt_left(age)} 前"
        return ""

    # ---------- 選單 ----------
    def show_menu(self, e):
        m = tk.Menu(self.root, tearoff=0, font=(FONT, 10))
        m.add_command(label="立即重新整理", command=lambda: self.poll(force=True))
        m.add_command(label=f"資料來源：{self.source}", state="disabled")
        pm = tk.Menu(m, tearoff=0, font=(FONT, 10))
        for key, name in CORNERS:
            pm.add_command(label=name, command=lambda k=key: self.set_corner(k))
        m.add_cascade(label="移到角落", menu=pm)
        m.add_separator()
        m.add_command(label="結束", command=self.root.destroy)
        m.tk_popup(e.x_root, e.y_root)

    def toggle_panel(self):
        if self.panel:
            self.panel.close()
        else:
            self.panel = Panel(self)

    def run(self):
        self.root.mainloop()


class Panel:
    ROWS = (("session", "5 小時（Session）", SESSION_SEC), ("week", "每週（全部模型）", WEEK_SEC),
            ("sonnet", "每週 Sonnet", WEEK_SEC), ("opus", "每週 Opus", WEEK_SEC),
            ("month", "月度額度", None))

    def __init__(self, app):
        self.app = app
        w = self.win = tk.Toplevel(app.root)
        w.title("Claude 用量 · 詳細")
        w.attributes("-topmost", True)
        w.attributes("-toolwindow", True)
        w.resizable(False, False)
        w.configure(bg=PANEL_BG)
        w.protocol("WM_DELETE_WINDOW", self.close)
        w.bind("<Escape>", lambda e: self.close())
        self.W = app.Si(330)
        self.rows = [r for r in self.ROWS if app.metric(r[0], r[2] or WEEK_SEC)[0] is not None
                     or r[0] in ("session", "week")]
        self.H = app.Si(18 + 52 * len(self.rows) + 100 + 34)
        self.cv = tk.Canvas(w, width=self.W, height=self.H, bg=PANEL_BG, highlightthickness=0)
        self.cv.pack()
        self.cv.bind("<ButtonRelease-1>", self.on_click)
        self.refresh_btn = None
        self.job = None
        self.position()
        self.draw()

    def position(self):
        a, w = self.app, self.win
        w.update_idletasks()
        pw, ph = w.winfo_reqwidth(), w.winfo_reqheight()
        l, t, r, b = work_area(a.root)
        gap = a.Si(10)
        x = a.tx + a.T - pw if a.side == "right" else a.tx
        y = a.ty - ph - gap - a.Si(32) if a.ty + a.T / 2 > (t + b) / 2 else a.ty + a.T + gap
        w.geometry(f"+{int(min(max(l, x), r - pw))}+{int(min(max(t, y), b - ph))}")

    def on_click(self, e):
        b = self.refresh_btn
        if b and b[0] <= e.x <= b[2] and b[1] <= e.y <= b[3]:
            self.app.poll(force=True)

    def draw(self):
        a, c, S = self.app, self.cv, self.app.S
        c.delete("all")
        pad, y = S(16), S(14)
        bw = self.W - 2 * pad
        ft = lambda sz, b=False: (FONT, sz, "bold" if b else "normal")
        for key, name, win in self.rows:
            util, left, col = a.metric(key, win or WEEK_SEC)
            c.create_text(pad, y + S(8), anchor="w", text=name, font=ft(10, True), fill=PINK_INK)
            c.create_text(self.W - pad, y + S(8), anchor="e",
                          text="--" if util is None else f"{util * 100:.0f}%",
                          font=ft(13, True), fill=col if util is not None else PANEL_SUB)
            by = y + S(24)
            c.create_line(pad, by, pad + bw, by, fill=BAR_BG, width=S(7), capstyle="round")
            if util:
                c.create_line(pad, by, pad + bw * min(1.0, util), by, fill=col, width=S(7),
                              capstyle="round")
            resets = a.snap[1][key][1] if a.snap else None
            info = ""
            if resets is not None and left is not None:
                info = f"重置 {resets.astimezone().strftime('%m/%d %H:%M')}（{fmt_left(left)} 後）"
            c.create_text(pad, y + S(38), anchor="w", text=info, font=ft(8), fill=PANEL_SUB)
            if win and left is not None:
                ratio, depl = pace_info(util, resets, win)
                if ratio is not None:
                    if depl:
                        txt = f"偏快 {ratio:.1f}×，約 {datetime.fromtimestamp(depl).strftime('%H:%M')} 用完"
                    else:
                        txt = "偏快" if ratio >= 1.15 else ("正常" if ratio >= 0.9 else "從容")
                        txt += f" {ratio:.1f}×"
                    c.create_text(self.W - pad, y + S(38), anchor="e", text=txt, font=ft(8),
                                  fill=col)
            y += S(52)

        c.create_text(pad, y + S(4), anchor="w", text="本輪 5 小時走勢（虛線＝平均速度）",
                      font=ft(8), fill=PANEL_SUB)
        gx0, gx1, gy0, gy1 = pad, pad + bw, y + S(16), y + S(88)
        c.create_rectangle(gx0, gy0, gx1, gy1, fill="white", outline=BAR_BG)
        resets = a.snap[1]["session"][1] if a.snap else None
        if resets is not None and resets > datetime.now(timezone.utc):
            start = resets.timestamp() - SESSION_SEC
            c.create_line(gx0, gy1, gx1, gy0, fill=BAR_BG, dash=(3, 3))
            pts = []
            for t, u in read_history(datetime.fromtimestamp(start, tz=timezone.utc)):
                fx = (t.timestamp() - start) / SESSION_SEC
                pts += [gx0 + (gx1 - gx0) * min(1, max(0, fx)), gy1 - (gy1 - gy0) * min(1, u)]
            if len(pts) >= 4:
                c.create_line(*pts, fill=a.metric("session", SESSION_SEC)[2], width=S(2),
                              joinstyle="round")
            nf = (datetime.now(timezone.utc).timestamp() - start) / SESSION_SEC
            nx = gx0 + (gx1 - gx0) * min(1, max(0, nf))
            c.create_line(nx, gy0, nx, gy1, fill=PANEL_SUB)
        y = gy1 + S(10)

        age = ""
        if a.snap:
            age = f"{fmt_left((datetime.now(timezone.utc) - a.snap[0]).total_seconds())} 前更新"
        c.create_text(pad, y + S(8), anchor="w", text=f"來源：{a.source}　{age}",
                      font=ft(8), fill=PANEL_SUB)
        bx1, bx0 = self.W - pad, self.W - pad - S(64)
        c.create_rectangle(bx0, y - S(1), bx1, y + S(17), fill=PILL_BG, outline="")
        c.create_text((bx0 + bx1) / 2, y + S(8), text="重新整理", font=ft(8), fill="white")
        self.refresh_btn = (bx0, y - S(1), bx1, y + S(17))
        self.job = self.win.after(1000, self.draw)

    def close(self):
        if self.job:
            self.win.after_cancel(self.job)
        self.app.panel = None
        self.win.destroy()


if __name__ == "__main__":
    Widget().run()
