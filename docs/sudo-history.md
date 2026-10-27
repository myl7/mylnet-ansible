# sudo history on the cityu GPU servers

Command-level attribution for privileged actions: who ran what through
sudo, when, from which directory, and as whom. Deployed by
`playbooks/sudo-history.yaml`; the knobs live in the playbook's `vars`
(no group_vars, no secrets). Below, server A is the forwarder whose
journal was volatile, and server B the receiver holding the merged history.

```
server A  /var/log/sudo.log              local events, forwarded to server B
server B  /var/log/sudo.log              merged history: own events + server A's
server A  /var/log/sudo-journal-rescue.log   sudo events rescued from the /run journal
```

Every sudo invocation — allowed or denied — already travels as an
authpriv message. rsyslog re-files those into `/var/log/sudo.log` on each
host (0640 `syslog:adm`, like `auth.log`); the routing rule has no
`stop`, so `auth.log` and `syslog` keep their copies and fail2ban is
unaffected. A line looks like:

```
Oct 17 14:03:29 serverA sudo:     alice : TTY=pts/2 ; PWD=/home/alice ; USER=root ; COMMAND=/usr/bin/apt update
```

## Querying

```bash
sudo grep -F 'alice : TTY=' /var/log/sudo.log      # one user, exact match
sudo grep COMMAND /var/log/sudo.log | tail         # recent commands
sudo zgrep alice /var/log/sudo.log.*.gz            # rotated weeks
ssh server-B 'sudo grep COMMAND /var/log/sudo.log | tail'   # both hosts at once
sudo grep COMMAND /var/log/sudo-journal-rescue.log # the pre-deploy history on server A
journalctl -t sudo                                # journal copy
```

The merged file on server B carries the hostname per line, so one `grep`
there covers both machines. The local file on each host is the fallback
when a machine was down or unreachable.

## Scope and assumptions

- Captured: every sudo invocation — user, tty, working directory, target
  user, full command with arguments, and denials (`user NOT in sudoers`,
  wrong password).
- Not captured: actions inside a root shell after `sudo su -` (only the
  `su` appears as one line), processes already running as root, and
  direct root logins (the login itself is in `auth.log`).
- Trust assumption: a sudoer can read, edit and delete these logs. This
  system attributes actions among honest users; it does not contain a
  hostile root. Users are told the logging exists.

## Retention

`logrotate` rotates `/var/log/sudo.log` weekly, keeps 52 compressed
copies (~a year; `sudo.log.1` stays plain via `delaycompress`). The
rescue export never rotates: it is a fixed snapshot.

## server A journal rescue

At deploy time server A's journal was volatile (`/var/log/journal`
absent): ~100G of journal lived in `/run` (1TB RAM makes the tmpfs caps
generous), holding the machine's only sudo history back to August — its
`auth.log` ***REMOVED*** ***REMOVED*** when the full root
partition ***REMOVED*** ***REMOVED***. The journal is far too large to
flush into the nearly full root partition, and vacuuming it down first
would destroy the very history worth saving, so the playbook exports just
the sudo events to `/var/log/sudo-journal-rescue.log` (root:root 0640,
ISO timestamps) and leaves the journal volatile. After the next reboot
the machine starts with a fresh journal, and `/var/log/sudo.log` plus the
server B merged copy carry the history from deploy day on.

Delete the rescue file and re-run the playbook to take a fresher snapshot
before a planned reboot. Once the root partition is properly cleaned up,
switching the journal to persistent storage the way hardening.yaml does
on the Mylnet hosts can be reconsidered.

## Operations

- Receiver down: the forwarder on server A queues to disk (up to 10M in
  `/var/spool/rsyslog/`) and retries forever; the local copy on server A
  is unaffected.
- server B listens on 514/tcp **on its tailnet IP only**,
  and `$AllowedSender` limits senders to the cityu tailnet addresses, so
  the campus network can neither reach nor spoof the input. If a firewall
  is ever installed on server B, allow 514/tcp on the tailscale interface.
- Re-running the playbook converges; it is safe any time.

## Pre-existing quirks (observed 2026-10-17, left alone)

- server B has a `/etc/sudoers.d/` ***REMOVED***
  ***REMOVED*** — ***REMOVED***
  copied from server A.
- server A's equivalent file ***REMOVED***
  ***REMOVED***
- server A's ***REMOVED***. ***REMOVED***
  the full root partition ***REMOVED*** ***REMOVED*** write errors, and the
  ***REMOVED*** (***REMOVED***
  `failed` with ***REMOVED*** ***REMOVED*** ***REMOVED***
  ***REMOVED*** ***REMOVED*** — no journal, no auth.log.
  ***REMOVED*** brought logging
  back; the rescue export ***REMOVED***,
  and ***REMOVED***.

## Manual cleanup scope

This playbook only provisions. Retiring the feature means removing
`playbooks/sudo-history.yaml`, `playbooks/templates/sudo-history/` and
this file, then on the hosts by hand:

```
# both hosts
sudo rm /etc/rsyslog.d/30-sudo-history.conf /etc/logrotate.d/sudo-history
# server B only (receiver)
sudo rm /etc/rsyslog.d/30-sudo-history-receive.conf
# both hosts, after removing the configs
sudo systemctl restart rsyslog
# forwarder queue leftovers on server A
sudo rm -f /var/spool/rsyslog/sudo-history*
# keep /var/log/sudo.log* and the rescue export if you still want the
# history; otherwise remove them too
```
