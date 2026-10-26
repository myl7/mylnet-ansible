"""Drive the gpu-idle-reaper reducer, state file, and adapters through their interfaces.

The program lives under playbooks/files, so it is loaded by path. NVML is
replaced by a fake that keeps the measured driver semantics. The /proc and
signal tests need Linux and skip elsewhere.
"""

import importlib.util
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from jinja2 import Environment, FileSystemLoader, StrictUndefined

PLAYBOOKS = Path(__file__).resolve().parents[1] / "playbooks"
MODULE_PATH = PLAYBOOKS / "files" / "gpu-idle-reaper" / "gpu_idle_reaper.py"
spec = importlib.util.spec_from_file_location("gpu_idle_reaper", MODULE_PATH)
assert spec is not None
reaper = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = reaper
spec.loader.exec_module(reaper)

LIVE = reaper.Config(idle_seconds=30.0, grace_seconds=5.0, dry_run=False)
DRY = reaper.Config(idle_seconds=30.0, grace_seconds=5.0, dry_run=True)
KEY = reaper.ProcKey(pid=4242, start=1000)
BOOT = "6f1d6b0e-0d7c-4b55-9a57-0d5e3c1e9f00"
WALL = 1_800_000_000.0
ON_LINUX = sys.platform == "linux"
MIB = 1024 * 1024


def proc(sample: int | None = None, gpus: Mapping[int, int | None] | None = None) -> Any:
    return reaper.ProcObs(
        username="alice",
        gpus={0: 20480} if gpus is None else gpus,
        newest_sample_us=sample,
        cmdline="python train.py",
    )


class Reaper:
    """Feeds polls to step the way the main loop does."""

    def __init__(self, cfg: Any, state: Any = None) -> None:
        self.cfg = cfg
        self.state = {} if state is None else state
        self.now = 100.0

    def poll(self, obs: Any, dt: float = 10.0) -> list[Any]:
        self.now += dt
        self.state, actions = reaper.step(self.state, obs, dt, self.now, self.cfg)
        return actions

    def countdown(self, obs: Any) -> dict[tuple[int, int], float]:
        return {(c.pid, c.gpu): c.seconds for c in reaper.countdowns(self.state, obs, self.cfg)}


