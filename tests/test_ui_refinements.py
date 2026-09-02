"""UI 借鉴落地回归测试（对标 qfluentwidgets 官方设置页 + Fluent-M3U8 任务卡）。

覆盖：
1. 设置页组间距对标官方标准（spacing=20，分组呼吸感）。
2. M3U8 队列行状态视觉：待下载灰点 / 运行中蓝点+进度+速度 / 成功绿✓ / 失败红✕，
   以及上移/移除/清空等队列操作不破坏状态。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"

import pytest

from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app_ctx():
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager
    app = QApplication.instance() or QApplication([])
    services = QtServices()
    services.task_manager = TaskManager(services)
    from gui_qt.components.theme_manager import ThemeManager
    services.theme_mgr = ThemeManager(services)

    class _Win:
        pass

    yield app, _Win(), services
    app.processEvents()


def _fg(panel, row):
    return panel.lst_queue.item(row).foreground().color().name().upper()


def _m3u8_panel(app_ctx):
    from gui_qt import nav_registry as nr
    app, win, services = app_ctx
    return nr.find_item("m3u8")["factory"](win, services)


def test_m3u8_panel_queue_basic(app_ctx):
    """M3U8 面板：批量添加队列 + 行状态色切换。"""
    app, win, services = app_ctx
    panel = _m3u8_panel(app_ctx)
    try:
        panel._queue.clear()
        panel.txt_url.setPlainText(
            "https://example.com/a.m3u8\nhttps://example.com/b.m3u8")
        panel._batch_add()
        assert panel.lst_queue.count() == 2, panel.lst_queue.count()
        assert len(panel._queue) == 2
        # 状态色：默认灰 → 运行蓝 → 成功绿（state_key: waiting/running/success/failed）
        panel._set_row_state(0, "running")
        assert _fg(panel, 0) == "#2F6BFF"
        panel._set_row_state(0, "success")
        assert _fg(panel, 0) == "#0FA47A"
    finally:
        panel.deleteLater()
        app.processEvents()


def test_settings_group_spacing(app_ctx):
    """设置页分组间距对标官方（20px 呼吸感）。"""
    from gui_qt.pages.settings_page import SettingsPage
    app, win, services = app_ctx
    page = SettingsPage(win, services)
    try:
        spacings = [scroll.widget().layout().spacing()
                    for _item, scroll in page._sections.values()]
        assert spacings, "未找到分页布局"
        assert all(s == 20 for s in spacings), f"组间距应为 20，实际 {spacings}"
    finally:
        page.deleteLater()
        app.processEvents()


def test_task_card_iconized_meta(app_ctx):
    """TaskCard 图标化元信息（对标 Fluent-M3U8）：速度图标行 + 元信息含文件大小。"""
    app, win, services = app_ctx
    from gui_qt import task_manager as tm
    from gui_qt.components.task_card import TaskCard
    t = tm.Task(task_id=1, name="测试视频.mp4", task_type="video",
                file_path="x.mp4", output_path="y.mp4",
                params={"fmt": "MP4"},
                input_size=3 * 1024 * 1024 + 512 * 1024,
                progress=55, speed="1.5MB/s", state=tm.RUNNING)
    card = TaskCard(t)
    try:
        # 元信息：格式 · 大小 · 优先级
        meta = card._meta_text()
        assert "MP4" in meta and "3.5 MB" in meta and "优先级" in meta, meta
        # 速度图标化：图标 + 文本
        assert not card.speed_icon.icon.isNull()
        assert card.speed_label.text() == "1.5MB/s"
        # 进度/状态流转不受影响
        card.on_progress(70, "下载中", "2.3MB/s")
        assert card.speed_label.text() == "2.3MB/s"
        card.on_state(tm.SUCCESS)
        assert card.badge.text() == "已完成"
    finally:
        card.deleteLater()
        app.processEvents()


def test_file_list_actions_follow_selection(app_ctx, tmp_path):
    """文件操作只在存在有效目标时启用，防止空操作和误操作。"""
    from gui_qt.widgets import FileListCard
    app, _win, _services = app_ctx
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    card = FileListCard(file_exts={".mp4"})
    try:
        assert card.btn_empty_add.isVisibleTo(card)
        assert card.btn_empty_dir.isVisibleTo(card)
        assert not card.btn_add.isVisibleTo(card)
        assert not card.btn_rm.isEnabled()
        assert not card.btn_clear.isEnabled()
        assert card.add_files([str(source)]) == 1
        assert card.table.item(0, 0).text() == "sample.mp4"
        assert card.table.item(0, 0).toolTip() == str(source)
        assert card.files() == [str(source)]
        assert card.row_of_file(str(source)) == 0
        assert card.btn_clear.isEnabled()
        assert card.btn_add.isVisibleTo(card)
        assert not card.btn_empty_add.isVisibleTo(card)
        assert not card.btn_rm.isEnabled()
        card.table.selectRow(0)
        app.processEvents()
        assert card.btn_rm.isEnabled()
        card.remove_selected()
        assert card.files() == []
        assert not card.btn_clear.isEnabled()
        assert card.btn_empty_add.isVisibleTo(card)
    finally:
        card.deleteLater()
        app.processEvents()


def test_file_list_accepts_macos_backspace_for_remove(app_ctx, tmp_path):
    """macOS 键盘的 Delete/Backspace 可以移除选中文件。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from gui_qt.widgets import FileListCard

    app, _win, _services = app_ctx
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    card = FileListCard(file_exts={".mp4"})
    try:
        card.add_files([str(source)])
        card.table.selectRow(0)
        card._on_key(QKeyEvent(QEvent.KeyPress, Qt.Key_Backspace,
                               Qt.NoModifier))
        assert card.files() == []
    finally:
        card.deleteLater()
        app.processEvents()


def test_video_frame_panel_isolates_outputs_and_reflows(app_ctx, tmp_path):
    """视频抽帧使用独立结果目录，文件操作与窄窗口布局保持一致。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.video_frame_panel import VideoFramePanelPage

    app, win, services = app_ctx
    panel = VideoFramePanelPage(win, services)
    try:
        assert panel.findChild(PageHeader) is not None
        assert panel.content_layout.indexOf(panel.action_bar) == -1
        assert not panel.action_bar.btn_go.isEnabled()
        assert not panel.btn_picker.isEnabled()

        first_dir = tmp_path / "one"
        second_dir = tmp_path / "two"
        output_dir = tmp_path / "output"
        first_dir.mkdir()
        second_dir.mkdir()
        output_dir.mkdir()
        first = first_dir / "same.mp4"
        second = second_dir / "same.mp4"
        first.write_bytes(b"video-a")
        second.write_bytes(b"video-b")
        panel.out_row.resolve_dir = lambda _path: str(output_dir)
        panel.file_card.add_files([str(first), str(second)])
        panel.cb_fmt.setCurrentText("JPG")
        app.processEvents()
        assert panel.btn_picker.isEnabled()
        assert "JPG" in panel.file_card._fmt_text

        panel._reserved_frame_dirs = set()
        task_a = panel._make_task(str(first))
        task_b = panel._make_task(str(second))
        assert Path(task_a["output_path"]).parts[-2:] == (
            "same_frames", "frame_00000.jpg")
        assert Path(task_b["output_path"]).parts[-2:] == (
            "same_frames_1", "frame_00000.jpg")

        panel.resize(700, 720)
        panel.show()
        app.processEvents()
        assert panel.frames_grid._columns == 1
        assert panel.sheet_grid._columns == 1
        panel.resize(1100, 720)
        app.processEvents()
        assert panel.frames_grid._columns == 2
        assert panel.sheet_grid._columns == 3
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_video_frame_total_progress_does_not_regress(app_ctx):
    """一个抽帧任务先完成后，批量总进度不能因移除任务而倒退。"""
    from gui_qt import task_manager as tm
    from gui_qt.panels.video_frame_panel import VideoFramePanelPage

    app, win, services = app_ctx
    panel = VideoFramePanelPage(win, services)
    try:
        first = tm.Task(9821, "a.mp4", "frame_extract", "a.mp4", "a/frame.png",
                        state=tm.RUNNING, progress=80)
        second = tm.Task(9822, "b.mp4", "frame_extract", "b.mp4", "b/frame.png",
                         state=tm.RUNNING, progress=20)
        services.task_manager._tasks.update({9821: first, 9822: second})
        panel._task_rows = {9821: ("a.mp4", -1), 9822: ("b.mp4", -1)}
        panel._batch_progress = {9821: 80, 9822: 20}
        panel._update_total()
        assert panel.action_bar.bar_total.value() == 50
        first.state = tm.SUCCESS
        panel._on_state(9821, tm.SUCCESS)
        assert panel.action_bar.bar_total.value() >= 50
    finally:
        services.task_manager._tasks.pop(9821, None)
        services.task_manager._tasks.pop(9822, None)
        panel.deleteLater()
        app.processEvents()


def test_subtitle_panel_uses_stable_params_and_isolates_outputs(
        app_ctx, tmp_path):
    """字幕菜单展示真实能力，任务参数不依赖翻译文案且同名输出不冲突。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.subtitle_panel import SubtitlePanelPage

    app, win, services = app_ctx
    panel = SubtitlePanelPage(win, services)
    try:
        assert panel.findChild(PageHeader) is not None
        assert panel.content_layout.indexOf(panel.action_bar) == -1
        assert not panel.action_bar.btn_go.isEnabled()
        assert "最准" in panel.cb_fps.itemText(0)
        assert "最快" in panel.cb_fps.itemText(2)
        assert panel.cb_lang.count() == 1

        panel.cb_fps.setCurrentIndex(0)
        panel.cb_region.setCurrentIndex(2)
        params = panel.collect_params()
        assert params["fps"] == 2.0
        assert params["region"] == "full"
        assert params["lang"] == "chi_sim+eng"
        assert not panel.cb_height.isEnabled()

        first_dir = tmp_path / "one"
        second_dir = tmp_path / "two"
        output_dir = tmp_path / "output"
        first_dir.mkdir()
        second_dir.mkdir()
        output_dir.mkdir()
        first = first_dir / "same.mp4"
        second = second_dir / "same.mp4"
        first.write_bytes(b"video-a")
        second.write_bytes(b"video-b")
        panel.out_row.resolve_dir = lambda _path: str(output_dir)
        panel._reserved_output_paths = set()
        task_a = panel._make_task(str(first))
        task_b = panel._make_task(str(second))
        assert task_a["output_path"].endswith("same.srt")
        assert task_b["output_path"].endswith("same_1.srt")
    finally:
        panel.deleteLater()
        app.processEvents()


