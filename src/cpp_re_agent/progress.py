"""
Live progress reporting for long, concurrent, LLM-bound work.

Stage 1 spends minutes inside a handful of parallel LLM calls, so an event-only
log goes quiet for long stretches and never says how far along it is. This
renders a bar that keeps ticking on a timer, names what is currently in flight,
and estimates what is left — while degrading to plain lines when stdout is not
a terminal (CI, pipes, redirects).

Thread-safe: worker threads call `start`/`finish` directly.
"""
import os
import shutil
import sys
import threading
import time
from typing import Optional, TextIO


def format_duration(seconds: float) -> str:
    """`0.4` -> `0.4s`, `7.4` -> `7.4s`, `92` -> `1m32s`, `4210` -> `1h10m`."""
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        # A fast call rounding to a bare "0s" reads like nothing happened.
        return f"{seconds:.1f}s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class Progress:
    """
    A progress bar for `total` units of work.

    `enabled=False` makes every method a no-op, so callers (and tests) never
    need to branch on whether reporting is wanted.
    """

    def __init__(self, total: int, *, stream: Optional[TextIO] = None,
                 enabled: bool = True, width: int = 22,
                 refresh: float = 1.0, label: str = ""):
        self.total = max(0, total)
        self.stream = stream if stream is not None else sys.stdout
        # NOT `enabled and total > 0`: callers legitimately construct with an
        # unknown total and set `.total` once the work has been planned, and
        # folding the total into `enabled` silently disabled those bars forever.
        self.enabled = bool(enabled)
        self.width = width
        self.refresh = refresh
        self.label = label

        self.done = 0
        self.failed = 0
        self._running: dict = {}          # name -> started-at
        self._started_at = time.monotonic()
        self._lock = threading.RLock()
        self._bar_shown = False
        self._ticker: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # A bar that rewrites its own line only makes sense on a terminal.
        # Elsewhere (CI logs, `| tee`, redirects) emit plain lines instead.
        self._live = bool(self.enabled and getattr(self.stream, "isatty", lambda: False)()
                          and not os.getenv("CPP_RE_NO_PROGRESS"))

    # --- lifecycle --------------------------------------------------------

    def __enter__(self):
        if self.enabled and self._live:
            self._ticker = threading.Thread(target=self._tick, daemon=True)
            self._ticker.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=2 * self.refresh)
            self._ticker = None
        with self._lock:
            self._clear()

    def _tick(self) -> None:
        """Redraw on a timer so elapsed/ETA stay live between events."""
        while not self._stop.wait(self.refresh):
            with self._lock:
                if self._running:
                    self._draw()

    # --- events -----------------------------------------------------------

    def note(self, message: str) -> None:
        """Print a line above the bar (scrolls; the bar stays at the bottom)."""
        if not self.enabled:
            return
        with self._lock:
            self._clear()
            self.stream.write(message.rstrip() + "\n")
            self.stream.flush()
            self._draw()

    def start(self, name: str) -> None:
        """Called from a worker thread as the unit actually begins."""
        if not self.enabled:
            return
        with self._lock:
            self._running[name] = time.monotonic()
            if self._live:
                self._draw()
            else:
                self.stream.write(f"  -> {name}\n")
                self.stream.flush()

    def finish(self, name: str, seconds: float, ok: bool = True,
               detail: str = "") -> None:
        if not self.enabled:
            return
        with self._lock:
            self._running.pop(name, None)
            self.done += 1
            if not ok:
                self.failed += 1
            mark = "ok " if ok else "FAIL"
            line = f"  {mark} {name} ({format_duration(seconds)})"
            if detail:
                line += f" — {detail}"
            self._clear()
            self.stream.write(line + "\n")
            self.stream.flush()
            self._draw()

    # --- rendering --------------------------------------------------------

    def _eta_seconds(self) -> Optional[float]:
        """
        Wall-clock estimate from observed throughput.

        Deliberately not "mean duration x remaining": that ignores concurrency
        and overestimates by the worker count. Elapsed/done already has the
        real parallelism baked in.
        """
        if self.done <= 0:
            return None
        elapsed = time.monotonic() - self._started_at
        return (elapsed / self.done) * max(0, self.total - self.done)

    def render(self) -> str:
        """The bar as a string (also the seam the tests assert on)."""
        frac = (self.done / self.total) if self.total else 0.0
        filled = int(round(frac * self.width))
        bar = "#" * filled + "-" * (self.width - filled)

        parts = [f"[{bar}] {self.done}/{self.total} {frac * 100:3.0f}%"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        parts.append(format_duration(time.monotonic() - self._started_at))
        eta = self._eta_seconds()
        if eta is not None and self.done < self.total:
            parts.append(f"eta {format_duration(eta)}")

        head = (f"{self.label} " if self.label else "") + "  ".join(parts)

        if self._running:
            head += self._running_suffix(len(head))
        return head

    def _running_suffix(self, head_len: int) -> str:
        """
        "  running: name 2m10s, other 8s, +3 more", trimmed to the terminal.

        Longest-running first: when a level stalls, that is the one worth
        naming. If only one fits, its *name* is truncated but its elapsed time
        is kept — "which call is slow, and how slow" is the whole point.
        """
        prefix = "  running: "
        budget = self._term_width() - head_len - len(prefix) - 1
        if budget < 14:
            return ""      # genuinely no room; the bar itself is more useful

        now = time.monotonic()
        ordered = sorted(self._running.items(), key=lambda kv: kv[1])
        items = [(n, format_duration(now - t)) for n, t in ordered]

        shown, used, body = [], 0, None
        for i, (name, dur) in enumerate(items):
            text = f"{name} {dur}"
            others = len(items) - i - 1
            tail = f", +{others} more" if others else ""
            if used + len(text) + (2 if shown else 0) + len(tail) > budget:
                if not shown:
                    # Keep the duration, shorten the name around it.
                    keep = max(6, budget - len(dur) - len(tail) - 5)
                    clipped = name if len(name) <= keep else name[:keep] + "..."
                    body = f"{clipped} {dur}{tail}"
                else:
                    body = ", ".join(shown) + f", +{len(items) - i} more"
                break
            used += len(text) + (2 if shown else 0)
            shown.append(text)
        if body is None:
            body = ", ".join(shown)
        # Belt and braces: the at-least-one case above can still overshoot on a
        # narrow terminal, and render() should never be the thing that wraps.
        return prefix + body[:budget]

    def _term_width(self) -> int:
        try:
            return shutil.get_terminal_size(fallback=(100, 24)).columns
        except Exception:
            return 100

    def _draw(self) -> None:
        if not self._live:
            return
        text = self.render()[: self._term_width() - 1]
        self.stream.write("\r\033[2K" + text)
        self.stream.flush()
        self._bar_shown = True

    def _clear(self) -> None:
        if self._live and self._bar_shown:
            self.stream.write("\r\033[2K")
            self.stream.flush()
            self._bar_shown = False