class StepTest(unittest.TestCase):
    def test_idle_accrues_to_threshold_then_terminates(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        self.assertEqual(r.poll(obs), [])
        self.assertEqual(r.countdown(obs), {(4242, 0): 30.0})
        self.assertEqual(r.poll(obs), [])
        self.assertEqual(r.poll(obs), [])
        self.assertEqual(r.countdown(obs), {(4242, 0): 10.0})
        self.assertEqual(r.poll(obs), [reaper.Terminate(KEY, obs[KEY], 30.0)])
        self.assertEqual(r.countdown(obs), {(4242, 0): 0.0})

    def test_new_sample_resets_the_clock(self) -> None:
        r = Reaper(LIVE)
        r.poll({KEY: proc()})
        r.poll({KEY: proc()})
        r.poll({KEY: proc()})
        active = {KEY: proc(sample=1_000)}
        self.assertEqual(r.poll(active), [])
        self.assertEqual(r.countdown(active), {(4242, 0): 30.0})
        idle = {KEY: proc()}
        self.assertEqual(r.poll(idle), [])
        self.assertEqual(r.poll(idle), [])
        self.assertEqual(r.poll(idle), [reaper.Terminate(KEY, idle[KEY], 30.0)])

    def test_sample_seen_again_does_not_reset(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc(sample=1_000)}
        r.poll(obs)
        r.poll(obs)
        r.poll(obs)
        self.assertEqual(r.poll(obs), [reaper.Terminate(KEY, obs[KEY], 30.0)])

    def test_sample_with_earlier_timestamp_counts_as_activity(self) -> None:
        r = Reaper(LIVE)
        r.poll({KEY: proc(sample=5_000)})
        r.poll({KEY: proc()})
        r.poll({KEY: proc()})
        stepped_back = {KEY: proc(sample=4_000)}
        self.assertEqual(r.poll(stepped_back), [])
        self.assertEqual(r.countdown(stepped_back), {(4242, 0): 30.0})

    def test_dry_run_warns_once_and_never_terminates(self) -> None:
        r = Reaper(DRY)
        obs = {KEY: proc()}
        actions = [r.poll(obs) for _ in range(20)]
        self.assertEqual(actions[3], [reaper.WouldKill(KEY, obs[KEY], 30.0)])
        self.assertEqual([a for poll in actions for a in poll], [reaper.WouldKill(KEY, obs[KEY], 30.0)])
        self.assertEqual(r.countdown(obs), {(4242, 0): 0.0})

    def test_active_again_after_would_kill(self) -> None:
        r = Reaper(DRY)
        idle = {KEY: proc()}
        for _ in range(4):
            r.poll(idle)
        active = {KEY: proc(sample=1_000)}
        self.assertEqual(r.poll(active), [reaper.ActiveAgain(KEY, active[KEY])])
        self.assertEqual(r.countdown(active), {(4242, 0): 30.0})
        r.poll(idle)
        r.poll(idle)
        self.assertEqual(r.poll(idle), [reaper.WouldKill(KEY, idle[KEY], 30.0)])

    def test_kill_follows_grace_and_ignores_activity_meanwhile(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        for _ in range(4):
            r.poll(obs)
        shutting_down = [{KEY: proc(sample=1_000 + i)} for i in range(6)]
        self.assertEqual([r.poll(o, dt=1.0) for o in shutting_down[:4]], [[], [], [], []])
        self.assertEqual(r.poll(shutting_down[4], dt=1.0), [reaper.Kill(KEY, shutting_down[4][KEY], 30.0)])
        self.assertEqual(r.poll(shutting_down[5], dt=1.0), [])
        self.assertEqual([r.poll(obs) for _ in range(10)], [[]] * 10)

    def test_reused_pid_is_a_new_process(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        for _ in range(4):
            r.poll(obs)
        reused = reaper.ProcKey(pid=KEY.pid, start=KEY.start + 500)
        successor = {reused: proc()}
        self.assertEqual(r.poll(successor, dt=10.0), [reaper.Gone(KEY)])
        self.assertEqual(r.countdown(successor), {(4242, 0): 30.0})
        self.assertEqual(r.poll(successor, dt=10.0), [])

    def test_activity_on_one_of_two_gpus_keeps_the_process(self) -> None:
        r = Reaper(LIVE)
        two_gpus = {0: 1024, 1: 2048}
        for i in range(10):
            obs = {KEY: proc(sample=1_000 + i, gpus=two_gpus)}
            self.assertEqual(r.poll(obs), [])
        self.assertEqual(r.countdown(obs), {(4242, 0): 30.0, (4242, 1): 30.0})

    def test_failed_poll_changes_nothing(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        r.poll(obs)
        r.poll(obs)
        r.poll(obs)
        before = r.state
        self.assertEqual(r.poll(None), [])
        self.assertEqual(r.state, before)
        self.assertEqual(r.countdown(obs), {(4242, 0): 10.0})
        self.assertEqual(r.poll(obs), [reaper.Terminate(KEY, obs[KEY], 30.0)])
        self.assertEqual(r.poll(None, dt=60.0), [])
        self.assertEqual(r.poll(obs, dt=5.0), [reaper.Kill(KEY, obs[KEY], 30.0)])

    def test_dt_is_capped(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        r.poll(obs, dt=0.0)
        self.assertEqual(r.poll(obs, dt=3600.0), [])
        self.assertEqual(r.countdown(obs), {(4242, 0): 20.0})

    def test_gone_only_for_signalled_processes(self) -> None:
        r = Reaper(LIVE)
        other = reaper.ProcKey(pid=7, start=50)
        for i in range(4):
            r.poll({KEY: proc(), other: proc(sample=1 + i)})
        self.assertEqual(r.poll({}), [reaper.Gone(KEY)])
        self.assertEqual(r.state, {})


class StateFileTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.json"

    def test_round_trip_keeps_idle_time_and_last_sample(self) -> None:
        before = Reaper(LIVE)
        seen = {KEY: proc(sample=1_000)}
        before.poll(seen)
        before.poll(seen)
        before.poll(seen)
        reaper.save_state(self.path, before.state, BOOT, saved_at=WALL)

        after = Reaper(LIVE, reaper.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 30))
        self.assertEqual(after.poll(seen, dt=0.0), [])
        self.assertEqual(after.countdown(seen), {(4242, 0): 10.0})
        self.assertEqual(after.poll(seen), [reaper.Terminate(KEY, seen[KEY], 30.0)])

    def test_interrupted_kill_repeats_after_restart(self) -> None:
        before = Reaper(LIVE)
        obs = {KEY: proc()}
        for _ in range(4):
            before.poll(obs)
        reaper.save_state(self.path, before.state, BOOT, saved_at=WALL)

        after = Reaper(LIVE, reaper.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 30))
        self.assertEqual(after.poll(obs, dt=0.0), [reaper.Terminate(KEY, obs[KEY], 30.0)])

    def test_state_from_another_boot_is_dropped(self) -> None:
        r = Reaper(LIVE)
        r.poll({KEY: proc()})
        reaper.save_state(self.path, r.state, BOOT, saved_at=WALL)
        with self.assertLogs("gpu-idle-reaper", "WARNING"):
            self.assertEqual(reaper.load_state(self.path, "another-boot", now=5.0, wall_now=WALL + 30), {})

    def test_unusable_files_give_empty_state(self) -> None:
        def saved(version: int, procs: dict[str, Any]) -> str:
            return json.dumps({"version": version, "boot_id": BOOT, "saved_at": WALL, "procs": procs})

        contents = [
            "{not json",
            "[]",
            saved(2, {}),
            saved(1, {"4242": {"idle_seconds": 1, "last_sample_us": 0}}),
            saved(1, {"4242:1": {"idle_seconds": float("nan"), "last_sample_us": 0}}),
            saved(1, {"4242:1": {"idle_seconds": 1, "last_sample_us": "0"}}),
        ]
        for content in contents:
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertLogs("gpu-idle-reaper", "WARNING"):
                    self.assertEqual(reaper.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 30), {})

    def test_state_saved_before_a_long_stop_is_dropped(self) -> None:
        r = Reaper(LIVE)
        r.poll({KEY: proc()})
        reaper.save_state(self.path, r.state, BOOT, saved_at=WALL)
        restored = reaper.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 590)
        self.assertEqual(set(restored), {KEY})
        with self.assertLogs("gpu-idle-reaper", "WARNING"):
            self.assertEqual(reaper.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 610), {})

    def test_missing_file_gives_empty_state(self) -> None:
        self.assertEqual(reaper.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 30), {})