def test_subtitle_panel_reflows_and_restores_legacy_preferences(app_ctx):
    """旧版翻译文案可恢复为稳定值，窄窗口表单安全降为单列。"""
    from gui_qt.panels.subtitle_panel import SubtitlePanelPage

    app, win, services = app_ctx
    panel = SubtitlePanelPage(win, services)
    try:
        panel.apply_prefs({
            "fps": "2 秒/帧（最准）",
            "region": "全屏（会连屏幕文字一起识别）",
            "height": "20%",
        })
        assert panel.collect_params()["fps"] == 0.5
        assert panel.collect_params()["region"] == "full"
        panel.resize(700, 720)
        panel.show()
        app.processEvents()
        assert panel.params_grid._columns == 1
        panel.resize(1100, 720)
        app.processEvents()
        assert panel.params_grid._columns == 2
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_video_unwarp_panel_restores_custom_ratio_and_isolates_outputs(
        app_ctx, tmp_path):
    """反挤压自定义比例可恢复，手动模式统一输出 MP4 且同名不覆盖。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.video_unwarp_panel import VideoUnwarpPanelPage

    app, win, services = app_ctx
    panel = VideoUnwarpPanelPage(win, services)
    try:
        assert panel.findChild(PageHeader) is not None
        assert panel.content_layout.indexOf(panel.action_bar) == -1
        assert not panel.action_bar.btn_go.isEnabled()
        assert not panel.btn_preview.isEnabled()

        panel.apply_prefs({"ratio": "21:9"})
        assert panel._target_ratio() == "21:9"
        assert panel.w_custom.isVisibleTo(panel)
        assert panel.sb_w.value() == 21
        assert panel.sb_h.value() == 9

        first_dir = tmp_path / "one"
        second_dir = tmp_path / "two"
        output_dir = tmp_path / "output"
        first_dir.mkdir()
        second_dir.mkdir()
        output_dir.mkdir()
        first = first_dir / "same.webm"
        second = second_dir / "same.webm"
        first.write_bytes(b"video-a")
        second.write_bytes(b"video-b")
        panel.out_row.resolve_dir = lambda _path: str(output_dir)
        panel._reserved_output_paths = set()
        task_a = panel._make_task(str(first))
        task_b = panel._make_task(str(second))
        assert task_a["output_path"].endswith("same_unwarped.mp4")
        assert task_b["output_path"].endswith("same_unwarped_1.mp4")
        assert task_a["runner_key"] == "video_unwarp"
    finally:
        worker = getattr(panel, "_info_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait(3000)
        panel.deleteLater()
        app.processEvents()


def test_video_unwarp_panel_reflows_and_auto_keeps_container(app_ctx,
                                                              tmp_path):
    """窄窗口表单降列；自动元数据修复保持源容器并显示目标摘要。"""
    from gui_qt.panels.video_unwarp_panel import VideoUnwarpPanelPage

    app, win, services = app_ctx
    panel = VideoUnwarpPanelPage(win, services)
    try:
        # 用户偏好可能保存为手动比例；此用例显式切回自动模式以验证容器契约。
        panel.apply_prefs({"ratio": "auto"})
        source = tmp_path / "source.mkv"
        source.write_bytes(b"video")
        panel.out_row.resolve_dir = lambda _path: str(tmp_path)
        panel._reserved_output_paths = set()
        task = panel._make_task(str(source))
        assert task["output_path"].endswith("source_unwarped.mkv")

        panel.resize(700, 720)
        panel.show()
        app.processEvents()
        assert panel.params_grid._columns == 1
        panel.resize(1100, 720)
        app.processEvents()
        assert panel.params_grid._columns == 2
    finally:
        worker = getattr(panel, "_info_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait(3000)
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_video_tools_has_one_active_mode_group_and_responsive_forms(app_ctx):
    """视频处理不再由两组同时可见的模式互相覆盖，窄窗口表单安全降列。"""
    from gui_qt import nav_registry as nr
    from gui_qt.components.page_header import PageHeader

    app, win, services = app_ctx
    panel = nr.find_item("video_tools")["factory"](win, services)
    try:
        panel.resize(1180, 760)
        panel.show()
        app.processEvents()
        assert panel.findChildren(PageHeader)
        assert panel.sg_category.currentRouteKey() == "basic"
        assert panel.w_basic_modes.isVisibleTo(panel)
        assert not panel.w_effect_modes.isVisibleTo(panel)
        assert panel.advanced_section.content.isHidden()

        panel.sg_category.setCurrentItem("effects")
        panel._mode_changed()
        app.processEvents()
        assert not panel.w_basic_modes.isVisibleTo(panel)
        assert panel.w_effect_modes.isVisibleTo(panel)
        assert panel.collect_params()["mode2"] == "reverse"

        panel.resize(760, 760)
        app.processEvents()
        assert panel.clip_grid._columns == 1
        assert panel.advanced_grid._columns == 2
        assert panel.delogo_grid._columns == 2
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_video_tools_rejects_invalid_clip_times_once(app_ctx, monkeypatch):
    """剪辑时间必须先整批校验，非法分钟/倒置区间不能进入任务队列。"""
    from gui_qt import nav_registry as nr
    from gui_qt.panels.video_edit_panel import _parse_time

    assert _parse_time("01:30") == 90
    assert _parse_time("00:60") is None
    assert _parse_time("-1") is None

    app, win, services = app_ctx
    panel = nr.find_item("video_tools")["factory"](win, services)
    warnings = []
    monkeypatch.setattr(
        "gui_qt.components.toast.show_warning",
        lambda _parent, message: warnings.append(message))
    try:
        panel.ed_start.setText("00:20")
        panel.ed_end.setText("00:10")
        assert not panel._validate_submission()
        assert len(warnings) == 1
        assert "结束时间" in warnings[0] or "End time" in warnings[0]
    finally:
        panel.deleteLater()
        app.processEvents()


def test_video_compress_page_has_contextual_actions_and_is_responsive(
        app_ctx, tmp_path):
    """视频压缩：操作归属明确、空态不可提交、预览跟随选中项。"""
    from gui_qt import nav_registry as nr
    from gui_qt.components.page_header import PageHeader

    app, win, services = app_ctx
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"a" * 11)
    second.write_bytes(b"b" * 17)
    panel = nr.find_item("video_compress")["factory"](win, services)
    try:
        panel.resize(1180, 760)
        panel.show()
        app.processEvents()
        header = panel.findChildren(PageHeader)[0]
        assert panel.action_bar.parent() is header
        assert not panel.btn_go.isEnabled()
        assert not panel.btn_preview.isEnabled()

        panel.file_card.add_files([str(first), str(second)])
        panel.file_card.table.selectRow(1)
        app.processEvents()
        assert panel.btn_go.isEnabled()
        assert panel.btn_preview.isEnabled()
        assert panel._selected_file() == str(second)
        assert panel.size_bar._before == 28
        assert panel.size_bar.parent() is panel.file_card

        panel.resize(760, 760)
        app.processEvents()
        assert panel.params_grid._columns == 1

        _runner_a, cancel_a = panel._new_task_runtime()
        _runner_b, cancel_b = panel._new_task_runtime()
        assert cancel_a.__self__ is not cancel_b.__self__
        assert services.task_manager._runner_factories["video_compress"] \
            == panel._restore_runner
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_settings_uses_expanded_horizontal_tabs(app_ctx):
    """11 个具体设置入口在同一行直接展开，每个入口对应独立分页。"""
    from PySide6.QtWidgets import QPushButton
    from gui_qt.pages.settings_page import SettingsPage
    app, win, services = app_ctx
    page = SettingsPage(win, services)
    try:
        page.resize(1400, 620)
        page.show()
        app.processEvents()
        assert page.section_tabs.isVisibleTo(page)
        assert not page.pivot.isVisible()
        assert not page.breadcrumb.isVisible()
        assert not hasattr(page, "group_tabs")
        visible = [button.routeKey() for button in page.section_tabs._buttons
                   if not button.isHidden()]
        assert visible == page._section_order
        assert page.section_tabs.currentTab().routeKey() == "general"
        assert not page.findChildren(QPushButton, "settingsTabArrow")
        page.section_tabs.tab("appearance").click()
        page.section_tabs.tab("convert").click()
        checked = [button.routeKey() for button in page.section_tabs._buttons
                   if button.isChecked()]
        assert checked == ["convert"]
        app.processEvents()
        assert page.pivot.currentRow() == page._section_order.index("convert")
        assert page.sg.currentWidget() == page._sections["convert"][1]
        assert page._sections["advanced"][1] is not page._sections["network"][1]
        page.resize(1100, 700)
        app.processEvents()
        assert page.section_tabs.currentTab().routeKey() == "convert"
        assert not page.pivot.isVisible()
        assert not page.breadcrumb.isVisible()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_settings_tabs_scroll_horizontally_when_narrow(app_ctx):
    """窄窗口仍保留全部入口，并通过单行横向滚动访问尾部标签。"""
    from gui_qt.pages.settings_page import SettingsPage

    app, win, services = app_ctx
    page = SettingsPage(win, services)
    try:
        page.resize(420, 620)
        page.show()
        app.processEvents()
        visible = [button.routeKey() for button in page.section_tabs._buttons
                   if not button.isHidden()]
        assert visible == page._section_order
        assert page.section_tabs._scroll.horizontalScrollBar().maximum() > 0
        page.section_tabs.setCurrentTab("about")
        app.processEvents()
        assert page.section_tabs.currentTab().routeKey() == "about"
        assert page.pivot.currentRow() == page._section_order.index("about")
        assert page.sg.currentWidget() == page._sections["about"][1]
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_primary_task_action_lives_in_page_header(app_ctx):
    """状态、进度和命令必须作为一个整体位于标题右侧。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.image_panel import ImagePanelPage

    app, win, services = app_ctx
    panel = ImagePanelPage(win, services)
    try:
        panel.resize(1100, 760)
        panel.show()
        app.processEvents()
        header = panel.findChild(PageHeader)
        assert header is not None
        # 图片页直接复用标准标题的副标题插槽，不再挂载外置说明标签。
        assert header.subtitle_label is not None
        assert header.subtitle_label.parent() is header
        assert "缩放" in header.subtitle_label.text()
        assert panel.action_bar.parent() is header
        assert panel.action_bar.btn_go.parent() is panel.action_bar
        assert panel.action_bar.btn_cancel.parent() is panel.action_bar
        assert panel.action_bar.status_label.parent() is panel.action_bar
        assert panel.action_bar.status_dot.parent() is panel.action_bar
        assert panel.action_bar.status_dot.size().width() == 10
        assert panel.action_bar.status_dot.property("state") == "idle"
        assert panel.action_bar.bar_total.parent() is panel.action_bar
        assert panel.action_bar.property("headerInline") is True
        assert panel.action_bar.height() == 40
        assert panel.content_layout.indexOf(panel.action_bar) == -1
        assert header.progress_layout.indexOf(panel.action_bar) == -1
        assert header.action_layout.indexOf(panel.action_bar) >= 0
        assert panel.action_bar.layout().indexOf(panel.action_bar.btn_go) >= 0
        assert panel.action_bar.bar_total.maximumWidth() == 220
        assert panel.action_bar._idle_spacer.isHidden()
        assert panel.action_bar.status_dot.x() <= 4
        assert panel.action_bar.status_label.x() < 60
        assert panel.action_bar.btn_go.x() > panel.action_bar.status_label.x()
        assert header.height() == 60
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_document_panel_detects_common_targets_for_whole_batch(app_ctx,
                                                               tmp_path):
    """文档批处理必须按整批文件求共同目标，不能只看第一项。"""
    from gui_qt.panels.doc_panel import DocPanelPage

    app, win, services = app_ctx
    panel = DocPanelPage(win, services)
    try:
        pdf = tmp_path / "账单.pdf"
        ofd = tmp_path / "发票.ofd"
        pdf.write_bytes(b"%PDF-1.4")
        ofd.write_bytes(b"OFD")
        panel.file_card.add_files([str(pdf), str(ofd)])
        app.processEvents()

        assert panel._target_exts == []
        assert not panel.cb_tgt.isEnabled()
        assert not panel.action_bar.btn_go.isEnabled()
        assert "PDF" in panel.source_label.text()
        assert "OFD" in panel.source_label.text()
        assert "分批" in panel.target_hint.text()

        panel.file_card.clear_files()
        docx = tmp_path / "合同.docx"
        text_file = tmp_path / "备注.txt"
        docx.write_bytes(b"PK")
        text_file.write_text("中文", encoding="utf-8")
        panel.file_card.add_files([str(docx), str(text_file)])
        app.processEvents()

        assert panel._target_exts == [".pdf", ".html", ".pptx", ".md", ".xlsx"]
        assert panel._selected_target_ext() == ".pdf"
        assert panel.action_bar.btn_go.isEnabled()
        assert "5" in panel.target_hint.text()
        assert "不会合并" in panel.target_hint.text()
        assert panel.need_ffmpeg is False
        panel.resize(760, 720)
        panel.show()
        app.processEvents()
        assert panel.params_grid._columns == 1
        panel.file_card.clear_files()
        assert "添加文件" in panel.target_hint.text()
        assert not panel.action_bar.btn_go.isEnabled()
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_document_preview_follows_selection_and_removal(app_ctx, tmp_path,
                                                      monkeypatch):
    """预览文件名与实际打开文件一致，移除所选网页后回退到剩余网页。"""
    from types import SimpleNamespace
    from gui_qt.panels.doc_panel import DocPanelPage

    app, win, services = app_ctx
    panel = DocPanelPage(win, services)
    opened = []
    monkeypatch.setitem(sys.modules, "gui_qt.components.html_preview",
                        SimpleNamespace(HtmlPreviewDialog=lambda path, parent:
                                        SimpleNamespace(exec=lambda: opened.append(path))))
    try:
        first = tmp_path / "第一份.html"
        second = tmp_path / "第二份.HTM"
        note = tmp_path / "说明.txt"
        for path in (first, second, note):
            path.write_text("预览测试", encoding="utf-8")
        assert panel.preview_row.isHidden()
        assert not panel.btn_preview_html.isEnabled()
        panel.file_card.add_files([str(first), str(second), str(note)])
        assert panel._selected_html_file() == str(first)
        panel.file_card.table.selectRow(1)
        assert panel.preview_source_label.text() == second.name
        assert panel.preview_source_label.toolTip() == str(second)
        panel._preview_html()
        assert opened == [str(second)]
        panel.file_card.remove_row(1)
        assert panel._selected_html_file() == str(first)
        assert panel.preview_source_label.text() == first.name
        panel.file_card.table.selectRow(1)
        assert panel._selected_html_file() == str(first)
        panel.file_card.clear_files()
        assert panel.preview_row.isHidden()
        assert not panel.btn_preview_html.isEnabled()
        assert panel.preview_source_label.text() == ""
    finally:
        panel.deleteLater()
        app.processEvents()


