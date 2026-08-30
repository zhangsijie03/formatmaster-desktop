"""PDF 编辑页局部调整：响应式工具栏、空态、操作范围和编辑回归。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FORMATMASTER_OFFSCREEN"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def editor_panel(tmp_path, monkeypatch):
    import pymupdf
    from gui_qt.panels.pdf_editor_panel import PdfEditorPanelPage, _ThumbWorker
    from gui_qt.services import QtServices

    app = QApplication.instance() or QApplication([])
    # 交互测试不启动后台渲染；真实缩略图另由页面截图检查。
    monkeypatch.setattr(_ThumbWorker, "start", lambda self: None)
    source = tmp_path / "source.pdf"
    with pymupdf.open() as doc:
        for _ in range(3):
            doc.new_page()
        doc.save(str(source))
    panel = PdfEditorPanelPage(object(), QtServices())
    monkeypatch.setattr(panel, "save_prefs", lambda: None)
    yield app, panel, source
    panel.cleanup()
    panel.close()
    panel.deleteLater()
    app.processEvents()


def test_editor_empty_state_and_toolbar_fit_narrow_window(editor_panel):
    app, panel, source = editor_panel
    panel.resize(720, 900)
    panel.show()
    app.processEvents()
    assert panel.empty_state.isVisible()
    assert not panel.grid.isVisible()
    assert not panel.btn_save.isEnabled()
    assert not panel.btn_undo.isEnabled()
    assert all(not button.isEnabled() for button in panel._document_buttons)
    assert panel.horizontalScrollBar().maximum() == 0

    panel._load_pdf(str(source))
    panel.lb_info.setText("非常长的文档名称" * 20 + ".pdf")
    panel._select_indices([0, 1, 2])
    for width in (720, 1280, 720):
        panel.resize(width, 900)
        app.processEvents()
        assert panel.grid.isVisible()
        assert not panel.empty_state.isVisible()
        assert panel.horizontalScrollBar().maximum() == 0
        assert panel.widget().width() <= panel.viewport().width()
        assert panel.grid.count() == 3
        assert panel._selected_indices() == [0, 1, 2]
        for button in panel._selection_buttons + panel._document_buttons + [panel.btn_undo]:
            assert button.geometry().right() < button.parentWidget().width()
    assert panel.lb_info.toolTip() == str(source)
    assert panel.grid.accessibleName()


def test_editor_selection_and_insertion_hints_match_actual_position(editor_panel):
    from gui_qt.i18n import tr

    app, panel, source = editor_panel
    panel._load_pdf(str(source))
    assert panel._insert_at() == 3
    assert all(tr("文档末尾", "end of the document") in button.toolTip()
               for button in panel._insert_buttons)
    assert not any(button.isEnabled() for button in panel._selection_buttons)
    panel._select_indices([1, 2])
    assert panel._insert_at() == 1
    assert all("2" in button.toolTip() for button in panel._insert_buttons)
    assert all(button.isEnabled() for button in panel._selection_buttons)
    panel._toggle_select_all()
    assert panel._selected_indices() == [0, 1, 2]
    assert panel.btn_all.text() == tr("取消全选", "Clear selection")
    panel._toggle_select_all()
    assert panel._selected_indices() == []
    assert panel.btn_all.text() == tr("全选", "Select all")
    panel._select_indices([1])
    panel._invert_selection()
    assert panel._selected_indices() == [0, 2]


def test_editor_rotate_duplicate_insert_undo_and_save(editor_panel, tmp_path):
    import pymupdf

    app, panel, source = editor_panel
    panel._load_pdf(str(source))
    panel._select_indices([1])
    panel._rotate_90()
    assert panel._selected_indices() == [1]
    assert panel.editor.modified
    assert panel.btn_save.isEnabled()
    assert panel.btn_undo.isEnabled()
    panel._duplicate_selected()
    assert panel.editor.page_count == 4
    assert panel._selected_indices() == [2]
    panel._undo()
    assert panel.editor.page_count == 3
    panel._select_indices([1])
    panel._insert_blank()
    assert panel.editor.page_count == 4
    target = tmp_path / "edited.pdf"
    panel._do_save(str(target))
    assert target.is_file()
    assert not panel.editor.modified
    assert not panel.btn_save.isEnabled()
    assert not panel.btn_undo.isEnabled()
    assert panel.lb_info.text() == target.name
    assert panel.lb_info.toolTip() == str(target)
    with pymupdf.open(str(target)) as saved:
        assert len(saved) == 4
        assert saved[2].rotation == 90
    with pymupdf.open(str(source)) as original:
        assert len(original) == 3
        assert original[1].rotation == 0


def test_editor_delete_still_requires_confirmation(editor_panel, monkeypatch):
    import gui_qt.panels.pdf_editor_panel as editor_module
    from types import SimpleNamespace

    app, panel, source = editor_panel
    decision = {"accepted": False}
    confirmations = []

    class Confirmation:
        def __init__(self, title, body, parent):
            confirmations.append(body)
            self.yesButton = SimpleNamespace(setText=lambda text: None)

        def exec(self):
            return decision["accepted"]

    monkeypatch.setattr(editor_module, "MessageBox", Confirmation)
    panel._load_pdf(str(source))
    panel._select_indices([1])
    panel._delete_selected()
    assert panel.editor.page_count == 3
    assert not panel.editor.modified
    decision["accepted"] = True
    panel._delete_selected()
    assert panel.editor.page_count == 2
    assert panel.editor.modified
    assert len(confirmations) == 2
