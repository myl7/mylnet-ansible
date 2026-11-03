# gpu-idle-kill

A root systemd service on the two cityu GPU servers (cs11369a and cs659b) and
a command of the same name for every user on those hosts.
The service tracks GPU processes that hold a CUDA context but have not
computed for 24 hours of observed time; the command lets any local user list
those processes and end them. The service never signals on its own.
`playbooks/gpu-idle-kill.yaml` deploys both, and its knobs are that playbook's
`vars`.
It ships in dry-run mode, where kill requests are checked and logged but no
signal is sent.

## The rule

Every second the service asks NVML for two things on each GPU.
The first is the list of processes with a CUDA context (`nvmlDeviceGetComputeRunningProcesses`).
The second is the per-process utilization samples the driver recorded recently (`nvmlDeviceGetProcessUtilization`).

Each process has an idle clock that starts at zero when the service first sees it.
A process is active in a poll when a sample for it appears on any GPU and that sample is not the one the service saw last.
Activity resets the clock to zero.
Otherwise the poll adds the time since the previous successful poll, capped at 10 seconds.

When the clock reaches `gpu_idle_kill_idle_seconds` (86400, i.e. 24 hours),
the process is eligible: any user on the host may end it through the command.
A granted request sends SIGTERM, and SIGKILL `gpu_idle_kill_grace_seconds` (60)
later if the process still holds its context.
Activity after SIGTERM does not cancel the kill, because a process may touch
the GPU while it shuts down.

Consequences of the rule:

- Holding GPU memory is not activity. A process that allocated memory and runs no kernels is idle.
- A process on several GPUs is active when it computes on any of them. It is ended as one process.
- A process is identified by its pid and its start time. A new process that reuses a pid starts with a fresh clock. Before each signal the service opens a pidfd and checks the start time again, so a signal never reaches a different process.
- Only observed time counts. A failed NVML poll adds nothing, and the time the service is stopped adds nothing.
- The clocks survive a restart through the state file when the service was down for at most 10 minutes, which covers deploys and crash restarts. After a longer stop, or a reboot, all clocks start from zero, because activity during the stop was not seen.
- Every user's processes are eligible alike, root and containers included. Only compute contexts are listed, so a display server is never a candidate.
- Every kill request is judged inside the service against the live clock, so eligibility cannot be forged or raced from the client side.

## Detection floor

NVML records a sample for a process only when the process used the GPU noticeably during the driver's sampling period.
Measured on drivers 590 and 595, bursts of 30 ms every 2 s and of 1 ms every 100 ms produce samples.
Bursts of 1 to 10 ms every 2 s never do.
The floor is therefore about 1% average utilization.

A process whose GPU work stays below the floor looks idle the whole time and is
eligible after 24 hours, even though it computes.
This hits only very light, steady loads, such as a service that runs a tiny
kernel every few seconds.
Bursts of 30 ms or more were always seen in the measurements, so ordinary
training or inference work resets the clock.
Compared with an automatic killer this is the main advantage of the manual
design: whoever requests the kill can still decide to leave such a process
alone.

## The command

`gpu-idle-kill list` prints every process that holds a GPU: pid, owner, GPUs
with memory, idle time, whether the process is eligible, and the command line.

```
$ gpu-idle-kill list
PID     USER   GPU           STATE     IDLE   COMMAND
84601   alice  0:20480MiB    idle      4h12m  python train.py
91372   bob    1:40476MiB    ELIGIBLE  1d6h   VLLM::EngineCore
run gpu-idle-kill kill <pid> to end an ELIGIBLE process
```

`gpu-idle-kill kill <pid>` asks the service to end one process.
The service, not the client, decides.
A request is granted only when the process is eligible, and every request,
granted or refused, names its requester in the log.
Both commands work for every user with an account on the host; no sudo, and
no password.
Repeated requests are idempotent, and each user is limited to 6 kill requests
per minute.

Refusals say why:

| response | meaning |
|---|---|
| idle for X of Y, Z to go | not idle long enough yet |
| no process with this pid holds a GPU context | wrong pid, or it left the GPU |
| the process just exited | it died between the list and the request |
| the pid was reused by a new process | the pid now belongs to a different process |
| NVML is unavailable | the driver poll is failing; try again |
| rate limited | more than 6 requests a minute; wait |

The client exits 0 for a granted request, 1 for a refusal, and 2 when it
cannot reach the service, such as after `systemctl stop gpu-idle-kill`.

## Dry-run and arming

The playbook ships with `gpu_idle_kill_dry_run: true`.
In dry-run a granted request logs `kill granted ... dry_run=True` and answers
`OK dry-run: the process is eligible, no signal was sent`, but nothing is
signalled.

To arm the service, set `gpu_idle_kill_dry_run: false` in the playbook's
`vars` and run `ansible-playbook playbooks/gpu-idle-kill.yaml`.
Change the value in the playbook rather than with `-e gpu_idle_kill_dry_run=false`, because that passes the string "false", which is truthy in the template and keeps dry-run on.
The restart keeps the clocks, so every process already eligible is killable at once.
Review `gpu-idle-kill list` and the log's `eligible` lines first.
The startup line in the log shows `dry_run=True` or `dry_run=False`.

## Logs and state

