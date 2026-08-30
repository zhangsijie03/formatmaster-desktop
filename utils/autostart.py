"""跨平台登录启动设置。

Windows 的启动项由设置页继续通过注册表管理；macOS 使用当前用户的
LaunchAgent，并通过 launchctl 加载到当前登录会话，避免要求管理员权限。
"""
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile


_MAC_LABEL = "com.formatmaster.app"


def _mac_plist_path():
    return os.path.expanduser(
        f"~/Library/LaunchAgents/{_MAC_LABEL}.plist")


def _mac_command():
    """返回登录后启动应用所需的参数，不经过 shell。"""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    script = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "main_qt.py"))
    return [sys.executable, script]


def _launchctl(args):
    launchctl = shutil.which("launchctl")
    if not launchctl:
        return False
    try:
        result = subprocess.run(
            [launchctl, *args],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def mac_autostart_enabled():
    """判断当前用户的 macOS 登录启动项是否存在。"""
    return sys.platform == "darwin" and os.path.isfile(_mac_plist_path())


def set_mac_autostart(enable):
    """创建或移除 macOS LaunchAgent，返回操作是否成功。"""
    if sys.platform != "darwin":
        return False

    plist_path = _mac_plist_path()
    plist_dir = os.path.dirname(plist_path)
    # macOS 提供 getuid；测试或兼容层模拟 Darwin 时可能没有该属性。
    # UID 环境变量仅作为兼容兜底，不影响真实 macOS 登录会话域。
    get_uid = getattr(os, "getuid", None)
    uid = get_uid() if get_uid else int(os.environ.get("UID", "0"))
    domain = f"gui/{uid}"

    if not enable:
        # 先卸载会话中的旧任务，再移除文件，确保关闭开机启动后立即生效。
        _launchctl(["bootout", domain, plist_path])
        try:
            os.remove(plist_path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    temp_path = ""
    try:
        os.makedirs(plist_dir, mode=0o700, exist_ok=True)
        payload = {
            "Label": _MAC_LABEL,
            "ProgramArguments": _mac_command(),
            "RunAtLoad": True,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
        }

        # 临时文件与目标文件放在同一目录，os.replace 保证 LaunchAgent 不会读到半个 plist。
        fd, temp_path = tempfile.mkstemp(
            dir=plist_dir, prefix=f".{_MAC_LABEL}-", suffix=".plist")
        with os.fdopen(fd, "wb") as stream:
            plistlib.dump(payload, stream, sort_keys=False)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, plist_path)
        temp_path = ""

        # 重新启用前先卸载旧实例，避免同一个 Label 被重复 bootstrap。
        _launchctl(["bootout", domain, plist_path])
        if not _launchctl(["bootstrap", domain, plist_path]):
            os.remove(plist_path)
            return False
        return True
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
