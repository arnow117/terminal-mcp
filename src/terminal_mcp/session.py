"""Connection-owned Unix sessions, asynchronous I/O, and process-group cleanup."""

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import errno
import fcntl
import os
import pty
import re
import signal as signals
import struct
import termios
import time
from uuid import uuid4

from .buffer import OutputBuffer
from .config import Config
from .contracts import ToolError, validate_arguments
from .keys import encode_keys
from .policy import CompleteLineTracker, Policy, PreparedStart


_REPORT_INTERVAL = 0.01


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Session:
    session_id: str
    name: str | None
    command: str | None
    cwd: str
    mode: str
    timeout_s: int
    buffer: OutputBuffer
    cols: int = 80
    rows: int = 24
    pid: int | None = None
    pgid: int | None = None
    status: str = "running"
    exit_code: int | None = None
    signal: str | None = None
    started_at: str = field(default_factory=_timestamp)
    ended_at: str | None = None
    last_activity_at: str = field(default_factory=_timestamp)
    ended_monotonic: float | None = None
    master_fd: int | None = None
    process: asyncio.subprocess.Process | None = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    tracker: CompleteLineTracker = field(default_factory=CompleteLineTracker)
    reader_tasks: list[asyncio.Task] = field(default_factory=list)
    reaper_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None
    gc_task: asyncio.Task | None = None
    spawn_task: asyncio.Task | None = None
    termination_task: asyncio.Task | None = None
    termination_reason: str | None = None
    escalated: bool = False
    writes_closed: bool = False
    group_cleaned: bool = False

    def notify(self) -> None:
        # Replacing rather than clearing the event prevents lost wakeups among readers.
        self.changed.set()
        self.changed = asyncio.Event()

    def result(self) -> dict:
        return {"session_id": self.session_id, "status": self.status,
                "exit_code": self.exit_code, "signal": self.signal}

    def summary(self) -> dict:
        return self.result() | {
            "name": self.name, "command": self.command, "cwd": self.cwd,
            "mode": self.mode, "pid": self.pid, "pgid": self.pgid,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "last_activity_at": self.last_activity_at,
            "output_start_cursor": self.buffer.start_offset,
            "output_end_cursor": self.buffer.end_offset,
            "output_total_bytes": self.buffer.end_offset, "timeout_s": self.timeout_s,
        }


