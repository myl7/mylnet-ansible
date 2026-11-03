"""Drive the gpu-idle-kill service, state file, client, and adapters through their interfaces.

The programs live under playbooks/files, so they are loaded by path. NVML is
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


def load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gik = load("gpu_idle_kill", PLAYBOOKS / "files" / "gpu-idle-kill" / "gpu_idle_kill.py")
cli = load("gpu_idle_kill_cli", PLAYBOOKS / "files" / "gpu-idle-kill" / "gpu_idle_kill_cli.py")

LIVE = gik.Config(idle_seconds=30.0, grace_seconds=5.0, dry_run=False)
DRY = gik.Config(idle_seconds=30.0, grace_seconds=5.0, dry_run=True)
KEY = gik.ProcKey(pid=4242, start=1000)
BOOT = "6f1d6b0e-0d7c-4b55-9a57-0d5e3c1e9f00"
WALL = 1_800_000_000.0
ON_LINUX = sys.platform == "linux"
MIB = 1024 * 1024


def proc(sample: int | None = None, gpus: Mapping[int, int | None] | None = None) -> Any:
    return gik.ProcObs(
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
        self.state, actions = gik.step(self.state, obs, dt, self.now, self.cfg)
        return actions

    def countdown(self, obs: Any) -> dict[tuple[int, int], float]:
        return {(c.pid, c.gpu): c.seconds for c in gik.countdowns(self.state, obs, self.cfg)}


class StepTest(unittest.TestCase):
    def test_idle_accrues_to_eligible_once_then_keeps_counting(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        self.assertEqual(r.poll(obs), [])
        self.assertEqual(r.countdown(obs), {(4242, 0): 30.0})
        self.assertEqual(r.poll(obs), [])
        self.assertEqual(r.poll(obs), [])
        self.assertEqual(r.countdown(obs), {(4242, 0): 10.0})
        self.assertEqual(r.poll(obs), [gik.Eligible(KEY, obs[KEY], 30.0)])
        self.assertEqual(r.countdown(obs), {(4242, 0): 0.0})
        self.assertEqual(r.poll(obs), [])
        self.assertEqual(r.poll(obs), [])

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
        self.assertEqual(r.poll(idle), [gik.Eligible(KEY, idle[KEY], 30.0)])

    def test_sample_seen_again_does_not_reset(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc(sample=1_000)}
        r.poll(obs)
        r.poll(obs)
        r.poll(obs)
        self.assertEqual(r.poll(obs), [gik.Eligible(KEY, obs[KEY], 30.0)])

    def test_sample_with_earlier_timestamp_counts_as_activity(self) -> None:
        r = Reaper(LIVE)
        r.poll({KEY: proc(sample=5_000)})
        r.poll({KEY: proc()})
        r.poll({KEY: proc()})
        stepped_back = {KEY: proc(sample=4_000)}
        self.assertEqual(r.poll(stepped_back), [])
        self.assertEqual(r.countdown(stepped_back), {(4242, 0): 30.0})

    def test_active_again_after_eligible(self) -> None:
        r = Reaper(DRY)
        idle = {KEY: proc()}
        for _ in range(4):
            r.poll(idle)
        active = {KEY: proc(sample=1_000)}
        self.assertEqual(r.poll(active), [gik.ActiveAgain(KEY, active[KEY])])
        self.assertEqual(r.countdown(active), {(4242, 0): 30.0})
        r.poll(idle)
        r.poll(idle)
        self.assertEqual(r.poll(idle), [gik.Eligible(KEY, idle[KEY], 30.0)])

    def test_granted_kill_follows_grace_and_ignores_activity_meanwhile(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        for _ in range(4):
            r.poll(obs)
        # The request server flips the phase, like a granted kill does.
        r.state[KEY] = gik.replace(r.state[KEY], phase=gik.Phase.TERM_SENT, phase_since=r.now)
        shutting_down = [{KEY: proc(sample=1_000 + i)} for i in range(6)]
        self.assertEqual([r.poll(o, dt=1.0) for o in shutting_down[:4]], [[], [], [], []])
        self.assertEqual(r.poll(shutting_down[4], dt=1.0), [gik.Kill(KEY, shutting_down[4][KEY], 30.0)])
        self.assertEqual(r.poll(shutting_down[5], dt=1.0), [])
        self.assertEqual([r.poll(obs) for _ in range(10)], [[]] * 10)

    def test_reused_pid_is_a_new_process(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        for _ in range(4):
            r.poll(obs)
        reused = gik.ProcKey(pid=KEY.pid, start=KEY.start + 500)
        successor = {reused: proc()}
        # An eligible process that vanishes is not reported; only signalled
        # ones earn a gone line.
        self.assertEqual(r.poll(successor, dt=10.0), [])
        self.assertEqual(r.countdown(successor), {(4242, 0): 30.0})
        self.assertEqual(r.poll(successor, dt=10.0), [])

    def test_gone_only_for_signalled_processes(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        r.poll(obs)
        r.state[KEY] = gik.replace(r.state[KEY], phase=gik.Phase.TERM_SENT, phase_since=r.now)
        self.assertEqual(r.poll({}), [gik.Gone(KEY)])
        self.assertEqual(r.state, {})

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
        self.assertEqual(r.poll(obs), [gik.Eligible(KEY, obs[KEY], 30.0)])
        self.assertEqual(r.poll(None, dt=60.0), [])

    def test_dt_is_capped(self) -> None:
        r = Reaper(LIVE)
        obs = {KEY: proc()}
        r.poll(obs, dt=0.0)
        self.assertEqual(r.poll(obs, dt=3600.0), [])
        self.assertEqual(r.countdown(obs), {(4242, 0): 20.0})


class DecideKillTest(unittest.TestCase):
    def state_with(self, tracked: Any) -> Any:
        return {KEY: tracked}

    def test_grants_when_idle_reached(self) -> None:
        obs = {KEY: proc()}
        state = self.state_with(gik.Tracked(30.0, 0, gik.Phase.ELIGIBLE, 0.0))
        self.assertEqual(gik.decide_kill(state, obs, LIVE, KEY.pid, KEY.start), gik.Grant(KEY, obs[KEY], 30.0))

    def test_refuses_below_threshold(self) -> None:
        obs = {KEY: proc()}
        state = self.state_with(gik.Tracked(3.0, 0, gik.Phase.WATCHING, 0.0))
        decision = gik.decide_kill(state, obs, LIVE, KEY.pid, KEY.start)
        self.assertIsInstance(decision, gik.Refusal)
        assert isinstance(decision, gik.Refusal)
        self.assertIn("idle for", decision.reason)

    def test_refuses_unknown_pid(self) -> None:
        decision = gik.decide_kill({}, {KEY: proc()}, LIVE, 999, None)
        self.assertIsInstance(decision, gik.Refusal)

    def test_refuses_exited_pid(self) -> None:
        state = self.state_with(gik.Tracked(30.0, 0, gik.Phase.ELIGIBLE, 0.0))
        decision = gik.decide_kill(state, {KEY: proc()}, LIVE, KEY.pid, None)
        self.assertIsInstance(decision, gik.Refusal)

    def test_refuses_reused_pid(self) -> None:
        state = self.state_with(gik.Tracked(30.0, 0, gik.Phase.ELIGIBLE, 0.0))
        decision = gik.decide_kill(state, {KEY: proc()}, LIVE, KEY.pid, KEY.start + 1)
        self.assertIsInstance(decision, gik.Refusal)
        assert isinstance(decision, gik.Refusal)
        self.assertIn("reused", decision.reason)

    def test_refuses_without_idle_history(self) -> None:
        decision = gik.decide_kill({}, {KEY: proc()}, LIVE, KEY.pid, KEY.start)
        self.assertIsInstance(decision, gik.Refusal)
        assert isinstance(decision, gik.Refusal)
        self.assertIn("just seen", decision.reason)

    def test_already_ending_is_idempotent(self) -> None:
        state = self.state_with(gik.Tracked(30.0, 0, gik.Phase.TERM_SENT, 0.0))
        self.assertEqual(gik.decide_kill(state, {KEY: proc()}, LIVE, KEY.pid, KEY.start), gik.AlreadyEnding(KEY.pid))

    def test_refuses_when_nvml_is_down(self) -> None:
        state = self.state_with(gik.Tracked(30.0, 0, gik.Phase.ELIGIBLE, 0.0))
        self.assertIsInstance(gik.decide_kill(state, None, LIVE, KEY.pid, KEY.start), gik.Refusal)


class RateLimiterTest(unittest.TestCase):
    def test_burst_then_refused_until_the_window_slides(self) -> None:
        rate = gik.RateLimiter(burst=3, period=60.0)
        self.assertTrue(all(rate.allow(7, now=100.0) for _ in range(3)))
        self.assertFalse(rate.allow(7, now=101.0))
        self.assertTrue(rate.allow(7, now=161.0))

    def test_refusal_logging_folds_to_once_an_hour(self) -> None:
        rate = gik.RateLimiter(burst=1, period=60.0)
        rate.allow(7, now=100.0)
        self.assertFalse(rate.allow(7, now=101.0))
        self.assertTrue(rate.should_log_refusal(7, now=101.0))
        self.assertFalse(rate.should_log_refusal(7, now=101.0 + 3599.0))
        self.assertTrue(rate.should_log_refusal(7, now=101.0 + 3601.0))


class ListRowsTest(unittest.TestCase):
    def test_rows_are_sorted_and_statused(self) -> None:
        busy = gik.ProcKey(pid=100, start=5)
        ready = gik.ProcKey(pid=50, start=6)
        ending = gik.ProcKey(pid=75, start=7)
        obs = {
            busy: gik.ProcObs("bob", {0: 1024}, 1_000, "a.py"),
            ready: gik.ProcObs("carol", {1: None}, None, "b.py"),
            ending: gik.ProcObs("dan", {2: 2048}, None, "c.py"),
        }
        state = {
            busy: gik.Tracked(1.0, 1_000, gik.Phase.WATCHING, 0.0),
            ready: gik.Tracked(30.0, 0, gik.Phase.ELIGIBLE, 0.0),
            ending: gik.Tracked(30.0, 0, gik.Phase.TERM_SENT, 0.0),
        }
        rows = gik.list_rows(state, obs, LIVE)
        self.assertEqual(
            rows,
            (
                gik.Row(50, "carol", "1:?MiB", "eligible", 30.0, "b.py"),
                gik.Row(75, "dan", "2:2048MiB", "ending", 30.0, "c.py"),
                gik.Row(100, "bob", "0:1024MiB", "idle", 1.0, "a.py"),
            ),
        )

    def test_no_observation_gives_no_rows(self) -> None:
        self.assertEqual(gik.list_rows({}, None, LIVE), ())


class KillServerTest(unittest.TestCase):
    def server(self, cfg: Any, state: Any, obs: Any) -> Any:
        shared = gik.Shared(state)
        shared.obs = obs
        return gik.KillServer(Path("/nonexistent"), shared, cfg, gik.Throttle(3600.0))

    def eligible_state(self) -> Any:
        return {KEY: gik.Tracked(30.0, 0, gik.Phase.ELIGIBLE, 0.0)}

    def test_list_renders_rows_and_config(self) -> None:
        obs = {KEY: proc()}
        response = self.server(LIVE, self.eligible_state(), obs)._respond(1000, "LIST")
        self.assertTrue(response.startswith("OK 1 threshold=30 dry_run=0\n"))
        self.assertIn("4242 alice 0:20480MiB eligible 30 python train.py", response)

    def test_list_reports_unavailable_nvml(self) -> None:
        response = self.server(LIVE, self.eligible_state(), None)._respond(1000, "LIST")
        self.assertTrue(response.startswith("NO "))

    def test_unknown_request(self) -> None:
        response = self.server(LIVE, self.eligible_state(), {KEY: proc()})._respond(1000, "DANCE")
        self.assertTrue(response.startswith("NO "))

    def test_non_numeric_pid(self) -> None:
        response = self.server(LIVE, self.eligible_state(), {KEY: proc()})._respond(1000, "KILL abc")
        self.assertTrue(response.startswith("NO "))

    def test_kill_refusal_is_logged_with_the_requester(self) -> None:
        obs = {KEY: proc()}
        state = {KEY: gik.Tracked(3.0, 0, gik.Phase.WATCHING, 0.0)}
        with self.assertLogs("gpu-idle-kill", "INFO") as logs:
            response = self.server(LIVE, state, obs)._respond(1000, "KILL 4242")
        self.assertTrue(response.startswith("NO "))
        self.assertIn("kill refused by=", logs.output[0])

    def test_kill_rate_limited(self) -> None:
        server = self.server(LIVE, self.eligible_state(), {KEY: proc()})
        responses = [server._respond(1000, "KILL 4242") for _ in range(gik.RATE_BURST + 1)]
        self.assertTrue(responses[-1].startswith("NO rate limited"))
        self.assertTrue(all(not r.startswith("NO rate limited") for r in responses[:-1]))

    def test_granted_kill_in_dry_run_logs_without_signalling(self) -> None:
        obs = {KEY: proc()}
        state = self.eligible_state()
        server = self.server(DRY, state, obs)
        with mock.patch.object(gik, "read_start", return_value=KEY.start), self.assertLogs("gpu-idle-kill", "INFO"):
            response = server._respond(1000, "KILL 4242")
        self.assertTrue(response.startswith("OK dry-run"))
        self.assertEqual(state[KEY].phase, gik.Phase.ELIGIBLE)

    def test_granted_kill_signals_and_flips_to_term_sent(self) -> None:
        obs = {KEY: proc()}
        state = self.eligible_state()
        server = self.server(LIVE, state, obs)
        with (
            mock.patch.object(gik, "read_start", return_value=KEY.start),
            mock.patch.object(gik, "send_signal", return_value="sent") as sent,
            self.assertLogs("gpu-idle-kill", "INFO") as logs,
        ):
            response = server._respond(1000, "KILL 4242")
        self.assertTrue(response.startswith("OK SIGTERM sent"))
        sent.assert_called_once_with(KEY, signal.SIGTERM)
        self.assertEqual(state[KEY].phase, gik.Phase.TERM_SENT)
        self.assertIn("kill granted by=", logs.output[0])
        self.assertTrue(any("SIGTERM sent" in line for line in logs.output))

    def test_kill_again_while_ending_is_idempotent(self) -> None:
        obs = {KEY: proc()}
        state = {KEY: gik.Tracked(30.0, 0, gik.Phase.TERM_SENT, 0.0)}
        with mock.patch.object(gik, "read_start", return_value=KEY.start):
            response = self.server(LIVE, state, obs)._respond(1000, "KILL 4242")
        self.assertTrue(response.startswith("OK the process is already being ended"))


class ClientTest(unittest.TestCase):
    def test_human_idle(self) -> None:
        self.assertEqual(cli.human_idle(45), "45s")
        self.assertEqual(cli.human_idle(90), "1m30s")
        self.assertEqual(cli.human_idle(3660), "1h01m")
        self.assertEqual(cli.human_idle(90000), "1d1h")

    def test_render_list_marks_eligible_and_hints_the_command(self) -> None:
        response = (
            "OK 2 threshold=86400 dry_run=1\n"
            "99 bob 1:?MiB idle 5 sleep\n"
            "4242 alice 0:20480MiB,2:1024MiB eligible 90000 python train.py --resume"
        )
        text = cli.render_list(response)
        self.assertIn("PID", text)
        self.assertIn("4242", text)
        self.assertIn("ELIGIBLE", text)
        self.assertIn("1d1h", text)
        self.assertIn("dry-run", text)
        self.assertIn("gpu-idle-kill kill <pid>", text)

    def test_render_list_without_dry_run_has_no_warning(self) -> None:
        response = "OK 1 threshold=86400 dry_run=0\n99 bob 1:?MiB idle 5 sleep"
        self.assertNotIn("dry-run", cli.render_list(response))

    def test_render_list_empty(self) -> None:
        self.assertEqual(cli.render_list("OK 0 threshold=86400 dry_run=0"), "no process holds a GPU context right now")


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
        gik.save_state(self.path, before.state, BOOT, saved_at=WALL)

        after = Reaper(LIVE, gik.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 30))
        self.assertEqual(after.poll(seen, dt=0.0), [])
        self.assertEqual(after.countdown(seen), {(4242, 0): 10.0})
        self.assertEqual(after.poll(seen), [gik.Eligible(KEY, seen[KEY], 30.0)])

    def test_gpu_idle_reaper_state_file_restores(self) -> None:
        # The retired gpu-idle-reaper wrote the same format; its saved clocks
        # carried over on the migration deploy.
        legacy = json.dumps(
            {
                "version": 1,
                "boot_id": BOOT,
                "saved_at": WALL,
                "procs": {"4242:1000": {"idle_seconds": 90000, "last_sample_us": 5}},
            }
        )
        self.path.write_text(legacy)
        after = Reaper(LIVE, gik.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 30))
        self.assertEqual(after.poll({KEY: proc()}, dt=0.0), [gik.Eligible(KEY, proc(), 90000.0)])

    def test_state_from_another_boot_is_dropped(self) -> None:
        r = Reaper(LIVE)
        r.poll({KEY: proc()})
        gik.save_state(self.path, r.state, BOOT, saved_at=WALL)
        with self.assertLogs("gpu-idle-kill", "WARNING"):
            self.assertEqual(gik.load_state(self.path, "another-boot", now=5.0, wall_now=WALL + 30), {})

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
                with self.assertLogs("gpu-idle-kill", "WARNING"):
                    self.assertEqual(gik.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 30), {})

    def test_state_saved_before_a_long_stop_is_dropped(self) -> None:
        r = Reaper(LIVE)
        r.poll({KEY: proc()})
        gik.save_state(self.path, r.state, BOOT, saved_at=WALL)
        restored = gik.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 590)
        self.assertEqual(set(restored), {KEY})
        with self.assertLogs("gpu-idle-kill", "WARNING"):
            self.assertEqual(gik.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 610), {})

    def test_missing_file_gives_empty_state(self) -> None:
        self.assertEqual(gik.load_state(self.path, BOOT, now=5.0, wall_now=WALL + 30), {})


class ThrottleTest(unittest.TestCase):
    def test_repeated_message_is_logged_once(self) -> None:
        throttle = gik.Throttle(3600.0)
        with self.assertLogs("gpu-idle-kill", "WARNING") as logs:
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
        self.assertEqual(gik.parse_start(stat), 987654)


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
        self.key = gik.ProcKey(self.pid, gik.read_start(self.pid))

    def reader(self, nvml: FakeNvml) -> Any:
        with mock.patch.dict(sys.modules, {"pynvml": nvml}):
            return gik.NvmlReader(gik.Throttle(3600.0))

    def test_process_on_two_gpus(self) -> None:
        nvml = FakeNvml([({self.pid: 3 * MIB}, {self.pid: 5_000}), ({self.pid: None}, {self.pid: 9_000})])
        obs = self.reader(nvml).read()
        self.assertEqual(
            obs, {self.key: gik.ProcObs(username(), {0: 3, 1: None}, 9_000, "sleep 60")}
        )

    def test_late_sample_for_another_pid_is_kept(self) -> None:
        me = gik.ProcKey(os.getpid(), gik.read_start(os.getpid()))
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
        with self.assertLogs("gpu-idle-kill", "WARNING"):
            self.assertIsNone(self.reader(nvml).read())

    def test_steady_activity_then_silence_reaches_eligible(self) -> None:
        nvml = FakeNvml([({self.pid: MIB}, {}), ({self.pid: MIB}, {})])
        reader = self.reader(nvml)
        r = Reaper(LIVE)
        for i in range(10):
            nvml.gpus[1][1][self.pid] = 1_000 + i
            self.assertEqual(r.poll(reader.read()), [])
        del nvml.gpus[1][1][self.pid]
        actions = [r.poll(reader.read()) for _ in range(3)]
        self.assertEqual([type(a) for poll in actions for a in poll], [gik.Eligible])


def username() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


@unittest.skipUnless(ON_LINUX, "pidfd is Linux only")
class SendSignalTest(unittest.TestCase):
    def test_signals_only_the_observed_process(self) -> None:
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        start = gik.read_start(child.pid)

        reused = gik.ProcKey(child.pid, start + 1)
        self.assertEqual(gik.send_signal(reused, signal.SIGTERM), "skipped, pid reused")
        self.assertIsNone(child.poll())

        observed = gik.ProcKey(child.pid, start)
        self.assertEqual(gik.send_signal(observed, signal.SIGTERM), "sent")
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(gik.send_signal(observed, signal.SIGKILL), "already exited")


class ServiceUnitTest(unittest.TestCase):
    def render(self, dry_run: bool) -> str:
        env = Environment(
            loader=FileSystemLoader(PLAYBOOKS / "templates" / "gpu-idle-kill"),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
        )
        return env.get_template("gpu-idle-kill.service.j2").render(
            inventory_hostname="cs11369a",
            tailnet_ip="100.104.99.124",
            gpu_idle_kill_port=8001,
            gpu_idle_kill_idle_seconds=86400,
            gpu_idle_kill_grace_seconds=60,
            gpu_idle_kill_dry_run=dry_run,
        )

    def options(self, unit: str) -> Any:
        exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        argv = shlex.split(exec_start.removeprefix("ExecStart="))
        self.assertEqual(argv[:2], ["/opt/gpu-idle-kill/bin/python", "/opt/gpu-idle-kill/gpu_idle_kill.py"])
        return gik.parse_args(argv[2:])

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
        self.assertEqual(options.socket_path, Path("/run/gpu-idle-kill/kill.sock"))
        self.assertEqual(options.state_file, Path("/var/lib/gpu-idle-kill/state.json"))
        self.assertIn("StateDirectory=gpu-idle-kill", unit)
        self.assertIn("RuntimeDirectory=gpu-idle-kill", unit)
        # Root, so it can signal every user's processes.
        self.assertNotIn("User=", unit)


if __name__ == "__main__":
    unittest.main()
