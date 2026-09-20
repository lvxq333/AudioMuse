"""真实 ASR 适配层测试：全部使用 MockTransport，不访问网络。"""

import httpx
import pytest

from app.services.asr import AsrFailure, transcribe


async def test_real_asr_posts_file_and_returns_text(tmp_path):
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"RIFF-fake-wave")

    async def handler(request):
        assert request.url == "https://asr.test/v1/audio/transcriptions"
        assert request.headers["Authorization"] == "Bearer secret"
        body = await request.aread()
        assert b'model' in body and b'whisper-large' in body
        assert b'meeting.wav' in body and b'RIFF-fake-wave' in body
        assert b'language' in body and b'zh' in body
        return httpx.Response(200, json={"text": "  这是一段真实转写。  "})

    result = await transcribe(
        recording_id="r1", task_id="t1", attempt_no=1,
        audio_path=audio, original_filename="meeting.wav",
        api_key="secret", base_url="https://asr.test/v1",
        model="whisper-large", language="zh",
        transport=httpx.MockTransport(handler),
    )
    assert result == "这是一段真实转写。"


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": 123}, []])
async def test_real_asr_rejects_invalid_response(tmp_path, payload):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"audio")

    async def handler(request):
        return httpx.Response(200, json=payload)

    with pytest.raises(AsrFailure, match="text"):
        await transcribe(
            recording_id="r1", task_id="t1", attempt_no=1,
            audio_path=audio, api_key="secret", base_url="https://asr.test/v1",
            transport=httpx.MockTransport(handler),
        )


async def test_real_asr_maps_http_error(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"audio")

    async def handler(request):
        return httpx.Response(429, json={"error": "rate limit"})

    with pytest.raises(AsrFailure, match="429"):
        await transcribe(
            recording_id="r1", task_id="t1", attempt_no=1,
            audio_path=audio, api_key="secret", base_url="https://asr.test/v1",
            transport=httpx.MockTransport(handler),
        )
