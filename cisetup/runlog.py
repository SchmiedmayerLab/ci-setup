#
# This source file is part of the SchmiedmayerLab ci-setup open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#

"""Timestamped, bounded run logs including inherited subprocess output.

Capture changes process-wide file descriptors, so use one RunLog at the CLI
boundary and close it before exec. Stdin is never inspected or redirected.
"""

from __future__ import annotations

import codecs
import errno
import fcntl
import os
import re
import selectors
import stat
import sys
import termios
import threading
import time
import tty
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path


LOG_DIR = Path.home() / "Library/Logs/ci-runner-setup"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_OWNED_NAME = re.compile(r"setup-(\d{4}-\d{2}-\d{2})\.log\Z")
_secrets: set[str] = set()
_secret_lock = threading.Lock()
_active: RunLog | None = None


def register_secret(value: str | None) -> None:
    """Protect a known secret before a command could print it.

    Values are retained for this process's lifetime, including between runs.
    This supplements safe command construction; it cannot discover unknown
    secrets printed by external programs.
    """
    if value:
        with _secret_lock:
            _secrets.add(value)


def _known_secrets() -> tuple[str, ...]:
    with _secret_lock:
        return tuple(sorted(_secrets, key=len, reverse=True))


def redact(text: str) -> str:
    for value in _known_secrets():
        text = text.replace(value, "<redacted>")
    return text


class _Redactor:
    """Withhold a possible secret suffix until the next pipe read arrives."""

    def __init__(self) -> None:
        self.pending = ""

    def feed(self, text: str, *, final: bool = False) -> str:
        text = self.pending + text
        self.pending = ""
        values = _known_secrets()
        if not values:
            return text
        result = []
        offset = 0
        while offset < len(text):
            match = next((value for value in values if text.startswith(value, offset)), None)
            if not final and any(
                len(text) - offset < len(value) and value.startswith(text[offset:])
                for value in values
            ):
                self.pending = text[offset:]
                break
            if match:
                result.append("<redacted>")
                offset += len(match)
            else:
                result.append(text[offset])
                offset += 1
        return "".join(result)


def _owned_logs(directory: Path) -> list[tuple[date, Path]]:
    found = []
    for path in directory.iterdir():
        match = _OWNED_NAME.fullmatch(path.name)
        if not match or not stat.S_ISREG(path.lstat().st_mode):
            continue
        try:
            day = date.fromisoformat(match[1])
        except ValueError:
            continue
        found.append((day, path))
    return sorted(found)


def _prune(directory: Path, retention_days: int, max_bytes: int, today: date) -> None:
    """Called under the directory lock; only this module's regular files count."""
    oldest_day = today - timedelta(days=retention_days - 1)
    files = []
    for day, path in _owned_logs(directory):
        if day < oldest_day:
            path.unlink()
        else:
            files.append(path)
    total = sum(path.stat().st_size for path in files)
    if total <= max_bytes:
        return
    # Leave headroom after crossing the cap. Trimming only one new record's
    # bytes would rewrite almost the entire retained log after every line.
    target_bytes = max_bytes * 3 // 4
    for path in files:
        if total <= target_bytes:
            break
        size = path.stat().st_size
        if size <= total - target_bytes:
            path.unlink()
            total -= size
            continue
        # Keep the newest complete records, including when a single busy day
        # consumes the entire budget. Bounded read: never load a whole log.
        keep = target_bytes - (total - size)
        with path.open("r+b") as stream:
            stream.seek(max(0, size - keep))
            tail = stream.read(keep)
            if size > keep:
                _, _, tail = tail.partition(b"\n")
            stream.seek(0)
            stream.write(tail)
            stream.truncate()
        total = total - size + len(tail)


