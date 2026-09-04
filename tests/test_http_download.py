from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from imu_data_collector.http_download import (
    ByteRange,
    _read_chunk,
    parse_byte_range,
)
from imu_data_collector.storage import ObjectInfo


@pytest.mark.parametrize(
    ("header", "expected"),
    (
        (None, ByteRange(0, 99, 200)),
        ("bytes=10-19", ByteRange(10, 19, 206)),
        ("bytes=90-", ByteRange(90, 99, 206)),
        ("bytes=-10", ByteRange(90, 99, 206)),
        ("bytes=90-200", ByteRange(90, 99, 206)),
    ),
)
def test_parse_byte_range(header: str | None, expected: ByteRange) -> None:
    assert parse_byte_range(header, 100, label="test") == expected


@pytest.mark.parametrize(
    "header",
    ("items=0-1", "bytes=", "bytes=0-1,3-4", "bytes=100-", "bytes=-0"),
)
def test_parse_byte_range_rejects_invalid_requests(header: str) -> None:
    with pytest.raises(HTTPException) as raised:
        parse_byte_range(header, 100, label="test")
    assert raised.value.status_code == 416
    assert raised.value.headers == {"Content-Range": "bytes */100"}


class FlakyStore:
    def __init__(self) -> None:
        self.attempts = 0

    def read_bytes(self, _key: str, start: int, end: int) -> bytes:
        self.attempts += 1
        if self.attempts < 3:
            raise TimeoutError("temporary")
        return bytes(range(start, end + 1))


def test_chunk_read_retries_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("imu_data_collector.http_download.time.sleep", lambda _delay: None)
    store = FlakyStore()
    assert _read_chunk(store, "artifact", 2, 5) == b"\x02\x03\x04\x05"
    assert store.attempts == 3


def test_object_info_fixture_remains_hash_addressable() -> None:
    info = ObjectInfo(
        key="artifact.bin",
        size_bytes=100,
        generation=1,
        content_type="application/octet-stream",
        metadata={"sha256": "a" * 64},
        updated_at_utc=datetime.now(UTC),
    )
    assert info.metadata["sha256"] == "a" * 64
