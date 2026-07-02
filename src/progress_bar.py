# src/progress_bar.py
"""
Progress Bar Utility - clean one-line terminal progress.
"""

import sys
import time


class ProgressBar:
    """Progress bar 1 baris dengan format visual lama + elapsed time + ETA."""

    def __init__(
        self,
        total: int,
        prefix: str = "",
        length: int = 30,
        update_every: int = 1,
        show_eta: bool = True
    ):
        self.total = max(int(total), 1)
        self.prefix = prefix
        self.length = length
        self.update_every = max(int(update_every), 1)
        self.show_eta = show_eta

        self.current = 0
        self.start_time = None
        self.last_rendered = -1

    def start(self):
        self.start_time = time.time()
        self.update(0, force=True)

    def update(self, current: int, force: bool = False):
        self.current = min(max(int(current), 0), self.total)

        should_render = (
            force
            or self.current == self.total
            or self.current - self.last_rendered >= self.update_every
        )

        if not should_render:
            return

        percent = self.current / self.total
        filled = int(self.length * percent)

        bar = "█" * filled + "░" * (self.length - filled)

        elapsed = time.time() - self.start_time if self.start_time else 0
        elapsed_text = self._format_time(elapsed)

        eta_text = ""
        if self.show_eta and self.current > 0 and self.current < self.total:
            eta = (elapsed / self.current) * (self.total - self.current)
            eta_text = f" | eta {self._format_time(eta)}"

        prefix_text = f"{self.prefix} " if self.prefix else ""

        text = (
            f"\r{prefix_text}"
            f"[{bar}] "
            f"{percent * 100:.1f}% "
            f"({self.current}/{self.total}) "
            f"| elapsed {elapsed_text}"
            f"{eta_text}"
        )

        sys.stdout.write(text + " " * 10)
        sys.stdout.flush()

        self.last_rendered = self.current

    def increment(self, step: int = 1):
        self.update(self.current + step)

    def finish(self):
        self.update(self.total, force=True)
        print()

    @staticmethod
    def _format_time(seconds: float) -> str:
        seconds = int(seconds)

        if seconds < 60:
            return f"{seconds}s"

        minutes, seconds = divmod(seconds, 60)

        if minutes < 60:
            return f"{minutes}m {seconds}s"

        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m"


class SimpleProgress(ProgressBar):
    pass


def print_section(title: str, char: str = "=", length: int = 60):
    print(f"\n{char * length}")
    print(title.center(length))
    print(f"{char * length}\n")


def print_step(step_num: int, title: str):
    print(f"[STEP {step_num}] {title}")


def print_success(message: str):
    print(f"[OK] {message}")


def print_error(message: str):
    print(f"[ERROR] {message}")


def print_info(message: str):
    print(f"[INFO] {message}")