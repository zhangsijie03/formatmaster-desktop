"""plugin_loader — 轻量插件系统。

约定：`plugins/` 目录下的每个 .py 文件是一个插件，可声明：
    PLUGIN_INFO = {"name", "description", "author", "version"}   # 元数据（必填 name）
    PANEL_CLASS = <QWidget 子类>                                  # 可选：提供界面
    def on_load(ctx): ...                                         # 可选：加载回调（ctx 为 dict）
    def on_unload(): ...                                          # 可选：卸载回调

扫描顺序：先用户数据目录 %APPDATA%/FormatMaster/plugins，再项目 plugins/。
纯逻辑，无 UI 依赖。
"""

import ast
import importlib.util
import logging
import os
import shutil
import stat
import sys
import tempfile
import tokenize
import zipfile
from dataclasses import dataclass

from utils.config import get_user_data_dir


logger = logging.getLogger(__name__)

MAX_PLUGIN_FILES = 100
MAX_PLUGIN_FILE_BYTES = 2 * 1024 * 1024
MAX_PLUGIN_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000


@dataclass
class PluginInfo:
    name: str
    description: str = ""
    author: str = ""
    version: str = ""
    module: object = None       # 加载后的模块
    panel_class: object = None  # PANEL_CLASS
    source: str = ""            # 插件文件路径


def plugin_dirs():
    """返回待扫描目录（用户数据目录优先，内置 plugins/ 其次）。

    内置插件目录用 get_resource_path 解析：打包后指向
    _MEIPASS/plugins（对应 build.py 的 --add-data plugins;plugins，
    onedir 下位于 _internal/plugins）；开发环境即项目根 plugins/。
    用 __file__ 推导在打包后指向 exe 旁目录，会找不到插件。
    """
    dirs = [os.path.join(get_user_data_dir(), "plugins")]
    try:
        from utils.config import get_resource_path
        rp = get_resource_path("plugins")
        if os.path.isdir(rp):
            dirs.append(rp)
    except Exception:  # noqa: BLE001 - 资源路径不可用时回退旧逻辑
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dirs.append(os.path.join(root, "plugins"))
    return dirs


def _load_module(path):
    """importlib 加载单文件模块，失败返回 None。"""
    try:
        name = f"fm_plugin_{os.path.splitext(os.path.basename(path))[0]}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 - 单个插件失败不影响其他
        sys.modules.pop(name, None)
        logger.exception("插件加载失败：%s", path)
        return None


def scan_plugins(include_errors=False):
    """扫描插件；可选同时返回未能加载的文件名，供管理页明确反馈。"""
    found = []
    errors = []
    # 用户插件目录优先；同名文件只加载第一份，使用户安装的版本稳定覆盖内置版。
    seen = set()
    for d in plugin_dirs():
        if not os.path.isdir(d):
            continue
        try:
            filenames = sorted(os.listdir(d))
        except OSError:
            logger.exception("插件目录无法读取：%s", d)
            errors.append(os.path.basename(d) or d)
            continue
        for fn in filenames:
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            path = os.path.join(d, fn)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            key = os.path.normcase(fn).casefold()
            if key in seen:
                continue
            seen.add(key)
            info = _load_plugin_file(path)
            if info:
                found.append(info)
            else:
                errors.append(fn)
    return (found, errors) if include_errors else found


def _load_plugin_file(path):
    """加载单个插件文件 → PluginInfo；失败返回 None。"""
    mod = _load_module(path)
    if mod is None:
        return None
    meta = getattr(mod, "PLUGIN_INFO", None) or {}
    if not isinstance(meta, dict):
        logger.error("插件 PLUGIN_INFO 不是字典：%s", path)
        return None
    name = str(meta.get("name") or os.path.splitext(os.path.basename(path))[0])
    return PluginInfo(
        name=name,
        description=str(meta.get("description", "")),
        author=str(meta.get("author", "")),
        version=str(meta.get("version", "")),
        module=mod,
        panel_class=getattr(mod, "PANEL_CLASS", None),
        source=path,
    )


def activate(info: PluginInfo, ctx: dict):
    """调用插件 on_load(ctx)；失败返回 False。"""
    if info.module is None:
        return False
    on_load = getattr(info.module, "on_load", None)
    if callable(on_load):
        try:
            on_load(ctx)
        except Exception:  # noqa: BLE001
            logger.exception("插件 on_load 执行失败：%s", info.source)
            return False
    return True


def deactivate(info: PluginInfo):
    """调用插件 on_unload()（若提供）。"""
    if info.module is None:
        return
    on_unload = getattr(info.module, "on_unload", None)
    if callable(on_unload):
        try:
            on_unload()
        except Exception:  # noqa: BLE001
            logger.exception("插件 on_unload 执行失败：%s", info.source)


