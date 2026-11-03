#!/usr/bin/env python3
"""End GPU processes that hold a CUDA context without computing, on request.

A process counts as idle while NVML records no utilization sample for it. After
--idle-seconds of observed idleness any user on the machine may end it through
the gpu-idle-kill command, which talks to this service over --socket. A granted
request sends SIGTERM, and SIGKILL --grace-seconds later if the context is
still held. The service never signals on its own. With --dry-run it checks and
logs kill requests without signaling. The rule and its detection floor are in
docs/gpu-idle-kill.md.
"""

import argparse
import enum
import json
import logging
import math
import os
import pwd
import re
import signal
import socket
import struct
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple

MAX_DT = 10.0
SAVE_EVERY = 60.0
# A longer stop discards the clocks, because activity during the stop was not
# seen and a restored clock could mark a process eligible that worked meanwhile.
MAX_RESTORE_GAP = 600.0
ERROR_LOG_EVERY = 3600.0
CMDLINE_MAX = 200
MIB = 1024 * 1024
STATE_VERSION = 1
METRIC = "gpu_idle_kill_in_seconds"
SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
# A request is one short line; anything longer is a client bug or garbage.
REQUEST_MAX_BYTES = 256
# Kill requests per user per window. A refusal is folded to one log line an
# hour so a tight loop cannot flood the log.
RATE_BURST = 6
RATE_PERIOD = 60.0
RATE_LOG_EVERY = 3600.0

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")

log = logging.getLogger("gpu-idle-kill")


class Phase(enum.Enum):
    WATCHING = "watching"
    ELIGIBLE = "eligible"
    TERM_SENT = "SIGTERM sent"
    KILL_SENT = "SIGKILL sent"


@dataclass(frozen=True)
class ProcKey:
    pid: int
    # Field 22 of /proc/<pid>/stat, in clock ticks since boot. A reused pid
    # gets a later start, so it becomes a new key with a fresh idle clock.
    start: int


@dataclass(frozen=True)
class ProcObs:
    username: str
    gpus: Mapping[int, int | None]
    newest_sample_us: int | None
    cmdline: str


Observation = dict[ProcKey, ProcObs]


@dataclass(frozen=True)
class Tracked:
    idle_seconds: float
    last_sample_us: int
    phase: Phase
    phase_since: float


State = dict[ProcKey, Tracked]


@dataclass(frozen=True)
class Config:
    idle_seconds: float
    grace_seconds: float
    dry_run: bool


@dataclass(frozen=True)
class Eligible:
    key: ProcKey
    obs: ProcObs
    idle_seconds: float


@dataclass(frozen=True)
class Kill:
    key: ProcKey
    obs: ProcObs
    idle_seconds: float


@dataclass(frozen=True)
class ActiveAgain:
    key: ProcKey
    obs: ProcObs


@dataclass(frozen=True)
class Gone:
    key: ProcKey


Action = Eligible | Kill | ActiveAgain | Gone

SIGNALLED = (Phase.TERM_SENT, Phase.KILL_SENT)


def step(state: State, obs: Observation | None, dt: float, now: float, cfg: Config) -> tuple[State, list[Action]]:
    """Advance every process by one poll.

    obs is None when the poll failed. Then nothing changes and no time accrues.
    dt is the monotonic time since the previous successful poll, now the
    monotonic time of this one.
    """
    if obs is None:
        return state, []
    dt = min(dt, MAX_DT)
    actions: list[Action] = [Gone(key) for key, old in state.items() if key not in obs and old.phase in SIGNALLED]
    new_state: State = {}
    for key, proc in obs.items():
        old = state.get(key)
        if old is None:
            sample = 0 if proc.newest_sample_us is None else proc.newest_sample_us
            new_state[key] = Tracked(0.0, sample, Phase.WATCHING, now)
            continue
        tracked, action = _advance(key, old, proc, dt, now, cfg)
        new_state[key] = tracked
        if action is not None:
            actions.append(action)
    return new_state, actions


