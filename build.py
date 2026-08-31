"""打包脚本 - 将格式大师打包为桌面应用（新版 PySide6 / Fluent Widgets）

风险规避：
1. 默认使用文件夹模式（--onedir），测试 PyMuPDF 等 C 扩展库能否正常加载；
   验证通过后可加 --onefile 参数切换为单文件模式。
2. 显式添加 --collect-all pymupdf，确保 PyMuPDF 的二进制资源被正确收集。
3. 添加 --collect-submodules PIL，避免 Pillow 子模块丢失。
"""
import subprocess
import sys
import os
import plistlib
import re
import shutil
import tempfile
import time

from utils.config import APP_VERSION


def _configure_console_encoding():
    """让 Windows CI 能稳定输出中文路径和构建提示。

    GitHub Windows Runner 在管道输出场景可能使用 cp1252；项目路径和
    构建提示包含中文时，默认 print 会在真正启动 PyInstaller 前抛出
    UnicodeEncodeError。统一改为 UTF-8，并用 replace 保留构建日志。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # 某些测试/宿主会提供不可重配置的伪终端；不影响实际构建。
            continue


def _add_data(cmd, source, target):
    """仅打包存在的资源，并使用当前平台的 PyInstaller 分隔符。"""
    if not os.path.exists(source):
        print(f"⚠ 跳过缺失资源：{source}")
        return
    cmd.extend(["--add-data", f"{source}{os.pathsep}{target}"])


def _check_env():
    """解释器依赖自检：build.py 由 sys.executable 调 PyInstaller，若用
    非 venv 解释器（如系统 python），可能缺 rapid_table/opencc/cryptography
    等打包关键依赖 → PyInstaller 分析阶段 ModuleNotFoundError 打包失败。
    启动前检查，缺失时给出明确指引而非裸报错。"""
    import importlib.util
    critical = ["PySide6", "qfluentwidgets", "rapid_table", "opencc",
                "cryptography", "rapidocr_onnxruntime", "onnxruntime",
                "pdf2docx", "reportlab", "pymupdf", "PIL"]
    missing = [m for m in critical if importlib.util.find_spec(m) is None]
    if missing:
        print("✗ 当前解释器缺少打包关键依赖：" + ", ".join(missing))
        venv_python = ("venv\\Scripts\\python.exe"
                       if os.name == "nt" else "venv/bin/python")
        print(f"  请改用项目虚拟环境解释器运行：{venv_python} build.py")
        sys.exit(1)


_MACOS_DOCUMENT_EXTENSIONS = [
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v",
    "mpg", "mpeg", "ts", "3gp", "mp3", "wav", "aac", "flac", "ogg",
    "m4a", "wma", "amr", "opus", "png", "jpg", "jpeg", "bmp", "gif",
    "tiff", "webp", "avif", "heic", "heif", "ico", "tga", "pdf", "doc",
    "docx", "xls", "xlsx", "ppt", "pptx", "dps", "txt", "csv", "html", "htm",
    "md", "rtf", "odt", "ofd", "wps", "et", "epub", "mobi", "prc",
    "azw", "azw3",
]


def _macos_bundle_versions(version):
    """将应用语义版本映射为 Apple 允许的营销版本与构建版本。"""
    match = re.fullmatch(
        r"(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?", version)
    if not match:
        raise ValueError(f"不支持的应用版本：{version}")
    base, channel, sequence = match.groups()
    if not channel:
        return base, base
    suffix = {"alpha": "a", "beta": "b", "rc": "fc"}[channel]
    return base, f"{base}{suffix}{sequence}"


def _find_macos_bundle(dist_path):
    """返回打包输出中的 .app 路径；不存在时返回空字符串。"""
    for root, dirs, _files in os.walk(dist_path):
        for name in dirs:
            if name.endswith(".app"):
                return os.path.join(root, name)
    return ""


def _configure_macos_bundle(dist_path):
    """为 macOS .app 注册 Finder「打开方式」支持。"""
    if sys.platform != "darwin":
        return False
    try:
        app_path = _find_macos_bundle(dist_path)
        if not app_path:
            return False
        info_path = os.path.join(app_path, "Contents", "Info.plist")
        with open(info_path, "rb") as stream:
            info = plistlib.load(stream)
        # 让 Finder「显示简介」和崩溃报告使用发布版本，而不是
        # PyInstaller 默认的 0.0.0，避免用户无法对应 GitHub Release。
        marketing_version, build_version = _macos_bundle_versions(APP_VERSION)
        info["CFBundleShortVersionString"] = marketing_version
        info["CFBundleVersion"] = build_version
        info["FormatMasterReleaseVersion"] = APP_VERSION
        document_types = info.get("CFBundleDocumentTypes") or []
        if isinstance(document_types, dict):
            document_types = [document_types]
        elif not isinstance(document_types, list):
            document_types = []
        if not any(item.get("CFBundleTypeName") == "FormatMaster files"
                   for item in document_types if isinstance(item, dict)):
            document_types.append({
                "CFBundleTypeName": "FormatMaster files",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Owner",
                "CFBundleTypeExtensions": _MACOS_DOCUMENT_EXTENSIONS,
            })
        info["CFBundleDocumentTypes"] = document_types

        fd, temp_path = tempfile.mkstemp(
            dir=os.path.dirname(info_path), prefix=".Info-", suffix=".plist")
        try:
            with os.fdopen(fd, "wb") as stream:
                plistlib.dump(info, stream, fmt=plistlib.FMT_BINARY,
                              sort_keys=False)
            os.replace(temp_path, info_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        return True
    except (OSError, StopIteration, plistlib.InvalidFileException, ValueError):
        return False


def _require_macos_tool(name):
    """确认 macOS 发布工具存在，返回绝对路径。"""
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"找不到 macOS 发布工具：{name}")
    return path


def _run_macos_release_command(cmd, label):
    """执行签名/镜像/公证命令，失败时保留 stderr 便于定位。"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"{label}无法启动：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{label}失败：{detail[-500:]}")
    return result


