"""生成真实训练快照，并可调用 SOFT3888 的只读合同校验器。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import h5py
import numpy as np

from imu_data_collector.artifacts import (
    create_training_snapshot_archive,
    export_aligned,
)
from imu_data_collector.config import ImuSettings
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ActivitySegment,
    AnnotationDocument,
    AnnotationEvent,
    BinaryLabel,
    EventKind,
    ReviewDocument,
    ReviewWorkflow,
    ReviewWorkflowState,
    SourceArtifact,
    SyncAnchor,
    SyncDocument,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--soft3888-root",
        type=Path,
        default=(Path(value) if (value := os.environ.get("SOFT3888_ROOT")) else None),
        help="SOFT3888_TU16_04 checkout；提供后会运行其 validate-team",
    )
    parser.add_argument(
        "--snapshot-output",
        type=Path,
        help="可选：把生成的确定性测试快照复制到该路径，供 import-team 验收",
    )
    return parser


def main() -> None:
    arguments = _parser().parse_args()
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
                    SyncAnchor(
                        imu_time_ns=200_000_000,
                        video_time_ns=200_000_000,
                        role="start_tap",
                        source_video_frame=6,
                        source_imu_sample=5,
                        video_interval_start_ns=200_000_000,
                        imu_interval_start_ns=200_000_000,
                        reviewer_id="xfan0282",
                    ),
                    SyncAnchor(
                        imu_time_ns=1_600_000_000,
                        video_time_ns=1_600_000_000,
                        role="end_tap",
                        source_video_frame=48,
                        source_imu_sample=40,
                        video_interval_start_ns=1_600_000_000,
                        imu_interval_start_ns=1_600_000_000,
                        reviewer_id="xfan0282",
                    ),
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
                        end_ns=410_000_000,
                        binary_label=BinaryLabel.NON_FALL,
                        activity_code="walking",
                        annotator_id="xfan0282",
                    ),
                    ActivitySegment(
                        segment_id="seg_002",
                        start_ns=410_000_000,
                        end_ns=1_500_000_000,
                        binary_label=BinaryLabel.FALL,
                        activity_code="forward_fall",
                        annotator_id="xfan0282",
                    ),
                    ActivitySegment(
                        segment_id="seg_003",
                        start_ns=1_500_000_000,
                        end_ns=2_000_000_000,
                        binary_label=BinaryLabel.NON_FALL,
                        activity_code="walking",
                        annotator_id="xfan0282",
                    )
                ],
                events=[
                    AnnotationEvent(
                        segment_id="seg_002",
                        kind=EventKind.ONSET,
                        time_ns=410_000_000,
                        annotator_id="xfan0282",
                    ),
                    AnnotationEvent(
                        segment_id="seg_002",
                        kind=EventKind.IMPACT,
                        time_ns=1_000_000_000,
                        annotator_id="xfan0282",
                    ),
                ],
            ),
            workflow=ReviewWorkflow(
                state=ReviewWorkflowState.COMPLETED,
                annotator_id="xfan0282",
                last_editor_id="xfan0282",
            ),
        )
        aligned = export_aligned(
            review,
            capture,
            video,
            root / "aligned.h5",
            ImuSettings(accel_counts_per_g=4090.0, gyro_counts_per_dps=16.4),
            taxonomy,
        )
        snapshot = create_training_snapshot_archive(
            [("xfan0282", "compatibility-1", aligned)],
            root / "cw12eu_contract_snapshot.tar",
        )
        if arguments.snapshot_output is not None:
            arguments.snapshot_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot, arguments.snapshot_output)
        with h5py.File(aligned, "r") as handle:
            if (
                str(handle.attrs.get("imu_schema_version")) != "3.1.0"
                or float(handle.attrs.get("sampling_rate_hz", 0.0)) != 25.0
                or set(handle.keys()) != {"samples", "sequences", "annotations"}
            ):
                raise RuntimeError("采集端没有生成 25 Hz temporal HDF5 v3.1 三表合同")
        if arguments.soft3888_root is None:
            print(f"采集端快照合同通过：{snapshot.name}；未指定 SOFT3888 checkout")
            return
        executable = arguments.soft3888_root / ".venv/bin/imu-data"
        if not executable.is_file():
            raise FileNotFoundError(f"找不到 SOFT3888 命令：{executable}")
        completed = subprocess.run(
            [str(executable), "validate-team", "--snapshot", str(snapshot)],
            cwd=arguments.soft3888_root,
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        if result.get("sequences") != 1 or result.get("annotations", 0) < 3:
            raise RuntimeError("SOFT3888 没有完整读取 temporal v3 快照")
        print(
            "跨仓库兼容性通过："
            f"{result['sequences']} 条序列，{result['rows']} 行，"
            f"{result['annotations']} 条标注"
        )


if __name__ == "__main__":
    main()
