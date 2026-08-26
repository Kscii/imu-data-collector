import json
from pathlib import Path

import h5py
import numpy as np
from fastapi.testclient import TestClient

from imu_data_collector.api import create_app
from imu_data_collector.config import Settings
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    DataTier,
    RecordingState,
    RecordingSummary,
    SyncExperimentDocument,
    SyncObservation,
)
from imu_data_collector.sync_experiment import (
    analyze_sync_experiment,
    load_sync_experiment,
    read_frame_times,
    read_sync_window,
    save_sync_experiment,
    sync_experiment_path,
    write_sync_experiment_report,
)


def _write_capture(path: Path, recording_id: str, offset_ns: int = 0) -> Path:
    mkv = path.with_suffix(".mkv")
    mkv.write_bytes(b"test-video-" + recording_id.encode())
    with h5py.File(path, "w") as handle:
        handle.attrs["recording_start_monotonic_ns"] = 10_000_000_000
        imu = handle.create_group("imu")
        samples = imu.create_group("samples")
        sample_time = 10_000_000_000 + np.arange(100, dtype=np.int64) * 40_000_000
        samples.create_dataset("time_monotonic_ns", data=sample_time)
        raw = np.zeros((100, 6), dtype=np.int16)
        raw[50, 0] = 5000
        samples.create_dataset("raw_counts", data=raw)
        trailer = np.arange(100, dtype=">u4").view(np.uint8).reshape(-1, 4)
        samples.create_dataset("trailer", data=trailer)
        samples.create_dataset("packet_index", data=np.arange(100) // 25)
        samples.create_dataset("sample_in_packet", data=np.arange(100) % 25)
        video = handle.create_group("video")
        frames = video.create_group("frames")
        video_time = np.arange(120, dtype=np.int64) * 33_333_333 + offset_ns
        frames.create_dataset("recording_time_ns", data=video_time)
        frames.create_dataset(
            "duration_ns", data=np.full(120, 33_333_333, dtype=np.int64)
        )
        frames.create_dataset("key_frame", data=np.arange(120) % 60 == 0)
    return mkv


def test_frame_table_and_sync_window_use_host_reconstructed_time(tmp_path: Path) -> None:
    h5_path = tmp_path / "capture.h5"
    _write_capture(h5_path, "capture")

    table = read_frame_times(h5_path)
    assert table["frame_count"] == 120
    assert table["time_ns"][3] == 99_999_999
    assert table["media_time_ns"][3] == 99_999_999

    window = read_sync_window(h5_path, frame_index=60, radius_seconds=0.5)
    assert window["video_frame_index"] == 60
    assert window["candidate_sample_index"] == [50]
    assert window["recommendation"]["sample_index"] == 50
    assert window["candidate_peaks"][0]["event_robust_z"] > 4
    chosen = window["sample_index"].index(50)
    assert window["time_ns"][chosen] == 2_000_000_000
    assert window["raw_counts"][chosen][0] == 5000


def test_sync_candidate_prefers_first_clear_response_over_largest_peak(
    tmp_path: Path,
) -> None:
    h5_path = tmp_path / "capture.h5"
    _write_capture(h5_path, "capture")

    with h5py.File(h5_path, "r+") as handle:
        raw = handle["imu/samples/raw_counts"]
        raw[48, 0] = 400
        raw[49, 0] = 1800
        raw[50, 0] = 5000

    # 即使时间先验落在峰后，正式候选仍必须优先同一冲击簇的首个明显响应。
    window = read_sync_window(
        h5_path,
        frame_index=62,
        radius_seconds=0.5,
        expected_video_minus_imu_ns=-13_333_354,
    )

    assert window["recommendation"]["sample_index"] == 48
    onset = next(
        item for item in window["candidate_peaks"] if item["sample_index"] == 48
    )
    assert onset["selection_basis"] == "event_onset"
    assert onset["event_robust_z"] > 4


def test_sync_experiment_is_atomic_and_enriches_exact_times(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    h5_path = tmp_path / "capture.h5"
    mkv_path = _write_capture(h5_path, "capture")
    document = SyncExperimentDocument(
        observations=[
            SyncObservation(
                observation_id="capture_tap_01",
                recording_id="capture",
                video_frame_index=60,
                video_time_ns=1,
                imu_sample_index=50,
                imu_time_ns=1,
                reviewer_id="xfan0282",
            )
        ]
    )

    saved = save_sync_experiment(
        data_root,
        document,
        {"capture": (h5_path, mkv_path)},
    )

    assert saved.revision == 1
    assert saved.observations[0].video_time_ns == 1_999_999_980
    assert saved.observations[0].imu_time_ns == 2_000_000_000
    assert saved.sources[0].h5_sha256 == sha256_file(h5_path)
    reloaded = load_sync_experiment(data_root, "sync_validation_01")
    assert reloaded == saved
    assert sync_experiment_path(data_root, "sync_validation_01").is_file()


def test_sync_experiment_api_round_trip(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    h5_path = tmp_path / "capture.h5"
    mkv_path = _write_capture(h5_path, "capture")
    settings = Settings(
        data_root=data_root,
        catalog_path=tmp_path / "catalog.sqlite3",
        activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
    )
    app = create_app(settings)
    app.state.coordinator.catalog.upsert(
        RecordingSummary(
            recording_id="capture",
            collection_id="sync_validation_01",
            participant_id="xfan0282",
            data_tier=DataTier.TEST,
            state=RecordingState.NEEDS_ATTENTION,
            started_at_utc="2026-08-26T00:00:00+00:00",
            duration_ns=4_000_000_000,
            h5_path=str(h5_path),
            mkv_path=str(mkv_path),
        )
    )
    payload = SyncExperimentDocument(
        observations=[
            SyncObservation(
                observation_id="capture_tap_01",
                recording_id="capture",
                video_frame_index=60,
                video_time_ns=0,
                imu_sample_index=50,
                imu_time_ns=0,
                reviewer_id="xfan0282",
            )
        ]
    ).model_dump(mode="json")

    with TestClient(app) as client:
        frames = client.get("/api/v1/recordings/capture/frame-times")
        window = client.get(
            "/api/v1/recordings/capture/sync-window",
            params={
                "frame_index": 60,
                "radius_seconds": 0.5,
                "expected_video_minus_imu_ns": 0,
            },
        )
        saved = client.put("/api/v1/sync-experiments/sync_validation_01", json=payload)
        loaded = client.get("/api/v1/sync-experiments/sync_validation_01")

    assert frames.status_code == 200
    assert frames.json()["frame_count"] == 120
    assert window.status_code == 200
    assert window.json()["candidate_sample_index"] == [50]
    assert window.json()["recommendation"]["sample_index"] == 50
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert loaded.json() == saved.json()


def test_analysis_compares_host_and_offset_without_changing_sources(
    tmp_path: Path,
) -> None:
    observations: list[SyncObservation] = []
    sources = []
    for recording_number, fixed_offset in ((1, 120_000_000), (2, 121_000_000)):
        recording_id = f"recording_{recording_number}"
        h5_path = tmp_path / f"{recording_id}.h5"
        mkv_path = _write_capture(h5_path, recording_id)
        sources.append(
            {
                "recording_id": recording_id,
                "h5_path": str(h5_path),
                "mkv_path": str(mkv_path),
                "h5_size_bytes": h5_path.stat().st_size,
                "mkv_size_bytes": mkv_path.stat().st_size,
                "h5_sha256": sha256_file(h5_path),
                "mkv_sha256": sha256_file(mkv_path),
            }
        )
        for anchor in range(5):
            imu_time = anchor * 1_000_000_000
            observations.append(
                SyncObservation(
                    observation_id=f"{recording_id}_tap_{anchor}",
                    recording_id=recording_id,
                    video_frame_index=anchor,
                    video_time_ns=imu_time + fixed_offset,
                    imu_sample_index=anchor,
                    imu_time_ns=imu_time,
                    reviewer_id="xfan0282",
                )
            )
    document = SyncExperimentDocument(
        revision=1,
        observations=observations,
        sources=sources,
    )
    experiment_path = tmp_path / "sync_validation_01.sync-experiment.json"
    experiment_path.write_text(
        json.dumps(document.model_dump(mode="json")), encoding="utf-8"
    )

    report = analyze_sync_experiment(experiment_path)

    host = report["timing"]["methods"]["host_only"]
    global_offset = report["timing"]["methods"][
        "global_fixed_offset_leave_one_recording_out"
    ]
    assert host["median_absolute_ms"] > 100
    assert global_offset["p95_absolute_ms"] == 1.0
    assert global_offset["meets_30fps_reference"] is True
    assert "uint32_be" in report["trailer"][
        "counter_candidates_consistent_in_all_recordings"
    ]

    json_report, markdown_report = write_sync_experiment_report(experiment_path)
    assert json_report.is_file()
    assert "主机时间，不校正" in markdown_report.read_text(encoding="utf-8")
