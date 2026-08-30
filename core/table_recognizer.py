"""table_recognizer — 表格识别（OCR 文字 + 位置聚类 → 结构化表格）。

基于 RapidOCR 的带坐标识别结果，按 y 中心聚类成行、按 x 排序成列，
输出 CSV 或 XLSX。不引入表格结构模型，适合规则/无复杂合并的表格。
"""
import os
import tempfile
from decimal import Decimal, InvalidOperation

from core.ocr_tool import _get_engine

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}
XLSX_EXTS = {".xlsx"}
CSV_EXTS = {".csv", ".txt"}
CHART_TYPES = {"bar", "line", "pie"}


def _report_error(progress_cb, message):
    if progress_cb:
        progress_cb(-1, message)


def _validate_paths(input_path, output_path, allowed_outputs, progress_cb,
                    require_input=True):
    """识别前验证外部路径，避免未知扩展名被静默写成 CSV。"""
    if require_input:
        if not os.path.isfile(input_path):
            _report_error(progress_cb, "错误: 找不到输入图片")
            return False
        if os.path.splitext(input_path)[1].lower() not in IMAGE_EXTS:
            _report_error(progress_cb, "错误: 不支持的图片格式")
            return False
    if os.path.splitext(output_path or "")[1].lower() not in allowed_outputs:
        expected = " / ".join(sorted(allowed_outputs))
        _report_error(progress_cb, f"错误: 输出格式必须为 {expected}")
        return False
    if require_input and os.path.normcase(
            os.path.abspath(input_path)) == os.path.normcase(
                os.path.abspath(output_path)):
        _report_error(progress_cb, "错误: 输出文件不能覆盖输入图片")
        return False
    return True


def _staged_output_path(output_path):
    """临时文件与目标文件同目录，保证最终替换为原子操作。"""
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    suffix = os.path.splitext(output_path)[1].lower()
    fd, staged_path = tempfile.mkstemp(
        prefix=".fm_table_ocr_", suffix=suffix, dir=output_dir)
    os.close(fd)
    os.remove(staged_path)
    return staged_path


def _scaled_progress(progress_cb):
    if progress_cb is None:
        return None

    def report(value, message):
        progress_cb(value if value < 0 else min(90, int(value * 0.9)), message)

    return report


def _atomic_export(output_path, suffix, writer, progress_cb):
    """完整写入临时文件后再提交，失败或取消时保留既有结果。"""
    staged_path = None
    try:
        staged_path = _staged_output_path(output_path)
        if not writer(staged_path, _scaled_progress(progress_cb)):
            return False
        if not os.path.isfile(staged_path) or os.path.getsize(staged_path) == 0:
            _report_error(progress_cb, f"错误: 生成的 {suffix} 文件为空")
            return False
        if progress_cb:
            progress_cb(95, "正在保存结果…")
        os.replace(staged_path, output_path)
        staged_path = None
        return True
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001 - 转换入口统一返回明确失败原因
        _report_error(progress_cb, f"错误: 无法生成 {suffix} - {exc}")
        return False
    finally:
        if staged_path:
            try:
                os.remove(staged_path)
            except OSError:
                pass