class SessionManager:
    """Own all children for one connection; shutdown_all must run before loop exit.

    Public operations are async and accept the corresponding tool's keyword arguments.
    Policy checks and pending input state share the same lock as actual writes.
    Registry persistence and audit dispatch belong to the server lifecycle layer.
    """

    def __init__(self, config: Config):
        self.config = config
        self.policy = Policy(config)
        self.sessions: dict[str, Session] = {}
        self._start_lock = asyncio.Lock()
        self._closing = False
        self._shutdown_task: asyncio.Task | None = None

    def _get(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise ToolError("SESSION_NOT_FOUND", "No retained session has this ID.") from None

    async def start(self, **arguments) -> dict:
        args = validate_arguments("terminal_start", arguments)
        prepared = self.policy.prepare_start(**{key: args[key] for key in ("command", "cwd", "env", "timeout_s") if key in args})
        async with self._start_lock:
            if self._closing:
                raise ToolError("INTERNAL_ERROR", "The session manager is shutting down.")
            if sum(session.status == "running" for session in self.sessions.values()) >= self.config.max_sessions:
                raise ToolError("LIMIT_REACHED", "The configured running session limit has been reached.")
            session = Session(str(uuid4()), args["name"], prepared.command, str(prepared.cwd),
                              args["mode"], prepared.timeout_s,
                              OutputBuffer(self.config.buffer_bytes, mode=args["mode"]),
                              cols=args["cols"], rows=args["rows"])
            self.sessions[session.session_id] = session
            session.spawn_task = asyncio.create_task(self._spawn(session, prepared))
            # A cancelled start request does not abandon an in-flight spawn or its error.
            session.spawn_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(session.spawn_task)
        result = await self._read(session, 0, wait_ms=args["wait_ms"], until=args["until"], strip_ansi=args["strip_ansi"])
        result.pop("ended_at")
        return result | {"pid": session.pid, "pgid": session.pgid, "mode": session.mode, "started_at": session.started_at}

    async def _spawn(self, session: Session, prepared: PreparedStart) -> None:
        environment = dict(prepared.env)
        argv = (["/bin/sh", "-c", prepared.command] if prepared.command is not None
                else [environment.get("SHELL") or "/bin/sh", "-l"])
        try:
            if session.mode == "pty":
                environment["TERM"] = "xterm-256color"
                size = struct.pack("HHHH", session.rows, session.cols, 0, 0)
                cwd = session.cwd
                executable = argv[0]
                pid, fd = pty.fork()
                if pid == 0:
                    # Keep the post-fork child path minimal: no logging, locks or awaits.
                    try:
                        os.chdir(cwd)
                        fcntl.ioctl(0, termios.TIOCSWINSZ, size)
                        os.execvpe(executable, argv, environment)
                    except BaseException:
                        os._exit(127)
                session.pid = session.pgid = pid
                session.master_fd = fd
                os.set_blocking(fd, False)
                asyncio.get_running_loop().add_reader(fd, self._read_pty, session)
            else:
                session.process = await asyncio.create_subprocess_exec(
                    *argv, cwd=session.cwd, env=environment, start_new_session=True,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                session.pid = session.pgid = session.process.pid
                for stream in ("stdout", "stderr"):
                    session.reader_tasks.append(asyncio.create_task(self._drain(session, stream)))
        except OSError as exc:
            # A parent-side setup failure may occur after fork; never abandon that child.
            if session.pid is not None:
                session.reaper_task = asyncio.create_task(self._reap(session))
                await self._terminate(session, "KILL", 0, "shutdown")
            self._close_pty(session)
            session.status = "failed_to_start"
            session.ended_at = session.last_activity_at = _timestamp()
            session.ended_monotonic = time.monotonic()
            session.exited.set()
            session.notify()
            self._schedule_gc(session)
            raise ToolError("START_FAILED", f"Cannot create session: {exc}.",
                            session_id=session.session_id, status=session.status) from exc
        session.reaper_task = asyncio.create_task(self._reap(session))
        session.watchdog_task = asyncio.create_task(self._watchdog(session))

    def _append(self, session: Session, stream: str, data: bytes) -> None:
        session.buffer.append(stream, data)
        session.last_activity_at = _timestamp()
        session.notify()

    def _read_pty(self, session: Session) -> None:
        if session.master_fd is None:
            return
        # Bound each callback to preserve fairness under continuous output.
        for _ in range(16):
            try:
                data = os.read(session.master_fd, 8192)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno not in (errno.EIO, errno.EBADF):
                    raise
                data = b""
            if not data:
                self._close_pty(session)
                return
            self._append(session, "pty", data)

    def _close_pty(self, session: Session) -> None:
        fd, session.master_fd = session.master_fd, None
        if fd is not None:
            loop = asyncio.get_running_loop()
            loop.remove_reader(fd)
            loop.remove_writer(fd)
            os.close(fd)
        if session.mode == "pty":
            session.buffer.close_stream("pty")
            session.notify()

    async def _drain(self, session: Session, stream: str) -> None:
        reader = getattr(session.process, stream)
        try:
            while data := await reader.read(8192):
                self._append(session, stream, data)
        finally:
            session.buffer.close_stream(stream)
            session.notify()

    async def _reap(self, session: Session) -> None:
        while True:
            if session.mode == "pty":
                pid, status = os.waitpid(session.pid, os.WNOHANG)
                if pid:
                    self._read_pty(session)
                    returncode = os.waitstatus_to_exitcode(status)
                    break
            elif session.process.returncode is not None:
                # Process.wait() can wait for inherited pipe EOF after leader exit.
                # The asyncio child watcher already reaps the leader independently.
                returncode = session.process.returncode
                await asyncio.sleep(0)
                break
            await asyncio.sleep(_REPORT_INTERVAL)
        session.exit_code = returncode if returncode >= 0 else None
        session.signal = signals.Signals(-returncode).name if returncode < 0 else None
        session.status = ("timed_out" if session.termination_reason == "timeout" else
                          "exited" if returncode >= 0 else "signaled")
        session.ended_at = session.last_activity_at = _timestamp()
        session.ended_monotonic = time.monotonic()
        session.writes_closed = True
        if session.process is not None and session.process.stdin is not None:
            session.process.stdin.close()
        session.exited.set()
        session.notify()
        self._schedule_gc(session)
        # Keep owning descendants after leader exit, but release dead groups promptly
        # so a retained result can never signal a PGID reused during its 600-second TTL.
        while self._group_exists(session.pgid):
            await asyncio.sleep(_REPORT_INTERVAL)
        await self._close_streams(session)
        session.group_cleaned = True
        if session.watchdog_task is not None:
            session.watchdog_task.cancel()

    def _schedule_gc(self, session: Session) -> None:
        if session.gc_task is None:
            session.gc_task = asyncio.create_task(self._gc(session))

    async def _gc(self, session: Session) -> None:
        await asyncio.sleep(self.config.exit_retention_s)
        await self._terminate(session, "TERM", self.config.kill_grace_ms, "shutdown")
        self.sessions.pop(session.session_id, None)

    async def _watchdog(self, session: Session) -> None:
        await asyncio.sleep(session.timeout_s)
        # Even a reaped leader may have left children holding the PTY or pipes.
        if not session.group_cleaned and self._group_exists(session.pgid):
            await self._terminate(session, "TERM", self.config.kill_grace_ms, "timeout")

    async def read(self, session_id: str, cursor: int, **arguments) -> dict:
        args = validate_arguments("terminal_read", {"session_id": session_id, "cursor": cursor, **arguments})
        session = self._get(session_id)
        return await self._read(session, **{key: args[key] for key in ("cursor", "max_bytes", "wait_ms", "until", "strip_ansi")})

    async def _read(self, session: Session, cursor: int, *, max_bytes: int = 65536,
                    wait_ms: int = 0, until: str | None = None, strip_ansi: bool = True) -> dict:
        # Validate the cursor against the start-of-call end, not a later append.
        session.buffer.read(cursor, max_bytes, strip_ansi)
        deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        pattern = re.compile(until) if until is not None else None
        checked_revision = -1
        reason = "immediate"
        while wait_ms:
            event = session.changed
            if pattern is not None and checked_revision != session.buffer.revision:
                checked_revision = session.buffer.revision
                if pattern.search(session.buffer.raw_text(cursor)):
                    reason = "until"
                    break
            if session.status != "running":
                reason = "exited"
                break
            if pattern is None:
                page = session.buffer.read(cursor, max_bytes, strip_ansi)
                if page.next_cursor > max(cursor, session.buffer.start_offset):
                    reason = "output"
                    break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                reason = "deadline"
                break
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                reason = "deadline"
                break
        return session.result() | asdict(session.buffer.read(cursor, max_bytes, strip_ansi)) | {"wait_reason": reason, "ended_at": session.ended_at}

    async def write(self, session_id: str, **arguments) -> dict:
        args = validate_arguments("terminal_write", {"session_id": session_id, **arguments})
        session = self._get(session_id)
        async with session.write_lock:
            if session.status == "lost":
                raise ToolError("SESSION_LOST", "The original server no longer owns this session.")
            if session.status != "running" or session.writes_closed:
                raise ToolError("SESSION_EXITED", "This session no longer accepts input.")
            self.policy.check_complete_lines(session.tracker, args["text"], args["keys"])
            data = args["text"].encode("utf-8") + encode_keys(args["keys"])
            cursor = session.buffer.end_offset
            try:
                await self._write_all(session, data)
            except asyncio.CancelledError:
                # The tracker may describe bytes not sent. Stop all further writes.
                session.writes_closed = True
                raise
            except (OSError, ConnectionError) as exc:
                session.writes_closed = True
                raise ToolError("SESSION_EXITED", "Session input closed before the write completed; read remaining output or start a new session.") from exc
            session.last_activity_at = _timestamp()
        result = await self._read(session, cursor, wait_ms=args["wait_ms"], until=args["until"], strip_ansi=args["strip_ansi"])
        result.pop("ended_at")
        return result | {"written_bytes": len(data)}

    async def _write_all(self, session: Session, data: bytes) -> None:
        if session.mode == "pipe":
            session.process.stdin.write(data)
            await session.process.stdin.drain()
            return
        view = memoryview(data)
        while view:
            fd = session.master_fd
            if fd is None:
                raise BrokenPipeError("PTY input is closed")
            try:
                written = os.write(fd, view)
                if not written:
                    raise BrokenPipeError("PTY write made no progress")
                view = view[written:]
            except BlockingIOError:
                loop = asyncio.get_running_loop()
                writable = loop.create_future()

                def ready():
                    if not writable.done():
                        writable.set_result(None)

                loop.add_writer(fd, ready)
                try:
                    # Also wake if the process exits while the descriptor is blocked.
                    exited = asyncio.create_task(session.exited.wait())
                    try:
                        await asyncio.wait((writable, exited), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        exited.cancel()
                        await asyncio.gather(exited, return_exceptions=True)
                finally:
                    if session.master_fd == fd:
                        loop.remove_writer(fd)
                if session.writes_closed:
                    raise BrokenPipeError("PTY input is closed")

    async def list(self, session_id: str | None = None, include_exited: bool = True) -> dict:
        validate_arguments("terminal_list", {"session_id": session_id, "include_exited": include_exited})
        sessions = ([self._get(session_id)] if session_id is not None else
                    [session for session in self.sessions.values() if include_exited or session.status == "running"])
        return {"sessions": [session.summary() for session in sessions], "count": len(sessions)}

    @staticmethod
    def _group_exists(pgid: int | None) -> bool:
        if pgid is None:
            return False
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS can report EPERM while an exiting group awaits reaping.
            return True

    async def kill(self, session_id: str, **arguments) -> dict:
        args = validate_arguments("terminal_kill", {"session_id": session_id, **arguments})
        session = self._get(session_id)
        if session.status == "lost":
            raise ToolError("SESSION_LOST", "The original server no longer owns this session.")
        await self._terminate(session, args["signal"], args["grace_ms"], "manual")
        if args["remove"]:
            if session.gc_task is not None:
                session.gc_task.cancel()
                await asyncio.gather(session.gc_task, return_exceptions=True)
            self.sessions.pop(session_id, None)
        return session.result() | {"termination_reason": session.termination_reason or "manual",
                                   "escalated": session.escalated, "removed": args["remove"], "ended_at": session.ended_at}

    async def _terminate(self, session: Session, signal: str, grace_ms: int, reason: str) -> None:
        if session.termination_task is None:
            session.termination_task = asyncio.create_task(self._terminate_group(session, signal, grace_ms, reason))
        # A cancelled handler must not cancel process ownership cleanup.
        await asyncio.shield(session.termination_task)

    async def _terminate_group(self, session: Session, signal: str, grace_ms: int, reason: str) -> None:
        session.writes_closed = True
        session.termination_reason = session.termination_reason or reason
        groups = {session.pgid} if session.pgid is not None and not session.group_cleaned else set()
        if session.master_fd is not None:
            try:
                foreground = os.tcgetpgrp(session.master_fd)
                if foreground > 0 and os.getsid(foreground) == session.pid:
                    groups.add(foreground)
            except (ProcessLookupError, OSError):
                pass

        def send(name):
            for pgid in groups:
                try:
                    os.killpg(pgid, getattr(signals, f"SIG{name}"))
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Exiting macOS groups can transiently reject signals. This is
                    # not success: the bounded group-disappearance check still applies.
                    pass

        send(signal)
        deadline = asyncio.get_running_loop().time() + (0 if signal == "KILL" else grace_ms / 1000)
        while any(self._group_exists(pgid) for pgid in groups):
            if asyncio.get_running_loop().time() >= deadline:
                if signal != "KILL":
                    session.escalated = True
                    send("KILL")
                break
            await asyncio.sleep(_REPORT_INTERVAL)
        # Allow the OS to reap orphan descendants; never waitpid unrelated children.
        deadline = asyncio.get_running_loop().time() + 1
        while any(self._group_exists(pgid) for pgid in groups):
            if asyncio.get_running_loop().time() >= deadline:
                raise ToolError("INTERNAL_ERROR", "The owned process group has not disappeared after SIGKILL.")
            await asyncio.sleep(_REPORT_INTERVAL)
        if session.reaper_task is not None:
            await session.reaper_task
        await self._close_streams(session)
        session.group_cleaned = True
        if session.watchdog_task is not None and not session.watchdog_task.done():
            session.watchdog_task.cancel()
        session.notify()

    async def _close_streams(self, session: Session) -> None:
        self._read_pty(session)
        self._close_pty(session)
        if session.process is not None:
            if session.process.stdin is not None:
                session.process.stdin.close()
                try:
                    await session.process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            if session.reader_tasks:
                # All owned writers are gone, so normal EOF drains pending output.
                await asyncio.gather(*session.reader_tasks)
            await session.process.wait()

    async def shutdown_all(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        async with self._start_lock:
            self._closing = True
            sessions = list(self.sessions.values())
        await asyncio.gather(*(session.spawn_task for session in sessions if session.spawn_task is not None), return_exceptions=True)
        results = await asyncio.gather(*(self._terminate(session, "TERM", self.config.kill_grace_ms, "shutdown") for session in sessions), return_exceptions=True)
        background = [task for session in sessions for task in (session.gc_task, session.watchdog_task) if task is not None]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