class ThrottleTest(unittest.TestCase):
    def test_repeated_message_is_logged_once(self) -> None:
        throttle = reaper.Throttle(3600.0)
        with self.assertLogs("gpu-idle-reaper", "WARNING") as logs:
            throttle.warning("NVML poll failed: NVMLError_Unknown: Unknown Error")
            throttle.warning("NVML poll failed: NVMLError_Unknown: Unknown Error")
            throttle.warning("NVML poll failed: NVMLError_GpuIsLost: GPU is lost")
        self.assertEqual(len(logs.records), 2)


class ParseStartTest(unittest.TestCase):
    def test_comm_with_spaces_and_parentheses(self) -> None:
        fields = [str(n) for n in range(3, 53)]
        fields[0] = "S"
        fields[22 - 3] = "987654"
        stat = b"4242 (a) b (c)) " + " ".join(fields).encode() + b"\n"
        self.assertEqual(reaper.parse_start(stat), 987654)


class FakeNvml:
    """NVML as measured: utilization samples only for recently active pids,
    NotFound when a GPU has none, and only samples newer than lastSeen."""

    class NVMLError(Exception):
        pass

    class NVMLError_NotFound(NVMLError):
        pass

    def __init__(self, gpus: list[tuple[dict[int, int | None], dict[int, int]]]) -> None:
        self.gpus = gpus
        self.broken = False

    def nvmlInit(self) -> None:
        pass

    def nvmlDeviceGetCount(self) -> int:
        return len(self.gpus)

    def nvmlDeviceGetHandleByIndex(self, index: int) -> int:
        return index

    def nvmlDeviceGetComputeRunningProcesses(self, handle: int) -> list[SimpleNamespace]:
        if self.broken:
            raise self.NVMLError("Unknown Error")
        return [SimpleNamespace(pid=pid, usedGpuMemory=used) for pid, used in self.gpus[handle][0].items()]

    def nvmlDeviceGetProcessUtilization(self, handle: int, last_seen: int) -> list[SimpleNamespace]:
        samples = [
            SimpleNamespace(pid=pid, timeStamp=ts) for pid, ts in self.gpus[handle][1].items() if ts > last_seen
        ]
        if not samples:
            raise self.NVMLError_NotFound()
        return samples