class RunLog:
    """Tee stdout/stderr to the console and timestamped daily log files.

    ``finish(code)`` records the CLI's returned status. Exceptions otherwise
    imply exit 1 (KeyboardInterrupt: 130, SystemExit: its numeric status).
    Records and console output redact values registered with register_secret.
    The total size limit can shorten the configured retention window.
    """

    def __init__(
        self,
        command: str,
        *,
        directory: Path = LOG_DIR,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        console: bool = True,
    ) -> None:
        if retention_days < 1 or max_bytes < 1024:
            raise ValueError("log retention must be positive and size limit at least 1024 bytes")
        self.command = command
        self.directory = Path(directory)
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.console = console
        self.run_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.path = self.directory / f"setup-{date.today().isoformat()}.log"
        self._exit_code = 0
        self._saved: dict[int, int] = {}
        self._readers: dict[int, int] = {}
        self._pty_readers: set[int] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._closed = False
        self._started = time.monotonic()
        self._stream_settings: list[tuple[object, bool, bool]] = []
        self._lock_fd: int | None = None

    def _record(self, stream: str, message: str) -> None:
        now = datetime.now().astimezone()
        path = self.directory / f"setup-{now.date().isoformat()}.log"
        message = redact(message).replace("\r", "")
        prefix = f"{now.isoformat(timespec='seconds')} [{self.run_id}] {stream} "
        data = (prefix + message + "\n").encode("utf-8", errors="replace")
        # Even a command/exception supplied by a caller cannot defeat the cap.
        if len(data) > self.max_bytes:
            suffix = b" [record truncated]\n"
            prefix_bytes = data[:self.max_bytes - len(suffix)]
            data = prefix_bytes.decode("utf-8", errors="ignore").encode("utf-8") + suffix
        assert self._lock_fd is not None
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("timed out locking run logs")
                time.sleep(0.01)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            _prune(self.directory, self.retention_days, self.max_bytes, now.date())
            self.path = path
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def __enter__(self) -> RunLog:
        global _active
        if self._closed or self._thread is not None:
            raise RuntimeError("a run log context cannot be reused")
        if _active is not None:
            raise RuntimeError("run log capture is already active")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_fd = os.open(
            self.directory / ".runlog.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            self._record("START", f"command={self.command}")
            for stream in (sys.stdout, sys.stderr):
                stream.flush()
                if hasattr(stream, "reconfigure"):
                    self._stream_settings.append((stream, stream.line_buffering, stream.write_through))
                    stream.reconfigure(line_buffering=True, write_through=True)
            for target in (1, 2):
                self._saved[target] = os.dup(target)
                terminal_output = self.console and os.isatty(self._saved[target])
                reader, writer = os.openpty() if terminal_output else os.pipe()
                self._readers[target] = reader
                try:
                    if terminal_output:
                        self._pty_readers.add(reader)
                        # Keep child stdout/stderr terminal-backed. libc then
                        # flushes pending prompts before a terminal stdin read,
                        # instead of buffering them while the user waits.
                        # These PTYs carry OUTPUT ONLY: stdin and the controlling
                        # terminal stay untouched, and nothing writes the master.
                        tty.setraw(writer)
                        try:
                            size = fcntl.ioctl(self._saved[target], termios.TIOCGWINSZ, b"\0" * 8)
                            fcntl.ioctl(writer, termios.TIOCSWINSZ, size)
                        except OSError:
                            pass  # A terminal with no reported size is usable.
                    os.set_blocking(reader, False)
                    os.dup2(writer, target)
                finally:
                    os.close(writer)
            _active = self
            self._thread = threading.Thread(target=self._drain, name="ci-setup-runlog", daemon=True)
            self._thread.start()
        except BaseException:
            self.close(1)
            raise
        return self

    def _drain(self) -> None:
        decoders = {fd: codecs.getincrementaldecoder("utf-8")("replace") for fd in self._readers}
        redactors = {fd: _Redactor() for fd in self._readers}
        pending = {fd: "" for fd in self._readers}
        selector = selectors.DefaultSelector()
        record_limit = min(16_384, self.max_bytes // 4)

        def emit(target: int, text: str, final: bool = False) -> None:
            clean = redactors[target].feed(text, final=final)
            if clean and self.console:
                try:
                    data = memoryview(clean.encode("utf-8", errors="replace"))
                    while data:
                        data = data[os.write(self._saved[target], data):]
                except OSError:
                    pass  # A closed console must not lose durable log output.
            pending[target] += clean
            while "\n" in pending[target] or len(pending[target]) >= record_limit:
                newline = pending[target].find("\n")
                length = newline if 0 <= newline < record_limit else record_limit
                line, pending[target] = pending[target][:length], pending[target][length:]
                if pending[target].startswith("\n"):
                    pending[target] = pending[target][1:]
                save(target, line)
            if final and pending[target]:
                save(target, pending[target])
                pending[target] = ""

        def save(target: int, line: str) -> None:
            if self._error is None:
                try:
                    self._record("stdout" if target == 1 else "stderr", line)
                except Exception as error:
                    self._error = error

        try:
            for target, reader in self._readers.items():
                selector.register(reader, selectors.EVENT_READ, target)
            deadline = None
            while selector.get_map():
                if self._stop.is_set() and deadline is None:
                    # Detached grandchildren may retain pipe writers. Drain
                    # available output, but never wait indefinitely for EOF.
                    deadline = time.monotonic() + 1
                events = selector.select(timeout=0.05)
                if deadline is not None and (not events or time.monotonic() >= deadline):
                    break
                for key, _ in events:
                    try:
                        data = os.read(key.fd, 32_768)
                    except BlockingIOError:
                        continue
                    except OSError as error:
                        # Some PTY implementations signal the slave's closure
                        # with EIO rather than a zero-byte EOF read.
                        if key.fd not in self._pty_readers or error.errno != errno.EIO:
                            raise
                        data = b""
                    if not data:
                        selector.unregister(key.fd)
                    else:
                        emit(key.data, decoders[key.data].decode(data))
            for target in self._readers:
                emit(target, decoders[target].decode(b"", final=True), final=True)
        except Exception as error:
            self._error = error
        finally:
            selector.close()
            for reader in self._readers.values():
                os.close(reader)

    def finish(self, exit_code: int) -> int:
        """Set the returned command status; convenient in ``return log.finish(code)``."""
        self._exit_code = exit_code
        return exit_code

    def close(self, exit_code: int | None = None) -> None:
        global _active
        if self._closed:
            return
        self._closed = True
        if exit_code is not None:
            self._exit_code = exit_code
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        for target, saved in self._saved.items():
            os.dup2(saved, target)
        for stream, line_buffering, write_through in self._stream_settings:
            stream.reconfigure(line_buffering=line_buffering, write_through=write_through)
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        else:
            for reader in self._readers.values():
                os.close(reader)
        for saved in self._saved.values():
            os.close(saved)
        if _active is self:
            _active = None
        try:
            if self._error is not None and self._exit_code == 0:
                self._exit_code = 1
            if self._lock_fd is not None:
                self._record(
                    "END",
                    f"command={self.command} exit={self._exit_code} "
                    f"duration={time.monotonic() - self._started:.1f}s",
                )
            if self._error is not None:
                raise OSError(f"run log capture failed: {self._error}") from self._error
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    def __exit__(self, kind, value, traceback) -> bool:
        if isinstance(value, KeyboardInterrupt):
            self._exit_code = 130
        elif isinstance(value, SystemExit):
            self._exit_code = value.code if isinstance(value.code, int) else int(value.code is not None)
        elif kind is not None:
            self._exit_code = 1
        self.close()
        return False
