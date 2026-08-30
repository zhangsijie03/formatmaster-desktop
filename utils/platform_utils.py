"""跨平台系统集成的小工具。

业务模块只需要表达“打开这个文件或目录”，不应各自拼接
``explorer`` / ``xdg-open`` / ``open``，否则新增平台时容易漏改。
"""
import os
import shutil
import subprocess
import sys


def open_path(path: str) -> bool:
    """使用当前系统的默认程序打开文件或目录。

    返回值用于让 UI 在系统命令不存在或启动失败时给出提示；这里捕获的是
    系统集成边界上的预期失败，不把异常传播到转换任务主流程。
    """
    if not path:
        return False
    try:
        target = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(target):
            return False
        if sys.platform == "win32":
            os.startfile(target)
            return True

        command = "open" if sys.platform == "darwin" else "xdg-open"
        if shutil.which(command) is None:
            return False
        subprocess.Popen([command, target])
        return True
    except (OSError, TypeError):
        return False
