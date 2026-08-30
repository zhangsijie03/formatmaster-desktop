"""文件夹/剪贴板监视稳定性、输出隔离、线程清理和页面回归测试。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FORMATMASTER_OFFSCREEN", "1")

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.panels.monitor_panel import MonitorPanelPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    page = MonitorPanelPage(object(), services)
    app.processEvents()
    yield page
    page.close()
    page.deleteLater()
    app.processEvents()


def test_new_file_must_be_stable_for_two_scans(panel, monkeypatch, tmp_path):
    panel.ed_dir.setText(str(tmp_path))
    panel._start(str(tmp_path))
    panel._timer.stop()
    queued = []
    monkeypatch.setattr(panel, "_convert", queued.append)
    source = tmp_path / "incoming.mp4"
    source.write_bytes(b"partial")

    panel._scan()
    assert queued == []

    source.write_bytes(b"partial-more")
    panel._scan()
    assert queued == []

    panel._scan()
    assert queued == [str(source)]


def test_output_never_overwrites_source_existing_or_reserved_target(
        panel, monkeypatch, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    (tmp_path / "clip_converted.mp4").write_bytes(b"existing")
    captured = []

    def fake_add_task(**kwargs):
        captured.append(kwargs)
        return 42

    monkeypatch.setattr(panel.services.task_manager, "add_task", fake_add_task)

    panel._submit_conversion("video", "mp4", None, str(source), str(tmp_path))

    output = captured[0]["output_path"]
    assert output == str(tmp_path / "clip_converted_2.mp4")
    assert output != str(source)
    assert panel._path_key(output) in panel._ignored_outputs


def test_failed_queue_releases_output_reservation(panel, monkeypatch, tmp_path):
    source = tmp_path / "clip.avi"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        panel.services.task_manager, "add_task", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="未就绪|not ready"):
        panel._submit_conversion(
            "video", "mp4", None, str(source), str(tmp_path))

    assert not panel._reserved_outputs
    assert not panel._ignored_outputs


def test_watcher_ignores_its_own_generated_output(
        panel, monkeypatch, tmp_path):
    panel._running = True
    panel._dir = str(tmp_path)
    panel.ed_dir.setText(str(tmp_path))
    output = tmp_path / "generated.mp4"
    output.write_bytes(b"result")
    panel._ignored_outputs.add(panel._path_key(str(output)))
    queued = []
    monkeypatch.setattr(panel, "_convert", queued.append)

    panel._scan()
    panel._scan()

    assert queued == []


def test_clipboard_image_hash_uses_full_pixels(panel):
    first = QImage(64, 64, QImage.Format_RGBA8888)
    first.fill(QColor("white"))
    second = first.copy()
    second.setPixelColor(63, 63, QColor("black"))

    assert panel._clip_img_new(first)
    assert not panel._clip_img_new(first)
    assert panel._clip_img_new(second)


def test_ocr_worker_reports_real_exception(monkeypatch):
    from gui_qt.panels.monitor_panel import _OcrWorker

    monkeypatch.setattr(
        "core.ocr_tool.ocr_to_file",
        lambda *_args: (_ for _ in ()).throw(ValueError("OCR engine broken")))
    worker = _OcrWorker("source.png", "result.txt")
    errors = []
    worker.sig_error.connect(errors.append)

    worker.run()

    assert errors == ["OCR engine broken"]


def test_ocr_only_temp_image_is_cleaned(panel, tmp_path):
    from gui_qt.panels.monitor_panel import _OcrWorker

    temp_image = tmp_path / ".clipboard_1_ocr.png"
    temp_image.write_bytes(b"temporary")
    worker = _OcrWorker(str(temp_image), str(tmp_path / "clipboard_1.txt"))
    panel._ocr_temp_paths[worker] = str(temp_image)

    panel._on_ocr_done(worker, str(tmp_path / "clipboard_1.txt"), False)

    assert not temp_image.exists()


def test_preferences_use_stable_keys_and_restore_legacy_indices(panel):
    panel.cb_kind.setCurrentIndex(1)
    panel.cb_fmt.setCurrentIndex(2)
    panel.cb_clip_kind.setCurrentIndex(2)
    panel.cb_clip_fmt.setCurrentIndex(1)
    panel.cb_clip_act.setCurrentIndex(2)

    prefs = panel.collect_prefs()

    assert prefs["kind"] == "audio"
    assert prefs["fmt"] == "aac"
    assert prefs["clip_kind"] == "image"
    assert prefs["clip_fmt"] == "jpg"
    assert prefs["clip_act"] == "save_ocr"

    panel.apply_prefs({
        "kind": 0, "fmt": 1, "clip_kind": 1,
        "clip_fmt": 2, "clip_act": 1,
    })
    assert panel.cb_kind.currentIndex() == 0
    assert panel.cb_fmt.currentIndex() == 1
    assert panel.cb_clip_kind.currentIndex() == 1
    assert panel.cb_clip_fmt.currentIndex() == 2
    assert panel.cb_clip_act.currentIndex() == 1


def test_running_locks_settings_and_close_stops_event_sources(
        panel, tmp_path):
    panel.ed_dir.setText(str(tmp_path))
    panel.ed_clip_dir.setText(str(tmp_path))
    panel._start(str(tmp_path))
    panel._start_clip()

    assert not panel.btn_browse.isEnabled()
    assert not panel.cb_kind.isEnabled()
    assert not panel.ed_clip_dir.isEnabled()

    panel.close()

    assert not panel._running
    assert not panel._clip_running
    assert not panel._timer.isActive()


def test_narrow_page_reflows_without_horizontal_scroll(panel, app):
    panel.resize(640, 760)
    panel.show()
    app.processEvents()

    assert panel.folder_grid._columns == 1
    assert panel.clip_grid._columns == 1
    assert panel.horizontalScrollBar().maximum() == 0

    panel.resize(1100, 760)
    app.processEvents()
    assert panel.folder_grid._columns == 2
    assert panel.clip_grid._columns == 2


def test_folder_guidance_explains_existing_files_and_output_policy(panel):
    guidance = panel.folder_hint.text().lower()

    assert "_converted" in guidance
    assert "已有文件" in guidance or "existing files" in guidance
    assert "监视目录" in guidance or "watched folder" in guidance


def test_clipboard_image_action_only_enabled_for_image_type(panel):
    from gui_qt.panels.monitor_panel import KIND_KEYS

    panel.cb_clip_kind.setCurrentIndex(0)
    assert not panel.cb_clip_act.isEnabled()
    assert "忽略" in panel.clip_hint.text() or "ignored" in panel.clip_hint.text()

    panel.cb_clip_kind.setCurrentIndex(KIND_KEYS.index("image"))
    assert panel.cb_clip_act.isEnabled()
    assert "截图动作" in panel.clip_hint.text() or "Image action" in panel.clip_hint.text()

    panel._set_clip_controls_enabled(False)
    panel._set_clip_controls_enabled(True)
    assert panel.cb_clip_act.isEnabled()

    panel.cb_clip_kind.setCurrentIndex(0)
    panel._set_clip_controls_enabled(False)
    panel._set_clip_controls_enabled(True)
    assert not panel.cb_clip_act.isEnabled()


def test_watch_statuses_have_accessible_names(panel):
    assert panel.status_label.accessibleName()
    assert panel.clip_status.accessibleName()