def _advance(
    key: ProcKey, old: Tracked, proc: ProcObs, dt: float, now: float, cfg: Config
) -> tuple[Tracked, Action | None]:
    if old.phase is Phase.KILL_SENT:
        return old, None
    if old.phase is Phase.TERM_SENT:
        if now - old.phase_since < cfg.grace_seconds:
            return old, None
        return replace(old, phase=Phase.KILL_SENT, phase_since=now), Kill(key, proc, old.idle_seconds)

    sample = proc.newest_sample_us
    # Any change counts, not only a larger value, because sample timestamps
    # are wall clock and a clock stepped back must not hide activity. An
    # unchanged value is the same sample seen again.
    if sample is not None and sample != old.last_sample_us:
        if old.phase is Phase.ELIGIBLE:
            return Tracked(0.0, sample, Phase.WATCHING, now), ActiveAgain(key, proc)
        return replace(old, idle_seconds=0.0, last_sample_us=sample), None

    idle = old.idle_seconds + dt
    if idle < cfg.idle_seconds:
        return replace(old, idle_seconds=idle), None
    if old.phase is Phase.ELIGIBLE:
        return replace(old, idle_seconds=idle), None
    return Tracked(idle, old.last_sample_us, Phase.ELIGIBLE, now), Eligible(key, proc, idle)


class Countdown(NamedTuple):
    gpu: int
    pid: int
    username: str
    seconds: float


def countdowns(state: State, obs: Observation, cfg: Config) -> tuple[Countdown, ...]:
    """One countdown per (process, GPU), for the state step returned for obs."""
    return tuple(
        Countdown(index, key.pid, proc.username, max(0.0, cfg.idle_seconds - state[key].idle_seconds))
        for key, proc in obs.items()
        for index in sorted(proc.gpus)
    )


class Row(NamedTuple):
    """One line of the list the client renders."""

    pid: int
    username: str
    gpus: str
    status: str
    idle_seconds: float
    cmdline: str


def list_rows(state: State, obs: Observation, cfg: Config) -> tuple[Row, ...]:
    if obs is None:
        return ()
    rows = []
    for key, proc in obs.items():
        tracked = state.get(key)
        if tracked is None:
            status = "idle"
            idle = 0.0
        elif tracked.phase in SIGNALLED:
            status = "ending"
            idle = tracked.idle_seconds
        else:
            idle = tracked.idle_seconds
            status = "eligible" if idle >= cfg.idle_seconds else "idle"
        gpus = ",".join(f"{index}:{'?' if mib is None else mib}MiB" for index, mib in sorted(proc.gpus.items()))
        rows.append(Row(key.pid, proc.username, gpus, status, idle, proc.cmdline))
    return tuple(sorted(rows))


@dataclass(frozen=True)
class Grant:
    """The pid is eligible; the caller may signal it."""

    key: ProcKey
    obs: ProcObs
    idle_seconds: float


@dataclass(frozen=True)
class Refusal:
    reason: str


@dataclass(frozen=True)
class AlreadyEnding:
    pid: int


Decision = Grant | Refusal | AlreadyEnding


def decide_kill(state: State, obs: Observation | None, cfg: Config, pid: int, start_now: int | None) -> Decision:
    """Judge one kill request against the current observation.

    start_now is the pid's start time read from /proc just now, or None when
    the pid no longer exists. It is compared against the tracked key so a
    reused pid is never judged with its predecessor's idle clock.
    """
    if obs is None:
        return Refusal("NVML is unavailable, try again")
    key = next((k for k in obs if k.pid == pid), None)
    if key is None:
        return Refusal("no process with this pid holds a GPU context")
    if start_now is None:
        return Refusal("the process just exited")
    if start_now != key.start:
        return Refusal("the pid was reused by a new process")
    tracked = state.get(key)
    if tracked is None:
        return Refusal("the process was just seen, no idle history yet, try again in a second")
    if tracked.phase in SIGNALLED:
        return AlreadyEnding(pid)
    if tracked.idle_seconds < cfg.idle_seconds:
        remaining = cfg.idle_seconds - tracked.idle_seconds
        return Refusal(f"idle for {tracked.idle_seconds / 3600:.1f}h of {cfg.idle_seconds / 3600:.0f}h, "
                       f"{remaining / 3600:.1f}h to go")
    return Grant(key, obs[key], tracked.idle_seconds)


def encode_state(state: State, boot_id: str, saved_at: float) -> str:
    procs = {
        f"{key.pid}:{key.start}": {"idle_seconds": t.idle_seconds, "last_sample_us": t.last_sample_us}
        for key, t in state.items()
    }
    return json.dumps(
        {"version": STATE_VERSION, "boot_id": boot_id, "saved_at": saved_at, "procs": procs}, indent=2, sort_keys=True
    )


_PROC_KEY = re.compile(r"([0-9]+):([0-9]+)")