def test_document_keeps_valid_target_when_batch_changes(app_ctx, tmp_path):
    """新增兼容文件不重置目标，混合格式说明随整批共同目标更新。"""
    from gui_qt.panels.doc_panel import DocPanelPage

    app, win, services = app_ctx
    panel = DocPanelPage(win, services)
    try:
        first = tmp_path / "first.md"
        second = tmp_path / "second.html"
        first.write_text("# Test", encoding="utf-8")
        second.write_text("<h1>Test</h1>", encoding="utf-8")
        panel.file_card.add_files([str(first)])
        panel.cb_tgt.setCurrentIndex(panel._target_exts.index(".txt"))
        panel.file_card.add_files([str(second)])
        assert panel.collect_params()["target"] == ".txt"
        assert str(len(panel._target_exts)) in panel.target_hint.text()
        panel.resize(720, 900)
        panel.show()
        app.processEvents()
        assert panel.params_grid._columns == 1
        assert panel.source_label.wordWrap()
        assert panel.preview_source_label.wordWrap()
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_gif_panel_uses_precise_range_controls_and_responsive_grid(app_ctx):
    """GIF 页可精确输入时间，“到结尾”不应保留无效时长控件。"""
    from gui_qt.panels.gif_panel import GifPanelPage

    app, win, services = app_ctx
    panel = GifPanelPage(win, services)
    try:
        assert not panel.action_bar.btn_go.isEnabled()
        # 页面会恢复用户记忆；固定时长测试必须主动选择模式，不能依赖本机偏好。
        panel.cb_all.setChecked(False)
        panel.sb_start.setValue(2.5)
        panel.sb_dur.setValue(6.5)
        params = panel.collect_params()
        assert params["start"] == 2.5
        assert params["duration"] == 6.5
        assert "2.5–9.0" in panel.range_summary.text()

        panel.cb_all.setChecked(True)
        assert not panel.sb_dur.isEnabled()
        assert panel.collect_params()["duration"] is None
        assert "结尾" in panel.range_summary.text()
        assert "6.5" not in panel.range_summary.text()

        panel.resize(760, 720)
        panel.show()
        app.processEvents()
        assert panel.params_grid._columns == 1
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_gif_range_mode_preferences_preserve_duration(app_ctx):
    """到结尾模式不丢弃记忆时长，返回固定时长后摘要与提交值一致。"""
    from gui_qt.panels.gif_panel import GifPanelPage

    app, win, services = app_ctx
    panel = GifPanelPage(win, services)
    try:
        panel.apply_prefs({"start": 1.5, "duration": 4.5, "all_duration": True})
        assert panel.collect_params()["duration"] is None
        assert not panel.sb_dur.isEnabled()
        assert panel.collect_prefs()["duration"] == 4.5
        assert "结尾" in panel.range_summary.text()
        panel.cb_all.setChecked(False)
        assert panel.collect_params()["duration"] == 4.5
        assert panel.sb_dur.isEnabled()
        assert "1.5–6.0" in panel.range_summary.text()
        panel.apply_prefs({"duration": "全部"})
        assert panel.cb_all.isChecked()
        assert panel.collect_params()["duration"] is None
    finally:
        panel.deleteLater()
        app.processEvents()