def _sign_macos_app(app_path, identity):
    """用 Developer ID 对 .app 及其嵌套二进制签名并验证。"""
    codesign = _require_macos_tool("codesign")
    _run_macos_release_command(
        [codesign, "--deep", "--force", "--options", "runtime",
         "--timestamp", "--sign", identity, app_path],
        "macOS 代码签名")
    _run_macos_release_command(
        [codesign, "--verify", "--deep", "--strict", "--verbose=2", app_path],
        "macOS 签名验证")


def _sign_macos_app_adhoc(app_path):
    """为未配置 Developer ID 的公开构建恢复有效的临时签名。"""
    codesign = _require_macos_tool("codesign")
    _run_macos_release_command(
        [codesign, "--deep", "--force", "--sign", "-", app_path],
        "macOS 临时签名")
    _run_macos_release_command(
        [codesign, "--verify", "--deep", "--strict", "--verbose=2", app_path],
        "macOS 临时签名验证")


def _create_macos_dmg(app_path, output_path):
    """将 .app 制作为带 Applications 快捷方式的压缩 DMG。

    直接把 .app 作为 hdiutil 的源目录虽然能生成镜像，但用户无法像
    常见 macOS 安装包一样把应用拖入 Applications。这里使用临时 staging
    目录构建标准拖拽布局，避免修改已签名的应用包。
    """
    hdiutil = _require_macos_tool("hdiutil")
    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix="formatmaster-dmg-", dir=output_dir) as staging:
        staged_app = os.path.join(staging, os.path.basename(app_path))
        shutil.copytree(app_path, staged_app, symlinks=True)
        # 用户无需进入 .app 包体即可查看产品许可、隐私和安全说明。
        project_dir = os.path.dirname(os.path.abspath(__file__))
        for notice in ("LICENSE", "NOTICE", "PRIVACY.md", "SECURITY.md",
                       "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.md"):
            source = os.path.join(project_dir, notice)
            if not os.path.isfile(source):
                raise RuntimeError(f"DMG 缺少发布声明文件：{notice}")
            shutil.copy2(source, os.path.join(staging, notice))
        # Applications 别名是 macOS 用户最熟悉的拖拽安装路径；失败时让
        # hdiutil 命令直接报错，避免生成一个看似成功但布局不完整的 DMG。
        os.symlink("/Applications", os.path.join(staging, "Applications"))
        command = [hdiutil, "create", "-volname", "FormatMaster",
                   "-srcfolder", staging, "-ov", "-format", "UDZO",
                   "-imagekey", "zlib-level=9", output_path]
        for attempt in range(1, 4):
            try:
                _run_macos_release_command(command, "macOS DMG 制作")
                break
            except RuntimeError as exc:
                # GitHub macOS runner 的 diskimages-helper 偶尔短暂占用资源；
                # 仅重试这一瞬时错误，避免掩盖空间不足或参数错误。
                if "Resource busy" not in str(exc) or attempt == 3:
                    raise
                if os.path.exists(output_path):
                    os.unlink(output_path)
                time.sleep(attempt * 2)
    return output_path


