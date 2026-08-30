"""后端接口与存储层共用的 API 和标注数据模型。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UNIKEY_RE = re.compile(r"^[a-z][a-z0-9]{2,31}$")
TaxonomyChangeOperation = Literal["seed", "create", "update", "delete", "migrate"]


class RecordingState(StrEnum):
    IDLE = "idle"
    ARMING = "arming"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    READY = "ready"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


class DeviceSessionState(StrEnum):
    """独立于录制生命周期的本机硬件会话状态。"""

    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    RELEASING = "releasing"
    ERROR = "error"


class ReviewWorkflowState(StrEnum):
    """标注快照的当前工作流状态；完成即已经具有有效训练导出。"""

    UNASSIGNED = "unassigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class DataTier(StrEnum):
    """录制开始时确定的数据用途分级。"""

    TEST = "test"
    PROD = "prod"


class PublishState(StrEnum):
    """采集制品从本机交付到标注存储的状态。"""

    NOT_REQUESTED = "not_requested"
    STORED_LOCAL = "stored_local"
    AUTH_REQUIRED = "auth_required"
    QUEUED = "queued"
    PACKAGING = "packaging"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    UPLOADED = "uploaded"
    PUBLISHED = "published"
    RETRY_WAIT = "retry_wait"
    VERIFIED = "verified"
    FAILED = "failed"


class PublishTarget(StrEnum):
    """发布目的地必须显式配置，禁止把本地对象目录冒充团队 Bucket。"""

    DISABLED = "disabled"
    LOCAL = "local"
    BROKER = "broker"
    DIRECT_GCS = "direct_gcs"


class BackgroundJobKind(StrEnum):
    """本机持久化后台任务类型。"""

    FINALIZE = "finalize"
    PUBLISH = "publish"


class BackgroundJobState(StrEnum):
    """后台任务只保存当前状态，不累积无界历史。"""

    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    WAITING_AUTH = "waiting_auth"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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
    data_tier: DataTier = DataTier.PROD
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
    imu_local_device_id: str | None = Field(default=None, max_length=128)


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


class ActivityTaxonomyEntry(BaseModel):
    """以稳定 code 标识、以可修改 name 展示的活动标签。"""

    model_config = ConfigDict(extra="ignore")
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=80)
    active: bool = True

    @model_validator(mode="before")
    @classmethod
    def fill_legacy_name(cls, value: object) -> object:
        """旧 taxonomy 没有 name 时用 code 显示，但不改写历史对象。"""

        if isinstance(value, dict) and not str(value.get("name", "")).strip():
            return {**value, "name": value.get("code", "")}
        return value

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()


class ActivityTaxonomyChange(BaseModel):
    """创建一个不可变 taxonomy 版本的团队操作。"""

    actor_unikey: str = Field(pattern=r"^[a-z][a-z0-9]{2,31}$")
    changed_at_utc: datetime
    operation: TaxonomyChangeOperation
    source_code: str | None = Field(
        default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    target_code: str | None = Field(
        default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )

    @field_validator("changed_at_utc")
    @classmethod
    def normalize_changed_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("changed_at_utc must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_codes(self) -> ActivityTaxonomyChange:
        if self.operation != "seed" and self.source_code is None:
            raise ValueError("taxonomy change requires source_code")
        if self.operation == "migrate" and self.target_code is None:
            raise ValueError("taxonomy migration change requires target_code")
        if self.operation != "migrate" and self.target_code is not None:
            raise ValueError("target_code is only valid for taxonomy migration")
        return self


class ActivityTaxonomyDefinition(BaseModel):
    """一个不可变的活动分类表版本。"""

    taxonomy_id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    revision: int = Field(default=1, ge=1)
    fall: list[ActivityTaxonomyEntry] = Field(min_length=1)
    non_fall: list[ActivityTaxonomyEntry] = Field(min_length=1)
    change: ActivityTaxonomyChange | None = None

    @model_validator(mode="after")
    def validate_unique_codes(self) -> ActivityTaxonomyDefinition:
        codes = [item.code for item in (*self.fall, *self.non_fall)]
        if len(codes) != len(set(codes)):
            raise ValueError("activity taxonomy codes must be unique")
        return self


class ActivityTaxonomyCreateRequest(BaseModel):
    expected_version: str = Field(min_length=1, max_length=80)
    binary_label: BinaryLabel
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()


class ActivityTaxonomyUpdateRequest(BaseModel):
    expected_version: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    active: bool | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def validate_change(self) -> ActivityTaxonomyUpdateRequest:
        if self.name is None and self.active is None:
            raise ValueError("taxonomy update requires at least one change")
        return self


class ActivityTaxonomyMigrationPreviewRequest(BaseModel):
    expected_version: str = Field(min_length=1, max_length=80)
    source_code: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )
    target_code: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"
    )


class ActivityTaxonomyMigrationApplyRequest(
    ActivityTaxonomyMigrationPreviewRequest
):
    plan_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: str = Field(min_length=1, max_length=160)


class SyncAnchor(BaseModel):
    imu_time_ns: int = Field(ge=0)
    video_time_ns: int = Field(ge=0)
    label: str = Field(default="tap", max_length=64)
    role: Literal["start_tap", "end_tap"]
    source_video_frame: int | None = Field(default=None, ge=0)
    source_imu_sample: int | None = Field(default=None, ge=0)
    video_interval_start_ns: int | None = Field(default=None, ge=0)
    imu_interval_start_ns: int | None = Field(default=None, ge=0)
    reviewer_id: str | None = None

    @model_validator(mode="after")
    def validate_sync_anchor(self) -> SyncAnchor:
        required = {
            "source_video_frame": self.source_video_frame,
            "source_imu_sample": self.source_imu_sample,
            "video_interval_start_ns": self.video_interval_start_ns,
            "imu_interval_start_ns": self.imu_interval_start_ns,
            "reviewer_id": self.reviewer_id,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError("正式同步锚点缺少来源字段：" + ", ".join(missing))
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


class TrainingExportReference(BaseModel):
    """当前 review 唯一有效的不可变训练导出。"""

    export_schema_version: Literal["1.0.0", "2.0.0"] = "1.0.0"
    hdf5_schema_version: str = "3.0.0"
    sampling_rate_hz: float = 30.0
    filename: str = "aligned30.h5"
    source_review_revision: int = Field(ge=0)
    object_key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    calibration_profile_id: str = Field(min_length=1, max_length=160)
    calibration_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at_utc: str

    @model_validator(mode="after")
    def validate_export_contract(self) -> TrainingExportReference:
        if self.sampling_rate_hz not in {25.0, 30.0}:
            raise ValueError("训练导出采样率必须为 25 Hz 或历史 30 Hz")
        if Path(self.filename).name != self.filename or not self.filename.endswith(".h5"):
            raise ValueError("训练导出 filename 无效")
        return self


class ReviewWorkflow(BaseModel):
    state: ReviewWorkflowState = ReviewWorkflowState.UNASSIGNED
    annotator_id: str | None = None
    last_editor_id: str | None = None
    updated_at_utc: str | None = None

    @model_validator(mode="after")
    def validate_ids(self) -> ReviewWorkflow:
        for field_name in ("annotator_id", "last_editor_id"):
            value = getattr(self, field_name)
            if value is not None and not UNIKEY_RE.fullmatch(value):
                raise ValueError(f"{field_name} must be a lowercase UniKey-like identifier")
        return self


class ReviewDocument(BaseModel):
    """原始 H5/MKV 之外唯一可变的同步与标注快照。"""

    schema_version: Literal["2.0.0"] = "2.0.0"
    recording_id: str
    revision: int = Field(default=0, ge=0)
    sources: list[SourceArtifact]
    sync: SyncDocument
    annotations: AnnotationDocument
    workflow: ReviewWorkflow = Field(default_factory=ReviewWorkflow)
    active_export: TrainingExportReference | None = None


class ReviewWorkflowRequest(BaseModel):
    action: Literal["assign", "complete", "reopen"]
    actor_id: str
    expected_revision: int = Field(ge=0)
    comment: str = Field(default="", max_length=2000)


class AnnotationReviewWorkflowRequest(BaseModel):
    """公网标注端请求；操作者只能由服务端登录会话提供。"""

    action: Literal["assign", "complete", "reopen"]
    expected_revision: int = Field(ge=0)
    comment: str = Field(default="", max_length=2000)


class RecordingDeleteRequest(BaseModel):
    confirmation: str


class AnnotationRecordingDeleteRequest(BaseModel):
    confirmation: str


class TrainingSnapshotDeleteRequest(BaseModel):
    confirmation: str


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class AnnotationSaveRequest(BaseModel):
    """带 review 乐观锁的正式标注保存请求。"""

    expected_revision: int = Field(ge=0)
    document: AnnotationDocument


class SyncSaveRequest(BaseModel):
    """带 review 乐观锁的同步草稿或正式同步保存请求。"""

    expected_revision: int = Field(ge=0)
    document: SyncDocument


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


class BackgroundJobStatus(BaseModel):
    """面向 API 和前端的后台任务当前快照。"""

    kind: BackgroundJobKind
    state: BackgroundJobState
    phase: str = "queued"
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=4, ge=1)
    next_attempt_at_utc: str | None = None
    last_error: str | None = None
    progress_bytes: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    created_at_utc: str
    updated_at_utc: str


class RecordingSummary(BaseModel):
    recording_id: str
    collection_id: str
    participant_id: str
    data_tier: DataTier
    state: RecordingState
    started_at_utc: str
    ended_at_utc: str | None = None
    duration_ns: int | None = None
    h5_path: str | None = None
    mkv_path: str | None = None
    # issues 只保存录制/收尾阶段的运行故障；验证器结论单独保存，
    # 否则调整质量策略时无法安全重评而不误删真实故障。
    issues: list[str] = Field(default_factory=list)
    validation_issues: list[str] = Field(default_factory=list)
    quality_warnings: list[str] = Field(default_factory=list)
    upload_state: PublishState = PublishState.NOT_REQUESTED
    publish_target: PublishTarget = PublishTarget.DISABLED
    index_state: Literal["not_requested", "pending", "indexed", "rejected"] = (
        "not_requested"
    )
    index_message: str = ""
    manifest_generation: int | None = None
    finalization_job: BackgroundJobStatus | None = None
    upload_job: BackgroundJobStatus | None = None


class ArtifactDescriptor(BaseModel):
    """不可变采集制品在对象存储中的身份。"""

    role: Literal["capture_h5", "video_mkv", "preview_mp4"]
    object_key: str = Field(min_length=1, max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(min_length=1, max_length=128)


class CalibrationProfile(BaseModel):
    """录制时冻结的设备专属校准档案；未验证时禁止训练导出。"""

    profile_id: str = "unverified"
    verified: bool = False
    accel_counts_per_g: float | None = Field(default=None, gt=0)
    gyro_counts_per_dps: float | None = Field(default=None, gt=0)
    accel_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gyro_bias_counts: tuple[float, float, float] = (0.0, 0.0, 0.0)
    raw_axis_order: tuple[int, int, int] = (0, 1, 2)
    axis_signs: tuple[Literal[-1, 1], Literal[-1, 1], Literal[-1, 1]] = (1, 1, 1)
    method: str = "unverified"
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_transform(self) -> CalibrationProfile:
        if sorted(self.raw_axis_order) != [0, 1, 2]:
            raise ValueError("raw_axis_order 必须是 0、1、2 的排列")
        if self.verified and (
            self.profile_id == "unverified"
            or self.accel_counts_per_g is None
            or self.gyro_counts_per_dps is None
        ):
            raise ValueError("已验证校准必须具有档案 ID 和两个物理尺度")
        return self


class CaptureManifestV2(BaseModel):
    """采集端与标注端之间唯一稳定的公开交接合同。"""

    schema_version: Literal["2.1.0"] = "2.1.0"
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


class AnnotationCapabilities(BaseModel):
    """标注端写入 Bucket、供采集端在上传前读取的能力合同。"""

    schema_version: Literal["1.0.0"] = "1.0.0"
    accepted_manifest_schema_versions: list[str]
    accepted_capture_h5_schema_versions: list[str]
    annotation_build_id: str
    generated_at_utc: str


class IndexReceipt(BaseModel):
    """一个 manifest 被标注端实际接收或拒绝的可查询回执。"""

    schema_version: Literal["1.0.0"] = "1.0.0"
    recording_id: str
    manifest_generation: int
    status: Literal["indexed", "rejected"]
    annotation_build_id: str
    processed_at_utc: str
    code: str = "indexed"
    message: str = ""


class IndexRefreshIssue(BaseModel):
    recording_id: str
    manifest_key: str
    stage: str
    code: str
    message: str


class IndexRefreshResult(BaseModel):
    imported: int = 0
    unchanged: int = 0
    skipped: int = 0
    issues: list[IndexRefreshIssue] = Field(default_factory=list)


class PublishStatus(BaseModel):
    recording_id: str
    state: PublishState = PublishState.NOT_REQUESTED
    message: str = ""
    completed_artifacts: int = Field(default=0, ge=0, le=3)
