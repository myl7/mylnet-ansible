# Hardening for personal Debian VPS hosts

`playbooks/hardening.yaml` bootstraps new Debian VPS hosts after `init.yaml`.
It targets the `hosts` inventory group. Select the new host explicitly:

```sh
ansible-playbook playbooks/hardening.yaml --limit NEW_HOST
```

The playbook does not uninstall retired packages or remove files from earlier
versions. Clean up existing deployments manually using the steps below.

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

## Existing deployment scope

Read-only checks before cleanup on 2026-10-08 found:

| Host | Old check packages | Custom pwck cron | Custom auditd report cron | Audit watch rules |
| --- | --- | --- | --- | --- |
| sg1 | Installed | Present | Present | Present |
| bwh | Installed | Present | Absent | Absent |
| jp1 | Installed | Present | Present | Present |

All three have the managed SSH and sysctl drop-ins, rkhunter, debsecan, debsums,
msmtp-mta, auditd, sysstat, and unattended-upgrades installed. Debsecan has both
its package-provided cron entry and the extra playbook entry on all three hosts.
Audit is enabled on sg1 and jp1. Bwh's kernel reports no audit support, and
auditd is disabled there. The `clients` host `cu` has none of the checked managed SSH/sysctl drop-ins,
custom report cron files, or pwck report script. These are current deployment
traces, not proof of the complete historical execution scope.

## Manual cleanup on sg1, bwh, and jp1

Run the following steps separately on each host. They remove only retired checks
and reporting components. Keep auditd, its watch rules, sysstat, unattended
upgrades, SSH limits, sysctl settings, and journald configuration.

### 1. Back up the old configuration

Connect from the control machine, choosing one host:

```sh
ssh -p 50000 myl@167.104.101.86  # sg1
ssh -p 50000 myl@104.243.29.231  # bwh
ssh -p 50000 myl@167.104.165.185  # jp1
```

Then enter a root shell and back up any files that exist:

```sh
sudo -i
backup_dir="/root/hardening-mail-backup-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$backup_dir"
for path in \
  /etc/cron.d/mylnet-auditd-watch /etc/cron.d/mylnet-pwck \
  /etc/cron.d/debsecan /etc/default/debsecan /etc/default/debsums \
  /etc/default/rkhunter /etc/rkhunter.conf /etc/rkhunter.conf.local \
  /etc/msmtprc /etc/aliases \
  /usr/local/sbin/mylnet-auditd-watch-report \
  /usr/local/sbin/mylnet-pwck-report /var/lib/mylnet-security; do
  if test -e "$path"; then cp -a --parents "$path" "$backup_dir/"; fi
done
```

The backup includes SMTP credentials. Keep it private.

### 2. Stop custom reports and remove the check packages

```sh
rm -f /etc/cron.d/mylnet-auditd-watch /etc/cron.d/mylnet-pwck
apt-get --simulate purge rkhunter debsecan debsums apt-show-versions
```

Read the simulated removal list. It should not include applications you use,
`auditd`, `sysstat`, or `unattended-upgrades`. Then run:

```sh
apt-get purge rkhunter debsecan debsums apt-show-versions
rm -f /etc/cron.d/debsecan /etc/rkhunter.conf.local
rm -f /usr/local/sbin/mylnet-auditd-watch-report \
  /usr/local/sbin/mylnet-pwck-report
rm -f /var/lib/mylnet-security/auditd-watch.checkpoint \
  /var/lib/mylnet-security/auditd-watch-login.checkpoint \
  /var/lib/mylnet-security/auditd-watch-fail.state
if test -d /var/lib/mylnet-security; then
  rmdir --ignore-fail-on-non-empty /var/lib/mylnet-security
fi
```

Purging removes the package-provided scan jobs as well. Do not run a blanket
`autoremove`. Keep existing audit logs and `/var/log/msmtp.log` for reference.

### 3. Remove system SMTP if no other task uses it

Check for other mail users after removing the reports:

```sh
grep -RlE 'sendmail|msmtp|MAILTO|mailx' \
  /etc/cron.d /etc/cron.daily /etc/cron.weekly /etc/cron.monthly \
  /etc/apt/apt.conf.d /etc/systemd/system /usr/local/bin /usr/local/sbin \
  /var/spool/cron/crontabs 2>/dev/null
apt-get --simulate purge msmtp-mta msmtp
```

The live check found only comments in `50unattended-upgrades` and the chrony
service besides the retired scan jobs. No APT mail recipient was configured.
Review any matches and the removal list. This search cannot detect all callers,
so also check applications configured to invoke the host's sendmail command.
Applications sending mail directly through their own SMTP settings do not need
host msmtp. If host SMTP is no longer used:

```sh
apt-get purge msmtp-mta msmtp
rm -f /etc/msmtprc
```

Keep `/etc/aliases`, which may predate this playbook. If another mail transport
is installed later, review its old `root:` and `default:` recipient mappings.

### 4. Verify

```sh
dpkg-query -W rkhunter debsecan debsums msmtp-mta msmtp 2>/dev/null
find /etc/cron.d /etc/cron.daily /etc/cron.weekly /etc/cron.monthly \
  -maxdepth 1 -type f \( -iname '*rkhunter*' -o -iname '*debsecan*' \
  -o -iname '*debsums*' -o -iname '*mylnet*' \) -print
systemctl is-active apt-daily.timer apt-daily-upgrade.timer \
  sysstat-collect.timer sysstat-summary.timer
```

The retired packages should no longer have installed status, and their cron
files should be absent. Other `mylnet` jobs may legitimately remain. On sg1 and
jp1 also check `systemctl is-active auditd` and `auditctl -l` to confirm that local
auditing remains enabled. Bwh's old deployment has no managed audit watch rules.
No reboot is needed for this reporting cleanup.

## Cleanup completed on 2026-10-08

Retired packages, custom report jobs, scripts, checkpoints, and `/etc/msmtprc`
were removed from all three VPS hosts. APT also removed `bsd-mailx`, which
depended on the mail transport. No blanket autoremove was performed.

| Host | Root-only backup directory |
| --- | --- |
| sg1 | `/root/hardening-mail-backup-20261008-132500` |
| bwh | `/root/hardening-mail-backup-20261008-132511` |
| jp1 | `/root/hardening-mail-backup-20261008-132520` |

The backups included the old configuration, package state, APT removal preview
and log, and verification records. All three backup directories were deleted
after verification on 2026-10-08 at the owner's request. Checks confirmed that retired packages and
report cron files were absent. Automatic update timers, sysstat timers, SSH,
and journald remained active. SSH, sysctl, APT, journal, audit watch rules where
present, and aliases retained their original checksums. Auditd remained active
on sg1 and jp1 and inactive on bwh. Running Docker container IDs were unchanged.

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
