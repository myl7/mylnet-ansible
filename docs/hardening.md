# Hardening for personal Debian VPS hosts

`playbooks/hardening.yaml` bootstraps new Debian VPS hosts after `init.yaml`.
It targets the `hosts` inventory group. Select the new host explicitly:

```sh
ansible-playbook playbooks/hardening.yaml --limit NEW_HOST
```

The playbook does not uninstall retired packages or remove files from earlier
versions. The one-time cleanup of earlier deployments ran on 2026-10-08; the
record below summarizes what was found and removed.

## What stays

- Automatic APT updates and unattended upgrades reduce exposure to known bugs.
- SSH pre-authentication limits bound the resources consumed by public SSH traffic.
- Disabling unused services and kernel protocols reduces available functionality.
- Core dump limits and sysctl settings restrict data exposure and kernel access.
- Auditd records security file changes locally when the VPS kernel supports audit.
- Sysstat keeps performance history for troubleshooting.
- A persistent, size-limited journal keeps recent logs without filling the disk.
- PAM password quality remains useful for local account passwords.

USB and FireWire module restrictions have little practical value on these VPS
hosts, but are inexpensive and remain part of the existing baseline.

## What was removed

- Rkhunter scans, property database initialization, and finding allowlists.
- Debsecan vulnerability reports and its custom cron job.
- Scheduled debsums checksum checks and the package installation.
- Auditd email reports, checkpoints, and the custom reporting script.
- Monthly passwd/shadow email reports and the custom pwck reporting script.
- SMTP configuration and secret requirements in Hardening. CLI mail is now
  configured separately by Init, as described below.
- Apt-show-versions installation, which only provided a manual inspection tool.

These checks mostly report findings rather than prevent access. For this personal
VPS setup, unattended security updates and local logs provide more practical
value than maintaining scan baselines and email filtering. Trusted APT sources
reduce one supply-chain risk, but do not cover compromised services or stolen
credentials. Rkhunter is a rootkit scanner, not a source of rootkits. Updating its
property baseline accepts the current files and should not be used merely to
silence unexplained warnings.

## Cleanup record (2026-10-08)

Read-only checks found the retired components on all three VPS hosts (sg1, bwh,
jp1): the old check packages, the custom pwck cron, and msmtp-based reporting on
every host, plus the auditd report cron on sg1 and jp1. Audit was enabled on sg1
and jp1, while bwh's kernel has no audit support. The cleanup then ran on each
host separately: configurations were backed up, then rkhunter, debsecan,
debsums, apt-show-versions, the custom report jobs, and host SMTP (msmtp-mta,
taking bsd-mailx with it) were removed. Verification confirmed the retired
packages and cron files gone while the update timers, sysstat, SSH limits,
journald, and auditd where present stayed active; no blanket autoremove was
run. The root-only per-host backup directories were deleted after verification
at the owner's request. The original step-by-step runbook with the per-host
findings table lives in this file's git history before 2026-10-08.

## CLI SMTP restored in Init

CLI mail remains useful independently of scheduled security reports. Its
`msmtp-mta` installation, `/etc/msmtprc` template, local aliases, and SMTP
variable checks now belong to `playbooks/init.yaml`. The SMTP secret block is
labelled `init.yaml (CLI system mail)` in the separate secrets repository.

To configure only SMTP on existing hosts without applying the other Init tasks:

```sh
ansible-playbook playbooks/init.yaml --tags smtp --limit sg1,bwh,jp1
```

This restores `msmtp` and a sendmail-compatible CLI. It does not install mailx or
restore the retired scanners and report jobs. SMTP credential values are loaded
by Ansible from the secrets file and do not need to be copied into chat. Retain
the Mailgun domain `noreply.myl.moe` and its SMTP credentials for this feature.

SMTP-only deployment completed on sg1, bwh, and jp1 on 2026-10-08. Checks
confirmed that the CLI commands were available, SMTP TLS connections worked,
retired report jobs remained absent, and retained services remained active. No
test email was sent.
