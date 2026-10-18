# Access and privacy

The hosted platform is private and allowlist-only. A self-hosted instance uses its
own accounts, cloud project, storage, and private configuration.

## Two access layers

1. Google IAP grants the account `roles/iap.httpsResourceAccessor` for the backend.
2. The app verifies the IAP JWT signature, issuer, and audience, maps the email
   through private `identity.email_to_unikey`, and checks `identity.allowed_unikeys`.

Both must pass. The app does not trust a browser-supplied actor ID. Local mode is
single-user development identity and must stay on loopback or behind an SSH tunnel.

## Members and roles

Members can view, claim, and explicitly take over tasks. Only the current owner can
edit an active task; the owner or an administrator can reopen a completed task.
All members can manage labels, subject to usage and revision checks. Administrator
operations include configuration approval, index refresh, external synchronization,
and snapshot cleanup. Optional model-catalog access has a separate code-level gate.

Choose application actor IDs before mapping accounts. Add them to
`identity.allowed_unikeys`, and the administrator subset to `identity.admins`.
The member command manages email mappings; it does not add a new allowed actor or
execute IAP IAM changes for you. Always specify your project/backend:

```bash
imu-annotation --config /etc/imu-annotation/config.yaml member-add   --email member@example.com --unikey operator01   --project YOUR_PROJECT --backend-service imu-annotation-backend
```

Inspect the dry-run output. To add access, apply the mapping using the same command
with `--apply`, restart/verify the service, then run the emitted IAP IAM command.
To remove access, revoke IAP first, remove the mapping with `member-remove --apply`,
and restart. Keep real mappings in private server configuration.

## Participants and exports

Capture does not assign participants. Review confirms the participant before export.
The private append-only `identity.subject_ids` maps actor-style participant IDs to
stable `cw12eu:subject-NNN` values. Never reuse or reassign a subject ID.
Training artifacts contain anonymous IDs, not emails, names, or operator IDs.

Test recordings cannot become training exports. Production intent still requires
calibration, configuration, synchronization, annotation, and source-integrity gates.
Calibration recordings are evidence and are not ordinary activity training data.

Training HDF5 excludes video. The separate client profile embeds video and therefore
needs independent authorization. The MIT license covers software, not recordings,
participant consent, or dataset redistribution. Avoid sharing accounts and arrange
collection safety, consent, and retention separately.

## Storage permissions and removal

Keep raw media, mutable reviews, and identity/configuration data private. Grant
benchmark consumers access only to the dataset prefix/managed folder they need,
not the entire recording bucket. Do not make the data bucket public for a viewer.

Deletion requires exact recording/snapshot confirmation. Recovery follows your
bucket's configured soft-delete policy; the UI is not a recovery console. Existing
benchmark snapshots are independent immutable evidence and are not automatically
removed when a platform TAR is cleaned up. Keep calibration evidence out of routine
garbage collection. See [Operations](operations.md).
