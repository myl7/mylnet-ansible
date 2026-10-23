# sudo history on the cityu GPU servers

Command-level attribution for privileged actions: who ran what through
sudo, when, from which directory, and as whom. Deployed by
`playbooks/sudo-history.yaml`; the knobs live in the playbook's `vars`
(no group_vars, no secrets).

```
cs11369a  /var/log/sudo.log              local events, forwarded to cs659b
cs659b    /var/log/sudo.log              merged history: own events + cs11369a's
cs11369a  /var/log/sudo-journal-rescue.log   sudo events rescued from the /run journal
```

Every sudo invocation — allowed or denied — already travels as an
authpriv message. rsyslog re-files those into `/var/log/sudo.log` on each
host (0640 `syslog:adm`, like `auth.log`); the routing rule has no
`stop`, so `auth.log` and `syslog` keep their copies and fail2ban is
unaffected. A line looks like:

```
Oct 17 14:03:29 cs11369a sudo:     myl : TTY=pts/2 ; PWD=/home/myl ; USER=root ; COMMAND=/usr/bin/apt update
```

## Querying

```bash
sudo grep -F ***REMOVED*** /var/log/sudo.log        # one user, exact match
sudo grep COMMAND /var/log/sudo.log | tail         # recent commands
sudo ***REMOVED*** /var/log/sudo.log.*.gz              # rotated weeks
ssh cs659b 'sudo grep COMMAND /var/log/sudo.log | tail'   # both hosts at once
sudo grep COMMAND /var/log/sudo-journal-rescue.log # the pre-deploy history on cs11369a
journalctl -t sudo                                # journal copy
```

The merged file on cs659b carries the hostname per line, so one `grep`
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

## cs11369a journal rescue

At deploy time cs11369a's journal was volatile (`/var/log/journal`
absent): ~100G of journal lived in `/run` (1TB RAM makes the tmpfs caps
generous), holding the machine's only sudo history back to August — its
`auth.log` ***REMOVED*** ***REMOVED*** when the full root
partition ***REMOVED*** ***REMOVED***. The journal is far too large to
flush into the nearly full root partition, and vacuuming it down first
would destroy the very history worth saving, so the playbook exports just
the sudo events to `/var/log/sudo-journal-rescue.log` (root:root 0640,
ISO timestamps) and leaves the journal volatile. After the next reboot
the machine starts with a fresh journal, and `/var/log/sudo.log` plus the
cs659b merged copy carry the history from deploy day on.

Delete the rescue file and re-run the playbook to take a fresher snapshot
before a planned reboot. Once the root partition is properly cleaned up,
switching the journal to persistent storage the way hardening.yaml does
on the Mylnet hosts can be reconsidered.

## Operations

- Receiver down: the forwarder on cs11369a queues to disk (up to 10M in
  `/var/spool/rsyslog/`) and retries forever; the local copy on cs11369a
  is unaffected.
- cs659b listens on 514/tcp **on its tailnet IP only** (`100.67.212.32`),
  and `$AllowedSender` limits senders to the cityu tailnet addresses, so
  the campus network can neither reach nor spoof the input. If a firewall
  is ever installed on cs659b, allow 514/tcp on the tailscale interface.
- Re-running the playbook converges; it is safe any time.

## Pre-existing quirks (observed 2026-10-17, left alone)

***REMOVED***
  ***REMOVED***
- cs11369a's `***REMOVED***` ***REMOVED***
  ***REMOVED***
- cs11369a's ***REMOVED***. ***REMOVED***
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
# cs659b only (receiver)
sudo rm /etc/rsyslog.d/30-sudo-history-receive.conf
# both hosts, after removing the configs
sudo systemctl restart rsyslog
# forwarder queue leftovers on cs11369a
sudo rm -f /var/spool/rsyslog/sudo-history*
# keep /var/log/sudo.log* and the rescue export if you still want the
# history; otherwise remove them too
```
