"""Console progress for streaming and patch-generation work."""

import time


class ConsoleProgress:
    """
    Single-line console progress used by the expensive GARC patch creator.
    """

    def __init__(self, total: int, label: str, width: int = 34) -> None:
        self.total = max(1, int(total))
        self.label = label
        self.width = width
        self.started = time.monotonic()
        self.last_len = 0
        self.last_draw = 0.0

    def update(self, done: int, detail: str = "", force: bool = False) -> None:
        now = time.monotonic()
        done = max(0, min(int(done), self.total))
        if not force and done < self.total and now - self.last_draw < 0.08:
            return
        self.last_draw = now
        ratio = done / self.total
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        elapsed = max(0.001, now - self.started)
        rate = done / elapsed
        eta = (self.total - done) / rate if rate > 0 else 0.0
        suffix = f"  {detail}" if detail else ""
        line = (
            f"\r[{bar}] {ratio * 100:6.2f}%  "
            f"{done:,}/{self.total:,}  elapsed {elapsed:0.1f}s"
        )
        if done < self.total and rate > 0:
            line += f"  ETA {eta:0.1f}s"
        line += suffix
        if len(line) < self.last_len:
            line += " " * (self.last_len - len(line))
        self.last_len = len(line)
        print(line, end="", flush=True)
        if done >= self.total:
            print()
