import sqlite3
from pathlib import Path

from imu_data_collector.catalog import RecordingCatalog
from imu_data_collector.models import RecordingState, RecordingSummary


def test_existing_catalog_rows_are_migrated_as_legacy_unclassified(
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
    assert summary.data_tier == "legacy_unclassified"
    assert summary.index_state == "not_requested"
    assert summary.index_message == ""
    assert summary.manifest_generation is None


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
