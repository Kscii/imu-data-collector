# Desktop OAuth and upload broker

Local capture works offline. Remote publication uses a separate upload broker,
with Google Desktop OAuth Authorization Code + PKCE. Desktop refresh tokens live
in the OS credential store; service-account keys and OAuth client secrets stay
out of installers. IAP browser cookies are not desktop credentials.

## Server setup

Start with [GCP deployment](gcp-deployment.md). In Google Auth Platform, configure
your consent screen and create a **Desktop app** OAuth client. Permit intended
accounts under its publishing/test-user settings. The public client ID is shared
with capture clients; the issued client secret is held only by the broker.

Create `/etc/imu-annotation/upload-broker.env`, root-owned, mode `0600`:

```dotenv
IMU_GOOGLE_OAUTH_CLIENT_ID=YOUR_CLIENT.apps.googleusercontent.com
IMU_GOOGLE_OAUTH_CLIENT_SECRET=YOUR_CLIENT_SECRET
IMU_UPLOAD_BROKER_HOST=0.0.0.0
```

systemd reads this file before dropping privileges. Use the same private YAML as
the annotation service for bucket and account mappings. The broker independently
verifies token signature, audience, verified email, and the application allowlist.

Create `/etc/systemd/system/imu-upload-broker.service` using the annotation unit
from the GCP guide, replacing these fields:

```ini
Description=IMU upload broker
EnvironmentFile=/etc/imu-annotation/upload-broker.env
ExecStart=/opt/imu-annotation/current/.venv/bin/imu-upload-broker --config /etc/imu-annotation/config.yaml
```

Retain the service user/group, working directory, XDG paths, writable state directory,
and hardening settings. Replace the original ExecStart rather than adding a second
one. Reload systemd and enable/start `imu-upload-broker.service`.

Add named port `upload:8770` to the instance group, a health check on `/health`, and
an HTTP upload backend with the group/port and a 3600-second timeout. Extend the
HTTPS certificate and URL map to route `upload.example.com` to this backend.
Point its DNS record to the same load balancer. Restrict VM ingress to the same
load-balancer/health-check ranges.

**Do not enable browser IAP on the upload backend.** Its own token validation
protects upload APIs; an IAP redirect would break the desktop OAuth exchange.
Do not apply forwarding-rule IAP to both hostnames.

## Client setup

Generate an independent configuration directory from the source checkout:

```bash
uv run --frozen python scripts/prepare_desktop_config.py \
  --output build/self-hosted-capture \
  --broker-url https://upload.example.com \
  --oauth-client-id YOUR_CLIENT.apps.googleusercontent.com
uv run --frozen imu-collector --config build/self-hosted-capture/default.yaml start
```

The helper replaces its output directory. Use a fresh build directory, never an
existing private configuration or recording directory. Its generated configuration
selects `publish.mode: broker`, keeps local storage, and includes only public OAuth
client configuration. For custom packaged clients, the packaging workflow reads
`DESKTOP_UPLOAD_BROKER_URL` and `DESKTOP_GOOGLE_OAUTH_CLIENT_ID` from repository variables.

Users sign in from capture when publishing. The broker exchanges the one-time code
and PKCE verifier using its server-held client secret. Subsequent refresh requests
also go through the broker. Tokens do not belong in YAML or logs. Sign-out removes
local credentials and attempts refresh-token revocation.

## Publication integrity

The client creates H5, MKV, and MP4, computes byte sizes/SHA-256, and submits its
versioned capture manifest draft. The broker authorizes only the exact recording's
object keys and issues resumable upload sessions. It re-reads and verifies all three
objects before writing the immutable manifest with a generation precondition.
Incomplete or mismatched uploads do not become visible recordings.

```text
captures/<recording_id>/capture.h5
captures/<recording_id>/video.mkv
captures/<recording_id>/preview.mp4
captures/<recording_id>/manifest.json
```

The annotation server then validates/indexes the manifest and writes a separate
receipt. The queue distinguishes local storage, waiting for login, bucket upload,
and annotation acceptance. Failed uploads retain local raw files and can be retried.

## Verify

Check local broker `/health`, its public HTTPS `/health`, and a real allowlisted
client login/test upload. Confirm the annotation index receipt and playback.
Non-allowlisted accounts must be denied; invalid/expired/wrong-audience tokens and
non-loopback OAuth redirects must be rejected. Verify that package contents and logs
contain no client secret, tokens, PKCE verifier, or resumable session URLs.

An administrator may separately configure `publish.mode: direct_gcs` with ADC on a
controlled host. That is not a configuration to distribute to desktop users.
