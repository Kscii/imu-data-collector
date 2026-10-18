# Synthetic motion review

Synthetic motion is an optional annotation-platform integration. An independent
producer publishes motion candidates, replay assets, and six-axis synthetic IMU.
This repository reviews those artifacts; it does not decode motion-capture sources,
run simulation, or perform heavy synthetic HDF5 construction.

## Candidate publication

Candidates must be completely published and pass machine QA before entering review.
A publication manifest freezes candidate/version identity, source/model/license
references, SI channel definitions, coordinate frame, sample rate, time origin,
mount/selection information, file sizes, and hashes. Data objects are verified before
the immutable commit marker. Incremental receipts accelerate discovery but do not
replace publication validation; full reconciliation remains necessary.

Dev and prod namespaces are separate. A local trial decision is not a production
review, and a passing machine check is not human acceptance.

## Review workflow

Open **Synthetic motion** to inspect replay and IMU traces. Quality review and formal
activity labeling are separate stages. A pass can initially lack a formal label;
a rejection needs a structured reason or explanation. The full-sequence chart offers
magnitude and six-axis views, with m/s² and rad/s on separate axes. Its cursor follows
the same motion/selection/layout as the replay.

Short task leases prevent simultaneous edits. Reviewers can skip, renew, filter, and
re-review previous decisions. Each decision is an immutable revision tied to the
candidate version and input hashes. Conflicts require refresh. Returning a candidate
to review preserves old labels/snapshots while excluding it from new snapshots until
it passes again.

**Data management** lists real and synthetic tasks with source, batch, status, risk,
and label filters. Administrators can pause new claims for a source/batch without
invalidating existing work. Indexed-candidate counts are not upload-byte progress.

## Labels

Shared activity concepts and motion-capture-only concepts have distinct scopes.
Source labels are reference information, not automatic formal labels. Administrators
can draft mappings from observed source values and validate them through replay
inspection before enabling them. Ambiguous/missing mappings require human selection;
fall labels are never automatically confirmed. Label and mapping revisions are frozen.

## Two synthetic dataset products

| Product | Eligibility | Contents |
| --- | --- | --- |
| Provisional core HDF5 3.3 | Complete publication and machine QA | Synthetic core data; no human acceptance, formal labels, replay, or models |
| Reviewed snapshot HDF5 3.3 | Latest quality decision is pass and formal labeling is complete | Immutable reviewed selection built by the compatible external worker |

The platform writes immutable snapshot requests and displays worker state/downloads.
A rejected, unreviewed, or unlabeled candidate cannot enter a new reviewed snapshot.
Existing frozen snapshots do not change after later review decisions.

Provisional datasets are listed separately. Their `candidate_index` identifies the
exact candidate/version for replay. Duration metadata must count unique motion clips,
not sum duplicate mount rows. Older manifests may only support an estimated duration.
Synthetic HDF5 **3.3** is a separate contract from real-data HDF5 **3.2**; do not pass
it to a 3.2 consumer based only on a similar extension or channel count.

## Deployment

Configure a single intended `annotation.synthetic_target` (`dev` or `prod`) and
matching `synthetic_bucket`; dev also needs `synthetic_run_id`. The producer and
snapshot worker must implement the compatible manifest/request/HDF5 contracts.
Do not switch a production installation to a trial prefix as an installation step.

The platform can browse and review valid publications independently of the worker,
but a requested snapshot needs that external worker to finish. Validate actual
publication, replay, review, worker failure recovery, and a small completed snapshot
before relying on the integration. Base platform health does not establish that the
external production pipeline is available.
