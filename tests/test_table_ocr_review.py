"""表格识别核心输出安全与输入防御回归测试。"""

from types import SimpleNamespace

import pytest


def test_csv_success_replaces_existing_file_atomically(monkeypatch, tmp_path):
    from core import table_recognizer

    source = tmp_path / "table.png"
    source.write_bytes(b"image")
    output = tmp_path / "table.csv"
    output.write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        table_recognizer, "recognize_rows",
        lambda *_args, **_kwargs: [["Name", "Value"], ["A", "10"]])
    progress = []

    assert table_recognizer.recognize_table(
        str(source), str(output),
        progress_cb=lambda pct, msg: progress.append(pct))
    assert "Name,Value" in output.read_text(encoding="utf-8-sig")
    assert progress[-1] == 95
    assert not list(tmp_path.glob(".fm_table_ocr_*.csv"))


def test_cancel_before_commit_preserves_existing_file(monkeypatch, tmp_path):
    from core import table_recognizer

    source = tmp_path / "table.png"
    source.write_bytes(b"image")
    output = tmp_path / "table.csv"
    output.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        table_recognizer, "recognize_rows",
        lambda *_args, **_kwargs: [["Name"], ["A"]])

    def cancel_before_commit(pct, _message):
        if pct == 95:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        table_recognizer.recognize_table(
            str(source), str(output), progress_cb=cancel_before_commit)
    assert output.read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(".fm_table_ocr_*.csv"))


def test_unknown_output_and_invalid_chart_are_rejected(monkeypatch, tmp_path):
    from core import table_recognizer

    source = tmp_path / "table.png"
    source.write_bytes(b"image")
    errors = []
    assert not table_recognizer.recognize_table(
        str(source), str(tmp_path / "table.bin"),
        progress_cb=lambda pct, msg: errors.append((pct, msg)))
    assert errors[-1][0] == -1

    monkeypatch.setattr(
        table_recognizer, "recognize_rows",
        lambda *_args, **_kwargs: [["Name", "Value"], ["A", "10"]])
    assert not table_recognizer.recognize_table(
        str(source), str(tmp_path / "table.xlsx"), chart_type="scatter")


def test_malformed_ocr_items_are_skipped():
    from core.table_recognizer import _items

    valid = [[[0, 0], [10, 0], [10, 10], [0, 10]], "Cell", 0.9]
    malformed = [None, [[], "Empty", 0.5], [[[]], "Bad", 0.5]]
    assert [item[-1] for item in _items([*malformed, valid])] == ["Cell"]


def test_make_runner_restores_chart_parameter(monkeypatch):
    from core import table_recognizer

    captured = []
    monkeypatch.setattr(
        table_recognizer, "recognize_table",
        lambda source, output, progress_cb=None, chart_type=None:
        captured.append((source, output, chart_type)) or True)
    task = SimpleNamespace(
        file_path="table.png", output_path="table.xlsx",
        params={"chart_type": "line"})

    assert table_recognizer.make_runner(task)(task, None)
    assert captured == [("table.png", "table.xlsx", "line")]


def test_excel_chart_converts_numeric_ocr_text_but_preserves_categories(
        monkeypatch, tmp_path):
    from core import table_recognizer
    from openpyxl import load_workbook

    source = tmp_path / "table.png"
    source.write_bytes(b"image")
    output = tmp_path / "table.xlsx"
    monkeypatch.setattr(
        table_recognizer, "recognize_rows",
        lambda *_args, **_kwargs: [
            ["月份", "销量", "编号"], ["一月", "1,200.5", "001"]])

    assert table_recognizer.recognize_table(
        str(source), str(output), chart_type="bar")
    sheet = load_workbook(output).active
    assert sheet["A2"].value == "一月"
    assert sheet["B2"].data_type == "n"
    assert sheet["B2"].value == 1200.5
    assert sheet["C2"].value == "001"