def test_gif_filmstrip_uses_selection_and_only_applies_confirmed_range(
        app_ctx, tmp_path, monkeypatch):
    """预览目标与选中行一致；取消不改参数，确认后切回固定时长。"""
    from types import SimpleNamespace
    from PySide6.QtCore import QRect
    from gui_qt.panels.gif_panel import GifPanelPage
    from gui_qt.components import toast

    app, win, services = app_ctx
    panel = GifPanelPage(win, services)
    opened = []
    result = [None]

    def dialog(path, parent):
        opened.append(path)
        return SimpleNamespace(move=lambda *_: None, rect=lambda: QRect(0, 0, 900, 600),
                               exec=lambda: None, range_secs=lambda: result[0])

    monkeypatch.setitem(sys.modules, "gui_qt.components.gif_filmstrip",
                        SimpleNamespace(GifFilmstripDialog=dialog))
    monkeypatch.setattr(toast, "show_success", lambda *_: None)
    try:
        assert not panel.btn_film.isEnabled()
        first = tmp_path / "first.mp4"
        second = tmp_path / "second.mov"
        for path in (first, second):
            path.write_bytes(b"video")
        panel.file_card.add_files([str(first), str(second)])
        assert panel.btn_film.isEnabled()
        assert panel._selected_film_file() == str(first)
        panel.file_card.table.selectRow(1)
        assert panel.film_source_label.text() == second.name
        assert panel.film_source_label.toolTip() == str(second)
        panel.cb_all.setChecked(True)
        before = panel.collect_params()
        panel._open_filmstrip()
        assert opened == [str(second)]
        assert panel.collect_params() == before
        result[0] = (2.5, 9.0)
        panel._open_filmstrip()
        assert panel.collect_params()["start"] == 2.5
        assert panel.collect_params()["duration"] == 6.5
        assert not panel.cb_all.isChecked()
        assert "2.5–9.0" in panel.range_summary.text()
        panel.file_card.remove_row(1)
        assert panel._selected_film_file() == str(first)
        assert panel.film_source_label.text() == first.name
        panel.file_card.clear_files()
        assert not panel.btn_film.isEnabled()
        assert "添加视频" in panel.film_source_label.text()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_ebook_panel_excludes_same_format_and_never_requires_ffmpeg(app_ctx,
                                                                   tmp_path):
    """电子书页不显示同格式目标，且内置转换不受 FFmpeg 状态影响。"""
    from gui_qt.panels.ebook_panel import EbookPanelPage

    app, win, services = app_ctx
    panel = EbookPanelPage(win, services)
    try:
        source = tmp_path / "book.epub"
        source.write_bytes(b"epub")
        panel.file_card.add_files([str(source)])
        app.processEvents()
        targets = [panel.cb_dst.itemText(index)
                   for index in range(panel.cb_dst.count())]
        assert "EPUB" not in targets
        assert panel.cb_dst.currentText() == "TXT"
        assert panel.action_bar.btn_go.isEnabled()
        assert panel.need_ffmpeg is False
        assert "内置" in panel.engine_label.text()

        panel.resize(760, 720)
        panel.show()
        app.processEvents()
        assert panel.params_grid._columns == 1
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_ebook_calibre_recheck_recovers_and_start_revalidates(app_ctx, tmp_path,
                                                          monkeypatch):
    """缺依赖禁用执行，重新检测可恢复；执行前依赖消失时不得继续提交。"""
    from gui_qt.panels import ebook_panel

    app, win, services = app_ctx
    available = [None]
    monkeypatch.setattr(ebook_panel, "_find_calibre_convert", lambda: available[0])
    panel = ebook_panel.EbookPanelPage(win, services)
    submitted = []
    monkeypatch.setattr(panel, "_submit_files", lambda: submitted.append(True))
    monkeypatch.setattr(ebook_panel.toast, "show_warning", lambda *_: None)
    try:
        source = tmp_path / "book.txt"
        source.write_text("Book text", encoding="utf-8")
        panel.file_card.add_files([str(source)])
        panel.cb_dst.setCurrentText("MOBI")
        assert not panel.action_bar.btn_go.isEnabled()
        assert not panel.engine_actions.isHidden()
        assert "Calibre" in panel.action_bar.btn_go.toolTip()
        available[0] = "/mock/ebook-convert"
        panel.btn_recheck.click()
        assert panel.action_bar.btn_go.isEnabled()
        assert "已就绪" in panel.engine_label.text()
        available[0] = None
        assert panel._start() is False
        assert not submitted
        assert not panel.action_bar.btn_go.isEnabled()
        panel.cb_dst.setCurrentText("EPUB")
        assert panel.action_bar.btn_go.isEnabled()
        assert panel.engine_actions.isHidden()
        assert "内置" in panel.engine_label.text()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_ebook_azw3_source_requires_calibre_for_text_output(app_ctx, tmp_path,
                                                       monkeypatch):
    """TXT 目标也可能因 AZW3 输入需要外部引擎，移除后恢复内置转换。"""
    from gui_qt.panels import ebook_panel

    app, win, services = app_ctx
    monkeypatch.setattr(ebook_panel, "_find_calibre_convert", lambda: None)
    panel = ebook_panel.EbookPanelPage(win, services)
    try:
        first = tmp_path / "book.azw3"
        second = tmp_path / "book.epub"
        first.write_bytes(b"azw3")
        second.write_bytes(b"epub")
        panel.file_card.add_files([str(first), str(second)])
        panel.cb_dst.setCurrentText("TXT")
        assert not panel.action_bar.btn_go.isEnabled()
        assert "不保留图片" in panel.target_hint.text()
        panel.file_card.remove_row(0)
        assert panel.cb_dst.currentText() == "TXT"
        assert panel.action_bar.btn_go.isEnabled()
        assert panel.engine_actions.isHidden()
        panel.file_card.clear_files()
        assert not panel.action_bar.btn_go.isEnabled()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_ebook_html_alias_and_exhausted_targets(app_ctx, tmp_path):
    """.htm 不得再选择 HTML；批次覆盖全部目标时提示分批而非显示可用引擎。"""
    from gui_qt.panels.ebook_panel import EbookPanelPage

    app, win, services = app_ctx
    panel = EbookPanelPage(win, services)
    try:
        paths = [tmp_path / ("book" + ext)
                 for ext in (".HTM", ".epub", ".txt", ".mobi", ".azw3")]
        for path in paths:
            path.write_bytes(b"book")
        panel.file_card.add_files([str(paths[0])])
        assert "HTML" not in [panel.cb_dst.itemText(i) for i in range(panel.cb_dst.count())]
        panel.file_card.add_files([str(path) for path in paths[1:]])
        assert not panel.cb_dst.isEnabled()
        assert not panel.action_bar.btn_go.isEnabled()
        assert "分批" in panel.target_hint.text()
        assert "暂无" in panel.engine_label.text()
        assert panel.engine_actions.isHidden()
        panel.file_card.clear_files()
        assert panel.cb_dst.isEnabled()
        assert not panel.action_bar.btn_go.isEnabled()
        assert "EPUB" in panel.target_hint.text()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_download_reveals_format_list_only_after_parse(app_ctx):
    """下载页初始不为空格式列表预留大块空间。"""
    from gui_qt.panels.download_panel import DownloadPanelPage
    app, win, services = app_ctx
    page = DownloadPanelPage(win, services)
    try:
        page.show()
        app.processEvents()
        assert not page.format_row.isVisible()
        assert page.lb_queue_empty.isVisible()
        assert not page.lst_queue.isVisible()
        page._on_formats([
            {"format_id": "18", "ext": "mp4", "resolution": "720p"}
        ], "Example", None)
        app.processEvents()
        assert page.format_row.isVisible()
        assert page.lst_formats.count() == 1
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_responsive_home_components_relayout(app_ctx):
    """首页快捷入口始终单行，编辑入口独立且不占用功能卡位。"""
    from gui_qt.components.quick_function_row import (
        DEFAULT_SHORTCUTS, QuickFunctionRow,
    )
    row = QuickFunctionRow()
    try:
        row._relayout(4)
        assert row._columns == len(DEFAULT_SHORTCUTS)
        assert row.shortcut_ids() == DEFAULT_SHORTCUTS
        assert [item.shortcut_id for item in row.items] == DEFAULT_SHORTCUTS
        assert row._grid.indexOf(row.items[4]) >= 0
        index = row._grid.indexOf(row.items[4])
        grid_row, grid_col, _row_span, _col_span = row._grid.getItemPosition(index)
        assert (grid_row, grid_col) == (0, 4)
        edit_index = row._grid.indexOf(row.edit_button)
        edit_row, edit_col, _row_span, _col_span = row._grid.getItemPosition(edit_index)
        assert (edit_row, edit_col) == (0, len(DEFAULT_SHORTCUTS))
        assert row.more_item.shortcut_id == "more"

        row.set_shortcuts(["audio", "plugin:not-installed", "audio"])
        assert row.shortcut_ids() == ["audio"]
        assert len(row.items) == 1
        for item in row.items:
            index = row._grid.indexOf(item)
            grid_row, _grid_col, _row_span, _col_span = row._grid.getItemPosition(index)
            assert grid_row == 0
    finally:
        row.deleteLater()


def test_home_plugin_shortcut_keeps_home_context():
    """首页插件快捷入口打开工具窗时不得先切换到底层插件中心。"""
    from types import SimpleNamespace
    from gui_qt.pages.home_page import HomePage

    opened = []

    class _PluginPage:
        def _ensure(self):
            return self

        def open_plugin(self, plugin_id, dialog_parent=None):
            opened.append((plugin_id, dialog_parent))

    switches = []
    dialog_parent = object()
    home = SimpleNamespace(main_window=SimpleNamespace(
        pages={"plugins": _PluginPage()},
        switchTo=lambda page: switches.append(page)),
        window=lambda: dialog_parent)
    HomePage._open_plugin_shortcut(home, "json_formatter")
    assert opened == [("json_formatter", dialog_parent)]
    assert switches == []


def test_home_file_entry_reuses_application_route(tmp_path):
    """首页主入口必须复用统一扩展名路由，并明确报告不支持的类型。"""
    from types import SimpleNamespace
    from gui_qt.app import _auto_open_convert_file

    added = []
    page = SimpleNamespace(file_card=SimpleNamespace(
        add_files=lambda paths: added.extend(paths)))
    switched = []
    window = SimpleNamespace(
        pages={"video": page}, switchTo=lambda target: switched.append(target))
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")

    assert _auto_open_convert_file(window, str(source)) is True
    assert switched == [page]
    assert added == [str(source)]
    assert _auto_open_convert_file(window, str(tmp_path / "sample.unknown")) is False


def test_home_folder_entry_opens_format_detection(tmp_path):
    """文件夹入口应携带路径进入格式检测，而不是把目录误当作单个文件。"""
    from types import SimpleNamespace
    from gui_qt.pages.home_page import HomePage

    path_field = SimpleNamespace(text="", setText=lambda value: setattr(
        path_field, "text", value))
    real_page = SimpleNamespace(ed_path=path_field)
    lazy_page = SimpleNamespace(_ensure=lambda: real_page)
    switched = []
    home = SimpleNamespace(main_window=SimpleNamespace(
        pages={"format_detect": lazy_page},
        switchTo=lambda page: switched.append(page)))

    HomePage._route_convert_folder(home, str(tmp_path))

    assert path_field.text == str(tmp_path)
    assert switched == [lazy_page]


def test_stat_card_missing_value_is_neutral(app_ctx):
    """无数据占位符不得因为包含减号而显示为失败红色。"""
    from qfluentwidgets import FluentIcon
    from gui_qt.components import design_system as ds
    from gui_qt.components.stat_card_new import StatCard

    app, _win, _services = app_ctx
    card = StatCard("成功率", "--", "--", "#F0A63A", FluentIcon.ACCEPT)
    try:
        card.set_delta("--")
        assert ds.ink_dis() in card.delta_label.styleSheet()
        card.set_delta("较昨日 -10%", tone="neutral")
        assert ds.ink_dis() in card.delta_label.styleSheet()
    finally:
        card.deleteLater()
        app.processEvents()


def test_macos_gpu_ignores_color_lcd(monkeypatch):
    """system_profiler 的屏幕名称 Color LCD 不能被当成显卡型号。"""
    from gui_qt.components import sysinfo

    monkeypatch.setattr(sysinfo.sys, "platform", "darwin")
    monkeypatch.setattr(sysinfo, "_gpu_cache", None)
    monkeypatch.setattr(sysinfo, "_mac_system_profiler", lambda _kind: {
        "SPDisplaysDataType": [{
            "_name": "Apple M2",
            "spdisplays_ndrvs": [{"_name": "Color LCD"}],
        }]
    })
    assert sysinfo.gpu_info() == "Apple M2"


def test_macos_cpu_uses_chip_type_not_machine_name(monkeypatch):
    """Apple Silicon 处理器必须显示芯片型号，不能误显示 MacBook 机型。"""
    from gui_qt.components import sysinfo

    monkeypatch.setattr(sysinfo.sys, "platform", "darwin")
    monkeypatch.setattr(sysinfo, "_mac_system_profiler", lambda _kind: {
        "SPHardwareDataType": [{
            "chip_type": "Apple M2",
            "machine_name": "MacBook Air",
        }]
    })
    assert sysinfo._cpu_name() == "Apple M2"


def test_home_desktop_system_info_spans_full_width(app_ctx):
    """运行状态固定展开，并按窗口宽度选择完整且紧凑的排列。"""
    from gui_qt.pages.home_page import (
        RUNTIME_STATUS_COMPACT_HEIGHT, RUNTIME_STATUS_HEIGHT, HomePage)

    app, win, services = app_ctx
    page = HomePage(win, services)
    try:
        assert not page.environment_content.isHidden()
        assert not hasattr(page, "btn_environment")
        page._relayout_main(False)
        assert page.environment_card.height() == RUNTIME_STATUS_HEIGHT
        index = page.main_grid.indexOf(page.sysinfo)
        row, column, row_span, column_span = page.main_grid.getItemPosition(index)
        assert (row, column, row_span, column_span) == (0, 1, 1, 1)
        assert page.sysinfo._horizontal is True

        page._relayout_main(True)
        assert page.environment_card.height() == RUNTIME_STATUS_COMPACT_HEIGHT
        index = page.main_grid.indexOf(page.sysinfo)
        row, column, row_span, column_span = page.main_grid.getItemPosition(index)
        assert (row, column, row_span, column_span) == (1, 0, 1, 1)
        assert page.sysinfo._horizontal is False
    finally:
        page.deleteLater()
        app.processEvents()


