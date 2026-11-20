# -*- coding: utf-8 -*-
"""
微信群聊同步助手 · 自动监听转发版

功能：盯住一个「源群」，把群里任何人发的新消息，按

        来自 源群名 的 发送者 ：
        --------------------------------
        消息内容

    的格式，自动转发到你指定的多个「目标群」。

原理：微信 4.x 不向系统暴露聊天消息（已实测），所以采用
      「窗口截图 → OCR 识别 → 比对新增 → UI 自动化发送」。
      发送过程中会临时切到目标群，发完自动切回源群。

限制：仅支持文字消息；图片/表情/文件/引用无法识别；
      监听期间微信必须保持登录，且不要手动操作微信窗口。

运行环境：Windows 10/11 + Python 3.8+ + 微信电脑版（3.x / 4.x）
首次运行会自动安装依赖：uiautomation / rapidocr-onnxruntime / mss
"""

import ctypes
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext


# ----------------------------------------------------------------------------
# 启动环境自检
# ----------------------------------------------------------------------------
def _fatal(title, message):
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(title, message)
        r.destroy()
    except Exception:
        print(f"{title}: {message}")
    sys.exit(1)


_REQUIRED = [
    ("uiautomation", "uiautomation"),
    ("rapidocr_onnxruntime", "rapidocr-onnxruntime"),
    ("mss", "mss"),
    ("PIL", "pillow"),
    ("numpy", "numpy"),
]


def _ensure_deps():
    missing = []
    for mod, pkg in _REQUIRED:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return

    root = tk.Tk()
    root.withdraw()
    ok = messagebox.askyesno(
        "缺少依赖",
        "运行本工具需要以下 Python 库：\n\n  " + "\n  ".join(missing) +
        "\n\n是否现在自动安装？（需联网，约 1~3 分钟）"
    )
    root.destroy()
    if not ok:
        sys.exit(0)

    mirror = ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"]
    base = [sys.executable, "-m", "pip", "install"] + missing
    for cmd in (base + mirror, base + ["--user"] + mirror, base):
        try:
            subprocess.check_call(cmd)
            break
        except Exception:
            continue
    else:
        _fatal("安装失败", "自动安装失败，请手动执行：\n\npip install " + " ".join(missing))

    for mod, pkg in _REQUIRED:
        try:
            __import__(mod)
        except ImportError:
            _fatal("安装未生效", f"{pkg} 仍无法导入，请重启命令行后再试。")


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _config_path():
    base = os.path.dirname(os.path.abspath(__file__))
    if os.access(base, os.W_OK):
        return os.path.join(base, "wechat_relay_config.json")
    home = os.path.join(os.path.expanduser("~"), ".wechat_group_sync")
    try:
        os.makedirs(home, exist_ok=True)
    except Exception:
        return os.path.join(base, "wechat_relay_config.json")
    return os.path.join(home, "wechat_relay_config.json")


if not sys.platform.startswith("win"):
    _fatal("不支持的系统", "本工具依赖 Windows UI 自动化，只能在 Windows 10/11 上运行。")

_ensure_deps()
import uiautomation as auto  # noqa: E402

CONFIG_PATH = _config_path()


# ----------------------------------------------------------------------------
# 窗口操作
# ----------------------------------------------------------------------------
def _dpi_scale():
    try:
        d = ctypes.windll.user32.GetDpiForSystem()
        if d:
            return d / 96.0
    except Exception:
        pass
    return 1.0


_MAIN_TITLES = ("微信", "WeChat", "Weixin")


def _enum_windows():
    """枚举所有顶层窗口（含隐藏的），返回 [(hwnd, title, cls, visible, rect)]。"""
    from ctypes import wintypes
    u = ctypes.windll.user32
    out = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        cbuf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cbuf, 256)
        cls = cbuf.value
        if not ("Qt5" in cls or "WeChat" in cls or "Weixin" in cls):
            return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        out.append((hwnd, title, cls, bool(u.IsWindowVisible(hwnd)),
                    (r.left, r.top, r.right, r.bottom)))
        return True

    u.EnumWindows(proc(cb), 0)
    return out


