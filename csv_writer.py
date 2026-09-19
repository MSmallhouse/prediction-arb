"""
Background CSV writer.

Arb OPEN/CLOSE rows and convergence ticks were written with a synchronous
`open()`/`write()` from inside the WS tick path. In asyncio that blocks the one
loop every other market is serviced by, so a slow disk write delays unrelated
price updates and order placement.

Rows are built on the event loop (cheap, and captures values at the instant they
were true) and handed to a daemon thread that owns all file I/O. The queue is
unbounded but drains far faster than it fills; `pending()` is surfaced in the
heartbeat so a backlog is visible rather than silent.
"""

import csv
import logging
import queue
import threading
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

_queue: "queue.Queue[tuple[Path, Sequence[str], list[dict]] | None]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()
_dropped = 0


def _worker() -> None:
    while True:
        item = _queue.get()
        if item is None:          # shutdown sentinel
            _queue.task_done()
            return
        filepath, fieldnames, rows = item
        try:
            write_header = not filepath.exists()
            with filepath.open("a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if write_header:
                    writer.writeheader()
                for row in rows:
                    writer.writerow(row)
        except Exception:
            log.exception("csv_writer: failed writing %d row(s) to %s", len(rows), filepath)
        finally:
            _queue.task_done()


def _ensure_started() -> None:
    global _thread  # noqa: PLW0603
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="csv-writer", daemon=True)
            _thread.start()


def queue_rows(filepath: Path, fieldnames: Sequence[str], rows: list[dict]) -> None:
    """Hand rows to the writer thread. Returns immediately; never raises."""
    if not rows:
        return
    _ensure_started()
    _queue.put((filepath, fieldnames, rows))


def pending() -> int:
    """Queued write batches not yet flushed to disk."""
    return _queue.qsize()


def flush(timeout: float = 5.0) -> bool:
    """Block until the queue drains. Used on shutdown; returns False on timeout."""
    if _thread is None:
        return True
    done = threading.Event()

    def _waiter() -> None:
        _queue.join()
        done.set()

    threading.Thread(target=_waiter, daemon=True).start()
    return done.wait(timeout)