def test_home_recent_tasks_has_fixed_height_and_limit(app_ctx):
    """首页仅保留固定数量的最近任务，完整历史由历史页承载。"""
    from types import SimpleNamespace
    from gui_qt.components.recent_tasks_table import (
        RECENT_TASK_LIMIT, RECENT_TASKS_HEIGHT)
    from gui_qt.pages.home_page import HomePage

    app, win, services = app_ctx
    tasks = [SimpleNamespace(
        task_id=str(index), state="success", created_at=index,
        file_path=f"{index}.png", name=f"task-{index}", output_path="out.jpg")
        for index in range(RECENT_TASK_LIMIT + 3)]
    services.task_manager.all_tasks = lambda: tasks
    page = HomePage(win, services)
    try:
        assert page.recent_tasks.height() == RECENT_TASKS_HEIGHT
        assert len(page.recent_tasks._rows) == RECENT_TASK_LIMIT
    finally:
        page.deleteLater()
        app.processEvents()


def test_home_preset_explains_parameters_and_opens_matching_tool(monkeypatch):
    """首页预设应说明用途，点击后应用参数并进入对应工具。"""
    from types import SimpleNamespace
    from gui_qt.components import toast
    from gui_qt.pages.home_page import (
        HomePage, _preset_summary, _preset_target, _preset_tool_label)

    panels = {"video": {
        "fmt": "mp4", "codec": "H.264", "res": "1920x1080"}}
    assert _preset_target(panels)[0] == "video"
    assert _preset_tool_label("video") == "视频转换"
    assert _preset_summary(panels["video"]) == "MP4 · H.264 · 1920x1080"

    applied = []
    opened = []
    messages = []
    video_page = SimpleNamespace(apply_prefs=lambda prefs: applied.append(prefs))
    home = SimpleNamespace(
        saved_presets=SimpleNamespace(
            store=SimpleNamespace(load=lambda _name: panels)),
        main_window=SimpleNamespace(pages={"video": video_page}),
        _nav_to=lambda key: opened.append(key))
    monkeypatch.setattr(
        toast, "show_success", lambda _parent, message: messages.append(message))

    HomePage._apply_saved_preset(home, "1080P MP4")

    assert applied == [panels["video"]]
    assert opened == ["video"]
    assert messages and "请选择文件" in messages[-1]


def test_home_manage_presets_opens_exact_settings_section():
    """首页管理按钮应直达转换预设，而不是停在设置首页。"""
    from types import SimpleNamespace
    from gui_qt.pages.home_page import HomePage

    selected = []
    opened = []
    settings = SimpleNamespace(section_tabs=SimpleNamespace(
        setCurrentTab=lambda key: selected.append(key)))
    wrapper = SimpleNamespace(_ensure=lambda: settings)
    home = SimpleNamespace(main_window=SimpleNamespace(
        pages={"settings": wrapper},
        switchTo=lambda page: opened.append(page)))

    HomePage._open_preset_settings(home)

    assert selected == ["presets"]
    assert opened == [wrapper]


def test_light_navigation_selected_text_has_explicit_contrast(monkeypatch):
    """亮色侧栏选中项必须使用深色前景，不能沿用深色主题缓存。"""
    from gui_qt import nav_style

    monkeypatch.setattr(nav_style, "isDarkTheme", lambda: False)
    assert nav_style._selected_text_color().name() == "#202124"
    monkeypatch.setattr(nav_style, "isDarkTheme", lambda: True)
    assert nav_style._selected_text_color().name() == "#f5f5f7"


def test_fluent_dialog_does_not_retry_window_activation(app_ctx):
    """macOS 弹窗关闭期间不得被定时 raise，避免按钮后闪烁两次。"""
    from gui_qt.components.dialog import FluentDialogBase

    app, _win, _services = app_ctx
    dialog = FluentDialogBase("activation regression")
    try:
        dialog.show()
        app.processEvents()
        dialog.accept()
        app.processEvents()
        assert not hasattr(dialog, "_ensure_active_window")
        assert not hasattr(dialog, "_active_retries")
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_fluent_dialog_closes_once_and_hides_immediately(app_ctx):
    """公共 Fluent 弹窗一次关闭即消失，重复完成不得再次发射 finished。"""
    from PySide6.QtWidgets import QDialog
    from gui_qt.components.dialog import FluentDialogBase

    app, _win, _services = app_ctx
    dialog = FluentDialogBase("single close regression")
    finished_codes = []
    dialog.finished.connect(finished_codes.append)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.isVisible()

        dialog.reject()
        # 不等待下一轮事件循环：用户点击后当前帧就必须看不到弹窗。
        assert not dialog.isVisible()
        assert not dialog.updatesEnabled()
        dialog.reject()
        app.processEvents()
        assert finished_codes == [QDialog.Rejected]
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_fluent_dialog_native_close_uses_single_hide_path(app_ctx):
    """标题栏交通灯关闭不得再叠加主动 hide，否则 macOS 会闪烁一次。"""
    from PySide6.QtWidgets import QDialog
    from gui_qt.components.dialog import FluentDialogBase

    app, _win, _services = app_ctx
    dialog = FluentDialogBase("native close regression")
    finished_codes = []
    dialog.finished.connect(finished_codes.append)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.close()
        app.processEvents()
        assert not dialog.isVisible()
        # 原生关闭由 QDialog/Cocoa 自己隐藏，不应执行按钮路径的禁用重绘。
        assert dialog.updatesEnabled()
        assert finished_codes == [QDialog.Rejected]
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_quick_editor_opens_nonblocking_and_applies_after_close(
        app_ctx, monkeypatch):
    """快捷编辑器用 open() 单次关闭，不进入 exec() 嵌套事件循环。"""
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QDialog
    from gui_qt.components import quick_function_row as module

    app, _win, _services = app_ctx

    class _FakeDialog(QObject):
        finished = Signal(int)
        latest = None

        def __init__(self, _candidates, _selected_ids, _parent=None):
            super().__init__()
            self.result = ["image"]
            self.open_count = 0
            self.raise_count = 0
            type(self).latest = self

        def open(self):
            self.open_count += 1

        def raise_(self):
            self.raise_count += 1

    monkeypatch.setattr(module, "_ShortcutEditorDialog", _FakeDialog)
    row = module.QuickFunctionRow()
    try:
        row._open_editor()
        app.processEvents()
        dialog = _FakeDialog.latest
        assert dialog is not None and dialog.open_count == 1
        dialog.finished.emit(QDialog.Accepted)
        app.processEvents()
        app.processEvents()
        assert row.shortcut_ids() == ["image"]
        assert row._editor_dialog is None
    finally:
        row.deleteLater()
        app.processEvents()


def test_plugin_panel_window_closes_without_nested_event_loop(app_ctx):
    """插件工具窗使用 open() 单实例展示，交通灯关闭后一次释放。"""
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDialog, QWidget
    from gui_qt.panels.plugin_panel import PluginPanelPage

    app, win, services = app_ctx
    page = PluginPanelPage(win, services)
    try:
        page._plugins = [SimpleNamespace(
            name="测试插件", description="", panel_class=QWidget)]
        page._open_panel(0)
        app.processEvents()
        dialog = page._panel_dialog
        assert dialog is not None
        assert dialog.isVisible()

        # 连续触发入口不能生成第二个模态窗口。
        page._open_panel(0)
        assert page._panel_dialog is dialog

        finished_codes = []
        dialog.finished.connect(finished_codes.append)
        assert dialog.close()
        app.processEvents()
        assert page._panel_dialog is None
        assert finished_codes == [QDialog.Rejected]
    finally:
        if page._panel_dialog is not None:
            page._panel_dialog.close()
        page.deleteLater()
        app.processEvents()


def test_action_bar_running_state(app_ctx):
    """任务运行和终态同步切换主操作、取消操作与语义指示灯。"""
    from gui_qt.widgets import ActionBar, ActionStatusState
    bar = ActionBar()
    try:
        bar.set_running(True)
        assert not bar.btn_go.isEnabled()
        assert bar.btn_cancel.isEnabled()
        assert not bar.bar_total.isHidden()
        assert bar._idle_spacer.isHidden()
        assert bar.status_dot.property("state") == "running"
        bar.set_running(False)
        assert bar.btn_go.isEnabled()
        assert not bar.btn_cancel.isEnabled()
        assert bar.bar_total.isHidden()
        assert bar._idle_spacer.isHidden()
        assert bar.status_dot.property("state") == "idle"
        for state in (ActionStatusState.SUCCESS, ActionStatusState.WARNING,
                      ActionStatusState.ERROR):
            bar.set_status("state", state)
            assert bar.status_dot.property("state") == state.value
        bar.set_batch_result(2)
        assert bar.status_label.text() == "全部处理完成（2 个任务）"
        assert bar.status_dot.property("state") == "success"
        bar.set_batch_result(1, 0, 1)
        assert "1 取消" in bar.status_label.text()
        assert bar.status_dot.property("state") == "warning"
    finally:
        bar.deleteLater()