`/var/log/gpu-idle-kill.log` holds the startup line with the full config, one
line per event, and errors.
Each distinct NVML or state-save error is logged at most once an hour.
The log rotates weekly and keeps 52 weeks (`/etc/logrotate.d/gpu-idle-kill`).

`/var/lib/gpu-idle-kill/state.json` holds each process's idle seconds and last sample timestamp.
It is written every 60 s and on stop.
On start, a file from an earlier boot, one saved more than 10 minutes ago, or one that fails to parse is ignored with a log line, and the clocks start from zero.

## Reading the log

```
2026-11-03T10:15:02+0800 INFO starting hostname=cs659b metrics=100.67.212.32:8001 socket=/run/gpu-idle-kill/kill.sock state_file=/var/lib/gpu-idle-kill/state.json idle_seconds=86400 grace_seconds=60 interval=1 dry_run=True gpus=2
2026-11-03T11:20:07+0800 INFO eligible user=bob pid=91372 gpus=1:40476MiB idle=24.0h cmd='VLLM::EngineCore'
2026-11-03T12:01:44+0800 INFO kill granted by=carol user=bob pid=91372 gpus=1:40476MiB idle=25.1h dry_run=True cmd='VLLM::EngineCore'
2026-11-04T09:03:41+0800 INFO kill refused by=carol pid=84601 reason=idle for 4.2h of 24h, 19.8h to go
2026-11-04T09:04:00+0800 INFO active again, no longer eligible user=bob pid=91372 gpus=1:40476MiB cmd='VLLM::EngineCore'
```

`by=` is the user who ran the command, taken from the socket connection, not
from anything the client sends.
Once armed, a granted request continues:

```
... INFO kill granted by=carol user=bob pid=91372 gpus=1:40476MiB idle=25.1h cmd='VLLM::EngineCore'
... INFO SIGTERM sent: user=bob pid=91372 gpus=1:40476MiB idle=25.1h cmd='VLLM::EngineCore'
... INFO gone pid=91372, its GPU memory is free
```

SIGKILL appears only when the process still held its context 60 s after SIGTERM.
Instead of `sent`, a signal line can say `already exited`, `skipped, pid
reused`, or `failed: ...`.
`gone` confirms that a signalled process no longer holds a CUDA context.

## Countdown column

The 逐用户占用 table on https://cityu-gpu.myl.moe has a `可结束倒计时` column.
It shows how much longer the process must stay idle before any user may end
it with the command.
It returns to 24 hours whenever the process computes.
0 means the process is eligible now.
An empty cell means the service does not report that process, e.g. the service
is down or saw the process for the first time less than a second ago.

The column comes from the gauge `gpu_idle_kill_in_seconds`, with labels
`hostname`, `index`, `pid`, and `username`, one series per process and GPU.
The service serves it on `tailnet_ip:8001/metrics`, and the panel's Prometheus
scrapes it as job `gpu-idle-kill`.

## Who can stop it

The service runs as root, so users without sudo cannot signal or stop it.
Sudoers can stop it with `sudo systemctl stop gpu-idle-kill`.
That command goes through sudo and lands in the sudo history (see
`docs/sudo-history.md`).
A stop longer than 10 minutes resets the clocks.
`Restart=always` brings the service back after a crash or a kill, but not
after `systemctl stop`.
While it is stopped the command answers with exit 2 and the countdown column
goes empty.

## Prerequisites

The tailnet ACL must let sg1 reach both cityu hosts on port 8001.
As of 2026-10-26 it does: a rule that lets a large group of nodes, sg1
included, reach every port on each cityu host already covers it.
If that rule is narrowed later, `playbooks/cityu-gpu.yaml` fails at "Check the
gpu-idle-kill scrape targets", and the countdown column stays empty.

Run `playbooks/gpu-idle-kill.yaml` before `playbooks/cityu-gpu.yaml`, because
the latter checks that Prometheus scrapes the service.

## Operations

- Deploy or change a knob with `ansible-playbook playbooks/gpu-idle-kill.yaml`. A new threshold applies to the saved clocks at once.
- The verify task prints the installed `nvidia-ml-py` and `prometheus-client` versions. pip runs with `state: present`, so upgrade by hand with `sudo /opt/gpu-idle-kill/bin/pip install -U nvidia-ml-py prometheus-client && sudo systemctl restart gpu-idle-kill`.

## Manual cleanup scope

By repository convention the playbook only provisions.
To retire gpu-idle-kill, delete `playbooks/gpu-idle-kill.yaml`,
`playbooks/files/gpu-idle-kill/`, `playbooks/templates/gpu-idle-kill/`,
`tests/test_gpu_idle_kill.py`, this document, the `gpu-idle-kill` scrape job
and its assert in `playbooks/cityu-gpu.yaml`, and query B of the 逐用户占用
table.
Then clean up both cityu hosts:

```sh
sudo systemctl disable --now gpu-idle-kill
sudo rm -rf /etc/systemd/system/gpu-idle-kill.service /etc/systemd/system/gpu-idle-kill.service.d /opt/gpu-idle-kill /var/lib/gpu-idle-kill /etc/logrotate.d/gpu-idle-kill /usr/local/bin/gpu-idle-kill /run/gpu-idle-kill
sudo systemctl daemon-reload
```
