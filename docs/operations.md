# Operations and recovery

Use [GCP deployment](gcp-deployment.md) for the initial server setup and
[Getting started](getting-started.md) for a local capture installation. The commands
below assume the independent GCP service account/layout from that guide.

## Health checks

```bash
sudo systemctl is-active imu-annotation.service
curl --fail http://127.0.0.1:8766/api/v1/health
sudo journalctl -u imu-annotation.service -n 100 --no-pager
```

When enabled, also check `imu-upload-broker.service` and
`http://127.0.0.1:8770/health`, plus the external worker's heartbeat and task state.
Check the load-balancer backend and an authenticated browser session. A healthy
process alone does not prove account access, indexing, playback, or exports work.

Monitor disk space, repeated restarts, HTTP failures, recent catalog scans, GCS
permission/schema errors, worker progress, and export failures. Alerting and billing
budgets need an actual monitored notification destination.

## Update and rollback

1. Record the current release path and back up private configuration, identity
   mappings, and databases. Finish active capture, finalization, and export work.
2. Build the desired clean commit using the installation commands into a **new**
   release directory; install its locked dependencies and matching frontend there.
3. Check the new version accepts the current private configuration and persisted
   formats. Keep the previous release available.
4. Stop the annotation service and any enabled upload/external worker services.
5. Prepare `current.next` pointing at the new release, then atomically rename it to
   `current`. Restart the services and verify health and authenticated workflows.
6. On failure, stop services, restore compatible configuration if necessary, and
   atomically switch back to the previous release. Recheck health and data access.

Do not delete current data, overwrite immutable objects, or roll back schema-bearing
configuration blindly. The checked-in automated deployer is specific to the hosted
installation and requires adaptation for another service user/project.
Normal main pushes do not deploy; hosted deployment workflows require explicit
manual confirmation. A documentation merge does not require a service restart.

## Backups and recovery

| State | Backup / recovery |
| --- | --- |
| Raw objects, manifests, reviews, snapshots | Private GCS retention/soft delete and tested restore procedure |
| Private YAML, account and participant mappings | Restricted backup; participant IDs must never be reassigned |
| OAuth/provider secrets | Controlled secret backup/rotation; exclude from public artifacts |
| SQLite catalogs | SQLite backup API or stop services before copying databases/WAL state |
| Cache and generated downloads | Rebuildable when authoritative objects are intact |
| External history | Immutable archives plus control/generation state; rebuild indexes from accepted manifests |

After restoring, verify object digests, participant mapping continuity, catalog
visibility, review revisions, and expected active exports. Do not retry an ambiguous
annotation write until the current revision/content has been read and reconciled.

## Cleanup

Preview cleanup before applying it:

```bash
sudo -u imu-annotation /opt/imu-annotation/current/.venv/bin/imu-annotation \
  --config /etc/imu-annotation/config.yaml cleanup-orphans --min-age-days 7
```

Inspect exact objects and recovery retention before using the command's explicit
apply option. Eligible cleanup covers old incomplete publications, orphan receipts,
unreferenced old exports, and snapshots without valid manifests; current exports
and complete recordings are not routine garbage. Do not mix calibration-evidence
migration with ordinary cleanup. External history has separate retention/state.

The checked-in GC service is a dry-run unless explicitly changed to apply. Adapt
its user/group to your installation before installing it; a running timer alone
must not be reported as evidence that objects were deleted.

## Release acceptance

Separate automated validation, infrastructure checks, and actual user workflows.
For an application release, verify login/denial, task ownership, playback, exact
interval navigation, coverage checks, completion/download, and a small snapshot
using dedicated acceptance data. Test uploads cannot establish prod eligibility.
Optional modules require their own worker and business-flow checks.

Hardware acceptance records should identify host/OS, sensor, camera, software
version, and observed results. Store session-specific logs and operational archives
privately; keep public guides focused on reusable procedures.