def find_wechat_window():
    """只认主窗口（标题为 微信/WeChat/Weixin），隐藏到托盘时自动还原。"""
    win_list = _enum_windows()
    best = None
    for hwnd, title, cls, visible, rect in win_list:
        if title not in _MAIN_TITLES:
            continue
        if "Qt5" not in cls and "WeChat" not in cls and "Weixin" not in cls:
            continue
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w < 800 or h < 500:          # 排除登录框/提示框
            continue
        best = (hwnd, visible, rect, w * h)
        if visible:
            break
    if best is None:
        return None

    hwnd, visible, _, _ = best
    if not visible:
        ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        time.sleep(0.5)
    try:
        return auto.ControlFromHandle(hwnd)
    except Exception:
        return None


def close_stray_windows():
    """关掉独立聊天窗、搜索窗等，避免它们盖住主窗口导致点错位置。"""
    closed = 0
    for hwnd, title, cls, visible, rect in _enum_windows():
        if not visible or title in _MAIN_TITLES:
            continue
        if "Qt51514QWindowIcon" not in cls:
            continue
        if (rect[2] - rect[0]) < 300 or (rect[3] - rect[1]) < 300:
            continue
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
        closed += 1
    if closed:
        time.sleep(0.8)
    return closed


def activate_window(win):
    try:
        hwnd = win.NativeWindowHandle
    except Exception:
        hwnd = 0
    if hwnd:
        try:
            u = ctypes.windll.user32
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)  # SW_RESTORE
            u.SetForegroundWindow(hwnd)
            return True
        except Exception:
            pass
    try:
        win.SetActive()
        return True
    except Exception:
        return False


