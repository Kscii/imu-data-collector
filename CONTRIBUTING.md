# Contributing

Bug reports, documentation improvements, and focused pull requests are welcome.
For substantial changes, open an issue describing the problem and proposed scope
before changing data formats or deployment behavior.

## Development setup

Use Python 3.12, uv, Node.js 24, and npm. Follow the platform prerequisites in
[Getting started](docs/getting-started.md), then run:

```bash
uv sync --frozen --dev
npm ci --prefix frontend
npm run build --prefix frontend
```

Run `uv run imu-collector start` for local capture or follow
[Self-hosting](docs/self-hosting.md) for annotation. Development servers are
`npm run dev --prefix frontend` and `npm run dev:annotation --prefix frontend`;
they proxy API requests to their respective local Python backends.

## Checks

```bash
uv run ruff check src tests scripts/verify-soft3888-compatibility.py
uv run pytest -q
npm test --prefix frontend
npm run build --prefix frontend
git diff --check
```

CI requires the Python/data-contract and frontend checks before merging. Tests
and builds do not establish real hardware compatibility: report the OS, sensor,
camera, version, and observed results separately when changing device support.

## Pull requests

Describe the problem, resulting behavior, and relevant validation. Keep changes
focused and include documentation when commands, configuration, or public
behavior changes. Use English for repository documentation.

Preserve raw data, timestamp semantics, immutable artifacts, and existing schema
compatibility. Normative data contracts have pinned provenance; see the
[handoff contract](docs/contracts/annotation-benchmark-contract.md) before editing
one. Do not regenerate device bootstrap snapshots as a documentation cleanup.

Use synthetic fixtures for tests. Keep credentials, private configuration, real
recordings, and local research archives out of commits. Report vulnerabilities
privately as described in [SECURITY.md](SECURITY.md).

Production deployment is a separate, manual operation. Merging a pull request
does not deploy the hosted platform.
