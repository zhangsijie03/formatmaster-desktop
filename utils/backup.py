"""backup — 用户数据导出/导入（设置页「数据备份」）。

打包 get_user_data_dir() 下的核心 JSON（偏好/历史/m3u8/预设等）
为单个 .zip；导入时覆盖同名文件。不涉及大文件与临时数据。
"""
import json
import os
import tempfile
import zipfile

from utils.config import get_user_data_dir


MAX_BACKUP_FILES = 100
MAX_BACKUP_FILE_SIZE = 20 * 1024 * 1024
MAX_BACKUP_TOTAL_SIZE = 50 * 1024 * 1024


def _data_files():
    """用户数据目录下参与备份的 .json 文件名列表。"""
    d = get_user_data_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if f.endswith(".json") and os.path.isfile(os.path.join(d, f)))


def export_backup(dest_path):
    """导出用户数据到 dest_path（zip）。返回写入的文件数；失败抛异常。"""
    files = _data_files()
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in files:
            z.write(os.path.join(get_user_data_dir(), name), name)
    return len(files)


def import_backup(src_path):
    """从备份 zip 恢复，覆盖同名 json。返回恢复的文件数；失败抛异常。

    所有条目先完成路径、体积与 JSON 校验，再写入目标目录，避免损坏备份
    导致用户数据只恢复一部分。每个文件通过同目录临时文件原子替换。
    """
    if not zipfile.is_zipfile(src_path):
        raise ValueError("备份文件不是有效的 ZIP")

    entries = []
    total_size = 0
    seen = set()
    with zipfile.ZipFile(src_path, "r") as archive:
        json_infos = [info for info in archive.infolist()
                      if not info.is_dir() and info.filename.endswith(".json")]
        if len(json_infos) > MAX_BACKUP_FILES:
            raise ValueError("备份中的数据文件数量超出限制")
        for info in json_infos:
            name = info.filename
            # 正常备份始终是根目录下的纯文件名；拒绝目录穿越和同名覆盖。
            if os.path.basename(name) != name or name in seen:
                raise ValueError(f"备份包含不安全或重复的路径：{name}")
            if info.file_size > MAX_BACKUP_FILE_SIZE:
                raise ValueError(f"备份数据文件过大：{name}")
            total_size += info.file_size
            if total_size > MAX_BACKUP_TOTAL_SIZE:
                raise ValueError("备份解压后的总大小超出限制")
            data = archive.read(info)
            json.loads(data.decode("utf-8"))
            seen.add(name)
            entries.append((name, data))

    d = get_user_data_dir()
    os.makedirs(d, exist_ok=True)
    for name, data in entries:
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                    mode="wb", dir=d, prefix=".backup_import_",
                    delete=False) as temp_file:
                temp_file.write(data)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = temp_file.name
            os.replace(temp_path, os.path.join(d, name))
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
    return len(entries)