def _notarize_macos_dmg(dmg_path, profile):
    """使用本机 notarytool Keychain profile 公证并装订票据。"""
    xcrun = _require_macos_tool("xcrun")
    _run_macos_release_command(
        [xcrun, "notarytool", "submit", dmg_path,
         "--keychain-profile", profile, "--wait"],
        "macOS notarization")
    _run_macos_release_command(
        [xcrun, "stapler", "staple", dmg_path],
        "macOS notarization 票据装订")
    _run_macos_release_command(
        [xcrun, "stapler", "validate", dmg_path],
        "macOS notarization 验证")


def _validate_macos_release_options(sign_identity=None,
                                    notarize_profile=None,
                                    make_dmg=False):
    """在耗时的 PyInstaller 构建前校验 macOS 发布参数组合。"""
    if sys.platform != "darwin":
        if sign_identity or notarize_profile or make_dmg:
            raise RuntimeError("--dmg/签名/公证参数只能在 macOS 上使用")
        return
    if notarize_profile and not make_dmg:
        raise RuntimeError("macOS 公证需要同时指定 --dmg")
    if notarize_profile and not sign_identity:
        raise RuntimeError("macOS 公证前必须先提供 --sign-identity")


def _finalize_macos_release(dist_path, out_root, sign_identity=None,
                            notarize_profile=None, make_dmg=False):
    """执行可选的 macOS 签名、DMG 和公证流程。"""
    _validate_macos_release_options(sign_identity, notarize_profile, make_dmg)
    if sys.platform != "darwin":
        return None

    app_path = _find_macos_bundle(dist_path)
    if not app_path:
        raise RuntimeError("未找到 PyInstaller 生成的 .app")
    if sign_identity:
        _sign_macos_app(app_path, sign_identity)
    else:
        # Finder 关联修改了 Info.plist；重新做 ad-hoc 签名，避免把一个
        # 看似成功但签名已失效的 .app 放进公开 DMG。
        _sign_macos_app_adhoc(app_path)
    if not make_dmg:
        return None

    dmg_path = os.path.join(out_root, "格式大师-macOS.dmg")
    _create_macos_dmg(app_path, dmg_path)
    if notarize_profile:
        _notarize_macos_dmg(dmg_path, notarize_profile)
    return dmg_path