def test_exception_pages_use_header_command_pattern(app_ctx):
    """检测、媒体信息和二维码不能再把主操作留在页面底部。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.detect_panel import DetectPanelPage
    from gui_qt.panels.mediainfo_panel import MediaInfoPanelPage
    from gui_qt.panels.qrcode_panel import QrcodePanelPage

    app, win, services = app_ctx
    pages = [DetectPanelPage(win, services),
             MediaInfoPanelPage(win, services),
             QrcodePanelPage(win, services)]
    try:
        for page in pages:
            page.resize(1100, 760)
            page.show()
            app.processEvents()
            header = page.findChild(PageHeader)
            assert page.action_bar.parent() is header
            assert header.action_layout.indexOf(page.action_bar) >= 0
            assert page.content_layout.indexOf(page.action_bar) == -1
        assert pages[2].btn_save.parent() is pages[2].header
        assert not pages[2].btn_save.isEnabled()
    finally:
        for page in pages:
            page.close()
            page.deleteLater()
        app.processEvents()


def test_m3u8_header_action_aligns_to_content_right(app_ctx):
    """M3U8 特殊页也必须让标题占满内容宽度，操作组位于右侧。"""
    from gui_qt.components.page_header import PageHeader

    app, _win, _services = app_ctx
    page = _m3u8_panel(app_ctx)
    try:
        page.resize(1200, 760)
        page.show()
        app.processEvents()
        header = page.findChild(PageHeader)
        assert header is not None
        assert page.content_layout.indexOf(header) == 0
        assert header.width() >= page.content.width() - 60
        assert header.action_layout.indexOf(page.action_bar) >= 0
        assert page.action_bar.x() > header.width() // 2
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_macos_title_bar_uses_native_window_controls():
    """macOS 不应同时显示右侧自绘窗口按钮与左侧系统交通灯。"""
    import inspect
    from PySide6.QtCore import QSize
    from gui_qt.app import MainWindow

    class _WindowState:
        @staticmethod
        def isFullScreen():
            return False

    # qfluentwidgets 默认返回 size.width() - 75，导致原生交通灯被强制放到
    # 右上角。主窗口必须覆盖为左侧 0..75 的 Cocoa 标准区域。
    rect = MainWindow.systemTitleBarRect(_WindowState(), QSize(1440, 38))
    assert rect.x() == 0
    assert rect.y() == 8
    assert rect.width() == 75
    source = inspect.getsource(MainWindow._configure_platform_title_bar)
    assert "setSystemTitleBarButtonVisible(True)" in source
    assert "buttonLayout.removeWidget(button)" in source
    assert "button.hide()" in source
    assert "self.titleBar.height() + 6" in source
    assert "panel.vBoxLayout.setContentsMargins" in source
    show_source = inspect.getsource(MainWindow.showEvent)
    assert "QTimer.singleShot(0, self._configure_platform_title_bar)" in show_source
    resize_source = inspect.getsource(MainWindow.resizeEvent)
    assert "self.titleBar.move(0, 0)" in resize_source
    assert "(self.width() - brand.width()) // 2" in resize_source
    setup_source = inspect.getsource(MainWindow._setup_title_buttons)
    assert "WA_TransparentForMouseEvents" in setup_source
    assert "bar.hBoxLayout.removeWidget(bar.iconLabel)" in setup_source
    assert "lay.setContentsMargins(0, 6, 10, 0)" in setup_source


def test_task_panel_replaces_last_progress_with_terminal_status(app_ctx):
    """任务完成后底栏不能继续显示转换器最后上报的“保存中”。"""
    from gui_qt import task_manager as tm
    from gui_qt.panels.image_panel import ImagePanelPage
    app, win, services = app_ctx
    panel = ImagePanelPage(win, services)
    try:
        task = tm.Task(
            task_id=9101, name="sample.jpg", task_type="image",
            file_path="sample.jpg", output_path="sample.png",
            state=tm.SUCCESS, progress=100)
        services.task_manager._tasks[task.task_id] = task
        panel._task_rows[task.task_id] = (task.file_path, -1)
        panel.action_bar.set_status("sample.jpg  保存中...")
        panel._on_state(task.task_id, tm.SUCCESS)
        app.processEvents()
        assert "保存中" not in panel.action_bar.status_label.text()
        assert panel.action_bar.status_label.text() == "全部处理完成（1 个任务）"
        assert panel.action_bar.status_dot.property("state") == "success"
        # 该测试没有向文件列表添加输入，终态后主操作应保持禁用；真实页面
        # 若文件仍在列表中会自动重新启用，避免空提交与按钮语义不一致。
        assert not panel.action_bar.btn_go.isEnabled()
        assert not panel.action_bar.btn_cancel.isEnabled()
    finally:
        services.task_manager._tasks.pop(9101, None)
        panel.deleteLater()
        app.processEvents()


def test_video_panel_keeps_promoted_cancel_visible_while_running(app_ctx, tmp_path,
                                                                  monkeypatch):
    """标题栏提升后的取消按钮必须随任务运行显示，终态不能残留“保存中”。"""
    from gui_qt import task_manager as tm
    from gui_qt.panels.video_panel import VideoPanelPage
    app, win, services = app_ctx
    panel = VideoPanelPage(win, services)
    try:
        source = tmp_path / "sample.mp4"
        source.write_bytes(b"video")
        assert panel.file_card.add_files([str(source)]) == 1
        monkeypatch.setattr(services, "ffmpeg_ready", lambda: True)
        monkeypatch.setattr(services.task_manager, "add_video_task",
                            lambda *_args, **_kwargs: 9301)

        panel._start()
        app.processEvents()
        assert not panel.action_bar.btn_cancel.isHidden()
        assert panel.action_bar.btn_cancel.isEnabled()
        assert panel.action_bar.status_dot.property("state") == "running"

        task = tm.Task(task_id=9301, name="sample.mp4", task_type="video",
                       file_path=str(source), output_path="sample.mov",
                       state=tm.SUCCESS, progress=100)
        services.task_manager._tasks[task.task_id] = task
        panel.action_bar.set_status("sample.mp4  保存中...")
        panel._on_state(task.task_id, tm.SUCCESS)
        app.processEvents()
        assert panel.action_bar.btn_cancel.isHidden()
        assert "保存中" not in panel.action_bar.status_label.text()
        assert panel.action_bar.status_dot.property("state") == "success"
    finally:
        services.task_manager._tasks.pop(9301, None)
        panel.deleteLater()
        app.processEvents()


def test_video_panel_keeps_subtitle_when_picker_is_cancelled(app_ctx, monkeypatch):
    """取消文件选择必须保持原字幕，不能把“取消”解释成“清除”。"""
    from PySide6.QtWidgets import QFileDialog
    from gui_qt.panels.video_panel import VideoPanelPage

    app, win, services = app_ctx
    panel = VideoPanelPage(win, services)
    try:
        panel._subtitle_path = "/tmp/original.srt"
        panel._lbl_sub.setText("original.srt")
        monkeypatch.setattr(QFileDialog, "getOpenFileName",
                            lambda *_args, **_kwargs: ("", ""))
        panel._pick_subtitle()
        assert panel._subtitle_path == "/tmp/original.srt"
        assert panel._lbl_sub.text() == "original.srt"
    finally:
        panel.deleteLater()
        app.processEvents()


def test_video_copy_mode_disables_ignored_controls(app_ctx):
    """复制流开启后禁用会被 FFmpeg 忽略的重编码控件。"""
    from gui_qt.panels.video_panel import VideoPanelPage

    app, win, services = app_ctx
    panel = VideoPanelPage(win, services)
    try:
        assert not panel.btn_go.isEnabled()
        panel.cb_copy.setChecked(True)
        app.processEvents()
        assert all(not control.isEnabled() for control in (
            panel.cb_res, panel.cb_codec, panel.cb_preset, panel.cb_fps,
            panel.cb_br, panel.cb_hw, panel.btn_sub, panel.cb_sub_font,
        ))
        panel.cb_copy.setChecked(False)
        assert all(control.isEnabled() for control in (
            panel.cb_res, panel.cb_codec, panel.cb_preset, panel.cb_fps,
            panel.cb_br, panel.cb_hw, panel.btn_sub, panel.cb_sub_font,
        ))
    finally:
        panel.deleteLater()
        app.processEvents()


def test_video_manual_parameter_change_marks_preset_custom(app_ctx):
    """应用快速预设后手动改参数，预设状态必须回到“自定义”。"""
    from gui_qt.i18n import tr
    from gui_qt.panels.video_panel import VideoPanelPage
    from utils.config import VIDEO_CONVERT_PRESETS

    app, win, services = app_ctx
    panel = VideoPanelPage(win, services)
    try:
        named = next(name for name in VIDEO_CONVERT_PRESETS
                     if name != tr("自定义", "Custom"))
        panel.cb_preset_tpl.setCurrentText(named)
        panel.cb_fmt.setCurrentIndex(
            (panel.cb_fmt.currentIndex() + 1) % panel.cb_fmt.count())
        assert panel.cb_preset_tpl.currentText() == tr("自定义", "Custom")
    finally:
        panel.deleteLater()
        app.processEvents()


def test_video_total_progress_keeps_completed_tasks_in_denominator(app_ctx):
    """批量任务完成一个后，总进度不得因为移除任务而倒退。"""
    from gui_qt import task_manager as tm
    from gui_qt.panels.video_panel import VideoPanelPage

    app, win, services = app_ctx
    panel = VideoPanelPage(win, services)
    try:
        first = tm.Task(9801, "a.mp4", "video", "a.mp4", "a.mov",
                        state=tm.RUNNING, progress=80)
        second = tm.Task(9802, "b.mp4", "video", "b.mp4", "b.mov",
                         state=tm.RUNNING, progress=20)
        services.task_manager._tasks.update({9801: first, 9802: second})
        panel._task_rows = {9801: ("a.mp4", -1), 9802: ("b.mp4", -1)}
        panel._batch_progress = {9801: 80, 9802: 20}
        panel._update_total()
        assert panel.bar_total.value() == 50
        first.state = tm.SUCCESS
        panel._on_state(9801, tm.SUCCESS)
        assert panel.bar_total.value() >= 50
    finally:
        services.task_manager._tasks.pop(9801, None)
        services.task_manager._tasks.pop(9802, None)
        panel.deleteLater()
        app.processEvents()


def test_video_panel_reflows_forms_and_header_when_narrow(app_ctx):
    """常用设置按三/双/单列响应，并将窄屏执行组放到标题第二行。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.video_panel import VideoPanelPage

    app, win, services = app_ctx
    panel = VideoPanelPage(win, services)
    try:
        panel.resize(700, 720)
        panel.show()
        app.processEvents()
        header = panel.findChild(PageHeader)
        assert panel.main_grid.getItemPosition(
            panel.main_grid.indexOf(panel.cb_preset_tpl))[0] == 1
        assert panel.main_grid.getItemPosition(
            panel.main_grid.indexOf(panel.cb_fmt))[0] == 3
        assert panel.main_grid.getItemPosition(
            panel.main_grid.indexOf(panel.cb_res))[0] == 5
        assert header.progress_layout.indexOf(panel.action_bar) >= 0
        panel.resize(1000, 720)
        app.processEvents()
        assert panel.main_grid.getItemPosition(
            panel.main_grid.indexOf(panel.cb_res))[0] == 3
        assert header.action_layout.indexOf(panel.action_bar) >= 0
        panel.resize(1280, 720)
        app.processEvents()
        assert panel.main_grid.getItemPosition(
            panel.main_grid.indexOf(panel.cb_res))[0] == 1
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_audio_panel_hides_unsupported_output_and_disables_lossless_bitrate(
        app_ctx):
    """AMR 仅可作为输入；无损输出不得提交无效的比特率参数。"""
    from gui_qt.panels.audio_panel import AudioPanelPage

    app, win, services = app_ctx
    panel = AudioPanelPage(win, services)
    try:
        outputs = [panel.cb_fmt.itemText(i)
                   for i in range(panel.cb_fmt.count())]
        assert "AMR" not in outputs
        panel.cb_fmt.setCurrentText("WAV")
        assert not panel.cb_br.isEnabled()
        assert panel.collect_params()["bitrate"] is None
        panel.cb_fmt.setCurrentText("MP3")
        assert panel.cb_br.isEnabled()
        assert panel.collect_params()["bitrate"] == panel.cb_br.currentText()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_audio_panel_persists_volume_and_uses_independent_cancellation(app_ctx):
    """音量参与偏好保存，每个并行任务必须拥有独立取消状态。"""
    from gui_qt.panels.audio_panel import AudioPanelPage

    app, win, services = app_ctx
    panel = AudioPanelPage(win, services)
    try:
        panel.vol_slider.setValue(135)
        prefs = panel.collect_prefs()
        assert prefs["volume"] == 135
        panel.vol_slider.setValue(100)
        panel.apply_prefs(prefs)
        assert panel.vol_slider.value() == 135

        _runner_a, cancel_a = panel._new_task_runtime()
        _runner_b, cancel_b = panel._new_task_runtime()
        assert cancel_a.__self__ is not cancel_b.__self__
        cancel_a()
        assert cancel_a.__self__._cancel is True
        assert cancel_b.__self__._cancel is False
    finally:
        panel.deleteLater()
        app.processEvents()


