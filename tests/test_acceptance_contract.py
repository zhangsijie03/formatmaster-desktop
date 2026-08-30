"""验收契约回归：REST API、FIFO 调度与任务日志。"""
import os


def test_rest_api_health():
    from fastapi.testclient import TestClient
    from api_server import app

    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["code"] == "OK"


def test_rest_api_video_convert_and_auto_rename(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api_server

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    target = tmp_path / "output.mp4"
    target.write_bytes(b"existing")
    captured = {}

    class FakeConverter:
        def convert(self, input_path, output_path, *args, **kwargs):
            captured["input"] = input_path
            captured["output"] = output_path
            with open(output_path, "wb") as stream:
                stream.write(b"converted")
            return True

    monkeypatch.setattr(api_server, "VideoConverter", FakeConverter)
    response = TestClient(api_server.app).post("/api/video/convert", json={
        "input_path": str(source),
        "output_path": str(target),
        "format": "mp4",
        "codec": "h264",
        "frame_rate": 30,
    })

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert captured["input"] == str(source)
    assert captured["output"].endswith("output_1.mp4")


def test_rest_api_document_convert(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import api_server

    source = tmp_path / "source.txt"
    source.write_text("hello", encoding="utf-8")
    target = tmp_path / "output.pdf"

    class FakeConverter:
        def convert(self, input_path, output_path):
            assert input_path == str(source)
            with open(output_path, "wb") as stream:
                stream.write(b"%PDF-test")
            return True

    monkeypatch.setattr(api_server, "DocumentConverter", FakeConverter)
    response = TestClient(api_server.app).post("/api/document/convert", json={
        "input_path": str(source),
        "output_path": str(target),
    })

    assert response.status_code == 200
    assert response.json()["data"]["output_path"] == str(target)


def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_task_queue_uses_500ms_fifo_dispatch(tmp_path):
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    _qapp()
    services = QtServices()
    manager = TaskManager(services)
    manager._dispatch_timer.stop()
    manager.max_parallel = 1
    submitted = []

    class FakeFuture:
        def add_done_callback(self, _callback):
            return None

    class FakeExecutor:
        def submit(self, _fn, task):
            submitted.append(task.task_id)
            return FakeFuture()

    manager._executor.shutdown(wait=False, cancel_futures=True)
    manager._executor = FakeExecutor()
    source = tmp_path / "input.txt"
    source.write_text("x", encoding="utf-8")
    first = manager.add_task(
        "first", "generic", str(source), str(tmp_path / "one.txt"), {},
        runner=lambda *_args: True, priority=0, need_ffmpeg=False)
    second = manager.add_task(
        "second", "generic", str(source), str(tmp_path / "two.txt"), {},
        runner=lambda *_args: True, priority=999, need_ffmpeg=False)

    assert manager._dispatch_timer.interval() == 500
    assert submitted == []
    manager._schedule_next()
    assert submitted == [first]
    assert manager._queue == [second]


def test_completed_batch_snapshot_excludes_historical_tasks(tmp_path):
    """完成通知只能统计本批任务，不能累加内存中保留的历史记录。"""
    from gui_qt.services import QtServices
    from gui_qt.task_manager import SUCCESS, TaskManager

    _qapp()
    manager = TaskManager(QtServices())
    manager._dispatch_timer.stop()
    source = tmp_path / "input.txt"
    source.write_text("x", encoding="utf-8")

    def add(name):
        return manager.add_task(
            name, "generic", str(source), str(tmp_path / f"{name}.txt"), {},
            runner=lambda *_args: True, need_ffmpeg=False)

    historical_id = add("historical")
    manager._set_state(manager.get_task(historical_id), SUCCESS)
    assert [task.task_id for task in manager.last_batch_tasks()] == [historical_id]

    first = add("first")
    second = add("second")
    manager._set_state(manager.get_task(first), SUCCESS)
    manager._set_state(manager.get_task(second), SUCCESS)

    assert [task.task_id for task in manager.last_batch_tasks()] == [first, second]
    manager._executor.shutdown(wait=False, cancel_futures=True)


def test_task_log_contract():
    from gui_qt.components.theme_manager import ThemeManager
    from gui_qt.pages.task_page import TaskPage
    from gui_qt.services import QtServices
    from gui_qt.task_manager import TaskManager

    app = _qapp()
    services = QtServices()
    services.task_manager = TaskManager(services)
    services.theme_mgr = ThemeManager(services)
    window = type("Window", (), {"pages": {}})()
    page = TaskPage(window, services)

    assert page.log_view.document().maximumBlockCount() == 50
    assert page.btn_clear_log.text()
    assert page.btn_clear_completed.text()
    assert page.btn_cancel_active.text()

    page.deleteLater()
    app.processEvents()


def test_retry_during_active_batch_not_counted(tmp_path):
    """批次运行中从任务中心重试旧任务，不得计入该批完成通知。

    否则「全部转换完成（N 个任务）」的 N 会把重试任务也算上，
    与发起转换的面板底栏统计（只算自己提交的任务）口径不一致。
    """
    from gui_qt.services import QtServices
    from gui_qt.task_manager import FAILED, RUNNING, SUCCESS, TaskManager

    _qapp()
    manager = TaskManager(QtServices())
    manager._dispatch_timer.stop()
    source = tmp_path / "input.txt"
    source.write_text("x", encoding="utf-8")

    def add(name, ok=True):
        return manager.add_task(
            name, "generic", str(source), str(tmp_path / f"{name}.txt"), {},
            runner=lambda *_args: ok, need_ffmpeg=False)

    # 历史失败任务（任务中心可重试）
    failed_id = add("failed", ok=False)
    manager._set_state(manager.get_task(failed_id), FAILED)

    # 面板新批次：两个任务运行中
    first = add("first")
    second = add("second")
    for tid in (first, second):
        manager.get_task(tid).state = RUNNING

    # 批次运行期间重试旧任务 + 全部任务结束
    assert manager.retry_task(failed_id) is True
    for tid in (first, second, failed_id):
        manager._set_state(manager.get_task(tid), SUCCESS)

    # 批次快照只含面板提交的两个任务，重试任务未计入
    assert [task.task_id for task in manager.last_batch_tasks()] == [
        first, second]
    manager._executor.shutdown(wait=False, cancel_futures=True)


def test_retry_when_idle_forms_own_batch(tmp_path):
    """空闲时重试仍自成一批：完成后正常发「1 个任务」的批量通知。"""
    from gui_qt.services import QtServices
    from gui_qt.task_manager import FAILED, SUCCESS, TaskManager

    _qapp()
    manager = TaskManager(QtServices())
    manager._dispatch_timer.stop()
    source = tmp_path / "input.txt"
    source.write_text("x", encoding="utf-8")

    failed_id = manager.add_task(
        "failed", "generic", str(source), str(tmp_path / "failed.txt"), {},
        runner=lambda *_args: False, need_ffmpeg=False)
    manager._set_state(manager.get_task(failed_id), FAILED)

    assert manager.retry_task(failed_id) is True
    manager._set_state(manager.get_task(failed_id), SUCCESS)

    assert [task.task_id for task in manager.last_batch_tasks()] == [failed_id]
    manager._executor.shutdown(wait=False, cancel_futures=True)