def _grab(left, top, right, bottom):
    import numpy as np
    from mss import mss
    from PIL import Image

    with mss() as sct:
        shot = sct.grab({"left": left, "top": top,
                         "width": right - left, "height": bottom - top})
        rgb = np.array(Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"))
    return rgb[:, :, ::-1].copy()


def _click(x, y):
    try:
        auto.Click(int(x), int(y))
        return
    except Exception:
        pass
    u = ctypes.windll.user32
    u.SetCursorPos(int(x), int(y))
    u.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    u.mouse_event(0x0004, 0, 0, 0, 0)


def _ocr_lines(engine, img, min_score=0.55):
    result, _ = engine(img)
    rows = []
    for box, text, score in result or []:
        text = (text or "").strip()
        if not text or float(score) < min_score:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        rows.append({"text": text, "x0": min(xs), "y0": min(ys),
                     "x1": max(xs), "y1": max(ys)})
    rows.sort(key=lambda r: r["y0"])
    return rows


_SECTION_WORDS = ("最常使用", "联系人", "群聊", "公众号", "服务号",
                  "小程序", "聊天记录", "功能", "搜索网络结果", "全部", "更多")


def _is_section_header(text):
    t = text.strip()
    return t.startswith("查看全部") or t in _SECTION_WORDS


def _name_ratio(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # 短名字（如 111/222）只允许完全一致，避免 111 命中 1111
    if len(a) < 5 or len(b) < 5:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _same_name(a, b):
    return _name_ratio(a, b) >= 0.85


def _best_row(rows, name):
    """在同一批候选行里选名字最匹配的一行：先完全一致，再高相似度。"""
    target = _norm(name)
    exact = [r for r in rows if _norm(r["text"]) == target]
    if exact:
        return exact[0]
    scored = [(_name_ratio(r["text"], name), r) for r in rows]
    scored = [c for c in scored if c[0] >= 0.85]
    if not scored:
        return None
    scored.sort(key=lambda c: (-c[0], c[1]["y0"]))
    return scored[0][1]


def _find_group_row(rows, name):
    """在搜索结果里定位群会话，返回 (行, 是否群聊分区) ；找不到返回 (None, False)。

    只在「群聊」和「最常使用」两个分区里找，绝不点「联系人」/「聊天记录」，
    避免把群消息发到同名的个人聊天。
    """
    headers = [(r["y0"], r["text"].strip()) for r in rows if _is_section_header(r["text"])]
    headers.sort()

    def band(start_y):
        end_y = None
        for y, _ in headers:
            if y > start_y:
                end_y = y
                break
        return [r for r in rows if r["y0"] > start_y and (end_y is None or r["y0"] < end_y)]

    for word, is_group in (("群聊", True), ("最常使用", False)):
        ys = [y for y, t in headers if t == word]
        if not ys:
            continue
        row = _best_row(band(ys[0]), name)
        if row is not None:
            return row, is_group
    return None, False


def read_chat_title(win, engine):
    """OCR 读取聊天区顶部标题（形如「群名 (人数)」）。"""
    r = win.BoundingRectangle
    W, H = r.right - r.left, r.bottom - r.top
    s = _dpi_scale()
    img = _grab(r.left + int(320 * s), r.top + int(25 * s),
                r.left + int(W * 0.72), r.top + int(95 * s))
    lines = _ocr_lines(engine, img, 0.45)
    if not lines:
        return ""
    lines.sort(key=lambda l: l["y0"])
    return lines[0]["text"].strip()   # 只取标题行


def _title_ok(title, name):
    if not title:
        return False
    t = re.sub(r"[\(（]\s*\d+\s*[\)）]\s*$", "", title).strip()
    return _same_name(t, name)


def goto_chat(win, name, engine, attempts=3):
    """点搜索框 → 输入群名 → 点精确匹配的行 → 校验标题。失败抛异常。"""
    dropdown_hints = ("最常使用", "群聊", "联系人", "聊天记录", "查看全部", "更多")
    seen_names = []
    for _ in range(attempts):
        activate_window(win)
        time.sleep(0.4)
        r = win.BoundingRectangle
        W, H = r.right - r.left, r.bottom - r.top
        s = _dpi_scale()

        # 点击会话列表顶部的搜索框（比 Ctrl+F 稳定）
        _click(r.left + int(167 * s), r.top + int(53 * s))
        time.sleep(0.7)
        auto.SendKeys("{Ctrl}a", waitTime=0.1)
        auto.SendKeys("{Delete}", waitTime=0.1)
        auto.SetClipboardText(name)
        auto.SendKeys("{Ctrl}v", waitTime=0.5)
        time.sleep(1.2)

        rows = _ocr_lines(engine, _grab(r.left, r.top,
                                        r.left + int(W * 0.26), r.top + int(H * 0.60)), 0.55)
        seen_names = [row["text"] for row in rows
                      if row["y0"] > 120 and not _is_section_header(row["text"])][:6]
        opened = any(any(h in row["text"] for h in dropdown_hints) for row in rows)
        if opened:
            row, from_group = _find_group_row(rows, name)
            if row is not None:
                _click(r.left + (row["x0"] + row["x1"]) / 2.0,
                       r.top + (row["y0"] + row["y1"]) / 2.0)
                time.sleep(1.0)
                title = read_chat_title(win, engine)
                # 「最常使用」里可能是同名的个人聊天，必须是群（标题带人数）
                group_like = bool(re.search(r"[\(（]\s*\d+\s*[\)）]", title))
                if _title_ok(title, name) and (from_group or group_like):
                    return True
        auto.SendKeys("{Esc}", waitTime=0.2)
        time.sleep(0.8)
    raise RuntimeError(f"无法定位会话「{name}」，请确认群名与微信里完全一致。"
                       f"搜索到的结果：{seen_names or '（无）'}")


def ensure_chat(win, name, engine):
    """已在目标会话就不重复搜索，否则执行定位。"""
    try:
        if _title_ok(read_chat_title(win, engine), name):
            return True
    except Exception:
        pass
    return goto_chat(win, name, engine)


def focus_input(win):
    """点击消息输入框，确保后续粘贴进入输入框而不是别处。"""
    r = win.BoundingRectangle
    s = _dpi_scale()
    _click(r.left + (r.right - r.left) * 0.5, r.bottom - int(210 * s))
    time.sleep(0.15)


def send_text(win, text, send_key="Enter"):
    activate_window(win)
    time.sleep(0.25)
    focus_input(win)
    auto.SendKeys("{Ctrl}a", waitTime=0.1)
    auto.SendKeys("{Delete}", waitTime=0.1)
    auto.SetClipboardText(text)
    auto.SendKeys("{Ctrl}v", waitTime=0.45)
    auto.SendKeys("{Ctrl}{Enter}" if send_key == "Ctrl+Enter" else "{Enter}", waitTime=0.35)


# ----------------------------------------------------------------------------
# 截图 + OCR
# ----------------------------------------------------------------------------
def grab_region(win, left_px=320, top_px=100, bottom_px=155):
    """按固定逻辑像素偏移截取消息区（窗口大小变化时依然有效）。"""
    r = win.BoundingRectangle
    s = _dpi_scale()
    left = r.left + int(left_px * s)
    top = r.top + int(top_px * s)
    right = r.right - 6
    bottom = r.bottom - int(bottom_px * s)
    if right - left < 50 or bottom - top < 50:
        raise RuntimeError("窗口尺寸异常，无法截图")
    return _grab(left, top, right, bottom)


_NOISE_RE = [
    r"^\d{1,2}:\d{2}$",
    r"^(昨天|今天|前天|上午|下午|凌晨|中午|晚上)(\s*\d{1,2}:\d{2})?$",
    r"^星期[一二三四五六日天]$",
    r"^\d{4}年\d{1,2}月\d{1,2}日.*$",
    r"^\d{1,2}月\d{1,2}日.*$",
]
_SYS_HINT = ("撤回了一条消息", "加入了群聊", "退出了群聊", "邀请", "修改群名",
             "开启了朋友验证", "你已添加", "以上是打招呼的内容")


def _is_noise_block(text):
    t = text.strip()
    if not t:
        return True
    for p in _NOISE_RE:
        if re.match(p, t):
            return True
    for h in _SYS_HINT:
        if h in t:
            return True
    return False


def parse_messages(engine, img, min_score=0.75):
    """把截图解析成 [(发送者, 内容), ...]，按从上到下排序。"""
    h, w = img.shape[:2]
    result, _ = engine(img)
    if not result:
        return []

    items = []
    for box, text, score in result:
        text = (text or "").strip()
        if not text or float(score) < min_score:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({
            "x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys),
            "text": text,
        })
    if not items:
        return []

    heights = sorted(i["y1"] - i["y0"] for i in items)
    p75 = heights[min(len(heights) - 1, int(len(heights) * 0.75))]
    min_h = max(12, p75 * 0.62)          # 过滤掉时间戳、转发卡片里的小字
    items = [i for i in items if (i["y1"] - i["y0"]) >= min_h]
    if not items:
        return []

    items.sort(key=lambda i: i["y0"])
    gap = max(16, p75 * 0.9)             # 同一消息内行距小，跨消息间距大
    blocks, cur, last_bottom = [], [], None
    for it in items:
        if cur and it["y0"] - last_bottom > gap:
            blocks.append(cur)
            cur = []
        cur.append(it)
        last_bottom = it["y1"]
    if cur:
        blocks.append(cur)

    out = []
    for b in blocks:
        if len(b) == 1:
            it = b[0]
            if _is_noise_block(it["text"]):
                continue
            center = (it["x0"] + it["x1"]) / 2.0
            out.append(("我" if center > w * 0.55 else "", it["text"]))
            continue

        first, second = b[0], b[1]
        sender, body = "", b
        # 群聊里发送者昵称在气泡上方，且比气泡正文更靠左（气泡有内边距）
        if len(first["text"]) <= 24 and first["x0"] < second["x0"] - 6:
            sender, body = first["text"], b[1:]

        content = "\n".join(x["text"] for x in body).strip()
        if not content or _is_noise_block(content):
            continue
        if not sender:
            sender = "我" if (first["x0"] + first["x1"]) / 2.0 > w * 0.55 else ""
        out.append((sender, content))
    return out


def format_message(source_group, sender, content):
    head = f"来自 {source_group} 的 {sender} ：" if sender else f"来自 {source_group} ："
    return f"{head}\n--------------------------------\n{content}"


def _norm(text):
    return re.sub(r"\s+", "", text).lower()


def is_seen(sig, seen):
    for s in seen[-80:]:
        if s == sig:
            return True
        if difflib.SequenceMatcher(None, s, sig).ratio() > 0.9:
            return True
    return False


# ----------------------------------------------------------------------------
# 主界面
# ----------------------------------------------------------------------------
class RelayApp:
    def __init__(self, root):
        self.root = root
        self.root.title("微信群聊同步助手 · 自动监听转发")
        self.root.geometry("760x720")
        self.root.minsize(680, 620)

        self._stop = threading.Event()
        self._thread = None
        self._engine = None

        self.targets = []
        self.source_var = tk.StringVar(value="")
        self.poll_var = tk.DoubleVar(value=3.0)
        self.interval_var = tk.DoubleVar(value=4.0)
        self.dry_run_var = tk.BooleanVar(value=True)
        self.send_key_var = tk.StringVar(value="Enter")
        self.left_var = tk.DoubleVar(value=320)
        self.top_var = tk.DoubleVar(value=100)
        self.bottom_var = tk.DoubleVar(value=155)

        self._build_ui()
        self._load_config()
        self._check_env()

    # ---------------- UI ----------------
    def _build_ui(self):
        tip = tk.Label(
            self.root, anchor="w", justify="left", fg="#8a5a00", bg="#fff7e0",
            padx=8, pady=6,
            text="监听期间请勿操作微信：工具会反复切换会话做识别和发送。\n"
                 "仅支持文字消息；图片/表情/引用会丢失。建议先勾选「演练模式」跑一轮确认效果。"
        )
        tip.pack(fill="x", padx=10, pady=(10, 4))

        # 源群
        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=10, pady=4)
        ttk.Label(row, text="源群（被监听）").pack(side="left")
        ttk.Entry(row, textvariable=self.source_var).pack(side="left", fill="x", expand=True, padx=6)

        # 目标群
        box1 = ttk.LabelFrame(self.root, text="目标群（转发到这些群，每行一个）")
        box1.pack(fill="both", expand=True, padx=10, pady=4)

        wrap = ttk.Frame(box1)
        wrap.pack(fill="both", expand=True, padx=6, pady=6)
        self.listbox = tk.Listbox(wrap, height=6, activestyle="dotbox")
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        btns = ttk.Frame(box1)
        btns.pack(fill="x", padx=6, pady=(0, 6))
        self.entry_group = ttk.Entry(btns)
        self.entry_group.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.entry_group.bind("<Return>", lambda e: self._add_group())
        ttk.Button(btns, text="添加", width=6, command=self._add_group).pack(side="left", padx=2)
        ttk.Button(btns, text="删除选中", width=9, command=self._remove_group).pack(side="left", padx=2)
        ttk.Button(btns, text="批量导入", width=9, command=self._import_groups).pack(side="left", padx=2)
        ttk.Button(btns, text="清空", width=6, command=self._clear_groups).pack(side="left", padx=2)

        # 选项
        box2 = ttk.LabelFrame(self.root, text="选项")
        box2.pack(fill="x", padx=10, pady=4)

        r1 = ttk.Frame(box2)
        r1.pack(fill="x", padx=6, pady=4)
        ttk.Label(r1, text="识别间隔(秒)").pack(side="left")
        ttk.Spinbox(r1, from_=2, to=60, increment=1, width=5, textvariable=self.poll_var).pack(side="left", padx=(4, 14))
        ttk.Label(r1, text="发送间隔(秒)").pack(side="left")
        ttk.Spinbox(r1, from_=2, to=120, increment=1, width=5, textvariable=self.interval_var).pack(side="left", padx=(4, 14))
        ttk.Label(r1, text="发送键").pack(side="left")
        ttk.Combobox(r1, textvariable=self.send_key_var, width=10, state="readonly",
                     values=["Enter", "Ctrl+Enter"]).pack(side="left", padx=4)

        r2 = ttk.Frame(box2)
        r2.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Checkbutton(r2, text="演练模式（只识别并打印，不发送）", variable=self.dry_run_var).pack(side="left")

        r3 = ttk.Frame(box2)
        r3.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(r3, text="识别区域：左边距").pack(side="left")
        ttk.Spinbox(r3, from_=100, to=700, increment=10, width=5, textvariable=self.left_var).pack(side="left", padx=2)
        ttk.Label(r3, text="上边距").pack(side="left")
        ttk.Spinbox(r3, from_=0, to=400, increment=5, width=5, textvariable=self.top_var).pack(side="left", padx=2)
        ttk.Label(r3, text="下边距").pack(side="left")
        ttk.Spinbox(r3, from_=50, to=400, increment=5, width=5, textvariable=self.bottom_var).pack(side="left", padx=2)
        ttk.Label(r3, text="(px)").pack(side="left")
        ttk.Button(r3, text="截图并解析（不发送）", command=self._test_ocr).pack(side="left", padx=10)

        # 操作
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=10, pady=6)
        self.btn_start = ttk.Button(actions, text="▶  开始监听", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(actions, text="■  停止", command=self._stop_relay, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(actions, text="清空日志", command=self._clear_log).pack(side="left")

        box3 = ttk.LabelFrame(self.root, text="运行日志")
        box3.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = scrolledtext.ScrolledText(box3, height=9, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text.tag_config("ok", foreground="#1a7f37")
        self.log_text.tag_config("err", foreground="#c62828")
        self.log_text.tag_config("warn", foreground="#b26a00")

    # ---------------- 数据 ----------------
    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        for g in self.targets:
            self.listbox.insert(tk.END, g)

    def _add_group(self):
        name = self.entry_group.get().strip()
        if not name:
            return
        if name == self.source_var.get().strip():
            self.log("目标群不能与源群相同（会造成无限转发）", "warn")
            return
        if name in self.targets:
            self.log(f"群「{name}」已在列表中", "warn")
        else:
            self.targets.append(name)
            self._refresh_list()
        self.entry_group.delete(0, tk.END)

    def _remove_group(self):
        for i in reversed(list(self.listbox.curselection())):
            del self.targets[i]
        self._refresh_list()

    def _clear_groups(self):
        if self.targets and not messagebox.askyesno("确认", "确定清空所有目标群？"):
            return
        self.targets.clear()
        self._refresh_list()

    def _import_groups(self):
        win = tk.Toplevel(self.root)
        win.title("批量导入目标群")
        win.geometry("420x400")
        ttk.Label(win, text="每行一个群名，空行忽略：").pack(anchor="w", padx=10, pady=(10, 4))
        txt = scrolledtext.ScrolledText(win, wrap="word")
        txt.pack(fill="both", expand=True, padx=10, pady=4)

        def do_import():
            added = 0
            for line in txt.get("1.0", "end").splitlines():
                name = line.strip()
                if name and name != self.source_var.get().strip() and name not in self.targets:
                    self.targets.append(name)
                    added += 1
            self._refresh_list()
            self.log(f"批量导入完成，新增 {added} 个群")
            win.destroy()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="导入", command=do_import).pack(side="right")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=6)

    # ---------------- 日志 / 状态 ----------------
    def log(self, msg, tag=None):
        def _write():
            self.log_text.config(state="normal")
            self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n", tag)
            self.log_text.see(tk.END)
            self.log_text.config(state="disabled")
        self.root.after(0, _write)

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _set_running(self, running):
        def _do():
            self.btn_start.config(state="disabled" if running else "normal")
            self.btn_stop.config(state="normal" if running else "disabled")
        self.root.after(0, _do)

    def _check_env(self):
        self.log(f"运行环境：Python {sys.version.split()[0]} / Windows")
        if not _is_admin():
            self.log("提示：若微信是“以管理员身份运行”，本工具也需同样提权，否则按键无效", "warn")
        if find_wechat_window() is None:
            self.log("未检测到微信窗口，请先登录并打开微信电脑版", "warn")
        else:
            self.log("已检测到微信窗口", "ok")

    # ---------------- 配置 ----------------
    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.source_var.set(cfg.get("source", ""))
            self.targets = list(cfg.get("targets", []))
            self.poll_var.set(float(cfg.get("poll", 3.0)))
            self.interval_var.set(float(cfg.get("interval", 4.0)))
            self.dry_run_var.set(bool(cfg.get("dry_run", True)))
            self.send_key_var.set(cfg.get("send_key", "Enter"))
            self.left_var.set(float(cfg.get("left_px", 320)))
            self.top_var.set(float(cfg.get("top_px", 100)))
            self.bottom_var.set(float(cfg.get("bottom_px", 155)))
            self._refresh_list()
            self.log(f"已载入配置：源群「{self.source_var.get()}」，{len(self.targets)} 个目标群")
        except Exception as e:
            self.log(f"读取配置失败：{e}", "warn")

    def _save_config(self):
        cfg = {
            "source": self.source_var.get().strip(),
            "targets": self.targets,
            "poll": float(self.poll_var.get() or 3),
            "interval": float(self.interval_var.get() or 4),
            "dry_run": bool(self.dry_run_var.get()),
            "send_key": self.send_key_var.get(),
            "left_px": float(self.left_var.get() or 320),
            "top_px": float(self.top_var.get() or 100),
            "bottom_px": float(self.bottom_var.get() or 155),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"保存配置失败：{e}", "warn")

    # ---------------- OCR ----------------
    def _get_engine(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self.log("正在加载 OCR 引擎（首次约需几秒）…")
            self._engine = RapidOCR()
            self.log("OCR 引擎就绪", "ok")
        return self._engine

    def _test_ocr(self):
        def work():
            try:
                win = find_wechat_window()
                if win is None:
                    self.log("未找到微信窗口", "err")
                    return
                engine = self._get_engine()
                activate_window(win)
                time.sleep(0.6)
                img = grab_region(win, float(self.left_var.get()),
                                  float(self.top_var.get()), float(self.bottom_var.get()))
                self.log(f"当前会话标题识别为：{read_chat_title(win, engine) or '（读取失败）'}")
                msgs = parse_messages(engine, img)
                self.log(f"识别到 {len(msgs)} 条消息：")
                for sender, content in msgs[-10:]:
                    self.log(f"  [{sender or '未知'}] {content}")
                if not msgs:
                    self.log("没有识别到消息，请检查识别区域比例是否覆盖聊天区", "warn")
            except Exception as e:
                self.log(f"测试失败：{e}", "err")

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 监听主流程 ----------------
    def _start(self):
        if self._thread and self._thread.is_alive():
            return
        source = self.source_var.get().strip()
        if not source:
            messagebox.showwarning("提示", "请先填写源群名称。")
            return
        targets = [t for t in self.targets if t and t != source]
        if not targets:
            messagebox.showwarning("提示", "请至少添加一个目标群（不能与源群相同）。")
            return
        if not self.dry_run_var.get() and not messagebox.askyesno(
            "确认", f"即将把「{source}」中的新消息自动转发到 {len(targets)} 个群。\n\n"
                    "开始后请勿操作微信窗口。是否继续？"
        ):
            return

        self._save_config()
        n = close_stray_windows()
        if n:
            self.log(f"已关闭 {n} 个游离窗口（独立聊天窗/搜索窗），避免点错位置", "warn")
        self._stop.clear()
        self._set_running(True)
        self._thread = threading.Thread(
            target=self._loop,
            args=(source, targets, float(self.poll_var.get()), float(self.interval_var.get()),
                  bool(self.dry_run_var.get()), self.send_key_var.get(),
                  float(self.left_var.get()), float(self.top_var.get()), float(self.bottom_var.get())),
            daemon=True,
        )
        self._thread.start()

    def _stop_relay(self):
        self._stop.set()
        self.log("已请求停止，本轮结束后退出…", "warn")

    def _loop(self, source, targets, poll, interval, dry_run, send_key,
              left_px, top_px, bottom_px):
        seen, forwarded = [], 0
        try:
            win = find_wechat_window()
            if win is None:
                self.log("未找到微信窗口，请先登录微信电脑版", "err")
                return
            engine = self._get_engine()
            self.log(f"开始监听「{source}」→ {len(targets)} 个目标群"
                     f"{'（演练模式，不发送）' if dry_run else ''}")

            first = True
            while not self._stop.is_set():
                win = find_wechat_window() or win
                try:
                    ensure_chat(win, source, engine)
                except Exception as e:
                    self.log(f"{e}（已暂停本轮，请检查窗口是否被遮挡）", "err")
                    self._sleep(3)
                    continue
                try:
                    img = grab_region(win, left_px, top_px, bottom_px)
                    msgs = parse_messages(engine, img)
                except Exception as e:
                    self.log(f"识别失败（重试中）：{e}", "warn")
                    time.sleep(2)
                    continue

                new_msgs = []
                for sender, content in msgs:
                    sig = _norm(sender + "|" + content)
                    if len(sig) < 2 or is_seen(sig, seen):
                        continue
                    seen.append(sig)
                    new_msgs.append((sender, content))

                if first:
                    first = False
                    self.log(f"已建立基线：当前画面 {len(new_msgs)} 条历史消息，不会转发")
                    self._sleep(poll)
                    continue

                for sender, content in new_msgs:
                    if self._stop.is_set():
                        break
                    text = format_message(source, sender, content)
                    preview = content if len(content) <= 60 else content[:60] + "…"
                    if dry_run:
                        self.log(f"[演练] {sender or '未知'}：{preview}", "warn")
                        continue
                    for t in targets:
                        if self._stop.is_set():
                            break
                        try:
                            ensure_chat(win, t, engine)
                        except Exception as e:
                            self.log(f"跳过「{t}」：{e}", "err")
                            continue
                        send_text(win, text, send_key)
                        time.sleep(0.4)
                        self.log(f"已转发到「{t}」：{preview}")
                        self._sleep(interval - 0.4 if interval > 0.4 else 0)
                    forwarded += 1
                if not dry_run and new_msgs:
                    self.log(f"累计转发 {forwarded} 条", "ok")

                self._sleep(poll)

        except Exception as e:
            self.log(f"运行出错：{e}", "err")
        finally:
            self._set_running(False)
            self.log("监听已停止")

    def _sleep(self, seconds):
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            if self._stop.is_set():
                return
            time.sleep(0.2)

    def on_close(self):
        self._save_config()
        self._stop.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = RelayApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
