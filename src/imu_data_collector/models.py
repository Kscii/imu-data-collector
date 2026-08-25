"""后端接口与存储层共用的 API 和标注数据模型。"""

from __future__ import annotations

import re
from enum import StrEnum

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


class BinaryLabel(StrEnum):
    FALL = "fall"
    NON_FALL = "non_fall"


class EventKind(StrEnum):
    ONSET = "onset"
    IMPACT = "impact"


class RecordingStartRequest(BaseModel):
    collection_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    participant_id: str
    body_location: str = "chest"
    protocol_id: str = "fall_binary_v1"

    @model_validator(mode="after")
    def validate_identity(self) -> RecordingStartRequest:
        if not UNIKEY_RE.fullmatch(self.participant_id):
            raise ValueError("participant_id must be a lowercase UniKey-like identifier")
        if self.body_location != "chest":
            raise ValueError("v1 supports chest placement only")
        return self


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


class AnnotationDocument(BaseModel):
    taxonomy_id: str
    taxonomy_version: str
    revision: int = Field(default=1, ge=1)
    finalized: bool = False
    segments: list[ActivitySegment] = Field(default_factory=list)
    events: list[AnnotationEvent] = Field(default_factory=list)


class SyncAnchor(BaseModel):
    imu_time_ns: int = Field(ge=0)
    video_time_ns: int = Field(ge=0)
    label: str = Field(default="tap", max_length=64)


class SyncDocument(BaseModel):
    anchors: list[SyncAnchor] = Field(default_factory=list)


class RecordingSummary(BaseModel):
    recording_id: str
    collection_id: str
    participant_id: str
    state: RecordingState
    started_at_utc: str
    ended_at_utc: str | None = None
    duration_ns: int | None = None
    h5_path: str | None = None
    mkv_path: str | None = None
    issues: list[str] = Field(default_factory=list)
    upload_state: str = "not_requested"