class BadStateError(ValueError):
    """The state file cannot be restored."""


def decode_state(text: str, boot_id: str, now: float, wall_now: float) -> State:
    """Parse a saved state into restored processes, raising ValueError if it is unusable."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        raise BadStateError("unknown state version")
    if data.get("boot_id") != boot_id:
        raise BadStateError("saved before the last reboot")
    saved_at = data.get("saved_at")
    if isinstance(saved_at, bool) or not isinstance(saved_at, int | float) or not math.isfinite(saved_at):
        raise BadStateError("saved_at is not a number")
    if abs(wall_now - saved_at) > MAX_RESTORE_GAP:
        raise BadStateError(f"saved {wall_now - saved_at:.0f} s ago, more than {MAX_RESTORE_GAP:.0f} s")
    procs = data.get("procs")
    if not isinstance(procs, dict):
        raise BadStateError("procs is not an object")
    state: State = {}
    for raw_key, raw in procs.items():
        match = _PROC_KEY.fullmatch(raw_key)
        if match is None or not isinstance(raw, dict):
            raise BadStateError(f"bad entry {raw_key!r}")
        idle = raw.get("idle_seconds")
        last = raw.get("last_sample_us")
        if (
            isinstance(idle, bool)
            or not isinstance(idle, int | float)
            or not math.isfinite(idle)
            or idle < 0
            or isinstance(last, bool)
            or not isinstance(last, int)
            or last < 0
        ):
            raise BadStateError(f"bad entry {raw_key!r}")
        state[ProcKey(int(match[1]), int(match[2]))] = Tracked(float(idle), last, Phase.WATCHING, now)
    return state


def load_state(path: Path, boot_id: str, now: float, wall_now: float) -> State:
    try:
        state = decode_state(path.read_text(), boot_id, now, wall_now)
    except FileNotFoundError:
        log.info("no state file at %s, starting empty", path)
        return {}
    except (OSError, ValueError) as err:
        log.warning("ignoring state file %s, starting empty: %s", path, err)
        return {}
    log.info("restored %d processes from %s", len(state), path)
    return state


def save_state(path: Path, state: State, boot_id: str, saved_at: float) -> None:
    save_text(path, encode_state(state, boot_id, saved_at))


class Throttle:
    """Logs each distinct message at most once per period."""

    def __init__(self, period: float) -> None:
        self._period = period
        self._last: dict[str, float] = {}

    def warning(self, message: str) -> None:
        now = time.monotonic()
        last = self._last.get(message)
        if last is None or now - last >= self._period:
            self._last[message] = now
            log.warning("%s", message)


class RateLimiter:
    """Allows RATE_BURST kill requests per user per RATE_PERIOD."""

    def __init__(self, burst: int = RATE_BURST, period: float = RATE_PERIOD) -> None:
        self._burst = burst
        self._period = period
        self._attempts: dict[int, deque[float]] = {}
        self._last_refusal_log: dict[int, float] = {}

    def allow(self, uid: int, now: float) -> bool:
        attempts = self._attempts.setdefault(uid, deque())
        while attempts and now - attempts[0] >= self._period:
            attempts.popleft()
        if len(attempts) >= self._burst:
            return False
        attempts.append(now)
        return True

    def should_log_refusal(self, uid: int, now: float) -> bool:
        """True once per RATE_LOG_EVERY, so repeated refusals stay visible but cheap."""
        last = self._last_refusal_log.get(uid)
        if last is None or now - last >= RATE_LOG_EVERY:
            self._last_refusal_log[uid] = now
            return True
        return False


def parse_start(stat: bytes) -> int:
    # comm (field 2) may hold spaces and parentheses, so count fields from
    # the last ")". The first field after it is field 3.
    return int(stat[stat.rindex(b")") + 2 :].split()[22 - 3])


def read_start(pid: int) -> int:
    return parse_start(Path(f"/proc/{pid}/stat").read_bytes())


def read_cmdline(pid: int) -> str:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return raw.rstrip(b"\0").replace(b"\0", b" ").decode(errors="replace")[:CMDLINE_MAX]


def username(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def peer_uid(conn: socket.socket) -> int:
    _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, struct.calcsize("3i")))
    return uid


class NvmlReader:
    def __init__(self, throttle: Throttle) -> None:
        import pynvml  # type: ignore[import-not-found]

        self._nvml: Any = pynvml
        self._throttle = throttle
        self._ready = False

    def _init(self) -> None:
        if not self._ready:
            self._nvml.nvmlInit()
            self._ready = True

    def _failed(self, err: Exception) -> None:
        self._throttle.warning(f"NVML poll failed: {type(err).__name__}: {err}")

    def gpu_count(self) -> int | None:
        try:
            self._init()
            return int(self._nvml.nvmlDeviceGetCount())
        except self._nvml.NVMLError as err:
            self._failed(err)
            return None

    def read(self) -> Observation | None:
        try:
            self._init()
            gpus, newest = self._read_devices()
        except self._nvml.NVMLError as err:
            self._failed(err)
            return None
        owners: dict[ProcKey, str] = {}
        memory: dict[ProcKey, dict[int, int | None]] = {}
        for index, procs in gpus.items():
            for pid, used in procs.items():
                try:
                    key = ProcKey(pid, read_start(pid))
                    owner = username(os.stat(f"/proc/{pid}").st_uid)
                except FileNotFoundError:
                    # The process left between the NVML call and this read.
                    continue
                owners[key] = owner
                memory.setdefault(key, {})[index] = None if used is None else used // MIB
        obs: Observation = {}
        for key, gpus_used in memory.items():
            try:
                cmdline = read_cmdline(key.pid)
            except FileNotFoundError:
                continue
            obs[key] = ProcObs(owners[key], gpus_used, newest.get(key.pid), cmdline)
        return obs

    def _read_devices(self) -> tuple[dict[int, dict[int, int | None]], dict[int, int]]:
        gpus: dict[int, dict[int, int | None]] = {}
        newest: dict[int, int] = {}
        for index in range(int(self._nvml.nvmlDeviceGetCount())):
            handle = self._nvml.nvmlDeviceGetHandleByIndex(index)
            gpus[index] = {p.pid: p.usedGpuMemory for p in self._nvml.nvmlDeviceGetComputeRunningProcesses(handle)}
            try:
                samples = self._nvml.nvmlDeviceGetProcessUtilization(handle, 0)
            except self._nvml.NVMLError_NotFound:
                # No process used this GPU since the driver started sampling.
                continue
            for sample in samples:
                newest[sample.pid] = max(newest.get(sample.pid, 0), sample.timeStamp)
        return gpus, newest


def send_signal(key: ProcKey, sig: signal.Signals) -> str:
    """Signal the observed process, never a later process that reused its pid."""
    if sys.platform != "linux":
        raise RuntimeError("pidfd signalling needs Linux")
    try:
        fd = os.pidfd_open(key.pid)
    except ProcessLookupError:
        return "already exited"
    except OSError as err:
        return f"failed: {err}"
    try:
        # The pidfd pins whichever process held the pid when it was opened.
        # If /proc still shows the observed start time after that, the pidfd
        # is that process, so the signal cannot reach a reused pid.
        if read_start(key.pid) != key.start:
            return "skipped, pid reused"
        signal.pidfd_send_signal(fd, sig)
    except (ProcessLookupError, FileNotFoundError):
        return "already exited"
    except OSError as err:
        return f"failed: {err}"
    finally:
        os.close(fd)
    return "sent"


def describe(key: ProcKey, proc: ProcObs) -> str:
    gpus = ",".join(f"{index}:{'?' if mib is None else mib}MiB" for index, mib in sorted(proc.gpus.items()))
    return f"user={proc.username} pid={key.pid} gpus={gpus}"


def perform(action: Action) -> None:
    if isinstance(action, Gone):
        log.info("gone pid=%d, its GPU memory is free", action.key.pid)
        return
    what = describe(action.key, action.obs)
    cmd = action.obs.cmdline
    if isinstance(action, ActiveAgain):
        log.info("active again, no longer eligible %s cmd=%r", what, cmd)
    elif isinstance(action, Eligible):
        log.info("eligible %s idle=%.1fh cmd=%r", what, action.idle_seconds / 3600, cmd)
    else:
        result = send_signal(action.key, signal.SIGKILL)
        log.info("SIGKILL %s: %s idle=%.1fh cmd=%r", result, what, action.idle_seconds / 3600, cmd)


class Shared:
    """The state and latest observation, guarded by one lock.

    The poll loop replaces them each second; the request server reads them and
    flips a granted process into TERM_SENT under the same lock.
    """

    def __init__(self, state: State) -> None:
        self.lock = threading.Lock()
        self.state = state
        self.obs: Observation | None = None


class KillServer:
    """Serves LIST and KILL requests on a local unix socket.

    Any local user may connect; SO_PEERCRED names the requester in every log
    line. Eligibility comes from the poll loop's state, so it cannot be forged
    or raced from this side.
    """

    def __init__(self, path: Path, shared: Shared, cfg: Config, throttle: Throttle) -> None:
        self._path = path
        self._shared = shared
        self._cfg = cfg
        self._throttle = throttle
        self._rate = RateLimiter()

    def start(self) -> None:
        threading.Thread(target=self._serve, name="kill-server", daemon=True).start()

    def _serve(self) -> None:
        # The unit's RuntimeDirectory creates the parent; a socket left by a
        # previous restart is replaced.
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self._path))
        os.chmod(self._path, 0o666)
        server.listen(8)
        while True:
            try:
                conn, _ = server.accept()
            except OSError as err:
                self._throttle.warning(f"accept on {self._path} failed: {err}")
                continue
            try:
                self._handle(conn)
            except OSError as err:
                self._throttle.warning(f"request on {self._path} failed: {err}")
            finally:
                conn.close()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        data = conn.recv(REQUEST_MAX_BYTES)
        if not data:
            return
        request = data.decode(errors="replace").strip()
        response = self._respond(peer_uid(conn), request)
        conn.sendall((response + "\n").encode())

    def _respond(self, uid: int, request: str) -> str:
        parts = request.split()
        if parts == ["LIST"]:
            return self._list()
        if len(parts) == 2 and parts[0] == "KILL":
            if not parts[1].isdigit():
                return "NO the pid must be a number"
            return self._kill(uid, int(parts[1]))
        return "NO unknown request, send LIST or KILL <pid>"

    def _list(self) -> str:
        with self._shared.lock:
            if self._shared.obs is None:
                return "NO NVML is unavailable, try again"
            rows = list_rows(self._shared.state, self._shared.obs, self._cfg)
        lines = [f"OK {len(rows)} threshold={self._cfg.idle_seconds:g} dry_run={int(self._cfg.dry_run)}"]
        lines += [f"{r.pid} {r.username} {r.gpus} {r.status} {r.idle_seconds:.0f} {r.cmdline}" for r in rows]
        return "\n".join(lines)

    def _kill(self, uid: int, pid: int) -> str:
        requester = username(uid)
        now = time.monotonic()
        if not self._rate.allow(uid, now):
            if self._rate.should_log_refusal(uid, now):
                log.info("kill refused by=%s pid=%d reason=rate limited", requester, pid)
            return "NO rate limited, try again in a minute"
        try:
            start_now: int | None = read_start(pid)
        except FileNotFoundError:
            start_now = None
        with self._shared.lock:
            decision = decide_kill(self._shared.state, self._shared.obs, self._cfg, pid, start_now)
            if isinstance(decision, Grant) and not self._cfg.dry_run:
                tracked = self._shared.state[decision.key]
                self._shared.state[decision.key] = replace(tracked, phase=Phase.TERM_SENT, phase_since=now)
        if isinstance(decision, Refusal):
            log.info("kill refused by=%s pid=%d reason=%s", requester, pid, decision.reason)
            return f"NO {decision.reason}"
        if isinstance(decision, AlreadyEnding):
            return "OK the process is already being ended"
        what = describe(decision.key, decision.obs)
        if self._cfg.dry_run:
            log.info(
                "kill granted by=%s %s idle=%.1fh dry_run=True cmd=%r",
                requester, what, decision.idle_seconds / 3600, decision.obs.cmdline,
            )
            return "OK dry-run: the process is eligible, no signal was sent"
        result = send_signal(decision.key, signal.SIGTERM)
        log.info("kill granted by=%s %s idle=%.1fh cmd=%r",
                 requester, what, decision.idle_seconds / 3600, decision.obs.cmdline)
        log.info("SIGTERM %s: %s idle=%.1fh cmd=%r",
                 result, what, decision.idle_seconds / 3600, decision.obs.cmdline)
        return f"OK SIGTERM {result}: SIGKILL in {self._cfg.grace_seconds:g}s if it holds the context"


class CountdownCollector:
    def __init__(self, hostname: str) -> None:
        self.hostname = hostname
        self.countdowns: tuple[Countdown, ...] = ()

    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily  # type: ignore[import-not-found]

        family = GaugeMetricFamily(
            METRIC,
            "Seconds of further GPU idleness before gpu-idle-kill may end the process",
            labels=["hostname", "index", "pid", "username"],
        )
        for c in self.countdowns:
            family.add_metric([self.hostname, str(c.gpu), str(c.pid), c.username], c.seconds)
        yield family


def serve_metrics(collector: CountdownCollector, address: str, port: int) -> None:
    from prometheus_client import CollectorRegistry, start_http_server  # type: ignore[import-not-found]

    registry = CollectorRegistry()
    registry.register(collector)
    start_http_server(port, address, registry=registry)


@dataclass(frozen=True)
class Options:
    hostname: str
    metrics_address: str
    metrics_port: int
    socket_path: Path
    state_file: Path
    interval: float
    config: Config


def parse_args(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(
        description="Track idle GPU processes and end them on request through gpu-idle-kill."
    )
    parser.add_argument("--hostname", required=True, help="value of the hostname metric label")
    parser.add_argument("--metrics-address", required=True)
    parser.add_argument("--metrics-port", type=int, required=True)
    parser.add_argument("--socket", type=Path, default=Path("/run/gpu-idle-kill/kill.sock"))
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=float, default=86400.0)
    parser.add_argument("--grace-seconds", type=float, default=60.0, help="SIGTERM to SIGKILL delay")
    parser.add_argument("--interval", type=float, default=1.0, help="poll period in seconds")
    parser.add_argument("--dry-run", action="store_true", help="check and log kill requests, but never signal")
    args = parser.parse_args(argv)
    if not args.idle_seconds > 0:
        parser.error("--idle-seconds must be positive")
    if not args.grace_seconds >= 0:
        parser.error("--grace-seconds must not be negative")
    if not args.interval > 0:
        parser.error("--interval must be positive")
    return Options(
        hostname=args.hostname,
        metrics_address=args.metrics_address,
        metrics_port=args.metrics_port,
        socket_path=args.socket,
        state_file=args.state_file,
        interval=args.interval,
        config=Config(idle_seconds=args.idle_seconds, grace_seconds=args.grace_seconds, dry_run=args.dry_run),
    )


def run(options: Options) -> int:
    # systemd appends stdout to a plain file, which has no timestamps of its own.
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    cfg = options.config
    throttle = Throttle(ERROR_LOG_EVERY)
    reader = NvmlReader(throttle)
    log.info(
        "starting hostname=%s metrics=%s:%d socket=%s state_file=%s idle_seconds=%g grace_seconds=%g interval=%g "
        "dry_run=%s gpus=%s",
        options.hostname,
        options.metrics_address,
        options.metrics_port,
        options.socket_path,
        options.state_file,
        cfg.idle_seconds,
        cfg.grace_seconds,
        options.interval,
        cfg.dry_run,
        reader.gpu_count(),
    )
    boot_id = BOOT_ID.read_text().strip()
    state = load_state(options.state_file, boot_id, time.monotonic(), time.time())
    shared = Shared(state)
    collector = CountdownCollector(options.hostname)
    serve_metrics(collector, options.metrics_address, options.metrics_port)
    KillServer(options.socket_path, shared, cfg, throttle).start()

    # A plain flag, because threading.Event.set takes a lock that the
    # interrupted main thread may hold, and the poll sleep is short anyway.
    stopping = False

    def request_stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)

    last_ok: float | None = None
    last_save = time.monotonic()
    while not stopping:
        started = time.monotonic()
        obs = reader.read()
        now = time.monotonic()
        dt = 0.0
        if obs is not None:
            dt = 0.0 if last_ok is None else now - last_ok
            last_ok = now
        with shared.lock:
            shared.state, actions = step(shared.state, obs, dt, now, cfg)
            shared.obs = obs
            if obs is not None:
                collector.countdowns = countdowns(shared.state, obs, cfg)
        for action in actions:
            perform(action)
        if now - last_save >= SAVE_EVERY:
            last_save = now
            try:
                with shared.lock:
                    text = encode_state(shared.state, boot_id, time.time())
                save_text(options.state_file, text)
            except OSError as err:
                throttle.warning(f"saving {options.state_file} failed: {err}")
        time.sleep(max(0.0, options.interval - (time.monotonic() - started)))

    log.info("stopping, saving %d processes", len(shared.state))
    try:
        with shared.lock:
            text = encode_state(shared.state, boot_id, time.time())
        save_text(options.state_file, text)
    except OSError as err:
        log.warning("saving %s failed: %s", options.state_file, err)
    return 0


def save_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(run(parse_args()))