@unittest.skipUnless(ON_LINUX, "reads /proc")
class NvmlReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.pid = child.pid
        self.key = reaper.ProcKey(self.pid, reaper.read_start(self.pid))

    def reader(self, nvml: FakeNvml) -> Any:
        with mock.patch.dict(sys.modules, {"pynvml": nvml}):
            return reaper.NvmlReader(reaper.Throttle(3600.0))

    def test_process_on_two_gpus(self) -> None:
        nvml = FakeNvml([({self.pid: 3 * MIB}, {self.pid: 5_000}), ({self.pid: None}, {self.pid: 9_000})])
        obs = self.reader(nvml).read()
        self.assertEqual(
            obs, {self.key: reaper.ProcObs(username(), {0: 3, 1: None}, 9_000, "sleep 60")}
        )

    def test_late_sample_for_another_pid_is_kept(self) -> None:
        me = reaper.ProcKey(os.getpid(), reaper.read_start(os.getpid()))
        nvml = FakeNvml([({me.pid: MIB}, {me.pid: 9_000}), ({self.pid: MIB}, {self.pid: 5_000})])
        obs = self.reader(nvml).read()
        self.assertEqual((obs[me].newest_sample_us, obs[self.key].newest_sample_us), (9_000, 5_000))

    def test_vanished_pid_is_skipped(self) -> None:
        missing = 4194304 + 1
        nvml = FakeNvml([({missing: MIB, self.pid: MIB}, {})])
        obs = self.reader(nvml).read()
        self.assertEqual(list(obs), [self.key])
        self.assertIsNone(obs[self.key].newest_sample_us)

    def test_nvml_error_yields_no_observation(self) -> None:
        nvml = FakeNvml([({self.pid: MIB}, {})])
        nvml.broken = True
        with self.assertLogs("gpu-idle-reaper", "WARNING"):
            self.assertIsNone(self.reader(nvml).read())

    def test_activity_on_the_second_gpu_keeps_the_process(self) -> None:
        nvml = FakeNvml([({self.pid: MIB}, {}), ({self.pid: MIB}, {})])
        reader = self.reader(nvml)
        r = Reaper(LIVE)
        for i in range(10):
            nvml.gpus[1][1][self.pid] = 1_000 + i
            self.assertEqual(r.poll(reader.read()), [])
        del nvml.gpus[1][1][self.pid]
        actions = [r.poll(reader.read()) for _ in range(3)]
        self.assertEqual([type(a) for poll in actions for a in poll], [reaper.Terminate])


def username() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


@unittest.skipUnless(ON_LINUX, "pidfd is Linux only")
class SendSignalTest(unittest.TestCase):
    def test_signals_only_the_observed_process(self) -> None:
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        start = reaper.read_start(child.pid)

        reused = reaper.ProcKey(child.pid, start + 1)
        self.assertEqual(reaper.send_signal(reused, signal.SIGTERM), "skipped, pid reused")
        self.assertIsNone(child.poll())

        observed = reaper.ProcKey(child.pid, start)
        self.assertEqual(reaper.send_signal(observed, signal.SIGTERM), "sent")
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(reaper.send_signal(observed, signal.SIGKILL), "already exited")


class ServiceUnitTest(unittest.TestCase):
    def render(self, dry_run: bool) -> str:
        env = Environment(
            loader=FileSystemLoader(PLAYBOOKS / "templates" / "gpu-idle-reaper"),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
        )
        return env.get_template("gpu-idle-reaper.service.j2").render(
            inventory_hostname="cs11369a",
            tailnet_ip="100.104.99.124",
            gpu_idle_reaper_port=8001,
            gpu_idle_reaper_idle_seconds=86400,
            gpu_idle_reaper_grace_seconds=60,
            gpu_idle_reaper_dry_run=dry_run,
        )

    def options(self, unit: str) -> Any:
        exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        argv = shlex.split(exec_start.removeprefix("ExecStart="))
        self.assertEqual(argv[:2], ["/opt/gpu-idle-reaper/bin/python", "/opt/gpu-idle-reaper/gpu_idle_reaper.py"])
        return reaper.parse_args(argv[2:])

    def test_dry_run_follows_the_variable(self) -> None:
        self.assertTrue(self.options(self.render(dry_run=True)).config.dry_run)
        self.assertFalse(self.options(self.render(dry_run=False)).config.dry_run)

    def test_exec_start_wiring(self) -> None:
        unit = self.render(dry_run=True)
        options = self.options(unit)
        self.assertEqual(options.hostname, "cs11369a")
        self.assertEqual((options.metrics_address, options.metrics_port), ("100.104.99.124", 8001))
        self.assertEqual(options.config.idle_seconds, 86400.0)
        self.assertEqual(options.config.grace_seconds, 60.0)
        self.assertIn("StateDirectory=gpu-idle-reaper", unit)
        self.assertEqual(options.state_file, Path("/var/lib/gpu-idle-reaper/state.json"))
        # Root, so it can signal every user's processes.
        self.assertNotIn("User=", unit)


if __name__ == "__main__":
    unittest.main()
