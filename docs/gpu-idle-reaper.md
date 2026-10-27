# gpu-idle-reaper

A root systemd service on the two cityu GPU servers.
It ends GPU processes that hold a CUDA context but have not computed for 24 hours of observed time.
`playbooks/gpu-idle-reaper.yaml` deploys it, and its knobs are that playbook's `vars`.
It ships in dry-run mode, where it logs what it would kill and never sends a signal.

## The rule

Every second the reaper asks NVML for two things on each GPU.
The first is the list of processes with a CUDA context (`nvmlDeviceGetComputeRunningProcesses`).
The second is the per-process utilization samples the driver recorded recently (`nvmlDeviceGetProcessUtilization`).

Each process has an idle clock that starts at zero when the reaper first sees it.
A process is active in a poll when a sample for it appears on any GPU and that sample is not the one the reaper saw last.
Activity resets the clock to zero.
Otherwise the poll adds the time since the previous successful poll, capped at 10 seconds.

When the clock reaches `gpu_idle_reaper_idle_seconds` (86400, i.e., 24 hours), the reaper sends SIGTERM.
If the process still holds its context `gpu_idle_reaper_grace_seconds` (60) later, the reaper sends SIGKILL.
Activity after SIGTERM does not cancel the kill, because a process may touch the GPU while it shuts down.

Consequences of the rule:

- Holding GPU memory is not activity. A process that allocated memory and runs no kernels is idle.
- A process on several GPUs is active when it computes on any of them. It is ended as one process.
- A process is identified by its pid and its start time. A new process that reuses a pid starts with a fresh clock. Before each signal the reaper opens a pidfd and checks the start time again, so a signal never reaches a different process.
- Only observed time counts. A failed NVML poll adds nothing, and the time the service is stopped adds nothing.
- The clocks survive a restart through the state file when the service was down for at most 10 minutes, which covers deploys and crash restarts. After a longer stop, or a reboot, all clocks start from zero, because activity during the stop was not seen.
- Every user's processes are covered, root and containers included. Only compute contexts are listed, so a display server is never a candidate.

## Detection floor

NVML records a sample for a process only when the process used the GPU noticeably during the driver's sampling period.
Measured on drivers 590 and 595, bursts of 30 ms every 2 s and of 1 ms every 100 ms produce samples.
Bursts of 1 to 10 ms every 2 s never do.
The floor is therefore about 1% average utilization.

A process whose GPU work stays below the floor looks idle the whole time and is ended after 24 hours, even though it computes.
This hits only very light, steady loads, such as a service that runs a tiny kernel every few seconds.
Bursts of 30 ms or more were always seen in the measurements, so ordinary training or inference work resets the clock.
Someone who must keep such a light process alive has to release the GPU between uses or raise its load above the floor.

## Dry-run and arming

The playbook ships with `gpu_idle_reaper_dry_run: true`.
In dry-run, a process whose clock reaches the threshold gets one `would kill` line in the log and stays on the GPU.
If it computes again later, the log gets an `active again` line and its clock restarts.

To arm the reaper, set `gpu_idle_reaper_dry_run: false` in the playbook's `vars` and run `ansible-playbook playbooks/gpu-idle-reaper.yaml`.
Change the value in the playbook rather than with `-e gpu_idle_reaper_dry_run=false`, because that passes the string "false", which is truthy in the template and keeps dry-run on.
The restart keeps the clocks.
So every process on the `would kill` list, whose countdown already shows 0, gets SIGTERM at the first poll unless it computes right then.
Review that list and the countdown column before arming.
The startup line in the log shows `dry_run=True` or `dry_run=False`.

## Logs and state

`/var/log/gpu-idle-reaper.log` holds the startup line with the full config, one line per action, and errors.
Each distinct NVML or state-save error is logged at most once an hour.
The log rotates weekly and keeps 52 weeks (`/etc/logrotate.d/gpu-idle-reaper`).

`/var/lib/gpu-idle-reaper/state.json` holds each process's idle seconds and last sample timestamp.
It is written every 60 s and on stop.
On start, a file from an earlier boot, one saved more than 10 minutes ago, or one that fails to parse is ignored with a log line, and the clocks start from zero.

## Reading the log