def validate_plugin_file(path):
    """静态校验插件源码，不在用户确认导入前执行第三方代码。"""
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return False, "插件必须是普通文件，不能是符号链接"
        if info.st_size > MAX_PLUGIN_FILE_BYTES:
            return False, "插件文件超过 2 MB 限制"
        with tokenize.open(path) as fh:
            source = fh.read(MAX_PLUGIN_FILE_BYTES + 1)
        if len(source.encode("utf-8")) > MAX_PLUGIN_FILE_BYTES:
            return False, "插件文件超过 2 MB 限制"
        tree = ast.parse(source, filename=os.path.basename(path))
    except (OSError, SyntaxError, UnicodeError) as e:
        return False, f"源码无法解析：{e}"

    meta = None
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign):
            target = next((item for item in node.targets
                           if isinstance(item, ast.Name)
                           and item.id == "PLUGIN_INFO"), None)
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)
              and node.target.id == "PLUGIN_INFO"):
            target = node.target
        if target is not None:
            try:
                meta = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return False, "PLUGIN_INFO 必须是字面量字典"
            break
    if not isinstance(meta, dict):
        return False, "缺少 PLUGIN_INFO（需含 name）"
    name = str(meta.get("name") or "").strip()
    if not name:
        return False, "缺少 PLUGIN_INFO（需含 name）"
    if any(ord(char) < 32 for char in name):
        return False, "PLUGIN_INFO.name 包含控制字符"
    return True, name


def import_plugin(source, target_dir):
    """导入插件源（.py / .zip / 文件夹）到 target_dir。

    全部校验通过才复制落地；有任何一个无效则整体拒绝（不留残件）。
    返回 (ok, 成功导入的插件名列表或错误原因)。
    """
    tmp_dir = None
    staged = []
    committed = []
    try:
        source = os.fspath(source)
        target_dir = os.fspath(target_dir)
        if os.path.isdir(source):
            files = [os.path.join(source, f) for f in sorted(os.listdir(source))
                     if f.endswith(".py") and not f.startswith("_")
                     and not os.path.islink(os.path.join(source, f))
                     and os.path.isfile(os.path.join(source, f))]
            if not files:
                return False, "文件夹里没有 .py 插件文件"
        elif source.lower().endswith(".zip"):
            with zipfile.ZipFile(source) as zf:
                if len(zf.infolist()) > MAX_ARCHIVE_ENTRIES:
                    return False, "压缩包条目数量超过 1000 个限制"
                entries = [entry for entry in zf.infolist()
                           if entry.filename.endswith(".py")
                           and not os.path.basename(entry.filename).startswith("_")
                           and not entry.is_dir()]
                if not entries:
                    return False, "压缩包里没有 .py 插件文件"
                if len(entries) > MAX_PLUGIN_FILES:
                    return False, "压缩包内插件数量超过 100 个限制"
                total_size = sum(entry.file_size for entry in entries)
                if (total_size > MAX_PLUGIN_ARCHIVE_BYTES
                        or any(entry.file_size > MAX_PLUGIN_FILE_BYTES
                               for entry in entries)):
                    return False, "压缩包解压内容超过安全限制"
                basenames = [os.path.basename(entry.filename) for entry in entries]
                if len({name.casefold() for name in basenames}) != len(basenames):
                    return False, "压缩包包含重名插件文件"
                tmp_dir = tempfile.mkdtemp(prefix="fm_imp_")
                for entry, base in zip(entries, basenames):
                    # 只取文件名落盘，忽略压缩包内目录结构（防路径穿越）
                    with zf.open(entry) as src, open(
                            os.path.join(tmp_dir, base), "wb") as dst:
                        shutil.copyfileobj(src, dst, length=64 * 1024)
            files = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)]
        elif source.lower().endswith(".py"):
            files = [source]
        else:
            return False, "不支持的格式（支持 .py / .zip / 文件夹）"

        if len(files) > MAX_PLUGIN_FILES:
            return False, "一次最多导入 100 个插件"
        basenames = [os.path.basename(path) for path in files]
        if len({name.casefold() for name in basenames}) != len(basenames):
            return False, "导入内容包含重名插件文件"

        # 先静态校验全部文件与目标冲突，再分阶段落盘，避免执行未信任源码。
        valid = []
        for f in files:
            ok, info = validate_plugin_file(f)
            if not ok:
                return False, f"{os.path.basename(f)}：{info}（已取消导入，未写入任何文件）"
            valid.append((f, info))

        conflicts = [name for name in basenames
                     if os.path.lexists(os.path.join(target_dir, name))]
        if conflicts:
            return False, f"目标中已存在同名插件：{', '.join(conflicts)}"

        os.makedirs(target_dir, exist_ok=True)
        imported = []
        for f, name in valid:
            dst = os.path.join(target_dir, os.path.basename(f))
            fd, temp_path = tempfile.mkstemp(prefix=".fm_plugin_",
                                             suffix=".tmp", dir=target_dir)
            os.close(fd)
            staged.append((temp_path, dst))
            shutil.copy2(f, temp_path)
            imported.append(name)
        for temp_path, dst in staged:
            os.replace(temp_path, dst)
            committed.append(dst)
        return True, imported
    except Exception as e:  # noqa: BLE001
        return False, f"导入失败：{e}"
    finally:
        # 新导入不允许覆盖旧文件，因此失败时可安全删除本次已提交项并恢复全无状态。
        if len(committed) != len(staged):
            for path in committed:
                try:
                    os.remove(path)
                except OSError:
                    pass
            for temp_path, _dst in staged:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
