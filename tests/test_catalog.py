import sqlite3
from pathlib import Path

import pytest

from imu_data_collector.catalog import RecordingCatalog
from imu_data_collector.models import (
    BackgroundJobKind,
    BackgroundJobState,
    RecordingState,
    RecordingSummary,
)


def test_existing_catalog_rows_without_tier_are_migrated_as_safe_test(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE recordings (
                recording_id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL,
                participant_id TEXT NOT NULL,
                state TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT,
                duration_ns INTEGER,
                h5_path TEXT,
                mkv_path TEXT,
                issues_json TEXT NOT NULL DEFAULT '[]',
                upload_state TEXT NOT NULL DEFAULT 'not_requested'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO recordings (
                recording_id, collection_id, participant_id, state, started_at_utc
            ) VALUES ('old-1', 'pilot', 'xfan0282', 'ready', '2026-08-25T00:00:00Z')
            """
        )

    catalog = RecordingCatalog(path)
    summary = catalog.get("old-1")

    assert summary is not None
    assert summary.data_tier == "test"
    assert summary.index_state == "not_requested"
    assert summary.index_message == ""
    assert summary.manifest_generation is None


def test_legacy_packet_residual_issue_is_migrated_to_validation_issue(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite3"
    residual_issue = "IMU packet timestamp maximum residual exceeds 0.2 seconds"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE recordings (
                recording_id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL,
                participant_id TEXT NOT NULL,
                data_tier TEXT NOT NULL,
                state TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT,
                duration_ns INTEGER,
                h5_path TEXT,
                mkv_path TEXT,
                issues_json TEXT NOT NULL DEFAULT '[]',
                upload_state TEXT NOT NULL DEFAULT 'not_requested',
                index_state TEXT NOT NULL DEFAULT 'not_requested',
                index_message TEXT NOT NULL DEFAULT '',
                manifest_generation INTEGER
            )
            """
        )
        connection.execute(
            """
            INSERT INTO recordings (
                recording_id, collection_id, participant_id, data_tier, state,
                started_at_utc, issues_json
            ) VALUES (?, 'pilot', 'xfan0282', 'prod', 'needs_attention',
                      '2026-08-27T00:00:00Z', ?)
            """,
            ("legacy-residual", f'["{residual_issue}", "摄像头收尾失败"]'),
        )

    summary = RecordingCatalog(path).get("legacy-residual")

    assert summary is not None
    assert summary.issues == ["摄像头收尾失败"]
    assert summary.validation_issues == [residual_issue]
    assert summary.quality_warnings == []


def test_new_catalog_rows_preserve_explicit_data_tier(tmp_path: Path) -> None:
    catalog = RecordingCatalog(tmp_path / "catalog.sqlite3")
    catalog.upsert(
        RecordingSummary(
            recording_id="new-1",
            collection_id="xfan0282_test_01",
            participant_id="xfan0282",
            data_tier="test",
            state=RecordingState.READY,
            started_at_utc="2026-08-25T00:00:00Z",
        )
    )

    summary = catalog.get("new-1")

    assert summary is not None
    assert summary.data_tier == "test"


def test_upsert_refreshes_formal_recording_start(tmp_path: Path) -> None:
    catalog = RecordingCatalog(tmp_path / "catalog.sqlite3")
    initial = RecordingSummary(
        recording_id="recording-1",
        collection_id="collection-1",
        participant_id="xfan0282",
        data_tier="prod",
        state=RecordingState.ARMING,
        started_at_utc="2026-08-27T01:41:21.043236+00:00",
    )
    catalog.upsert(initial)
    catalog.upsert(
        initial.model_copy(
            update={
                "state": RecordingState.RECORDING,
                "started_at_utc": "2026-08-27T01:41:21.351792+00:00",
            }
        )
    )

    summary = catalog.get("recording-1")

    assert summary is not None
    assert summary.started_at_utc == "2026-08-27T01:41:21.351792+00:00"


def test_background_job_retries_are_persistent_and_bounded(tmp_path: Path) -> None:
    catalog = RecordingCatalog(tmp_path / "catalog.sqlite3")
    summary = RecordingSummary(
        recording_id="retry-1",
        collection_id="collection-1",
        participant_id="xfan0282",
        data_tier="prod",
        state=RecordingState.FINALIZING,
        started_at_utc="2026-08-27T00:00:00Z",
    )
    catalog.upsert(summary)
    catalog.enqueue_job(
        summary.recording_id,
        BackgroundJobKind.FINALIZE,
        {"source": "test"},
        max_attempts=2,
    )

    first = catalog.claim_next_job()
    assert first is not None
    assert first[1].attempts == 1
    assert first[2] == {"source": "test"}
    waiting = catalog.fail_job(
        summary.recording_id,
        BackgroundJobKind.FINALIZE,
        "temporary failure",
        (0,),
    )
    assert waiting.state == BackgroundJobState.RETRY_WAIT

    second = catalog.claim_next_job()
    assert second is not None
    assert second[1].attempts == 2
    failed = catalog.fail_job(
        summary.recording_id,
        BackgroundJobKind.FINALIZE,
        "permanent failure",
        (0,),
    )
    assert failed.state == BackgroundJobState.FAILED
    assert catalog.claim_next_job() is None


def test_final_paths_and_job_completion_commit_atomically(tmp_path: Path) -> None:
    catalog = RecordingCatalog(tmp_path / "catalog.sqlite3")
    partial = RecordingSummary(
        recording_id="atomic-1",
        collection_id="collection-1",
        participant_id="xfan0282",
        data_tier="prod",
        state=RecordingState.FINALIZING,
        started_at_utc="2026-08-27T00:00:00Z",
        h5_path="/data/atomic-1.partial.h5",
        mkv_path="/data/atomic-1.partial.mkv",
    )
    catalog.upsert(partial)
    catalog.enqueue_job(partial.recording_id, BackgroundJobKind.FINALIZE)

    not_running = partial.model_copy(
        update={
            "state": RecordingState.READY,
            "h5_path": "/data/atomic-1.h5",
            "mkv_path": "/data/atomic-1.mkv",
        }
    )
    with pytest.raises(RuntimeError, match="任务状态已变化"):
        catalog.commit_finalization(not_running)
    unchanged = catalog.get(partial.recording_id)
    assert unchanged is not None
    assert unchanged.h5_path == partial.h5_path
    assert unchanged.finalization_job is not None
    assert unchanged.finalization_job.state == BackgroundJobState.QUEUED

    assert catalog.claim_next_job() is not None
    catalog.commit_finalization(not_running)
    completed = catalog.get(partial.recording_id)
    assert completed is not None
    assert completed.state == RecordingState.READY
    assert completed.h5_path == "/data/atomic-1.h5"
    assert completed.finalization_job is not None
    assert completed.finalization_job.state == BackgroundJobState.SUCCEEDED
