import io
import subprocess
import tarfile

import pytest
from conftest import FakeResponse

from voice_to_note.gateways import GatewayError, bootstrap


def fake_run(returncode=0, stderr=""):
    """A subprocess.run stand-in that never touches the outside world."""
    return lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, returncode, "", stderr)


# --- checking build tools are present --------------------------------------


def test_require_tools_passes_when_everything_needed_is_on_the_path(monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _tool: "/usr/bin/x")

    bootstrap.require_tools()


def test_a_missing_tool_is_named_with_an_install_hint(monkeypatch):
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda tool: None if tool == "cmake" else "/usr/bin/x"
    )

    with pytest.raises(GatewayError, match="cmake"):
        bootstrap.require_tools()


# --- cloning and building whisper.cpp --------------------------------------


def test_cloning_whisper_runs_a_shallow_clone_into_the_vendor_path(monkeypatch, tmp_path):
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)

    bootstrap.clone_whisper(tmp_path / "vendor")

    assert seen["cmd"][:3] == ["git", "clone", "--depth"]
    assert seen["cmd"][-1] == str(tmp_path / "vendor")


def test_a_failing_clone_is_a_gateway_error_naming_what_git_said(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bootstrap.subprocess, "run", fake_run(1, "fatal: could not resolve host")
    )

    with pytest.raises(GatewayError, match="could not resolve host"):
        bootstrap.clone_whisper(tmp_path / "vendor")


def test_building_whisper_configures_before_it_compiles(monkeypatch, tmp_path):
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    vendor = tmp_path / "vendor"

    bootstrap.build_whisper(vendor)

    assert seen[0][0] == "cmake" and "-S" in seen[0] and str(vendor) in seen[0]
    assert seen[1] == [
        "cmake", "--build", str(vendor / "build"), "-j", "--config", "Release",
    ]


def test_a_failing_build_is_a_gateway_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bootstrap.subprocess, "run", fake_run(1, "error: no member named 'x' in 'y'")
    )

    with pytest.raises(GatewayError, match="no member named"):
        bootstrap.build_whisper(tmp_path / "vendor")


# --- building the meeting-capture helper -----------------------------------


def test_building_the_capture_helper_without_swiftc_names_how_to_get_it(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _tool: None)

    with pytest.raises(GatewayError, match="swiftc"):
        bootstrap.build_capture(
            tmp_path / "capture.swift", tmp_path / "Info.plist", tmp_path / "bin" / "vtn-capture"
        )


def test_building_the_capture_helper_embeds_its_plist_then_signs_it(monkeypatch, tmp_path):
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _tool: "/usr/bin/swiftc")
    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    plist = tmp_path / "Info.plist"
    dst = tmp_path / "bin" / "vtn-capture"

    bootstrap.build_capture(tmp_path / "capture.swift", plist, dst)

    # the linked-in plist is what makes macOS name this helper in its
    # permission prompts, and the fixed identifier is what keeps the answer
    assert seen[0][0] == "swiftc"
    assert "__info_plist" in seen[0] and str(plist) in seen[0]
    assert seen[1] == [
        "codesign", "--force", "--sign", "-", "--identifier", "app.vtn.capture", str(dst),
    ]
    assert dst.parent.is_dir()


# --- building the menu bar recorder ----------------------------------------


def test_building_the_menu_bar_recorder_without_swiftc_names_how_to_get_it(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _tool: None)

    with pytest.raises(GatewayError, match="swiftc"):
        bootstrap.build_menubar(
            tmp_path / "menubar.swift",
            tmp_path / "menubar-Info.plist",
            tmp_path / "bin" / "VTN Recorder.app",
        )


def test_building_the_menu_bar_recorder_assembles_a_signed_app_bundle(monkeypatch, tmp_path):
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _tool: "/usr/bin/swiftc")
    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    plist = tmp_path / "menubar-Info.plist"
    plist.write_text("<plist/>")
    app = tmp_path / "bin" / "VTN Recorder.app"

    bootstrap.build_menubar(tmp_path / "menubar.swift", plist, app)

    # macOS attributes recording permission to the bundle a capture is launched
    # from, so the binary and its plist have to land in the layout it reads
    assert seen[0][0] == "swiftc"
    assert seen[0][-1] == str(app / "Contents" / "MacOS" / "vtn-menubar")
    assert (app / "Contents" / "Info.plist").read_text() == "<plist/>"
    assert seen[1] == [
        "codesign", "--force", "--sign", "-", "--identifier", "app.vtn.menubar", str(app),
    ]


