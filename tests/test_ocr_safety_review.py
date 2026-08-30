"""高级 OCR 输出安全与取消语义回归测试。"""

import os

import pytest


def test_txt_success_replaces_existing_file_atomically(monkeypatch, tmp_path):
    from core import ocr_batch

    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    output = tmp_path / "result.txt"
    output.write_text("old", encoding="utf-8")
    monkeypatch.setattr(ocr_batch, "ocr_image", lambda *_args, **_kwargs: "new")
    progress = []

    assert ocr_batch.ocr_file_to_txt(
        str(source), str(output), progress_cb=lambda pct, msg: progress.append(pct))
    assert output.read_text(encoding="utf-8") == "new"
    assert progress[-1] == 95
    assert not list(tmp_path.glob(".fm_ocr_*.txt"))


def test_txt_cancel_before_commit_preserves_existing_file(monkeypatch, tmp_path):
    from core import ocr_batch

    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    output = tmp_path / "result.txt"
    output.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(ocr_batch, "ocr_image", lambda *_args, **_kwargs: "new")

    def cancel_before_commit(pct, _msg):
        if pct == 95:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        ocr_batch.ocr_file_to_txt(
            str(source), str(output), progress_cb=cancel_before_commit)
    assert output.read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(".fm_ocr_*.txt"))


def test_docx_save_failure_preserves_existing_file(monkeypatch, tmp_path):
    from core import ocr_batch

    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    output = tmp_path / "result.docx"
    output.write_bytes(b"existing")

    def fake_impl(_source, staged, *_args, **_kwargs):
        with open(staged, "wb") as stream:
            stream.write(b"generated")
        return True

    monkeypatch.setattr(ocr_batch, "_ocr_file_to_docx_impl", fake_impl)
    monkeypatch.setattr(ocr_batch.os, "replace", lambda *_args: (_ for _ in ()).throw(
        OSError("disk unavailable")))

    assert not ocr_batch.ocr_file_to_docx(str(source), str(output))
    assert output.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".fm_ocr_*.docx"))


@pytest.mark.parametrize(
    ("source_name", "output_name"),
    [("source.csv", "result.txt"), ("source.png", "result.csv")],
)
def test_invalid_extensions_are_rejected(tmp_path, source_name, output_name):
    from core import ocr_batch

    source = tmp_path / source_name
    source.write_bytes(b"data")
    errors = []
    assert not ocr_batch.ocr_file_to_txt(
        str(source), str(tmp_path / output_name),
        progress_cb=lambda pct, msg: errors.append((pct, msg)))
    assert errors and errors[-1][0] == -1


def test_ocr_tool_does_not_swallow_cancel(tmp_path):
    from core import ocr_tool

    source = tmp_path / "source.png"
    source.write_bytes(b"image")

    def cancel_during_recognition(pct, _msg):
        if pct == 50:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        ocr_tool.ocr_image_raw(str(source), progress_cb=cancel_during_recognition)

