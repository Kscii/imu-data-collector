# Deploy on Google Cloud

This guide sets up an independent private annotation platform. Use your own
project, domain, accounts, and bucket. For a single-user trial, start with
[Local annotation](self-hosting.md#local-annotation).

## 1. Requirements and architecture

```text
Browser -> HTTPS load balancer -> IAP -> annotation service :8766 -> private GCS
Desktop -> HTTPS upload hostname -> upload broker :8770 ---------> private GCS
```

The upload broker validates Google tokens itself and is configured separately;
IAP protects the annotation backend. Capture hardware stays on the desktop.

| Resource | Starting point |
| --- | --- |
| VM | Ubuntu 24.04 LTS, x86_64; no GPU required for annotation |
| CPU / RAM | 2 vCPU / 4 GiB (`e2-medium`) reference deployment |
| Disk | Reference deployment has a 30 GB boot disk; provision more for caches and export jobs |
| Object storage | Private regional GCS bucket near the VM; size for raw media and retained snapshots |
| Network | Domain, static global IP, HTTPS load balancer, Google-managed certificate |
| Software | Python 3.12, uv, Node.js 24/npm for builds, Git, FFmpeg/FFprobe |

The reference sizing is an observed installation, not a measured minimum or
concurrency guarantee. HDF5 delivery jobs need local space for downloads and the
complete generated output; do not size disk only for the code. Monitor free space,
RAM, request latency, storage growth, and download traffic. Synthetic production,
model training, and external workers have separate requirements. VM, disk, load
balancing, storage, and egress all contribute to cost; set a billing budget.

## 2. Project, storage, and VM

In your administrator terminal, install and authenticate the Google Cloud CLI.
Choose explicit values (the examples below are placeholders):

```bash
export IMU_GCP_PROJECT='your-project-id'
export IMU_GCP_REGION='australia-southeast1'
export IMU_GCP_ZONE='australia-southeast1-a'
export IMU_GCP_BUCKET='your-globally-unique-private-bucket'
export IMU_GCP_VM='imu-annotation'
export IMU_GCP_SA="imu-runtime@${IMU_GCP_PROJECT}.iam.gserviceaccount.com"

gcloud services enable compute.googleapis.com storage.googleapis.com \
  iap.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
  --project="$IMU_GCP_PROJECT"
gcloud iam service-accounts create imu-runtime --project="$IMU_GCP_PROJECT"
gcloud storage buckets create "gs://$IMU_GCP_BUCKET" \
  --project="$IMU_GCP_PROJECT" --location="$IMU_GCP_REGION" \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://$IMU_GCP_BUCKET" --soft-delete-duration=7d
gcloud storage buckets add-iam-policy-binding "gs://$IMU_GCP_BUCKET" \
  --member="serviceAccount:$IMU_GCP_SA" --role=roles/storage.objectAdmin
```

Bucket-scoped Object Admin supports the app's object creation, reading, and explicit
delete/cleanup operations. Do not grant this identity to desktop users or create a
shared service-account key. The VM uses its attached runtime identity.

Create the VM in Compute Engine with the OS/resources above, the `imu-runtime`
service account, and the **cloud-platform** access scope. Put it in your chosen VPC
and add the network tag `imu-annotation`. Do not enable broad HTTP/HTTPS firewall
rules to the VM. A VM without an external IP needs Cloud NAT or another controlled
egress path for dependency installation and external APIs.

Allow ingress to tagged instances on TCP 8766 (and 8770 if using uploads) only from
load-balancer/health-check ranges `35.191.0.0/16` and `130.211.0.0/22`.
For IAP SSH, separately allow TCP 22 from `35.235.240.0/20` and grant administrators
the appropriate IAP tunnel and OS Login permissions. Keep these rules scoped to
this VM/network tag. Do not expose the application ports directly to the internet.

## 3. Install an application release

SSH to the VM. Install system packages:

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git ffmpeg curl ca-certificates
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and
[Node.js 24](https://nodejs.org/en/download) using their official installation
instructions. Confirm `uv --version`, `node --version`, and `npm --version` in the
login shell. Node/npm are needed to build, not to run the finished service.

Build as your login user from a clean checkout. Replace `RELEASE_REF` with the
reviewed tag or commit you intend to deploy:

```bash
git clone https://github.com/Kscii/imu-data-collector.git imu-build
cd imu-build
git checkout RELEASE_REF
export IMU_RELEASE_ID="$(git rev-parse HEAD)"
npm ci --prefix frontend
npm run build:annotation --prefix frontend

sudo useradd --system --user-group --home-dir /var/lib/imu-annotation \
  --create-home --shell /usr/sbin/nologin imu-annotation
sudo install -d -m 0755 /opt/imu-annotation/releases
sudo install -d -o imu-annotation -g imu-annotation -m 0750 /var/lib/imu-annotation
sudo install -d -o root -g imu-annotation -m 0750 /etc/imu-annotation
sudo install -d -m 0755 "/opt/imu-annotation/releases/$IMU_RELEASE_ID"
tar -cf - LICENSE README.md pyproject.toml uv.lock configs src frontend/dist-annotation \
  | sudo tar -xf - -C "/opt/imu-annotation/releases/$IMU_RELEASE_ID"
sudo env UV_CACHE_DIR=/var/cache/imu-annotation-uv "$(command -v uv)" sync \
  --project "/opt/imu-annotation/releases/$IMU_RELEASE_ID" \
  --python /usr/bin/python3.12 --frozen --no-dev
sudo ln -s "releases/$IMU_RELEASE_ID" /opt/imu-annotation/current
```

The user-creation and initial symlink commands are first-install steps. Keep release
files administrator-owned and readable by the service. Subsequent versions get a
new release directory; see [Operations](operations.md) for switching and rollback.

## 4. Private application configuration

```bash
sudo install -o root -g imu-annotation -m 0640 \
  configs/server.annotation.example.yaml /etc/imu-annotation/config.yaml
sudoedit /etc/imu-annotation/config.yaml
```

Replace the GCS project/bucket, application actor IDs, administrator subset, and
Google email mappings. Set `auth.mode: iap`; the audience is completed in step 6.
Keep the listener on `0.0.0.0:8766` so the load balancer can reach it through the
restricted firewall. Catalog and cache paths must be writable under
`/var/lib/imu-annotation`.

Configure private `identity.subject_ids` before production export; values are
unique `cw12eu:subject-NNN` IDs that cannot be reused. The example deliberately
leaves this mapping empty. Device calibration and snapshot approval are separate
requirements: do not claim a newly attached device is calibrated by copying an
example. See [Device configuration](device-configuration-v2.md).

Create `/etc/systemd/system/imu-annotation.service`:

```ini
[Unit]
Description=IMU annotation platform
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=imu-annotation
Group=imu-annotation
WorkingDirectory=/opt/imu-annotation/current
Environment=PYTHONUNBUFFERED=1
Environment=XDG_DATA_HOME=/var/lib/imu-annotation/data
Environment=XDG_CACHE_HOME=/var/lib/imu-annotation/cache
ExecStart=/opt/imu-annotation/current/.venv/bin/imu-annotation --config /etc/imu-annotation/config.yaml serve
Restart=on-failure
RestartSec=5
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/imu-annotation

[Install]
WantedBy=multi-user.target
```

The checked-in production units/deployer contain the maintainer's Unix username
and environment assumptions. This self-hosting unit uses your independent service
account instead; do not run the production deployer unchanged on a new VM.

## 5. HTTPS load balancer and domain

Use the Google Cloud console to create these resources in your project:

1. An **unmanaged instance group** in the VM's zone containing the VM. Assign named
   port `annotation:8766`.
2. An HTTP health check on port 8766, path `/api/v1/health`.
3. A **global external Application Load Balancer** with a backend service named
   `imu-annotation-backend`, protocol HTTP, the instance group, named port
   `annotation`, the health check, and a **3600-second timeout**. Leave Cloud CDN off.
4. A reserved global static IP, Google-managed certificate for your annotation
   hostname (for example `imu.example.com`), and HTTPS frontend on port 443.
5. A URL map routing that hostname to the annotation backend.
6. A DNS A record pointing to the reserved IP. If using Cloudflare, use DNS-only
   rather than proxying this hostname. Wait for the certificate to become active.

Use [Google's load-balancer setup guide](https://cloud.google.com/load-balancing/docs/https/setup-global-ext-https-compute)
for current console screens. Keep the app in IAP mode throughout setup: a reachable
page or health endpoint does not authorize its protected APIs.

## 6. IAP and the application allowlist

Enable IAP for **the annotation backend service**, not a forwarding rule shared
with the upload broker. Follow Google's
[IAP setup](https://cloud.google.com/iap/docs/enabling-compute-howto) and
[custom OAuth configuration](https://cloud.google.com/iap/docs/custom-oauth-configuration).
Choose an OAuth audience/client that permits the intended accounts; the
Google-managed internal-organization path alone does not admit arbitrary external
Google accounts. Complete external consent/test-user configuration when applicable.

Read the numeric project and backend IDs:

```bash
gcloud projects describe "$IMU_GCP_PROJECT" --format='value(projectNumber)'
gcloud compute backend-services describe imu-annotation-backend \
  --project="$IMU_GCP_PROJECT" --global --format='value(id)'
```

Set the application's private audience to:

```yaml
auth:
  mode: iap
  iap_audience: /projects/PROJECT_NUMBER/global/backendServices/BACKEND_SERVICE_ID
```

Grant each permitted account access to this backend:

```bash
gcloud iap web add-iam-policy-binding --project="$IMU_GCP_PROJECT" \
  --resource-type=backend-services --service=BACKEND_SERVICE_ID \
  --member='user:operator@example.com' --role=roles/iap.httpsResourceAccessor
```

The same account needs a private application email mapping to an allowed actor ID.
Both layers are required; see [Access and privacy](access-and-privacy.md).

## 7. Start and verify

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now imu-annotation.service
sudo systemctl status imu-annotation.service --no-pager
curl --fail http://127.0.0.1:8766/api/v1/health
sudo journalctl -u imu-annotation.service -n 50 --no-pager
```

Check that the load-balancer backend becomes healthy. In a browser, open your
hostname with `?lang=en`: unauthenticated users should encounter Google login,
allowlisted users should see their mapped actor, and an account absent from either
access layer must be rejected. The new store will have no recordings. Verify a
short test upload and indexing before attempting production annotation/export.

## 8. Uploads and large downloads

Follow [Desktop upload](desktop-upload.md) to create a separate Desktop OAuth
client, private broker environment, service, upload backend/hostname, and client
configuration. Never bundle cloud keys or OAuth secrets into a desktop installer.

A runtime service account can use GCS signed downloads when it can sign on its own
behalf. Enable the IAM Credentials API (step 2), then optionally grant:

```bash
gcloud iam service-accounts add-iam-policy-binding "$IMU_GCP_SA" \
  --project="$IMU_GCP_PROJECT" --member="serviceAccount:$IMU_GCP_SA" \
  --role=roles/iam.serviceAccountTokenCreator
```

This binding is scoped to that service account, not the project. Its existing
bucket read permission is also required. See
[Google's signing requirements](https://cloud.google.com/storage/docs/access-control/signing-urls-with-helpers).
Without signing, authenticated proxy downloads remain available. Retain the
3600-second backend timeout and verify Range downloads before distributing large H5s.

## 9. Operations and optional services

Back up private configuration/identity mappings, preserve GCS recovery retention,
and monitor services, disk, scan progress, and export failures. See
[Operations](operations.md) for the release and recovery checklist.

External history needs its own credentialed worker; synthetic snapshots need the
independent compatible producer/worker. Enable them only when configured. A separate
public maintenance-page bucket is optional; never publish the private data bucket.

The repository's GitHub deployment workflows use environment variables, Workload
Identity Federation, and maintainer-specific deployment scripts. They are not a
ready-to-run deployment pipeline for a fork. Adapt the Unix user, resource names,
health URLs, WIF repository restrictions, and rollback/configuration assumptions
before enabling them. Normal main pushes run CI and do not deploy production.
