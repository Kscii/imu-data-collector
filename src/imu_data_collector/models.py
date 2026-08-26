"""后端接口与存储层共用的 API 和标注数据模型。"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

UNIKEY_RE = re.compile(r"^[a-z][a-z0-9]{2,31}$")


class RecordingState(StrEnum):
    IDLE = "idle"
    ARMING = "arming"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    READY = "ready"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


class ReviewWorkflowState(StrEnum):
    """标注快照的当前工作流状态；不保存逐次修订历史。"""

    UNASSIGNED = "unassigned"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    EXPORTED = "exported"


class DataTier(StrEnum):
    """录制开始时确定的数据用途分级。"""

    TEST = "test"
    PROD = "prod"


class PublishState(StrEnum):
    """采集制品从本机交付到标注存储的状态。"""

    NOT_REQUESTED = "not_requested"
    PACKAGING = "packaging"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    PUBLISHED = "published"
    FAILED = "failed"


class ReviewPolicy(StrEnum):
    """标注完成后是否强制由另一名用户审核。"""

    SINGLE_USER = "single_user"
    TWO_PERSON = "two_person"


class BinaryLabel(StrEnum):
    FALL = "fall"
    NON_FALL = "non_fall"


class EventKind(StrEnum):
    ONSET = "onset"
    IMPACT = "impact"


class ExclusionReason(StrEnum):
    """已人工复核、但明确禁止进入训练的区间原因。"""

    SYNC_TAP = "sync_tap"
    SETUP = "setup"
    SENSOR_ADJUSTMENT = "sensor_adjustment"
    SENSOR_REMOVED = "sensor_removed"
    QUALITY_ISSUE = "quality_issue"
    AMBIGUOUS = "ambiguous"
    PRIVACY = "privacy"
    OTHER = "other"


class RecordingStartRequest(BaseModel):
    collection_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    participant_id: str
    data_tier: DataTier = DataTier.TEST
    body_location: str = "chest"
    protocol_id: str = "fall_binary_v1"
    camera_id: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_identity(self) -> RecordingStartRequest:
        if not UNIKEY_RE.fullmatch(self.participant_id):
            raise ValueError("participant_id must be a lowercase UniKey-like identifier")
        if self.body_location != "chest":
            raise ValueError("v1 supports chest placement only")
        return self


class PreviewStartRequest(BaseModel):
    camera_id: str | None = Field(default=None, max_length=512)


class ActivitySegment(BaseModel):
    segment_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    start_ns: int = Field(ge=0)
    end_ns: int = Field(gt=0)
    binary_label: BinaryLabel
    activity_code: str = Field(min_length=1, max_length=64)
    annotator_id: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_bounds_and_annotator(self) -> ActivitySegment:
        if self.end_ns <= self.start_ns:
            raise ValueError("segment must satisfy start_ns < end_ns")
        if not UNIKEY_RE.fullmatch(self.annotator_id):
            raise ValueError("annotator_id must be a lowercase UniKey-like identifier")
        return self


class AnnotationEvent(BaseModel):
    segment_id: str
    kind: EventKind
    time_ns: int = Field(ge=0)
    source_video_frame: int | None = Field(default=None, ge=0)
    source_imu_sample: int | None = Field(default=None, ge=0)
    annotator_id: str

    @model_validator(mode="after")
    def validate_annotator(self) -> AnnotationEvent:
        if not UNIKEY_RE.fullmatch(self.annotator_id):
            raise ValueError("annotator_id must be a lowercase UniKey-like identifier")
        return self


class ExclusionInterval(BaseModel):
    exclusion_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$"
    )
    start_ns: int = Field(ge=0)
    end_ns: int = Field(gt=0)
    reason: ExclusionReason
    annotator_id: str
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_bounds_and_annotator(self) -> ExclusionInterval:
        if self.end_ns <= self.start_ns:
            raise ValueError("exclusion must satisfy start_ns < end_ns")
        if not UNIKEY_RE.fullmatch(self.annotator_id):
            raise ValueError("annotator_id must be a lowercase UniKey-like identifier")
        return self


class AnnotationDocument(BaseModel):
    taxonomy_id: str
    taxonomy_version: str
    revision: int = Field(default=1, ge=1)
    finalized: bool = False
    segments: list[ActivitySegment] = Field(default_factory=list)
    events: list[AnnotationEvent] = Field(default_factory=list)
    exclusions: list[ExclusionInterval] = Field(default_factory=list)


class SyncAnchor(BaseModel):
    imu_time_ns: int = Field(ge=0)
    video_time_ns: int = Field(ge=0)
    label: str = Field(default="tap", max_length=64)
    role: Literal["start_tap", "end_tap", "legacy"] = "legacy"
    source_video_frame: int | None = Field(default=None, ge=0)
    source_imu_sample: int | None = Field(default=None, ge=0)
    video_interval_start_ns: int | None = Field(default=None, ge=0)
    imu_interval_start_ns: int | None = Field(default=None, ge=0)
    reviewer_id: str | None = None

    @model_validator(mode="after")
    def validate_sync_anchor(self) -> SyncAnchor:
        if self.role != "legacy":
            required = {
                "source_video_frame": self.source_video_frame,
                "source_imu_sample": self.source_imu_sample,
                "video_interval_start_ns": self.video_interval_start_ns,
                "imu_interval_start_ns": self.imu_interval_start_ns,
                "reviewer_id": self.reviewer_id,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(
                    "正式同步锚点缺少来源字段：" + ", ".join(missing)
                )
        if self.video_interval_start_ns is not None:
            if self.video_interval_start_ns > self.video_time_ns:
                raise ValueError("video interval start must not exceed selected frame time")
        if self.imu_interval_start_ns is not None:
            if self.imu_interval_start_ns > self.imu_time_ns:
                raise ValueError("IMU interval start must not exceed selected sample time")
        if self.reviewer_id is not None and not UNIKEY_RE.fullmatch(self.reviewer_id):
            raise ValueError("reviewer_id must be a lowercase UniKey-like identifier")
        return self


class SyncDocument(BaseModel):
    anchors: list[SyncAnchor] = Field(default_factory=list)
    policy: Literal["conditional_fixed_offset_v1"] = "conditional_fixed_offset_v1"
    apply_fixed_offset: bool = False
    reviewer_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0, exclude=True)

    @model_validator(mode="after")
    def validate_reviewer(self) -> SyncDocument:
        if self.reviewer_id is not None and not UNIKEY_RE.fullmatch(self.reviewer_id):
            raise ValueError("reviewer_id must be a lowercase UniKey-like identifier")
        return self


class SourceArtifact(BaseModel):
    role: Literal["capture_h5", "video_mkv"]
    filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewWorkflow(BaseModel):
    state: ReviewWorkflowState = ReviewWorkflowState.UNASSIGNED
    annotator_id: str | None = None
    reviewer_id: str | None = None
    review_comment: str = Field(default="", max_length=2000)
    updated_at_utc: str | None = None
    review_policy: ReviewPolicy = ReviewPolicy.TWO_PERSON

    @model_validator(mode="after")
    def validate_ids(self) -> ReviewWorkflow:
        for field_name in ("annotator_id", "reviewer_id"):
            value = getattr(self, field_name)
            if value is not None and not UNIKEY_RE.fullmatch(value):
                raise ValueError(f"{field_name} must be a lowercase UniKey-like identifier")
        return self


class ReviewDocument(BaseModel):
    """原始 H5/MKV 之外唯一可变的同步、标注与审核快照。"""

    schema_version: Literal["1.0.0"] = "1.0.0"
    recording_id: str
    revision: int = Field(default=0, ge=0)
    sources: list[SourceArtifact]
    sync: SyncDocument
    annotations: AnnotationDocument
    workflow: ReviewWorkflow = Field(default_factory=ReviewWorkflow)


class ReviewWorkflowRequest(BaseModel):
    action: Literal["assign", "submit", "accept", "reject", "reopen", "mark_exported"]
    actor_id: str
    expected_revision: int = Field(ge=0)
    comment: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_actor(self) -> ReviewWorkflowRequest:
        if not UNIKEY_RE.fullmatch(self.actor_id):
            raise ValueError("actor_id must be a lowercase UniKey-like identifier")
        return self


class RecordingDeleteRequest(BaseModel):
    confirmation: str


class AnnotationRecordingDeleteRequest(BaseModel):
    actor_id: str
    confirmation: str

    @model_validator(mode="after")
    def validate_actor(self) -> AnnotationRecordingDeleteRequest:
        if not UNIKEY_RE.fullmatch(self.actor_id):
            raise ValueError("actor_id must be a lowercase UniKey-like identifier")
        return self


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class QuarantineRequest(BaseModel):
    relative_path: str = Field(min_length=1, max_length=1000)


class SyncObservation(BaseModel):
    """人工确认的同一物理轻拍在视频与 IMU 中的对应位置。"""

    observation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9._-]+$",
    )
    recording_id: str = Field(min_length=1, max_length=128)
    video_frame_index: int = Field(ge=0)
    video_time_ns: int = Field(ge=0)
    imu_sample_index: int = Field(ge=0)
    imu_time_ns: int = Field(ge=0)
    label: str = Field(default="tap", min_length=1, max_length=64)
    reviewer_id: str
    notes: str = Field(default="", max_length=2000)
    selection_mode: Literal[
        "legacy_manual", "auto_recommended", "manual_candidate"
    ] = "legacy_manual"
    recommendation_algorithm: str | None = Field(default=None, max_length=64)
    recommended_sample_index: int | None = Field(default=None, ge=0)
    recommendation_confidence: Literal["high", "medium", "low"] | None = None
    candidate_strength_rank: int | None = Field(default=None, ge=1)
    candidate_score: float | None = Field(default=None, ge=0)
    expected_video_minus_imu_ns: int | None = None

    @model_validator(mode="after")
    def validate_reviewer(self) -> SyncObservation:
        if not UNIKEY_RE.fullmatch(self.reviewer_id):
            raise ValueError("reviewer_id must be a lowercase UniKey-like identifier")
        return self


class SyncExperimentSource(BaseModel):
    """同步实验引用的不可变原始录制及其校验信息。"""

    recording_id: str
    h5_path: str
    mkv_path: str
    h5_size_bytes: int = Field(ge=0)
    mkv_size_bytes: int = Field(ge=0)
    h5_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mkv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SyncExperimentDocument(BaseModel):
    """独立于正式同步模型的实验观察记录。"""

    schema_version: Literal["1.0.0"] = "1.0.0"
    experiment_id: str = Field(
        default="sync_validation_01",
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9._-]+$",
    )
    revision: int = Field(default=0, ge=0)
    updated_at_utc: str | None = None
    observations: list[SyncObservation] = Field(default_factory=list)
    sources: list[SyncExperimentSource] = Field(default_factory=list)


class CharacterizationStage(StrEnum):
    PIPELINE_SMOKE_UNCONTROLLED = "pipeline_smoke_uncontrolled"
    LONG_STATIC_BUTTON_UP = "long_static_button_up"
    BUTTON_FACE_UP = "button_face_up"
    BUTTON_FACE_DOWN = "button_face_down"
    INTERFACE_FACE_UP = "interface_face_up"
    INTERFACE_OPPOSITE_FACE_UP = "interface_opposite_face_up"
    PENDANT_END_UP_EXPLORATORY = "pendant_end_up_exploratory"
    PENDANT_END_DOWN_EXPLORATORY = "pendant_end_down_exploratory"
    GYRO_X_POSITIVE = "gyro_x_positive"
    GYRO_X_NEGATIVE = "gyro_x_negative"
    GYRO_Y_POSITIVE = "gyro_y_positive"
    GYRO_Y_NEGATIVE = "gyro_y_negative"
    GYRO_Z_POSITIVE = "gyro_z_positive"
    GYRO_Z_NEGATIVE = "gyro_z_negative"


class CharacterizationStartRequest(BaseModel):
    operator_id: str
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_operator(self) -> CharacterizationStartRequest:
        if not UNIKEY_RE.fullmatch(self.operator_id):
            raise ValueError("operator_id must be a lowercase UniKey-like identifier")
        return self


class CharacterizationStageRequest(BaseModel):
    stage_code: CharacterizationStage
    notes: str = Field(default="", max_length=2000)


class RecordingSummary(BaseModel):
    recording_id: str
    collection_id: str
    participant_id: str
    data_tier: DataTier | Literal["legacy_unclassified"] = "legacy_unclassified"
    state: RecordingState
    started_at_utc: str
    ended_at_utc: str | None = None
    duration_ns: int | None = None
    h5_path: str | None = None
    mkv_path: str | None = None
    issues: list[str] = Field(default_factory=list)
    upload_state: str = "not_requested"


class ArtifactDescriptor(BaseModel):
    """不可变采集制品在对象存储中的身份。"""

    role: Literal["capture_h5", "video_mkv", "preview_mp4"]
    object_key: str = Field(min_length=1, max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(min_length=1, max_length=128)


class CalibrationProfile(BaseModel):
    """录制时生效的物理尺度配置；未验证时禁止训练导出。"""

    profile_id: str = "unverified"
    verified: bool = False
    accel_counts_per_g: float | None = Field(default=None, gt=0)
    gyro_counts_per_dps: float | None = Field(default=None, gt=0)


class CaptureManifestV2(BaseModel):
    """采集端与标注端之间唯一稳定的公开交接合同。"""

    schema_version: Literal["2.0.0"] = "2.0.0"
    recording_id: str
    collection_id: str
    participant_id: str
    data_tier: DataTier
    body_location: Literal["chest"] = "chest"
    captured_at_utc: str
    duration_ns: int = Field(gt=0)
    source_h5_schema_version: str
    software_revision: str
    calibration: CalibrationProfile = Field(default_factory=CalibrationProfile)
    artifacts: list[ArtifactDescriptor]

    @model_validator(mode="after")
    def validate_artifacts(self) -> CaptureManifestV2:
        roles = [item.role for item in self.artifacts]
        required = {"capture_h5", "video_mkv", "preview_mp4"}
        if set(roles) != required or len(roles) != len(required):
            raise ValueError("manifest 必须且只能包含 H5、MKV 和 MP4 三种制品")
        prefix = f"captures/{self.recording_id}/"
        if any(not item.object_key.startswith(prefix) for item in self.artifacts):
            raise ValueError("manifest 制品对象键必须位于本录制前缀")
        return self


class PublishStatus(BaseModel):
    recording_id: str
    state: PublishState = PublishState.NOT_REQUESTED
    message: str = ""
    completed_artifacts: int = Field(default=0, ge=0, le=3)