def main(onefile=False, sign_identity=None, notarize_profile=None,
         make_dmg=False):
    try:
        _validate_macos_release_options(sign_identity, notarize_profile,
                                        make_dmg)
    except RuntimeError as exc:
        print(f"✗ macOS 发布参数无效：{exc}")
        sys.exit(2)
    _check_env()
    project_dir = os.path.dirname(os.path.abspath(__file__))
    main_script = os.path.join(project_dir, "main_qt.py")

    # 输出目录：默认项目外同盘上级（避免项目内 build/dist 被清理/污染）
    out_root = os.environ.get("FORMATMASTER_DIST",
                              os.path.join(os.path.dirname(project_dir), "FormatMaster_dist"))
    dist_path = os.path.join(out_root, "dist")
    work_path = os.path.join(out_root, "build")
    os.makedirs(out_root, exist_ok=True)

    # Windows 使用 ICO；macOS 应提供 ICNS。图标缺失时不阻断功能包构建，
    # 由 PyInstaller 使用默认图标，避免当前源码不含二进制资源时直接失败。
    icon_name = "icon.ico" if os.name == "nt" else "icon.icns"
    icon_path = os.path.join(project_dir, "assets", icon_name)
    icon_args = ["--icon", icon_path] if os.path.isfile(icon_path) else []
    bundle_args = (["--osx-bundle-identifier", "io.github.zhangsijie03.formatmaster"]
                   if sys.platform == "darwin" else [])
    # macOS 交付契约使用 FormatMaster.app；Windows 保留原有中文文件名。
    product_name = "FormatMaster" if sys.platform == "darwin" else "格式大师"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--distpath", dist_path,
        "--workpath", work_path,
        "--specpath", out_root,
        # 风险规避：默认 onedir，确认 PyMuPDF 等 C 扩展加载正常后再切 --onefile
        "--onefile" if onefile else "--onedir",
        "--windowed",
        "--name", product_name,
        *icon_args,
        *bundle_args,
        "--paths", project_dir,
        # 核心模块（collect-submodules core 全量收集：函数内延迟 import 的
        # crypto_advanced/auto_recover/file_repair/doc_excel 等模块均防丢）
        "--hidden-import", "core",
        "--collect-submodules", "core",
        "--hidden-import", "core.video_converter",
        "--hidden-import", "core.audio_converter",
        "--hidden-import", "core.image_converter",
        "--hidden-import", "core.doc_converter",
        "--hidden-import", "core.ffmpeg_executor",
        "--hidden-import", "core.tools",
        "--hidden-import", "utils",
        "--hidden-import", "utils.config",
        "--hidden-import", "utils.ffmpeg_manager",
        "--hidden-import", "utils.autostart",
        # 文档转换依赖
        "--hidden-import", "docx",
        "--hidden-import", "openpyxl",
        "--hidden-import", "pptx",
        "--hidden-import", "pypdf",
        "--hidden-import", "pdf2docx",
        "--hidden-import", "reportlab",
        # 文档转换补充依赖（函数内延迟导入，显式收集防丢）
        "--hidden-import", "striprtf",
        "--hidden-import", "odf",
        "--hidden-import", "xlrd",
        "--hidden-import", "chardet",
        "--hidden-import", "psutil",
        # 风险规避：完整收集 PyMuPDF 的二进制与资源。项目代码使用
        # import pymupdf；需要兼容旧版 pdf2docx 时由运行时代码绑定 fitz 别名。
        "--collect-all", "pymupdf",
        "--hidden-import", "PIL",
        "--collect-submodules", "PIL",
        # 二维码
        "--hidden-import", "qrcode",
        # 工具模块
        "--hidden-import", "utils.presets",
        "--hidden-import", "utils.format_helpers",
        "--hidden-import", "utils.hardware_accel",
        # 应用层
        "--hidden-import", "app",
        "--hidden-import", "app.exceptions",
        # 本地 REST API（main_qt --api-server 为延迟导入）
        "--hidden-import", "api_server",
        "--hidden-import", "fastapi",
        "--hidden-import", "uvicorn",
        # 新版 GUI（PySide6）
        "--hidden-import", "gui_qt",
        "--hidden-import", "gui_qt.app",
        "--hidden-import", "gui_qt.services",
        "--hidden-import", "gui_qt.task_manager",
        "--hidden-import", "gui_qt.nav_registry",
        "--hidden-import", "gui_qt.update_checker",
        "--hidden-import", "gui_qt.widgets",
        "--hidden-import", "gui_qt.i18n",
        "--hidden-import", "gui_qt.context_menu",
        "--hidden-import", "gui_qt.components",
        "--hidden-import", "gui_qt.pages",
        "--hidden-import", "gui_qt.panels",
        # 风险规避：nav_registry 用 importlib 动态加载面板/页面/组件，
        # PyInstaller 静态分析无法发现，必须 collect-submodules 全量收集
        "--collect-submodules", "gui_qt.panels",
        "--collect-submodules", "gui_qt.pages",
        "--collect-submodules", "gui_qt.components",
        # PySide6 / qfluentwidgets
        "--collect-submodules", "qfluentwidgets",
        "--hidden-import", "qfluentwidgets",
        # 核心模块
        "--hidden-import", "core.image_cropper",
        "--hidden-import", "core.m3u8_downloader",
        "--hidden-import", "core.ocr_tool",
        "--hidden-import", "core.audio_trimmer",
        "--hidden-import", "core.hash_tool",
        "--hidden-import", "core.watermark_tool",
        "--hidden-import", "core.thumbnail_sheet",
        "--hidden-import", "core.pdf_extract",
        "--hidden-import", "core.pdf_to_image",
        "--hidden-import", "core.video_tools",
        "--hidden-import", "core.video_downloader",
        # 视频/M3U8 下载 Python 引擎（函数内延迟 import，静态分析发现不了；
        # bin 目录随包打入平台对应的 yt-dlp 可执行文件，此 hidden-import
        # 保证 Python API 路径（格式解析等）在打包后也可用）
        "--hidden-import", "yt_dlp",
        "--hidden-import", "core.table_recognizer",
        # 表格识别：rapid_table 由 core/ocr_table 函数内延迟导入（PyInstaller
        # 静态分析发现不了），且模型 slanet-plus.onnx 在包内，必须 collect-all
        "--collect-all", "rapid_table",
        # 插件运行时依赖（插件经 importlib 动态加载，PyInstaller 分析不到，
        # 不显式收集则打包后对应插件功能缺失）：
        # 简繁转换（opencc C 扩展）+ YAML 插件（pyyaml）
        "--hidden-import", "opencc",
        "--collect-binaries", "opencc",
        "--hidden-import", "yaml",
        # 网页转 PDF 插件 / ECharts 大屏：QtWebEngine 延迟 import，
        # 显式 hidden-import 触发 PyInstaller 官方 PySide6 hook 收集
        # QtWebEngineProcess.exe / resources 等全部资源
        "--hidden-import", "PySide6.QtWebEngineWidgets",
        "--hidden-import", "PySide6.QtWebEngineCore",
        "--hidden-import", "PySide6.QtWebChannel",
        # 文档→PDF 多级降级引擎（doc_word/doc_misc 函数内延迟导入，显式收集防丢）
        "--hidden-import", "core.doc_office_pdf",
        "--hidden-import", "pillow_avif",
        "--hidden-import", "pillow_heif",
        "--hidden-import", "core.id_photo",
        "--hidden-import", "core.image_album",
        "--hidden-import", "core.audio_tools",
        "--hidden-import", "core.subtitle_extract",
        "--hidden-import", "utils.panel_presets",
        "--hidden-import", "gui_qt.panels.id_photo_panel",
        "--hidden-import", "core.video_compress",
        "--hidden-import", "gui_qt.panels.video_compress_panel",
        "--hidden-import", "gui_qt.panels.image_merge_panel",
        "--hidden-import", "gui_qt.panels.audio_enhance_panel",
        "--hidden-import", "gui_qt.panels.subtitle_panel",
        # 局域网服务（lan_transfer_panel 函数内 import，运行时延迟加载；内部还有
        # lan_transfer↔lan_receiver/lan_sender 循环延迟导入，显式收集防丢）
        "--hidden-import", "core.lan_service",
        "--hidden-import", "core.lan_transfer",
        "--hidden-import", "core.lan_receiver",
        "--hidden-import", "core.lan_sender",
        "--hidden-import", "rapidocr_onnxruntime",
        "--hidden-import", "onnxruntime",
        "--collect-all", "rapidocr_onnxruntime",
        # 体积瘦身：onnxruntime 只收二进制与核心包（OCR 推理不需要 transformers/
        # quantization/tools 等可选工具子包，可省 100+ MB）
        "--collect-binaries", "onnxruntime",
        "--exclude-module", "onnxruntime.transformers",
        "--exclude-module", "onnxruntime.quantization",
        "--exclude-module", "onnxruntime.tools",
        # 文件安全工具：cryptography（含 C 扩展 _rust，必须 collect-binaries）
        "--hidden-import", "cryptography",
        "--hidden-import", "cryptography.hazmat.backends.openssl",
        "--collect-binaries", "cryptography",
        "--collect-submodules", "cryptography",
        # 标准库
        "--hidden-import", "json",
        "--hidden-import", "threading",
        "--hidden-import", "subprocess",
        "--hidden-import", "re",
        "--hidden-import", "csv",
        "--hidden-import", "shutil",
        "--hidden-import", "urllib.request",
        "--hidden-import", "urllib.error",
        "--hidden-import", "socket",
        "--noconfirm",
        "--clean",
    ]

    # 动态插件、外部工具和 AI 模型均可能由发布流程单独提供；资源缺失时
    # 允许先构建基础版，运行时由对应功能给出“组件未安装”提示。
    _add_data(cmd, os.path.join(project_dir, "bin"), "bin")
    _add_data(cmd, os.path.join(project_dir, "assets"), "assets")
    _add_data(cmd, os.path.join(project_dir, "plugins"), "plugins")
    _add_data(cmd, os.path.join(project_dir, "data", "models"), "data/models")
    for notice in ("LICENSE", "NOTICE", "PRIVACY.md", "SECURITY.md",
                   "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.md"):
        _add_data(cmd, os.path.join(project_dir, notice), ".")

    # COM 只存在于 Windows。macOS/Linux 若保留这些 hidden-import，会让
    # PyInstaller 分析阶段产生无意义的缺失模块告警甚至构建失败。
    if os.name == "nt":
        cmd.extend([
            "--hidden-import", "win32com",
            "--hidden-import", "win32com.client",
            "--hidden-import", "pythoncom",
        ])
    cmd.append(main_script)

    mode = "单文件" if onefile else "文件夹"
    print(f"正在打包格式大师（{mode}模式）...")
    result = subprocess.run(cmd, cwd=project_dir)
    if result.returncode == 0:
        print(f"\n打包成功！输出目录: {dist_path}")
        if sys.platform == "darwin":
            if _configure_macos_bundle(dist_path):
                print("已注册 Finder 文件关联：可通过「打开方式 → 格式大师」打开文件。")
            else:
                print("警告：未能写入 Finder 文件关联，请检查 .app 是否生成。")
            try:
                dmg = _finalize_macos_release(
                    dist_path, out_root, sign_identity=sign_identity,
                    notarize_profile=notarize_profile, make_dmg=make_dmg)
                if dmg:
                    print(f"macOS DMG 已生成：{dmg}")
                if not sign_identity:
                    print("提示：当前使用 ad-hoc 签名且未公证；首次启动可能需要在 macOS 隐私与安全性中选择“仍要打开”。")
            except RuntimeError as exc:
                print(f"✗ macOS 发布步骤失败：{exc}")
                sys.exit(1)
        elif not onefile:
            print("提示：测试通过后可用 'python build.py --onefile' 重新打包为单文件")
    else:
        print("打包失败")
        sys.exit(1)


if __name__ == "__main__":
    _configure_console_encoding()
    import argparse

    parser = argparse.ArgumentParser(description="构建 FormatMaster 桌面应用")
    parser.add_argument("--onefile", action="store_true",
                        help="构建单文件模式")
    parser.add_argument("--dmg", action="store_true",
                        help="macOS：将 .app 制作为压缩 DMG")
    parser.add_argument("--sign-identity",
                        default=os.environ.get("MACOS_SIGN_IDENTITY", ""),
                        help="macOS：Developer ID Application 签名身份")
    parser.add_argument("--notarize-profile",
                        default=os.environ.get("MACOS_NOTARY_PROFILE", ""),
                        help="macOS：xcrun notarytool Keychain profile")
    args = parser.parse_args()
    main(onefile=args.onefile, sign_identity=args.sign_identity or None,
         notarize_profile=args.notarize_profile or None,
         make_dmg=args.dmg)
