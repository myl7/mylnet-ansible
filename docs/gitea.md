# Gitea

`playbooks/gitea.yaml` deploys Gitea on sg1. Keep the floating
`docker.gitea.com/gitea` stable image: each deployment pulls it and verifies
that the running version is at least **28.4.0**, rather than pinning an old tag.
The health check also requires `/api/healthz` to report `pass`.
The existing container's image is preserved with an image-ID-based rollback tag.
An unchanged image reuses the same tag; the playbook does not stop the service
or create a data archive merely to preserve that image.

## Before an authorized upgrade of an existing instance

Data backup is an explicit operator prerequisite, not an automatic side effect
of this provisioning playbook. It does not create or prune data backups.

1. Inspect the current image/version and the intended stable image. If the image
   will not change, do not stop Gitea or make another upgrade backup.
2. Check free space and existing backups on sg1 using `df -B1 /srv/gitea
   /srv/backups` and `sudo du -sB1 /srv/gitea/data /srv/backups/gitea` (if the
   backup directory exists). Also check the apparent data size with
   `sudo du -sb /srv/gitea/data`. Allow at least the larger data size plus
   tar overhead, the new image's space and operating headroom. Compression is
   not guaranteed to save space. If capacity is insufficient, do not start
   a local backup or upgrade; arrange another backup destination first.
3. Record the current container image ID and save `/opt/stacks/gitea/compose.yaml`.
   Stop Gitea writers, archive all of `/srv/gitea/data` to a unique backup
   destination, and validate the archive before restarting the old container.
   A stopped copy keeps SQLite, repositories and attachments consistent.
   Ensure the original container is restarted even if the backup fails.
4. Confirm the backup is readable and complete before running the playbook.
   Record its location and matching image ID. Do not proceed after a failed or
   partial backup. Review accumulated backups manually; nothing here deletes
   them automatically.

## Verification and rollback

Check mode skips live HTTP/version checks and image preservation. Local syntax
and template tests are not an actual deployment or restore test. After an
upgrade, also verify repository reads and authenticated Git operations.

A rollback after a database migration needs the matching data backup **and**
old image, not just the old image. Stop Gitea, preserve the failed deployment's
data separately, restore the backup and saved Compose settings, then select the
recorded rollback image without pulling a new one. Writes since the backup may
otherwise be lost. Check health, version and Git operations before declaring
recovery. Restoration is a separate explicit operation, not an automatic action
on a failed health check.

The playbook expects the Compose project `gitea`, container `gitea-gitea-1`, and
local data mount `/srv/gitea/data`. A new install has no existing image to save.
