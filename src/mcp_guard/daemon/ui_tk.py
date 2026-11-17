"""Tk UI: one always-on-top pop-up per pending request, plus an activity window.

Tk isn't thread-safe, so other threads only put messages on a queue that the
Tk thread drains every 50 ms.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable

from mcp_guard import paths
from mcp_guard.config import Settings
from mcp_guard.daemon.broker import ApprovalBroker, PendingApproval

log = logging.getLogger(__name__)

ALLOW_BG, BLOCK_BG, ACCENT = "#1f8f4e", "#c0392b", "#f5a623"


def _enable_windows_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def _flash_window(widget: tk.Misc) -> None:
    """Flash the taskbar entry (Windows)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        hwnd = int(widget.wm_frame(), 16)
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 5, 0)  # ALL | TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def _format_arguments(args: Any, limit: int) -> str:
    try:
        text = json.dumps(args, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(args)
    if len(text) > limit:
        text = text[:limit] + f"\n... ({len(text) - limit} more characters truncated)"
    return text


class ApprovalPopup:
    def __init__(self, presenter: "TkPresenter", pending: PendingApproval, slot: int) -> None:
        self.presenter = presenter
        self.pending = pending
        self.closed = False
        info = pending.info
        settings = presenter.settings

        top = self.top = tk.Toplevel(presenter.root)
        top.title(f"MCP Guard - approve {info.get('method', 'request')}?")
        top.configure(bg="white")
        top.resizable(True, True)
        top.protocol("WM_DELETE_WINDOW", lambda: self._decide("block"))
        top.bind("<Escape>", lambda _e: self._decide("block"))
        top.bind("<Alt-a>", lambda _e: self._decide("allow"))
        top.bind("<Alt-b>", lambda _e: self._decide("block"))

        header = tk.Frame(top, bg=ACCENT)
        header.pack(fill="x")
        tk.Label(
            header,
            text="⚠  An MCP request is waiting for your approval",
            bg=ACCENT, fg="black", font=("Segoe UI", 12, "bold"), padx=14, pady=10, anchor="w",
        ).pack(fill="x")

        # Packed before the body so the buttons keep their space on small windows.
        buttons = tk.Frame(top, bg="#f4f4f4", padx=14, pady=10)
        buttons.pack(fill="x", side="bottom")
        tk.Button(
            buttons, text="Allow", underline=0, width=12, bg=ALLOW_BG, fg="white",
            activebackground=ALLOW_BG, font=("Segoe UI", 10, "bold"), relief="flat",
            command=lambda: self._decide("allow"),
        ).pack(side="right", padx=(8, 0))
        block_btn = tk.Button(
            buttons, text="Block", underline=0, width=12, bg=BLOCK_BG, fg="white",
            activebackground=BLOCK_BG, font=("Segoe UI", 10, "bold"), relief="flat",
            command=lambda: self._decide("block"),
        )
        block_btn.pack(side="right")
        tk.Label(buttons, text="Esc = Block   Alt+A = Allow", bg="#f4f4f4", fg="#888",
                 font=("Segoe UI", 8)).pack(side="left")

        body = tk.Frame(top, bg="white", padx=14, pady=10)
        body.pack(fill="both", expand=True)

        rows = [
            ("Requested by", info.get("source", "unknown client")),
            ("MCP server", info.get("server", "?")),
            ("Tool / target", info.get("target", "?")),
            ("Method", info.get("method", "?")),
            ("Transport", info.get("transport", "?")),
            ("Received", time.strftime("%H:%M:%S", time.localtime(pending.created_at))),
        ]
        grid = tk.Frame(body, bg="white")
        grid.pack(fill="x")
        for r, (label, value) in enumerate(rows):
            tk.Label(grid, text=label + ":", bg="white", fg="#555", font=("Segoe UI", 10)).grid(
                row=r, column=0, sticky="w", padx=(0, 10), pady=1
            )
            bold = label in ("MCP server", "Tool / target")
            tk.Label(
                grid, text=str(value), bg="white", font=("Segoe UI", 10, "bold" if bold else "normal"),
                wraplength=420, justify="left",
            ).grid(row=r, column=1, sticky="w", pady=1)

        tk.Label(body, text="Arguments:", bg="white", fg="#555", font=("Segoe UI", 10)).pack(
            anchor="w", pady=(10, 2)
        )
        self.countdown = tk.Label(body, bg="white", fg="#888", font=("Segoe UI", 9))
        self.countdown.pack(side="bottom", anchor="w")
        self.remember = tk.BooleanVar(value=False)
        tk.Checkbutton(
            body, bg="white", variable=self.remember, font=("Segoe UI", 9),
            text=f"Remember my choice for {info.get('server')}:{info.get('target')} until MCP Guard restarts",
        ).pack(side="bottom", anchor="w", pady=(8, 0))

        args_box = ScrolledText(body, height=6, wrap="word", font=("Consolas", 10), relief="solid", bd=1)
        args_box.insert("1.0", _format_arguments(info.get("arguments", {}), settings.max_preview_chars))
        args_box.configure(state="disabled")
        args_box.pack(fill="both", expand=True)

        # Size from content and DPI; a fixed size cut off the buttons at 125%+ scaling.
        top.update_idletasks()
        scale = max(1.0, top.winfo_fpixels("1i") / 96.0)
        sx, sy = top.winfo_screenwidth(), top.winfo_screenheight()
        w = min(max(int(640 * scale), top.winfo_reqwidth()), int(sx * 0.9))
        h = min(top.winfo_reqheight(), int(sy * 0.85))
        top.minsize(min(w, int(480 * scale)), min(h, int(400 * scale)))
        offset = int((slot % 8) * 36 * scale)
        x = max(0, min((sx - w) // 2 + offset, sx - w))
        y = max(0, min((sy - h) // 4 + offset, sy - h - 40))
        top.geometry(f"{w}x{h}+{x}+{y}")

        self._raise()
        block_btn.focus_set()  # a stray key press can only block
        top.bell()
        _flash_window(top)
        self._tick()

    def _raise(self) -> None:
        if self.closed:
            return
        self.top.attributes("-topmost", True)
        self.top.deiconify()
        self.top.lift()
        try:
            self.top.focus_force()
        except tk.TclError:
            pass

    def _tick(self) -> None:
        if self.closed:
            return
        remaining = int(self.pending.deadline - time.time())
        action = self.presenter.settings.timeout_action
        self.countdown.configure(
            text=f"Auto-{action} in {max(0, remaining)}s if you do not respond"
        )
        # Don't re-set topmost here: with several pop-ups open it makes them flicker.
        self.top.after(1000, self._tick)

    def _decide(self, decision: str) -> None:
        if self.closed:
            return
        self.presenter.broker.decide(
            self.pending.approval_id, decision, by="user", remember=self.remember.get()
        )
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.top.destroy()
            except tk.TclError:
                pass


class TkPresenter:
    def __init__(self, broker: ApprovalBroker, settings: Settings, start_hidden: bool = False,
                 on_quit: Callable[[], None] = lambda: None) -> None:
        _enable_windows_dpi_awareness()
        self.broker = broker
        self.settings = settings
        self.on_quit = on_quit
        self._queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.popups: dict[str, ApprovalPopup] = {}
        self._slot = 0

        self.root = tk.Tk()
        self.root.title("MCP Guard")
        self.root.geometry("900x420")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self._build_main_window()
        if start_hidden:
            self.root.withdraw()
        self.root.after(50, self._pump)

    # Called from any thread.
    def show(self, pending: PendingApproval) -> None:
        self._queue.put(("show", pending))

    def dismiss(self, approval_id: str) -> None:
        self._queue.put(("dismiss", approval_id))

    def activity(self, record: dict[str, Any]) -> None:
        self._queue.put(("activity", record))

    def bring_to_front(self) -> None:
        self._queue.put(("front", None))

    def request_quit(self) -> None:
        self._queue.put(("quit", None))

    def _build_main_window(self) -> None:
        top = tk.Frame(self.root, padx=10, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="\U0001f6e1  MCP Guard", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status = tk.Label(top, fg="#555", font=("Segoe UI", 9))
        self.status.pack(side="left", padx=14)
        self._update_status()

        cols = ("time", "event", "source", "server", "target", "result")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", height=12)
        widths = (80, 120, 170, 140, 180, 180)
        for col, width in zip(cols, widths):
            self.tree.heading(col, text=col.title())
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("allow", foreground=ALLOW_BG)
        self.tree.tag_configure("block", foreground=BLOCK_BG)
        self.tree.pack(fill="both", expand=True, padx=10)

        bar = tk.Frame(self.root, padx=10, pady=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Hide to background", command=self.hide).pack(side="left")
        ttk.Button(bar, text="Forget remembered choices", command=self.broker.forget_session_rules).pack(
            side="left", padx=6
        )
        ttk.Button(bar, text="Open audit log", command=self._open_audit).pack(side="left")
        ttk.Button(bar, text="Quit MCP Guard", command=self._quit).pack(side="right")

    def _update_status(self) -> None:
        n = len(self.popups)
        self.status.configure(
            text=f"Listening on {self.settings.daemon_url}   |   Pending approvals: {n}   |   "
            f"Closing this window keeps MCP Guard running"
        )

    def _add_row(self, r: dict[str, Any]) -> None:
        kind = r.get("kind", "")
        if kind == "decision":
            result = f"{str(r.get('decision', '')).upper()} ({r.get('decided_by', '')})"
            tag = r.get("decision", "")
        else:
            result, tag = r.get("detail", ""), ""
        values = (
            time.strftime("%H:%M:%S"), kind, r.get("source", ""), r.get("server", ""),
            r.get("target", r.get("command", "")), result,
        )
        self.tree.insert("", 0, values=[str(v) for v in values], tags=(tag,))
        for extra in self.tree.get_children()[500:]:
            self.tree.delete(extra)

    def hide(self) -> None:
        self.root.withdraw()

    def _open_audit(self) -> None:
        path = str(paths.audit_path())
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except OSError as exc:
            log.warning("Cannot open audit log: %s", exc)

    def _quit(self) -> None:
        self.on_quit()
        self.root.quit()

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "show":
                    if payload.approval_id not in self.popups and payload.decision is None:
                        self.popups[payload.approval_id] = ApprovalPopup(self, payload, self._slot)
                        self._slot = self._slot + 1 if self.popups else 0
                elif kind == "dismiss":
                    popup = self.popups.pop(payload, None)
                    if popup:
                        popup.close()
                    if not self.popups:
                        self._slot = 0
                elif kind == "activity":
                    self._add_row(payload)
                elif kind == "front":
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
                elif kind == "quit":
                    self._quit()
                    return
                self._update_status()
        except queue.Empty:
            pass
        except Exception:
            log.exception("UI pump error")
        self.root.after(50, self._pump)

    def run(self) -> None:
        self.root.mainloop()
