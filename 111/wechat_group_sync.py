# -*- coding: utf-8 -*-
"""
微信群聊同步助手（半自动 · 仅文字）

用途：把同一条文字消息，依次发送到多个微信群。
原理：用 Windows UI 自动化（uiautomation）驱动「微信电脑版」，
      对每个群执行  Ctrl+F 搜索群名 → 回车进入 → 粘贴内容 → 发送。

运行环境：Windows 10/11 + Python 3.8+ + 微信电脑版（3.x / 4.x 均可）
首次运行会自动检查并安装依赖库 uiautomation。

注意：微信个人号没有开放接口，本工具属于客户端自动化，
      请低频、少量使用，避免账号被风控。
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext


# ----------------------------------------------------------------------------
# 启动环境自检（发给别人时，这些判断能避免一打开就报错）
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


def _ensure_uiautomation():
    """缺少 uiautomation 时引导自动安装；已安装则直接返回。"""
    try:
        import uiautomation  # noqa: F401
        return
    except ImportError:
        pass

    root = tk.Tk()
    root.withdraw()
    ok = messagebox.askyesno(
        "缺少依赖",
        "运行本工具需要 Python 库 uiautomation。\n\n"
        "是否现在自动安装？（需联网，约 1 分钟）"
    )
    root.destroy()
    if not ok:
        sys.exit(0)

    mirror = ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"]
    base = [sys.executable, "-m", "pip", "install", "uiautomation"]
    for cmd in (base + mirror, base + ["--user"] + mirror, base):
        try:
            subprocess.check_call(cmd)
            break
        except Exception:
            continue
    else:
        _fatal("安装失败", "自动安装 uiautomation 失败。\n\n请手动执行：\n"
                           "pip install uiautomation\n\n然后再运行本程序。")

    try:
        import uiautomation  # noqa: F401
    except ImportError:
        _fatal("安装未生效", "依赖已安装但无法导入，请重启命令行后重新运行本程序。")


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _config_path():
    """优先放在脚本同目录（便于随文件夹一起带走），无写权限时退回用户目录。"""
    base = os.path.dirname(os.path.abspath(__file__))
    if os.access(base, os.W_OK):
        return os.path.join(base, "wechat_sync_config.json")
    home = os.path.join(os.path.expanduser("~"), ".wechat_group_sync")
    try:
        os.makedirs(home, exist_ok=True)
    except Exception:
        return os.path.join(base, "wechat_sync_config.json")
    return os.path.join(home, "wechat_sync_config.json")


if not sys.platform.startswith("win"):
    _fatal("不支持的系统",
           "本工具依赖 Windows UI 自动化，只能在 Windows 10/11 上运行。\n"
           "macOS / Linux 无法使用。")

_ensure_uiautomation()
import uiautomation as auto

CONFIG_PATH = _config_path()


# ----------------------------------------------------------------------------
# 微信窗口定位
# ----------------------------------------------------------------------------
def _has_render_child(win):
    """4.x 主窗口包含 MMUIRenderSubWindowHW 渲染子窗口，登录框没有。"""
    try:
        for c in win.GetChildren():
            if "MMUI" in (c.Name or ""):
                return True
    except Exception:
        pass
    return False


def find_wechat_window():
    """在顶层窗口中找出微信主窗口，返回 WindowControl 或 None。"""
    root = auto.GetRootControl()
    candidates = []
    try:
        children = root.GetChildren()
    except Exception:
        return None

    for w in children:
        try:
            name = w.Name or ""
            cls = w.ClassName or ""
            r = w.BoundingRectangle
        except Exception:
            continue

        is_wechat = ("WeChat" in cls or "Weixin" in cls) or \
                    name in ("微信", "Weixin", "WeChat")
        if not is_wechat:
            continue

        # 最小化时矩形为 -32000 之类的隐藏坐标，不能据此算面积
        area = 0
        try:
            if r.right > r.left and r.left > -20000:
                area = (r.right - r.left) * (r.bottom - r.top)
        except Exception:
            pass

        # 有渲染子窗口的主窗口优先级最高，其次比面积
        score = area + ((1 << 32) if _has_render_child(w) else 0)
        candidates.append((score, w))

    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


def activate_window(win):
    """还原并置前窗口；Win32 方式失败时退回 UIA。"""
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


def detect_chat_title(win, max_depth=8):
    """尽力读取右侧会话标题（仅用于提示，读取失败不影响发送）。"""
    candidates = []

    def walk(ctrl, depth):
        if depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for ch in children:
            try:
                if ch.ControlTypeName == "TextControl" and ch.Name:
                    r = ch.BoundingRectangle
                    wr = win.BoundingRectangle
                    dx = r.left - wr.left
                    dy = r.top - wr.top
                    if 200 < dx < (wr.right - wr.left) and -10 < dy < 110 and (r.right - r.left) > 0:
                        candidates.append((dy, dx, ch.Name))
            except Exception:
                pass
            walk(ch, depth + 1)

    walk(win, 0)
    if not candidates:
        return ""
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


# ----------------------------------------------------------------------------
# 主界面
# ----------------------------------------------------------------------------
class GroupSyncApp:
    def __init__(self, root):
        self.root = root
        self.root.title("微信群聊同步助手 · 半自动")
        self.root.geometry("720x660")
        self.root.minsize(640, 560)

        self._stop = threading.Event()
        self._worker_thread = None

        self.groups = []
        self.interval_var = tk.DoubleVar(value=5.0)
        self.open_wait_var = tk.DoubleVar(value=1.2)
        self.confirm_var = tk.BooleanVar(value=True)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.send_key_var = tk.StringVar(value="Enter")

        self._build_ui()
        self._load_config()
        self._check_env()

    # ---------------- 环境自检 ----------------
    def _check_env(self):
        self.log(f"运行环境：Python {sys.version.split()[0]} / {sys.platform} / 微信助手 v1.0")
        if _is_admin():
            self.log("当前以管理员身份运行（微信若也是管理员启动，这是必需的）")
        else:
            self.log("提示：如果微信是“以管理员身份运行”的，本工具也要同样以管理员身份启动，否则按键无效", "warn")
        if find_wechat_window() is None:
            self.log("未检测到微信窗口，请先登录并打开微信电脑版", "warn")
        else:
            self.log("已检测到微信窗口，可以开始", "ok")
        self.log(f"配置保存位置：{CONFIG_PATH}")

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        tip = tk.Label(
            self.root, anchor="w", justify="left", fg="#8a5a00", bg="#fff7e0",
            padx=8, pady=6,
            text="使用前：请先登录并打开「微信电脑版」（3.x / 4.x 均可）。\n"
                 "群名请填完整、唯一的名字，避免搜到同名群；频次不要太高，避免账号被风控。"
        )
        tip.pack(fill="x", padx=10, pady=(10, 4))

        # --- 群列表 ---
        box1 = ttk.LabelFrame(self.root, text="目标群聊（每行一个，可粘贴导入）")
        box1.pack(fill="both", expand=True, padx=10, pady=4)

        list_wrap = ttk.Frame(box1)
        list_wrap.pack(fill="both", expand=True, padx=6, pady=6)

        self.listbox = tk.Listbox(list_wrap, height=7, activestyle="dotbox")
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        btns = ttk.Frame(box1)
        btns.pack(fill="x", padx=6, pady=(0, 6))
        self.entry_group = ttk.Entry(btns)
        self.entry_group.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.entry_group.bind("<Return>", lambda e: self._add_group())
        ttk.Button(btns, text="添加", width=6, command=self._add_group).pack(side="left", padx=2)
        ttk.Button(btns, text="删除选中", width=9, command=self._remove_group).pack(side="left", padx=2)
        ttk.Button(btns, text="批量粘贴导入", width=13, command=self._import_groups).pack(side="left", padx=2)
        ttk.Button(btns, text="清空", width=6, command=self._clear_groups).pack(side="left", padx=2)

        # --- 消息内容 ---
        box2 = ttk.LabelFrame(self.root, text="要同步的消息（仅文字）")
        box2.pack(fill="both", expand=True, padx=10, pady=4)
        self.msg_text = scrolledtext.ScrolledText(box2, height=6, wrap="word", undo=True)
        self.msg_text.pack(fill="both", expand=True, padx=6, pady=6)

        # --- 选项 ---
        box3 = ttk.LabelFrame(self.root, text="选项")
        box3.pack(fill="x", padx=10, pady=4)

        row1 = ttk.Frame(box3)
        row1.pack(fill="x", padx=6, pady=4)
        ttk.Label(row1, text="群间隔(秒)").pack(side="left")
        ttk.Spinbox(row1, from_=1, to=60, increment=1, width=5,
                    textvariable=self.interval_var).pack(side="left", padx=(4, 16))
        ttk.Label(row1, text="进群后等待(秒)").pack(side="left")
        ttk.Spinbox(row1, from_=0.3, to=10, increment=0.1, width=5,
                    textvariable=self.open_wait_var).pack(side="left", padx=(4, 16))
        ttk.Label(row1, text="发送键").pack(side="left")
        ttk.Combobox(row1, textvariable=self.send_key_var, width=10, state="readonly",
                     values=["Enter", "Ctrl+Enter"]).pack(side="left", padx=4)

        row2 = ttk.Frame(box3)
        row2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Checkbutton(row2, text="每个群发送前弹窗确认(推荐)", variable=self.confirm_var).pack(side="left")
        ttk.Checkbutton(row2, text="演练模式(只打开群聊，不发送)", variable=self.dry_run_var).pack(side="left", padx=16)

        # --- 操作按钮 ---
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=10, pady=6)
        self.btn_start = ttk.Button(actions, text="▶  开始同步", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(actions, text="■  停止", command=self._stop_sync, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(actions, text="清空日志", command=self._clear_log).pack(side="left")

        # --- 日志 ---
        box4 = ttk.LabelFrame(self.root, text="运行日志")
        box4.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = scrolledtext.ScrolledText(box4, height=8, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text.tag_config("ok", foreground="#1a7f37")
        self.log_text.tag_config("err", foreground="#c62828")
        self.log_text.tag_config("warn", foreground="#b26a00")

    # ---------------- 群列表操作 ----------------
    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        for g in self.groups:
            self.listbox.insert(tk.END, g)

    def _add_group(self):
        name = self.entry_group.get().strip()
        if not name:
            return
        if name in self.groups:
            self.log(f"群「{name}」已存在，跳过", "warn")
        else:
            self.groups.append(name)
            self._refresh_list()
        self.entry_group.delete(0, tk.END)

    def _remove_group(self):
        sel = list(self.listbox.curselection())
        for i in reversed(sel):
            del self.groups[i]
        self._refresh_list()

    def _clear_groups(self):
        if self.groups and not messagebox.askyesno("确认", "确定清空所有目标群？"):
            return
        self.groups.clear()
        self._refresh_list()

    def _import_groups(self):
        win = tk.Toplevel(self.root)
        win.title("批量导入群名")
        win.geometry("420x400")
        ttk.Label(win, text="每行一个群名，空行忽略：").pack(anchor="w", padx=10, pady=(10, 4))
        txt = scrolledtext.ScrolledText(win, wrap="word")
        txt.pack(fill="both", expand=True, padx=10, pady=4)

        def do_import():
            raw = txt.get("1.0", "end")
            added = 0
            for line in raw.splitlines():
                name = line.strip()
                if name and name not in self.groups:
                    self.groups.append(name)
                    added += 1
            self._refresh_list()
            self.log(f"批量导入完成，新增 {added} 个群")
            win.destroy()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="导入", command=do_import).pack(side="right")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=6)

    # ---------------- 日志 ----------------
    def log(self, msg, tag=None):
        def _write():
            self.log_text.config(state="normal")
            ts = time.strftime("%H:%M:%S")
            self.log_text.insert(tk.END, f"[{ts}] {msg}\n", tag)
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

    # ---------------- 配置 ----------------
    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.groups = list(cfg.get("groups", []))
            self.interval_var.set(float(cfg.get("interval", 5.0)))
            self.open_wait_var.set(float(cfg.get("open_wait", 1.2)))
            self.confirm_var.set(bool(cfg.get("confirm", True)))
            self.dry_run_var.set(bool(cfg.get("dry_run", False)))
            self.send_key_var.set(cfg.get("send_key", "Enter"))
            self._refresh_list()
            self.log(f"已载入配置：{len(self.groups)} 个群")
        except Exception as e:
            self.log(f"读取配置失败：{e}", "warn")

    def _save_config(self):
        cfg = {
            "groups": self.groups,
            "interval": float(self.interval_var.get() or 5),
            "open_wait": float(self.open_wait_var.get() or 1.2),
            "confirm": bool(self.confirm_var.get()),
            "dry_run": bool(self.dry_run_var.get()),
            "send_key": self.send_key_var.get(),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"保存配置失败：{e}", "warn")

    # ---------------- 发送主流程 ----------------
    def _start(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return

        groups, seen = [], set()
        for g in self.groups:
            if g not in seen:
                seen.add(g)
                groups.append(g)
        if not groups:
            messagebox.showwarning("提示", "请先添加至少一个目标群。")
            return

        text = self.msg_text.get("1.0", "end").rstrip("\n")
        if not text.strip():
            messagebox.showwarning("提示", "消息内容不能为空。")
            return

        if not self.dry_run_var.get():
            preview = text if len(text) <= 120 else text[:120] + "…"
            if not messagebox.askyesno(
                "确认发送",
                f"将把以下消息依次发送到 {len(groups)} 个群：\n\n{preview}\n\n"
                f"群间隔 {self.interval_var.get()} 秒，"
                f"{'逐群确认' if self.confirm_var.get() else '不逐群确认'}。\n\n是否继续？"
            ):
                return

        self._save_config()
        self._stop.clear()
        self._set_running(True)
        self.log(f"开始同步：{len(groups)} 个群")

        self._worker_thread = threading.Thread(
            target=self._worker,
            args=(groups, text, float(self.interval_var.get()),
                  float(self.open_wait_var.get()), bool(self.confirm_var.get()),
                  bool(self.dry_run_var.get()), self.send_key_var.get()),
            daemon=True,
        )
        self._worker_thread.start()

    def _stop_sync(self):
        self._stop.set()
        self.log("已请求停止，当前群处理完后结束…", "warn")

    def _ask_confirm(self, title, message):
        """在工作线程里弹出主线程对话框：True=发送 False=跳过 None=停止"""
        holder = {}
        done = threading.Event()

        def _show():
            holder["v"] = messagebox.askyesnocancel(title, message)
            done.set()

        self.root.after(0, _show)
        done.wait()
        return holder.get("v")

    def _worker(self, groups, text, interval, open_wait, confirm, dry_run, send_key):
        ok = fail = skipped = 0
        try:
            win = find_wechat_window()
            if win is None:
                self.log("未找到微信窗口。请先登录并打开微信电脑版后重试。", "err")
                return

            self.log(f"已连接微信窗口：{win.Name}")
            activate_window(win)
            time.sleep(0.6)

            total = len(groups)
            for idx, group in enumerate(groups, 1):
                if self._stop.is_set():
                    self.log("已停止。", "warn")
                    break

                # 每次重新定位窗口，防止用户在过程中挪动/最小化
                win = find_wechat_window() or win
                activate_window(win)
                time.sleep(0.35)

                self.log(f"[{idx}/{total}] 搜索并进入群：{group}")

                # 1) 搜索群
                auto.SetClipboardText(group)
                auto.SendKeys("{Ctrl}f", waitTime=0.25)
                auto.SendKeys("{Ctrl}a", waitTime=0.1)
                auto.SendKeys("{Ctrl}v", waitTime=0.9)
                auto.SendKeys("{Enter}", waitTime=0.8)
                time.sleep(open_wait)

                # 2) 尽力核对会话标题
                detected = ""
                try:
                    detected = detect_chat_title(win)
                except Exception:
                    pass

                if detected and detected != group:
                    self.log(f"  提示：检测到当前会话标题为「{detected}」，与目标不一致，请留意。", "warn")
                elif detected:
                    self.log(f"  已进入会话：{detected}", "ok")

                if dry_run:
                    self.log(f"  演练模式：已定位到群「{group}」，未发送。")
                    ok += 1
                    time.sleep(interval)
                    continue

                # 3) 逐群确认
                if confirm:
                    preview = text if len(text) <= 200 else text[:200] + "…"
                    hint = (f"当前会话：{detected}" if detected
                            else "未能自动读取会话标题，请确认微信当前打开的正是该群")
                    ans = self._ask_confirm(
                        "确认发送",
                        f"即将发送到「{detected or group}」\n({hint})\n\n{preview}\n\n"
                        f"是 = 发送，否 = 跳过，取消 = 停止全部"
                    )
                    if ans is None:
                        self._stop.set()
                        self.log("用户终止了任务。", "warn")
                        break
                    if ans is False:
                        self.log(f"  已跳过该群。", "warn")
                        skipped += 1
                        time.sleep(interval)
                        continue
                    # 弹窗会抢走焦点，发送前必须把焦点还给微信
                    activate_window(win)
                    time.sleep(0.5)

                # 4) 粘贴并发送
                auto.SetClipboardText(text)
                auto.SendKeys("{Ctrl}v", waitTime=0.5)
                seq = "{Ctrl}{Enter}" if send_key == "Ctrl+Enter" else "{Enter}"
                auto.SendKeys(seq, waitTime=0.4)

                self.log(f"  已发送到「{detected or group}」", "ok")
                ok += 1
                time.sleep(interval)

        except Exception as e:
            self.log(f"运行出错：{e}", "err")
            fail += 1
        finally:
            self.log(f"任务结束：成功 {ok}，跳过 {skipped}，异常 {fail}", "ok")
            self._set_running(False)
            self._stop.clear()

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
    app = GroupSyncApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