def test_audio_volume_reset_preserves_other_parameters(app_ctx):
    """恢复原音量只改变百分比，不重置用户选定的输出格式。"""
    from gui_qt.panels.audio_panel import AudioPanelPage

    app, win, services = app_ctx
    panel = AudioPanelPage(win, services)
    try:
        panel.cb_fmt.setCurrentText("FLAC")
        panel.vol_slider.setValue(135)
        assert panel.btn_reset_volume.isEnabled()
        panel.btn_reset_volume.click()
        assert panel.collect_params()["volume"] == 100
        assert panel.vol_label.text() == "100%"
        assert not panel.btn_reset_volume.isEnabled()
        assert panel.cb_fmt.currentText() == "FLAC"
        assert panel.collect_params()["bitrate"] is None
    finally:
        panel.deleteLater()
        app.processEvents()


def test_audio_waveform_expands_once_and_blocks_busy_refresh(app_ctx, tmp_path,
                                                           monkeypatch):
    """展开即加载；已缓存、空列表和加载中不会产生额外解码请求。"""
    from types import SimpleNamespace
    from gui_qt.panels.audio_panel import AudioPanelPage

    app, win, services = app_ctx
    panel = AudioPanelPage(win, services)
    calls = []
    monkeypatch.setattr(panel, "_show_waveform", lambda: calls.append(True))
    try:
        assert not panel.btn_wave.isEnabled()
        panel.wave_section.btn.click()
        assert not calls
        panel.wave_section.btn.click()
        source = tmp_path / "audio.wav"
        source.write_bytes(b"audio")
        panel.file_card.add_files([str(source)])
        assert panel.btn_wave.isEnabled()
        panel.wave_section.btn.click()
        assert calls == [True]
        panel._wave_source = str(source)
        panel.wave_section.btn.click()
        panel.wave_section.btn.click()
        assert calls == [True]
        panel._wave_worker = SimpleNamespace(isRunning=lambda: True)
        panel._sync_wave_button()
        assert not panel.btn_wave.isEnabled()
        panel._wave_worker = None
        panel._sync_wave_button()
        assert panel.btn_wave.isEnabled()
        panel._wave_pending = str(source)
        panel._clear_waveform()
        panel._on_wave_done(str(source), ([0.5], 1))
        assert panel._wave_source == ""
    finally:
        panel._wave_worker = None
        panel.deleteLater()
        app.processEvents()


def test_audio_total_progress_keeps_completed_tasks_in_denominator(app_ctx):
    """批量音频任务结束一项后，总进度不能向后跳。"""
    from gui_qt import task_manager as tm
    from gui_qt.panels.audio_panel import AudioPanelPage

    app, win, services = app_ctx
    panel = AudioPanelPage(win, services)
    try:
        first = tm.Task(9811, "a.wav", "audio", "a.wav", "a.mp3",
                        state=tm.RUNNING, progress=80)
        second = tm.Task(9812, "b.wav", "audio", "b.wav", "b.mp3",
                         state=tm.RUNNING, progress=20)
        services.task_manager._tasks.update({9811: first, 9812: second})
        panel._task_rows = {9811: ("a.wav", -1), 9812: ("b.wav", -1)}
        panel._batch_progress = {9811: 80, 9812: 20}
        panel._update_total()
        assert panel.bar_total.value() == 50
        first.state = tm.SUCCESS
        panel._on_state(9811, tm.SUCCESS)
        assert panel.bar_total.value() >= 50
    finally:
        services.task_manager._tasks.pop(9811, None)
        services.task_manager._tasks.pop(9812, None)
        panel.deleteLater()
        app.processEvents()


