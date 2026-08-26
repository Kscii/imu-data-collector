"""用合成校准参数验证采集端 aligned30.h5 可被 SOFT3888 v3 直接读取。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import h5py
import numpy as np
from fall_detection.data import iter_aligned_recording_file

from imu_data_collector.artifacts import export_aligned30
from imu_data_collector.config import ImuSettings
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ActivitySegment,
    AnnotationDocument,
    BinaryLabel,
    ReviewDocument,
    ReviewWorkflow,
    ReviewWorkflowState,
    SourceArtifact,
    SyncAnchor,
    SyncDocument,
)


def main() -> None:
    taxonomy = {
        "taxonomy_id": "fall_binary_v1",
        "version": "1.0.0",
        "fall": [{"code": "forward_fall"}],
        "non_fall": [{"code": "walking"}],
    }
    with tempfile.TemporaryDirectory(prefix="imu-soft3888-") as temporary:
        root = Path(temporary)
        capture = root / "capture.h5"
        video = root / "video.mkv"
        video.write_bytes(b"synthetic-video")
        with h5py.File(capture, "w") as handle:
            handle.attrs.update(
                {
                    "recording_id": "compatibility-1",
                    "participant_id": "xfan0282",
                    "body_location": "chest",
                    "data_tier": "prod",
                    "duration_ns": 2_000_000_000,
                }
            )
            imu = handle.create_group("imu")
            imu.attrs["observed_rate_hz"] = 25.0
            samples = imu.create_group("samples")
            samples.create_dataset(
                "recording_time_ns", data=np.arange(50, dtype=np.int64) * 40_000_000
            )
            samples.create_dataset(
                "raw_counts", data=np.tile([0, 0, 4090, 0, 0, 0], (50, 1))
            )
            frames = handle.create_group("video").create_group("frames")
            frames.create_dataset(
                "recording_time_ns",
                data=np.arange(60, dtype=np.int64) * 33_333_333,
            )

        def source(path: Path, role: str) -> SourceArtifact:
            return SourceArtifact(
                role=role,
                filename=path.name,
                size_bytes=path.stat().st_size,
                sha256=sha256_file(path),
            )

        review = ReviewDocument(
            recording_id="compatibility-1",
            sources=[source(capture, "capture_h5"), source(video, "video_mkv")],
            sync=SyncDocument(
                anchors=[
                    SyncAnchor(imu_time_ns=200_000_000, video_time_ns=200_000_000),
                    SyncAnchor(imu_time_ns=1_600_000_000, video_time_ns=1_600_000_000),
                ]
            ),
            annotations=AnnotationDocument(
                taxonomy_id="fall_binary_v1",
                taxonomy_version="1.0.0",
                finalized=True,
                segments=[
                    ActivitySegment(
                        segment_id="seg_001",
                        start_ns=0,
                        end_ns=2_000_000_000,
                        binary_label=BinaryLabel.NON_FALL,
                        activity_code="walking",
                        annotator_id="xfan0282",
                    )
                ],
            ),
            workflow=ReviewWorkflow(
                state=ReviewWorkflowState.ACCEPTED,
                annotator_id="xfan0282",
                reviewer_id="rkim6933",
            ),
        )
        aligned = export_aligned30(
            review,
            capture,
            video,
            root / "aligned30.h5",
            ImuSettings(accel_counts_per_g=4090.0, gyro_counts_per_dps=16.4),
            taxonomy,
        )
        recordings = list(iter_aligned_recording_file(aligned))
        if len(recordings) != 1 or recordings[0].supervision_kind != "temporal":
            raise RuntimeError("SOFT3888 没有按 temporal v3 读取合成产物")
        print(
            f"兼容性通过：{recordings[0].recording_id}，"
            f"{len(recordings[0].values)} 行，{len(recordings[0].annotations)} 条标注"
        )


if __name__ == "__main__":
    main()