```
2026-10-27T10:15:02+0800 INFO starting hostname=gpu-a metrics=<tailnet-ip>:8001 state_file=/var/lib/gpu-idle-reaper/state.json idle_seconds=86400 grace_seconds=60 interval=1 dry_run=True gpus=4
2026-10-28T09:03:41+0800 INFO would kill user=alice pid=123456 gpus=0:20480MiB,1:20480MiB idle=24.0h cmd='python train.py --resume'
2026-10-28T11:20:07+0800 INFO active again, no longer would kill user=alice pid=123456 gpus=0:20480MiB,1:20480MiB cmd='python train.py --resume'
```

`gpus` lists each GPU index with the memory the process holds there, or `?` when NVML does not report it.
`idle` is the observed idle time in hours.
`cmd` is the command line, cut at 200 characters.

To review dry-run results, run `grep -E 'would kill|active again' /var/log/gpu-idle-reaper.log`.
A pid with a `would kill` line and no later `active again` stayed idle until it exited or until now, so arming would have ended it.
A pid with both lines worked again after more than 24 idle hours.
Arming would have ended it during that gap, so check whether its owner expects such long pauses.
A pid that its owner says was working the whole time points at the detection floor.

Once armed, the lines become:

```
... INFO SIGTERM sent: user=alice pid=123456 gpus=0:20480MiB idle=24.0h cmd='python train.py --resume'
... INFO SIGKILL sent: user=alice pid=123456 gpus=0:20480MiB idle=24.0h cmd='python train.py --resume'
... INFO gone pid=123456, its GPU memory is free
```

SIGKILL appears only when the process still held its context 60 s after SIGTERM.
`gone` confirms that a signalled process no longer holds a CUDA context.
Instead of `sent`, a signal line can say `already exited`, or `skipped, pid reused` when the pid now belongs to a different process.

## Countdown column

The 逐用户占用 table on https://cityu-gpu.myl.moe has a `kill 倒计时` column.
It shows how much longer the process can stay idle before it gets SIGTERM.
It returns to 24 hours whenever the process computes.
0 means the process is on the `would kill` list in dry-run, or is being ended once armed.
An empty cell means the reaper does not report that process, e.g., the reaper is down or saw the process for the first time less than a second ago.

The column comes from the gauge `gpu_idle_reaper_kill_in_seconds`, with labels `hostname`, `index`, `pid`, and `username`, one series per process and GPU.
The reaper serves it on `tailnet_ip:8001/metrics`, and the panel's Prometheus scrapes it as job `gpu-idle-reaper`.

## Who can stop it

The service runs as root, so users without sudo cannot signal or stop it.
Sudoers can stop it with `sudo systemctl stop gpu-idle-reaper`.
That command goes through sudo and lands in the sudo history (see `docs/sudo-history.md`).
A stop longer than 10 minutes resets the clocks.
`Restart=always` brings the service back after a crash or a kill, but not after `systemctl stop`.

## Prerequisites

The tailnet ACL must let sg1 reach both cityu hosts on port 8001.
As of 2026-10-26 it does: a rule that lets a large group of nodes, sg1 included, reach every port on each cityu host already covers it.
If that rule is narrowed later, `playbooks/cityu-gpu.yaml` fails at "Check the gpu-idle-reaper scrape targets", and the countdown column stays empty.

Run `playbooks/gpu-idle-reaper.yaml` before `playbooks/cityu-gpu.yaml`, because the latter checks that Prometheus scrapes the reaper.

## Operations

- Deploy or change a knob with `ansible-playbook playbooks/gpu-idle-reaper.yaml`. A new threshold applies to the saved clocks at once.
- The verify task prints the installed `nvidia-ml-py` and `prometheus-client` versions. pip runs with `state: present`, so upgrade by hand with `sudo /opt/gpu-idle-reaper/bin/pip install -U nvidia-ml-py prometheus-client && sudo systemctl restart gpu-idle-reaper`.

## Manual cleanup scope

By repository convention the playbook only provisions.
To retire the reaper, delete `playbooks/gpu-idle-reaper.yaml`, `playbooks/files/gpu-idle-reaper/`, `playbooks/templates/gpu-idle-reaper/`, `tests/test_gpu_idle_reaper.py`, this document, the `gpu-idle-reaper` scrape job and its assert in `playbooks/cityu-gpu.yaml`, and query B of the 逐用户占用 table.
Then clean up both cityu hosts:

```sh
sudo systemctl disable --now gpu-idle-reaper
sudo rm -rf /etc/systemd/system/gpu-idle-reaper.service /etc/systemd/system/gpu-idle-reaper.service.d /opt/gpu-idle-reaper /var/lib/gpu-idle-reaper /etc/logrotate.d/gpu-idle-reaper
sudo systemctl daemon-reload
```