def test_a_model_download_script_runs_with_its_own_arguments_and_streams_output(
    monkeypatch, tmp_path
):
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, None, None)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    vendor = tmp_path / "vendor"

    bootstrap.download_model_script(vendor, "download-ggml-model.sh", ["large-v3-turbo", "models"])

    assert seen["cmd"] == [
        "bash", str(vendor / "models" / "download-ggml-model.sh"),
        "large-v3-turbo", "models",
    ]
    # curl's own progress bar must reach the terminal, so nothing here may capture it
    assert seen["kwargs"] == {}


def test_a_failing_model_script_is_a_gateway_error_naming_the_script(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run(1))

    with pytest.raises(GatewayError, match="download-vad-model.sh failed"):
        bootstrap.download_model_script(tmp_path / "vendor", "download-vad-model.sh", ["silero-v5.1.2"])


# --- downloading a single file ---------------------------------------------


def test_fetching_a_file_writes_it_under_its_final_name(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bootstrap.urllib.request, "urlopen", lambda _url, timeout=None: FakeResponse(b"model bytes")
    )
    dst = tmp_path / "model.onnx"

    bootstrap.fetch("https://example.com/model.onnx", dst)

    assert dst.read_bytes() == b"model bytes"
    assert not dst.with_suffix(dst.suffix + ".part").exists()


def test_fetching_a_file_reports_progress_as_chunks_arrive(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bootstrap.urllib.request,
        "urlopen",
        lambda _url, timeout=None: FakeResponse(b"0123456789", length=10, chunk_size=4),
    )
    dst = tmp_path / "model.onnx"
    ticks = []

    bootstrap.fetch(
        "https://example.com/model.onnx", dst, tick=lambda read, total: ticks.append((read, total))
    )

    assert ticks == [(4, 10), (8, 10), (10, 10)]
    assert dst.read_bytes() == b"0123456789"


def test_a_failed_fetch_leaves_no_file_behind(monkeypatch, tmp_path):
    def urlopen(_url, timeout=None):
        raise bootstrap.urllib.error.URLError("connection refused")

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", urlopen)
    dst = tmp_path / "model.onnx"

    with pytest.raises(GatewayError, match="connection refused"):
        bootstrap.fetch("https://example.com/model.onnx", dst)

    # a killed download must not leave anything a later run mistakes for done
    assert not dst.exists()
    assert not dst.with_suffix(dst.suffix + ".part").exists()


# --- downloading and unpacking a tar.bz2 archive ---------------------------


def make_tar_bz2(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_fetching_a_tar_archive_extracts_it_into_the_destination(monkeypatch, tmp_path):
    body = make_tar_bz2({"sherpa-onnx-pyannote-segmentation-3-0/model.onnx": b"weights"})
    monkeypatch.setattr(
        bootstrap.urllib.request, "urlopen", lambda _url, timeout=None: FakeResponse(body)
    )

    bootstrap.fetch_tar_bz2("https://example.com/seg.tar.bz2", tmp_path)

    extracted = tmp_path / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    assert extracted.read_bytes() == b"weights"


def test_fetching_a_tar_archive_leaves_no_temp_file_behind(monkeypatch, tmp_path):
    body = make_tar_bz2({"a/b.onnx": b"x"})
    monkeypatch.setattr(
        bootstrap.urllib.request, "urlopen", lambda _url, timeout=None: FakeResponse(body)
    )

    bootstrap.fetch_tar_bz2("https://example.com/seg.tar.bz2", tmp_path)

    assert list(tmp_path.glob("*.part")) == []


def test_fetching_a_tar_archive_reports_progress_as_chunks_arrive(monkeypatch, tmp_path):
    body = make_tar_bz2({"a/b.onnx": b"x" * 10})
    monkeypatch.setattr(
        bootstrap.urllib.request,
        "urlopen",
        lambda _url, timeout=None: FakeResponse(body, length=len(body), chunk_size=len(body) // 2),
    )
    ticks = []

    bootstrap.fetch_tar_bz2(
        "https://example.com/seg.tar.bz2",
        tmp_path,
        tick=lambda read, total: ticks.append((read, total)),
    )

    assert len(ticks) >= 2
    assert ticks[-1] == (len(body), len(body))


def test_a_failed_tar_fetch_raises_a_gateway_error(monkeypatch, tmp_path):
    def urlopen(_url, timeout=None):
        raise bootstrap.urllib.error.URLError("connection refused")

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", urlopen)

    with pytest.raises(GatewayError, match="connection refused"):
        bootstrap.fetch_tar_bz2("https://example.com/seg.tar.bz2", tmp_path)
