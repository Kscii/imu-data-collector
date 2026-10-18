# Self-hosting

The [hosted platform](https://imu.kscii.tech/) is allowlist-only. You can run an
independent instance using local storage or your own Google Cloud project.
The software license does not grant access to the hosted instance or its data.

| Setup | Intended use | Authentication and storage |
| --- | --- | --- |
| Local annotation | Single-user development and local datasets | Loopback only, local identity, filesystem objects |
| GCP deployment | A private platform shared by multiple people | HTTPS, Google IAP, application allowlist, GCS |

## Local annotation

Install Python 3.12, uv, Node.js 24, and npm. No BLE device or camera is needed to
browse the annotation app. FFmpeg/FFprobe are needed for media processing workflows.
From a clone of this repository:

```bash
uv sync --frozen
npm ci --prefix frontend
npm run build:annotation --prefix frontend
mkdir -p ~/.config/imu-data-collector
cp configs/local.annotation.example.yaml ~/.config/imu-data-collector/annotation.yaml
uv run --frozen imu-annotation --config ~/.config/imu-data-collector/annotation.yaml start
```

Open `http://127.0.0.1:8766/?lang=en`. An empty recording list is expected until
valid recordings have been published into the configured object store.

The example uses the independent local identity `localuser`, filesystem storage
at `~/IMUObjects`, and a local annotation catalog/cache. Match the capture app's
`storage.root` to `~/IMUObjects` and use `publish.mode: local` to share recordings.
The source H5/MKV directory is not the object store: publication must finish and
write `manifest.json` before the annotation server indexes a recording.

`auth.mode: local` trusts one configured identity; it is not a login system. Keep
the server on `127.0.0.1`. For a single-user remote session, use an SSH tunnel:

```bash
ssh -L 8766:127.0.0.1:8766 your-server
```

Do not expose local authentication through a public reverse proxy.

## Configuration

Pass `--config /absolute/path/to/config.yaml` to select a private YAML. See the
[local example](../configs/local.annotation.example.yaml) and
[GCP example](../configs/server.annotation.example.yaml).

| Setting | Purpose |
| --- | --- |
| `storage.backend` | `local` or `gcs` |
| `storage.root` | Local published objects; shared with local capture |
| `storage.bucket`, `storage.project` | Your GCS bucket and project |
| `storage.cache_root` | Downloaded and generated local cache |
| `annotation.catalog_path` | Writable SQLite index for this instance |
| `annotation.server_host`, `annotation.server_port` | Listener; local default is `127.0.0.1:8766` |
| `identity.allowed_unikeys`, `identity.admins` | Application actor IDs and administrator subset |
| `identity.subject_ids` | Private, append-only actor-to-anonymous-participant mapping |
| `auth.mode`, `auth.local_actor_id` | Local single-user identity |
| `auth.iap_audience`, `identity.email_to_unikey` | IAP audience and private Google-account mappings |

Actor IDs use lowercase letters/digits, start with a letter, and contain 3–32
characters. `subject_ids` values use `cw12eu:subject-NNN` and cannot be reassigned
or shared. Configure this mapping before exporting production datasets.

Example files configure identities and storage, not sensor trust. A valid
production export also requires matching device configuration, verified SI
calibration, source hashes, synchronization, and completed annotations.

## Private GCP platform

Follow [GCP deployment](gcp-deployment.md) for server requirements and the complete
installation, domain, HTTPS, IAP, and storage setup. Follow
[Desktop upload](desktop-upload.md) if capture clients will publish remotely.

Optional modules have separate dependencies:

- External device history requires provider credentials and a separate worker.
- Synthetic motion consumes artifacts from an independent producer; snapshot
  construction needs the compatible external worker.
- Dataset/model catalogs need compatible manifests and objects. Model viewing
  currently also has a code-level allowlist; an administrator role alone does
  not enable it for an arbitrary self-hosted identity.

The core capture/annotation workflow does not require these integrations. See
[Operations](operations.md) for updates and recovery.
