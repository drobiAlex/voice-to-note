import json
import subprocess
import urllib.error

import pytest
from conftest import FakeResponse
from test_error_handling import fake_missing_binary, fake_run

from voice_to_note.gateways import GatewayError, youtube

URL = "https://www.youtube.com/watch?v=abc123"


def test_a_machine_without_ytdlp_is_told_how_to_install_it(monkeypatch):
    fake_missing_binary(monkeypatch, youtube)

    with pytest.raises(GatewayError, match="brew install yt-dlp"):
        youtube.video_info(URL)


def test_a_stuck_fetch_reports_the_timeout_it_ran_into(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(youtube.subprocess, "run", run)

    with pytest.raises(GatewayError, match="timed out"):
        youtube.video_info(URL)


def test_a_video_ytdlp_cannot_read_reports_its_words_and_the_upgrade_hint(monkeypatch):
    fake_run(monkeypatch, youtube, returncode=1, stderr="ERROR: Private video")

    with pytest.raises(GatewayError) as err:
        youtube.video_info(URL)

    assert "Private video" in str(err.value)
    assert "brew upgrade yt-dlp" in str(err.value)


def test_a_readable_info_dump_comes_back_as_parsed_json(monkeypatch):
    fake_run(monkeypatch, youtube, stdout=json.dumps({"id": "abc123", "title": "Talk"}))

    assert youtube.video_info(URL)["title"] == "Talk"


def test_an_unreadable_info_dump_is_a_gateway_error_not_a_traceback(monkeypatch):
    fake_run(monkeypatch, youtube, stdout="not json at all")

    with pytest.raises(GatewayError, match="unreadable JSON"):
        youtube.video_info(URL)


def test_the_json3_format_is_forced_onto_a_track_url_that_lacks_it(monkeypatch):
    seen = {}

    def urlopen(url, timeout=None):
        seen["url"] = url
        return FakeResponse(b'{"events": []}')

    monkeypatch.setattr(youtube.urllib.request, "urlopen", urlopen)

    youtube.captions("https://captions.example/t?v=abc")

    assert seen["url"] == "https://captions.example/t?v=abc&fmt=json3"


def test_a_track_url_already_asking_for_json3_is_left_alone(monkeypatch):
    seen = {}

    def urlopen(url, timeout=None):
        seen["url"] = url
        return FakeResponse(b"{}")

    monkeypatch.setattr(youtube.urllib.request, "urlopen", urlopen)

    youtube.captions("https://captions.example/t?fmt=json3")

    assert seen["url"] == "https://captions.example/t?fmt=json3"


def test_a_failed_caption_download_reads_as_one_fixable_line(monkeypatch):
    def urlopen(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(youtube.urllib.request, "urlopen", urlopen)

    with pytest.raises(GatewayError, match="caption download failed"):
        youtube.captions("https://captions.example/t")


def test_availability_answers_without_raising_whatever_which_says(monkeypatch):
    monkeypatch.setattr(youtube.shutil, "which", lambda _name: "/opt/homebrew/bin/yt-dlp")
    assert youtube.available() is True

    monkeypatch.setattr(youtube.shutil, "which", lambda _name: None)
    assert youtube.available() is False
