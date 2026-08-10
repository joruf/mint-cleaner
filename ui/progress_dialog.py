#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Modal progress dialog for long running Mint Cleaner operations.

Shows a determinate progress bar, the current step, the percentage and a full
checklist of every step with its status, so it is visible what has been done
and what is still pending. Used for the startup size analysis, manual refreshes
and the cleanup run itself.

All methods must be called from the Tk main thread. Background workers report
their progress through a queue that the application drains in the main loop.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import List, Optional, Sequence

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_GLYPHS = {
    STATUS_PENDING: "○",   # ○
    STATUS_ACTIVE: "▶",    # ▶
    STATUS_DONE: "✔",      # ✔
    STATUS_FAILED: "✖",    # ✖
}


class ProgressDialog(tk.Toplevel):
    """
    Modal window with a step checklist and a determinate progress bar.
    """

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        subtitle: str = "",
        steps: Optional[Sequence[str]] = None,
        list_height: int = 14,
    ) -> None:
        """
        Create the dialog and show it centered above its parent.

        @param parent Parent window used for modality and placement
        @param title Window title and headline
        @param subtitle Explanatory line below the headline
        @param steps Initial step labels
        @param list_height Visible lines of the step checklist
        """
        super().__init__(parent)
        self.transient(parent)
        self.title(title)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        self._labels: List[str] = list(steps or [])
        self._status: List[str] = [STATUS_PENDING for _ in self._labels]
        self._results: List[str] = ["" for _ in self._labels]
        self._active_index: int = -1
        self._closed = False

        container = ttk.Frame(self, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        ttk.Label(container, text=title, style="CardTitle.TLabel").pack(anchor="w")

        self._subtitle_var = tk.StringVar(master=self, value=subtitle)
        ttk.Label(
            container,
            textvariable=self._subtitle_var,
            style="Hint.TLabel",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 10))

        self._bar = ttk.Progressbar(
            container, orient=tk.HORIZONTAL, mode="determinate", length=520, maximum=100.0
        )
        self._bar.pack(fill=tk.X)

        status_row = ttk.Frame(container)
        status_row.pack(fill=tk.X, pady=(6, 10))

        self._current_var = tk.StringVar(master=self, value="Preparing ...")
        ttk.Label(
            status_row,
            textvariable=self._current_var,
            style="CardTitle.TLabel",
            wraplength=420,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, anchor="w")

        self._counter_var = tk.StringVar(master=self, value="")
        ttk.Label(status_row, textvariable=self._counter_var, style="Hint.TLabel").pack(
            side=tk.RIGHT, anchor="e"
        )

        list_frame = ttk.Frame(container)
        list_frame.pack(fill=tk.BOTH, expand=True)

        self._list = tk.Text(
            list_frame,
            height=list_height,
            width=64,
            wrap=tk.NONE,
            font=("Consolas", 9),
            bg="#f8f9fa",
            fg="#2c3e50",
            relief=tk.FLAT,
            bd=1,
            highlightthickness=0,
            cursor="arrow",
        )
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self._list.yview)
        self._list.configure(yscrollcommand=scrollbar.set)
        self._list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._list.tag_configure(STATUS_PENDING, foreground="#8a94a0")
        self._list.tag_configure(STATUS_ACTIVE, foreground="#1a5fb4", font=("Consolas", 9, "bold"))
        self._list.tag_configure(STATUS_DONE, foreground="#1e7f3b")
        self._list.tag_configure(STATUS_FAILED, foreground="#b3261e")
        self._list.configure(state=tk.DISABLED)

        self._render()
        self._center_on_parent(parent)
        self._grab()

    # ----------------------------- Public API -----------------------------

    def set_steps(self, labels: Sequence[str]) -> None:
        """
        Replace the whole step list and reset all statuses.

        @param labels New step labels
        """
        self._labels = list(labels)
        self._status = [STATUS_PENDING for _ in self._labels]
        self._results = ["" for _ in self._labels]
        self._active_index = -1
        self._render()

    def add_steps(self, labels: Sequence[str]) -> None:
        """
        Append additional steps that became known while running.

        @param labels Step labels to append
        """
        for label in labels:
            self._labels.append(label)
            self._status.append(STATUS_PENDING)
            self._results.append("")
        self._render()

    def begin_step(self, index: int, label: str = "", note: str = "") -> None:
        """
        Mark a step as running and update the headline information.

        @param index Zero based step index
        @param label Optional label that replaces the stored one
        @param note Optional extra hint shown next to the step name
        """
        self._ensure_index(index)
        if label:
            self._labels[index] = label
        self._status[index] = STATUS_ACTIVE
        self._active_index = index
        text = self._labels[index]
        self._current_var.set(f"{text} {note}".strip() if note else text)
        self._render()

    def end_step(self, index: int, result: str = "", failed: bool = False) -> None:
        """
        Mark a step as finished and store its result text.

        @param index Zero based step index
        @param result Short result text, for example a size
        @param failed True when the step failed
        """
        self._ensure_index(index)
        self._status[index] = STATUS_FAILED if failed else STATUS_DONE
        self._results[index] = result
        self._render()

    def set_subtitle(self, text: str) -> None:
        """
        Replace the explanatory line below the headline.

        @param text New subtitle text
        """
        self._subtitle_var.set(text)

    def close(self) -> None:
        """Release the grab and destroy the dialog."""
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    # ----------------------------- Internals -----------------------------

    def _ensure_index(self, index: int) -> None:
        """
        Grow the step list so the given index exists.

        @param index Zero based step index
        """
        while index >= len(self._labels):
            self._labels.append(f"Step {len(self._labels) + 1}")
            self._status.append(STATUS_PENDING)
            self._results.append("")

    def _render(self) -> None:
        """Redraw the checklist, the progress bar and the counters."""
        if self._closed:
            return
        total = len(self._labels)
        done = sum(1 for state in self._status if state in (STATUS_DONE, STATUS_FAILED))

        try:
            self._bar.configure(maximum=float(max(total, 1)))
            self._bar.configure(value=float(done))
            percent = int(round(done * 100.0 / total)) if total else 0
            self._counter_var.set(f"Step {min(done + 1, total)} of {total}  ({percent} %)")

            self._list.configure(state=tk.NORMAL)
            self._list.delete("1.0", tk.END)
            for index, label in enumerate(self._labels):
                state = self._status[index]
                result = self._results[index]
                suffix = f"   {result}" if result else ""
                self._list.insert(tk.END, f" {_GLYPHS[state]}  {label}{suffix}\n", state)
            self._list.configure(state=tk.DISABLED)

            if self._active_index >= 0:
                self._list.see(f"{self._active_index + 1}.0")
        except tk.TclError:
            pass

    def _center_on_parent(self, parent: tk.Misc) -> None:
        """
        Place the dialog in the center of its parent window.

        @param parent Parent window
        """
        try:
            self.update_idletasks()
            width = self.winfo_width()
            height = self.winfo_height()
            parent_x = parent.winfo_rootx()
            parent_y = parent.winfo_rooty()
            parent_width = parent.winfo_width()
            parent_height = parent.winfo_height()
            if parent_width <= 1 or parent_height <= 1:
                parent_x, parent_y = 0, 0
                parent_width = self.winfo_screenwidth()
                parent_height = self.winfo_screenheight()
            x = parent_x + max((parent_width - width) // 2, 0)
            y = parent_y + max((parent_height - height) // 3, 0)
            self.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

    def _grab(self, attempt: int = 0) -> None:
        """
        Make the dialog modal, retrying while the window is not yet viewable.

        @param attempt Current retry counter
        """
        if self._closed:
            return
        try:
            self.grab_set()
            self.lift()
        except tk.TclError:
            if attempt < 20:
                self.after(50, lambda: self._grab(attempt + 1))
