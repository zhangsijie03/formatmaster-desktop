"""输出命名的共同规则：保护源文件、已有文件和待执行任务的目标。"""
import os
from collections.abc import Collection


def path_key(path: str) -> str:
    """解析符号链接，防止同一目标通过不同路径绕过占用检查。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def unique_output_path(target: str, *, source: str = "",
                       reserved: Collection[str] = (),
                       overwrite: bool = False) -> str:
    """reserved 为规范化路径；即使允许覆盖，也不能覆盖源文件或活动目标。"""
    source_key = path_key(source) if source else None
    base, ext = os.path.splitext(target)
    candidate = target
    index = 0
    while (path_key(candidate) == source_key
           or path_key(candidate) in reserved
           or (not overwrite and os.path.lexists(candidate))):
        index += 1
        candidate = f"{base}_{index}{ext}"
    return candidate