def _items(result):
    """把 rapidocr result 转为 (cy, cx, height, text) 列表并排序。"""
    out = []
    for item in result or []:
        try:
            bbox, text = item[0], item[1]
            xs = [float(point[0]) for point in bbox]
            ys = [float(point[1]) for point in bbox]
            if not xs or not ys or not str(text).strip():
                continue
            cy = sum(ys) / len(ys)
            cx = sum(xs) / len(xs)
            h = max(ys) - min(ys)
            out.append((cy, cx, max(h, 1.0), str(text)))
        except (TypeError, ValueError, IndexError):
            # OCR 属于外部数据源；单条异常不应拖垮其余可用单元格。
            continue
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def recognize_rows(input_path, progress_cb=None):
    """识别图片 → 行列表 [[cell, cell, ...], ...]。"""
    if progress_cb:
        progress_cb(30, "打开图片…")
    engine = _get_engine()
    if progress_cb:
        progress_cb(50, "识别文字与位置…")
    result, _elapse = engine(input_path)
    if not result:
        return []
    items = _items(result)
    heights = sorted(t[2] for t in items)
    med_h = heights[len(heights) // 2] if heights else 20.0

    rows = []
    cur = []
    last_cy = None
    for cy, cx, h, text in items:
        if last_cy is not None and cy - last_cy > med_h * 0.7:
            rows.append(sorted(cur, key=lambda t: t[0]))
            cur = []
        cur.append((cx, text))
        last_cy = cy if last_cy is None else max(last_cy, cy)
    if cur:
        rows.append(sorted(cur, key=lambda t: t[0]))
    return [[t for _cx, t in row] for row in rows]


def _chart_cell_value(value):
    """仅把明确的数值文本转为 Excel 数值，分类和疑似编号保持原文。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    normalized = text.replace(",", "")
    unsigned = normalized.lstrip("+-")
    if not unsigned or unsigned.count(".") > 1:
        return value
    if not unsigned.replace(".", "", 1).isdigit():
        return value
    integer_part = unsigned.split(".", 1)[0]
    if len(integer_part) > 1 and integer_part.startswith("0") and "," not in text:
        return value
    try:
        number = Decimal(normalized)
        return number if number.is_finite() else value
    except InvalidOperation:
        return value


def _table_to_csv_impl(input_path, output_path, progress_cb=None):
    """识别表格并保存为 CSV（UTF-8-sig，Excel 可直接打开）。"""
    import csv
    rows = recognize_rows(input_path, progress_cb)
    if not rows:
        if progress_cb:
            progress_cb(-1, "未识别到文字")
        return False
    if progress_cb:
        progress_cb(80, "写入 CSV…")
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    if progress_cb:
        progress_cb(100, "识别完成")
    return True


def table_to_csv(input_path, output_path, progress_cb=None):
    """安全导出 CSV；失败或取消时不破坏已有文件。"""
    if not _validate_paths(
            input_path, output_path, CSV_EXTS, progress_cb,
            require_input=False):
        return False
    return _atomic_export(
        output_path, "CSV",
        lambda staged, callback: _table_to_csv_impl(
            input_path, staged, callback),
        progress_cb)


def _table_to_xlsx_impl(input_path, output_path, progress_cb=None,
                        chart_type=None):
    """识别表格并保存为 XLSX。

    chart_type: None 不生成图表 / "bar" 柱状 / "line" 折线 / "pie" 饼图。
    图表取首行为表头、首列为分类、其余列为数值（需 ≥2 行 ≥2 列）。
    """
    from openpyxl import Workbook
    rows = recognize_rows(input_path, progress_cb)
    if not rows:
        if progress_cb:
            progress_cb(-1, "未识别到文字")
        return False
    if progress_cb:
        progress_cb(80, "写入 Excel…")
    wb = Workbook()
    ws = wb.active
    ws.title = "表格"
    for row_index, row in enumerate(rows):
        if chart_type and row_index > 0:
            row = [value if column_index == 0 else _chart_cell_value(value)
                   for column_index, value in enumerate(row)]
        ws.append(row)
    if chart_type:
        try:
            from openpyxl.chart import BarChart, LineChart, PieChart
            from openpyxl.chart.reference import Reference
            ncols = max(len(r) for r in rows)
            if len(rows) >= 2 and ncols >= 2:
                cls = {"bar": BarChart, "line": LineChart,
                       "pie": PieChart}[chart_type]
                chart = cls()
                data = Reference(ws, min_col=2, min_row=1,
                                 max_col=ncols, max_row=len(rows))
                cats = Reference(ws, min_col=1, min_row=2,
                                 max_row=len(rows))
                chart.add_data(data, titles_from_data=True)
                chart.set_categories(cats)
                chart.title = "数据图表"
                ws.add_chart(chart, f"H{len(rows) + 3}")
            elif progress_cb:
                progress_cb(90, "数据不足，跳过图表")
        except Exception as exc:  # noqa: BLE001 - 表格数据仍可安全导出
            if progress_cb:
                progress_cb(90, f"图表生成失败，已保留表格数据: {exc}")
    wb.save(output_path)
    if progress_cb:
        progress_cb(100, "识别完成")
    return True


def table_to_xlsx(input_path, output_path, progress_cb=None, chart_type=None):
    """安全导出 XLSX；图表类型使用稳定枚举值。"""
    if not _validate_paths(
            input_path, output_path, XLSX_EXTS, progress_cb,
            require_input=False):
        return False
    if chart_type is not None and chart_type not in CHART_TYPES:
        _report_error(progress_cb, "错误: 不支持的图表类型")
        return False
    return _atomic_export(
        output_path, "Excel",
        lambda staged, callback: _table_to_xlsx_impl(
            input_path, staged, callback, chart_type),
        progress_cb)


def recognize_table(input_path, output_path, progress_cb=None, chart_type=None):
    """按输出扩展名自动选择 CSV / XLSX。"""
    if not _validate_paths(
            input_path, output_path, CSV_EXTS | XLSX_EXTS, progress_cb):
        return False
    ext = os.path.splitext(output_path)[1].lower()
    if ext in XLSX_EXTS:
        return table_to_xlsx(input_path, output_path, progress_cb, chart_type)
    if ext in CSV_EXTS:
        return table_to_csv(input_path, output_path, progress_cb)
    _report_error(progress_cb, "错误: 输出格式必须为 .csv 或 .xlsx")
    return False


def make_runner(_task):
    """TaskManager 快照恢复用 runner 工厂。"""
    def runner(task, progress_cb):
        params = task.params or {}
        return recognize_table(
            task.file_path, task.output_path, progress_cb=progress_cb,
            chart_type=params.get("chart_type"))

    return runner