def test_audio_panel_reflows_and_blocks_empty_submission(app_ctx):
    """窄窗口参数改为单列，无文件时主按钮保持不可用。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.audio_panel import AudioPanelPage

    app, win, services = app_ctx
    panel = AudioPanelPage(win, services)
    try:
        assert not panel.btn_go.isEnabled()
        panel.resize(700, 720)
        panel.show()
        app.processEvents()
        header = panel.findChild(PageHeader)
        assert panel.params_grid.getItemPosition(
            panel.params_grid.indexOf(panel.cb_fmt))[0] == 1
        assert panel.params_grid.getItemPosition(
            panel.params_grid.indexOf(panel.cb_br))[0] == 3
        assert header.progress_layout.indexOf(panel.action_bar) >= 0
        panel.resize(1100, 720)
        app.processEvents()
        assert panel.params_grid.getItemPosition(
            panel.params_grid.indexOf(panel.cb_br))[0] == 1
        assert header.action_layout.indexOf(panel.action_bar) >= 0
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_cancelled_batch_is_not_reported_as_success():
    """全取消批次不播放成功音，也不发送“全部转换完成”通知。"""
    from gui_qt import task_manager as tm
    from gui_qt.app import MainWindow

    task = tm.Task(task_id=9401, name="cancelled", task_type="image",
                   file_path="input.png", output_path="output.jpg",
                   state=tm.CANCELLED)

    class _Manager:
        @staticmethod
        def last_batch_tasks():
            return [task]

    class _Services:
        @staticmethod
        def get_pref(_key, default=None):
            return default

    calls = []

    class _Window:
        task_manager = _Manager()
        services = _Services()

        def _play_done_sound(self, failed):
            calls.append(("sound", failed))

        def _notify_done(self, count):
            calls.append(("notify", count))

        def _open_last_output_dir(self, tasks):
            calls.append(("open", len(tasks)))

    MainWindow._on_batch_done(_Window())
    assert calls == [("open", 1)]


def test_submit_files_skips_file_with_active_task(app_ctx, tmp_path):
    """同一文件已有任务在队列/运行中时不得重复入队。

    否则同一批会出现重复任务，完成通知的任务数与文件行数对不上。
    """
    from gui_qt import task_manager as tm
    from gui_qt.panels.image_panel import ImagePanelPage
    app, win, services = app_ctx
    panel = ImagePanelPage(win, services)
    try:
        source = tmp_path / "a.png"
        source.write_bytes(b"png")
        assert panel.file_card.add_files([str(source)]) == 1
        task = tm.Task(task_id=9201, name="a.png", task_type="image",
                       file_path=str(source), output_path="a.jpg",
                       state=tm.WAITING)
        services.task_manager._tasks[task.task_id] = task
        panel._task_rows[task.task_id] = (str(source), 0)

        panel._submit_files()

        # 未重复入队：任务管理器中该文件仍只有一个任务
        same = [t for t in services.task_manager._tasks.values()
                if t.file_path == str(source)]
        assert len(same) == 1
        assert "已跳过" in panel.action_bar.status_label.text()
    finally:
        services.task_manager._tasks.pop(9201, None)
        panel.deleteLater()
        app.processEvents()


def test_image_panel_localized_params_and_responsive_layout(app_ctx,
                                                             monkeypatch):
    """图片页英文参数、窄窗口布局与主操作状态保持一致。"""
    from types import SimpleNamespace

    from gui_qt.panels.image_panel import ImagePanelPage

    app, win, services = app_ctx
    panel = ImagePanelPage(win, services)
    captured = {}

    def _convert(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(services.image_conv, "convert", _convert)
    try:
        assert not panel.action_bar.btn_go.isEnabled()
        panel.resize(760, 720)
        panel.show()
        app.processEvents()
        assert panel.main_grid._columns == 1
        assert panel.adv_grid._columns == 1
        assert panel.fx_grid._columns == 1

        task = SimpleNamespace(
            file_path="source.png", output_path="result.jpg",
            params={
                "quality": "95 (high)", "size": "Original size",
                "watermark": "中文水印", "watermark_pos": "Top left",
                "rotate": "0°", "crop": "Crop to square",
                "effect": "None",
            })
        assert panel._runner(task, lambda *_: None)
        assert captured["args"][2] == 95
        assert captured["args"][4] == "中文水印"
        assert captured["args"][5] == "左上角"
        assert captured["kwargs"]["crop_mode"] == "裁剪为正方形"
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_image_quality_controls_follow_encoder_and_keep_selection(app_ctx):
    """不适用质量参数的格式明确禁用，往返切换保留原有选择。"""
    from gui_qt.panels.image_panel import ImagePanelPage, QUALITY_VALUES

    app, win, services = app_ctx
    panel = ImagePanelPage(win, services)
    try:
        panel.cb_q.setCurrentText(QUALITY_VALUES[-1])
        for fmt in ("PNG", "BMP", "GIF", "TIFF", "ICO", "TGA"):
            panel.cb_fmt.setCurrentText(fmt)
            assert not panel.cb_q.isEnabled()
            assert fmt in panel.quality_hint.text()
        for fmt in ("JPG", "WEBP", "AVIF", "HEIC"):
            panel.cb_fmt.setCurrentText(fmt)
            assert panel.cb_q.isEnabled()
            assert panel.cb_q.currentText() == QUALITY_VALUES[-1]
        for legacy in ("100（无损）", "100 (lossless)"):
            panel.apply_prefs({"quality": legacy})
            assert panel.collect_params()["quality"] == QUALITY_VALUES[0]
            assert panel.collect_prefs()["quality"].startswith("100")
    finally:
        panel.deleteLater()
        app.processEvents()


def test_image_watermark_position_requires_text_and_wide_layout(app_ctx):
    """水印留空禁用位置但不清空选择，宽屏常用参数排列在同一行。"""
    from gui_qt.panels.image_panel import ImagePanelPage, WATERMARK_POS_VALUES

    app, win, services = app_ctx
    panel = ImagePanelPage(win, services)
    try:
        assert not panel.cb_wm_pos.isEnabled()
        panel.wm_edit.setText("   ")
        assert not panel.cb_wm_pos.isEnabled()
        panel.wm_edit.setText("水印")
        assert panel.cb_wm_pos.isEnabled()
        panel.cb_wm_pos.setCurrentText(WATERMARK_POS_VALUES[-1])
        panel.wm_edit.clear()
        assert not panel.cb_wm_pos.isEnabled()
        assert panel.collect_params()["watermark"] == ""
        panel.wm_edit.setText("作者")
        assert panel.collect_params()["watermark_pos"] == WATERMARK_POS_VALUES[-1]
        panel.resize(1280, 1000)
        panel.show()
        app.processEvents()
        for control in (panel.cb_fmt, panel.cb_q, panel.cb_sz):
            assert panel.main_grid.getItemPosition(
                panel.main_grid.indexOf(control))[0] == 1
        panel.resize(1000, 1000)
        app.processEvents()
        assert panel.main_grid._columns == 2
        panel.resize(720, 1000)
        app.processEvents()
        assert panel.main_grid._columns == 1
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_image_panel_restores_advanced_preferences(app_ctx):
    """对比度、饱和度、锐度和特效返回页面后不能丢失。"""
    from gui_qt.panels.image_panel import EFFECT_VALUES, ImagePanelPage

    app, win, services = app_ctx
    panel = ImagePanelPage(win, services)
    try:
        effect = EFFECT_VALUES[-1]
        panel.apply_prefs({
            "contrast": 1.4,
            "saturation": 1.7,
            "sharpness": 2.2,
            "effect": effect,
        })
        assert panel.sp_contrast.value() == pytest.approx(1.4)
        assert panel.sp_saturation.value() == pytest.approx(1.7)
        assert panel.sp_sharpness.value() == pytest.approx(2.2)
        assert panel.cb_effect.currentText() == effect
        assert panel.sp_contrast.accessibleName()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_pdf_panel_uses_one_header_action_cluster_and_mode_constraints(
        app_ctx, tmp_path):
    """PDF 页的预览、编辑、状态和执行集中在标题区，并遵守模式文件数约束。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.pdf_panel import MODE_VALUES, PdfPanelPage

    app, win, services = app_ctx
    panel = PdfPanelPage(win, services)
    try:
        # 文件数约束针对合并模式，不依赖用户上次保存的模式。
        panel.cb_mode.setCurrentItem(MODE_VALUES[0])
        panel._mode_changed()
        header = panel.findChild(PageHeader)
        assert header is panel.header
        assert panel.content_layout.indexOf(panel.action_bar) == -1
        assert panel.btn_preview.parent() is header
        assert panel.btn_editor.parent() is header
        assert not hasattr(panel, "adv_toggle")
        assert not panel.btn_preview.isEnabled()
        assert not panel.action_bar.btn_go.isEnabled()

        first = tmp_path / "first.pdf"
        second = tmp_path / "second.pdf"
        first.write_bytes(b"%PDF-1.4")
        second.write_bytes(b"%PDF-1.4")
        panel.file_card.add_files([str(first)])
        app.processEvents()
        assert panel.btn_preview.isEnabled()
        assert not panel.action_bar.btn_go.isEnabled()

        panel.file_card.add_files([str(second)])
        app.processEvents()
        assert panel.action_bar.btn_go.isEnabled()

        panel.cb_mode.setCurrentItem(MODE_VALUES[5])  # 压缩仅需一个输入
        app.processEvents()
        assert panel.action_bar.btn_go.isEnabled()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_pdf_panel_reflows_all_modes_without_losing_values(app_ctx, monkeypatch):
    """窄窗口使用紧凑模式入口和单列表单，恢复宽屏时不重建参数。"""
    from gui_qt.panels.pdf_panel import MODE_HINTS, MODE_VALUES, PdfPanelPage

    app, win, services = app_ctx
    panel = PdfPanelPage(win, services)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    try:
        panel.ed_range.setText("1-3,5")
        panel.ed_wm_text.setText("Draft")
        panel.resize(720, 1000)
        panel.show()
        sections = (None, panel.sec_split, panel.sec_split, panel.sec_encrypt,
                    panel.sec_decrypt, panel.sec_compress, panel.sec_wm,
                    panel.sec_pn, panel.sec_img)
        for mode, section in zip(MODE_VALUES, sections):
            panel.cb_mode_compact.setCurrentText(mode)
            app.processEvents()
            assert panel.cb_mode.currentRouteKey() == mode
            assert panel.cb_mode_compact.isVisible()
            assert not panel.cb_mode.isVisible()
            assert panel.mode_hint.text() == MODE_HINTS[mode]
            if section is not None:
                assert section.isVisible()
            assert all(grid._columns == 1 for grid in panel._parameter_grids)
            assert panel.horizontalScrollBar().maximum() == 0
            assert panel.widget().width() <= panel.viewport().width()

        panel.resize(1280, 1000)
        panel.cb_mode.setCurrentItem(MODE_VALUES[3])
        app.processEvents()
        assert panel.cb_mode.isVisible()
        assert not panel.cb_mode_compact.isVisible()
        assert panel.cb_mode_compact.currentText() == MODE_VALUES[3]
        assert all(grid._columns == 2 for grid in panel._parameter_grids)
        assert panel.ed_range.text() == "1-3,5"
        assert panel.ed_wm_text.text() == "Draft"
        assert panel.horizontalScrollBar().maximum() == 0
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_pdf_panel_range_controls_follow_extraction_mode(app_ctx, monkeypatch):
    """逐页提取禁用无效范围输入，切回拆分时恢复并保留页码。"""
    from gui_qt.panels.pdf_panel import MODE_VALUES, PdfPanelPage

    app, win, services = app_ctx
    panel = PdfPanelPage(win, services)
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    try:
        panel.ed_range.setText("1-3")
        panel.cb_mode_compact.setCurrentText(MODE_VALUES[2])
        panel.cb_extract_mode.setCurrentIndex(1)
        assert panel.cb_extract_mode.isEnabled()
        assert not panel.ed_range.isEnabled()
        panel.cb_mode_compact.setCurrentText(MODE_VALUES[1])
        assert not panel.cb_extract_mode.isEnabled()
        assert panel.ed_range.isEnabled()
        assert panel.collect_params()["range"] == "1-3"
        panel.cb_mode_compact.setCurrentText(MODE_VALUES[2])
        assert not panel.ed_range.isEnabled()
        panel.cb_extract_mode.setCurrentIndex(2)
        assert panel.ed_range.isEnabled()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_pdf_panel_previews_selected_file_with_first_file_fallback(
        app_ctx, tmp_path, monkeypatch):
    """预览跟随选中项；取消选择时回退第一份，无文件时禁用。"""
    from types import SimpleNamespace
    from gui_qt.panels.pdf_panel import PdfPanelPage

    app, win, services = app_ctx
    panel = PdfPanelPage(win, services)
    opened = []

    class Preview:
        def __init__(self, path, parent):
            opened.append(path)

        def exec(self):
            pass

    monkeypatch.setitem(sys.modules, "gui_qt.components.pdf_preview",
                        SimpleNamespace(PdfPreviewDialog=Preview))
    try:
        first = tmp_path / "first.pdf"
        second = tmp_path / "second.pdf"
        first.write_bytes(b"%PDF-1.4")
        second.write_bytes(b"%PDF-1.4")
        panel.file_card.add_files([str(first), str(second)])
        panel.file_card.table.selectRow(1)
        panel._preview_pdf()
        assert opened == [str(second)]
        assert "second.pdf" in panel.btn_preview.toolTip()
        panel.file_card.table.clearSelection()
        panel._preview_pdf()
        assert opened[-1] == str(first)
        assert "first.pdf" in panel.btn_preview.toolTip()
        panel.file_card.clear_files()
        assert not panel.btn_preview.isEnabled()
    finally:
        panel.deleteLater()
        app.processEvents()


def test_pdf_panel_rejects_invalid_range_before_queueing(app_ctx, tmp_path,
                                                          monkeypatch):
    """非法页码范围必须在主线程提交前提示，不能先生成失败任务。"""
    from gui_qt.panels.pdf_panel import MODE_VALUES, PdfPanelPage

    app, win, services = app_ctx
    panel = PdfPanelPage(win, services)
    calls = []
    monkeypatch.setattr(services.task_manager, "add_task",
                        lambda **kwargs: calls.append(kwargs))
    try:
        source = tmp_path / "source.pdf"
        source.write_bytes(b"%PDF-1.4")
        panel.file_card.add_files([str(source)])
        panel.cb_mode.setCurrentItem(MODE_VALUES[1])
        panel.ed_range.setText("5-2")
        assert panel._start() is False
        assert calls == []
    finally:
        panel.deleteLater()
        app.processEvents()


def test_pdf_panel_uses_stable_positions_in_english_ui(app_ctx, monkeypatch):
    """展示文案可以翻译，但核心 PDF 引擎收到的位置值必须稳定。"""
    from types import SimpleNamespace
    import gui_qt.panels.pdf_panel as pdf_panel

    app, win, services = app_ctx
    panel = pdf_panel.PdfPanelPage(win, services)
    captured = {}

    def _watermark(_src, _dst, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(pdf_panel, "pdf_add_watermark", _watermark)
    try:
        task = SimpleNamespace(
            file_path="source.pdf", output_path="output.pdf",
            params={
                "mode": pdf_panel.MODE_VALUES[6], "files": ["source.pdf"],
                "wm_text": "Draft", "wm_pos": "Top left",
                "wm_pos_index": 0, "wm_opacity": 0.3, "wm_rotate": 0,
            })
        assert panel._runner(task, lambda *_args: None)
        assert captured["pos"] == "左上角"
    finally:
        panel.deleteLater()
        app.processEvents()


def test_pdf_editor_uses_header_actions_and_disables_invalid_commands(app_ctx):
    """编辑器采用统一标题操作簇，无文档时保存和编辑命令不能误触。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.pdf_editor_panel import PdfEditorPanelPage

    app, win, services = app_ctx
    panel = PdfEditorPanelPage(win, services)
    try:
        assert panel.findChild(PageHeader) is panel.header
        assert panel.btn_open.parent() is panel.header
        assert panel.btn_save.parent() is panel.header
        assert panel.btn_save_as.parent() is panel.header
        assert not panel.btn_save.isEnabled()
        assert not panel.btn_save_as.isEnabled()
        assert not panel.btn_undo.isEnabled()
        assert all(not button.isEnabled()
                   for button in panel._selection_buttons)
        assert panel.grid.minimumHeight() == 360
        assert panel.grid.maximumHeight() > 360
    finally:
        panel.cleanup()
        panel.deleteLater()
        app.processEvents()


def test_crop_panel_uses_consistent_header_preview_and_responsive_form(app_ctx):
    """封面裁剪采用统一标题操作区，展示画布比例并支持窄窗口重排。"""
    from gui_qt.components.page_header import PageHeader
    from gui_qt.panels.crop_panel import CropPanelPage

    app, win, services = app_ctx
    panel = CropPanelPage(win, services)
    try:
        header = panel.findChild(PageHeader)
        assert header is not None
        assert panel.content_layout.indexOf(panel.action_bar) == -1
        assert not panel.action_bar.btn_go.isEnabled()
        params = panel.collect_params()
        assert params["preset_size"] == [1080, 1080]
        assert params["crop_mode"] == "cover"
        assert panel.aspect_preview.accessibleName()
        assert "1080×1080" in panel.file_card._fmt_text

        panel.resize(720, 700)
        panel.show()
        app.processEvents()
        assert panel.params_grid._columns == 1
        panel.resize(1100, 700)
        app.processEvents()
        assert panel.params_grid._columns == 2
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()
