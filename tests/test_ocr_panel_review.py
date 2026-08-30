"""“高级 OCR”菜单页面与任务链路定向回归测试。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.panels.ocr_panel import OcrPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = OcrPanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_page_exposes_only_the_bundled_model_and_restores_legacy_prefs(panel):
    assert panel.need_ffmpeg is False
    assert panel.cb_lang.count() == 1
    panel.apply_prefs({"lang": "jpn", "export_fmt": "docx"})
    params = panel.collect_params()
    assert params["lang"] == "chi_sim+eng"
    assert params["export_fmt"] == "docx"
    assert panel.sw_table.isEnabled()
    assert panel.sw_image.isEnabled()
    assert "ocr_batch" in panel.services.task_manager._runner_factories


def test_export_format_updates_controls_summary_and_action(panel, app):
    panel.cb_export.setCurrentIndex(0)
    panel.sw_batch.setChecked(False)
    app.processEvents()
    assert not panel.sw_table.isEnabled()
    assert not panel.sw_image.isEnabled()
    assert "TXT" in panel.file_card._fmt_text
    assert panel.result_card.isVisibleTo(panel)

    panel.cb_export.setCurrentIndex(1)
    panel.sw_batch.setChecked(True)
    app.processEvents()
    assert panel.sw_table.isEnabled()
    assert panel.sw_image.isEnabled()
    assert "DOCX" in panel.file_card._fmt_text
    assert not panel.result_card.isVisibleTo(panel)
    assert panel.action_bar.btn_go.text() in (
        "开始批量识别", "Run batch OCR")


def test_same_named_batch_outputs_are_unique_and_recoverable(panel, tmp_path):
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    output_dir = tmp_path / "out"
    first_dir.mkdir()
    second_dir.mkdir()
    output_dir.mkdir()
    first = first_dir / "scan.png"
    second = second_dir / "scan.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    panel.out_row.set_state(panel.out_row.MODE_CUSTOM, str(output_dir))
    panel.cb_export.setCurrentIndex(0)
    panel._reserved_output_paths.clear()

    first_task = panel._make_task(str(first))
    second_task = panel._make_task(str(second))

    assert first_task["output_path"].endswith("scan.txt")
    assert second_task["output_path"].endswith("scan_1.txt")
    assert first_task["runner_key"] == "ocr_batch"


def test_single_mode_rejects_multiple_files(panel, monkeypatch):
    panel.sw_batch.setChecked(False)
    monkeypatch.setattr(panel.file_card, "files", lambda: ["a.png", "b.png"])
    submitted = []
    monkeypatch.setattr(panel, "_submit_files", lambda: submitted.append(True))
    monkeypatch.setattr("gui_qt.panels.ocr_panel.toast.show_warning",
                        lambda *_args, **_kwargs: None)

    assert panel._start() is False
    assert not submitted


def test_settings_reflow_on_narrow_window(panel, app):
    panel.resize(700, 700)
    panel.show()
    app.processEvents()
    assert panel.settings_grid.getItemPosition(
        panel.settings_grid.indexOf(panel.cb_export))[0] > 0
    panel.resize(1100, 700)
    app.processEvents()
    assert panel.settings_grid.getItemPosition(
        panel.settings_grid.indexOf(panel.cb_export))[0] == 1
