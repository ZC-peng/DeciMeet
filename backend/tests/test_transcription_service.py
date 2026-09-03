"""ASR reliability tests with no cloud credentials or live database."""

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.services.transcription_service import TranscriptionService
from app.services.meeting_service import MeetingService


def test_normalize_segments_removes_blanks_and_duplicate_sequence_ids() -> None:
    normalized = TranscriptionService._normalize_segments(
        [
            {
                "speaker": " A ",
                "content": " second ",
                "seq_index": 2,
                "start_time": "2.0",
                "end_time": "1.0",
            },
            {"speaker": "B", "content": "", "seq_index": 1},
            {"speaker": "C", "content": "first", "seq_index": 2},
        ]
    )

    assert [item["seq_index"] for item in normalized] == [0, 1]
    assert [item["content"] for item in normalized] == ["second", "first"]
    assert normalized[0]["end_time"] == normalized[0]["start_time"] == 2.0


@pytest.mark.asyncio
async def test_auto_provider_degrades_to_mock_without_cloud(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"offline")
    service = TranscriptionService()
    service.provider = "auto"
    mock_segments = [{"content": "mock", "seq_index": 0}]

    service._transcribe_via_dashscope = AsyncMock(return_value=None)
    service._mock_transcribe = AsyncMock(return_value=mock_segments)
    segments, mode = await service.transcribe(str(audio))

    assert segments == mock_segments
    assert mode == "mock"


@pytest.mark.asyncio
async def test_oss_object_is_cleaned_when_asr_raises(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"offline")

    class FakeOSS:
        is_available = True

        def __init__(self):
            self.deleted = []

        def upload_audio(self, _path):
            return "https://example.invalid/temp.wav"

        def delete_object(self, url):
            self.deleted.append(url)
            return True

    fake_oss = FakeOSS()
    fake_asr = SimpleNamespace(
        is_available=True,
        transcribe=AsyncMock(side_effect=RuntimeError("quota exhausted")),
    )
    service = TranscriptionService()

    with patch("app.services.transcription_service.oss_service", fake_oss), patch(
        "app.services.transcription_service.dashscope_asr_service", fake_asr
    ):
        result = await service._transcribe_via_dashscope(str(audio), "zh")

    assert result is None
    assert fake_oss.deleted == ["https://example.invalid/temp.wav"]


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_terminal_asr_failure_updates_failed_state() -> None:
    service = TranscriptionService()
    service.provider = "dashscope"
    service.transcribe = AsyncMock(return_value=(None, "real"))
    service._mark_failed = AsyncMock()

    initial_session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
        commit=AsyncMock(),
    )
    factory = lambda: _SessionContext(initial_session)
    meeting_id = uuid.uuid4()

    with patch("app.db.session.async_session_factory", factory):
        success, mode = await service.transcribe_and_store(
            str(meeting_id), "current.wav"
        )

    assert (success, mode) == (False, "real")
    service._mark_failed.assert_awaited_once_with(meeting_id, "current.wav")


@pytest.mark.asyncio
async def test_success_replaces_transcript_snapshot_in_one_store_pass() -> None:
    service = TranscriptionService()
    service.transcribe = AsyncMock(
        return_value=(
            [
                {"speaker": "A", "content": "one", "seq_index": 7},
                {"speaker": "B", "content": "two", "seq_index": 7},
            ],
            "mock",
        )
    )
    meeting_id = uuid.uuid4()
    audio_path = "current.wav"

    initial_session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
        commit=AsyncMock(),
    )
    meeting = SimpleNamespace(audio_url=audio_path, status="transcribing")
    selected = SimpleNamespace(scalar_one_or_none=lambda: meeting)
    store_session = SimpleNamespace(
        execute=AsyncMock(side_effect=[selected, SimpleNamespace()]),
        add=Mock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    class Factory:
        def __init__(self):
            self.contexts = iter(
                [_SessionContext(initial_session), _SessionContext(store_session)]
            )

        def __call__(self):
            return next(self.contexts)

    with patch("app.db.session.async_session_factory", Factory()):
        success, mode = await service.transcribe_and_store(
            str(meeting_id), audio_path
        )

    assert (success, mode) == (True, "mock")
    stored = [call.args[0] for call in store_session.add.call_args_list]
    assert [row.seq_index for row in stored] == [0, 1]
    assert "DELETE FROM transcripts" in str(
        store_session.execute.await_args_list[1].args[0]
    )
    assert meeting.status == "processed"
    assert meeting.transcription_mode == "mock"


@pytest.mark.asyncio
async def test_same_browser_filename_gets_distinct_safe_worker_paths(tmp_path: Path) -> None:
    service = MeetingService()
    meeting = SimpleNamespace(id=uuid.uuid4(), audio_url=None, status="pending")
    service.get_meeting = AsyncMock(return_value=meeting)
    db = SimpleNamespace(flush=AsyncMock(), refresh=AsyncMock())

    with patch("app.services.meeting_service.settings.UPLOAD_DIR", str(tmp_path)):
        await service.save_audio(
            db, meeting.id, b"first", "../same.wav"
        )
        first_path = meeting.audio_url
        await service.save_audio(
            db, meeting.id, b"second", "../same.wav"
        )
        second_path = meeting.audio_url

    assert first_path != second_path
    assert Path(first_path).parent == Path(second_path).parent
    assert ".." not in Path(first_path).name
    assert Path(first_path).read_bytes() == b"first"
    assert Path(second_path).read_bytes() == b"second"
