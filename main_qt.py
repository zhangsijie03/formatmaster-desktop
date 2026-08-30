"""格式大师 — PySide6 + Fluent Widgets UI 入口。

运行：python main_qt.py
支持：
  python main_qt.py --convert "文件路径"          右键菜单 / Finder → 打开面板 + 路由文件
  python main_qt.py --quick-convert "文件路径"    CLI 后台静默转换（不显示 GUI）
  python main_qt.py --api-server [--port 5000]  启动本地 REST API
  python main_qt.py --install-shell              注册 Windows 右键菜单
  python main_qt.py --remove-shell               移除 Windows 右键菜单
  python main_qt.py --self-test-package          验证正式包核心依赖与内置工具
（旧 tkinter 入口 main.py 已删除，本文件为唯一入口）
"""
import os
import sys

# PyInstaller 的 macOS onedir 包会先把 Frameworks 放入 sys.path；OpenCV
# 自带加载器若把扩展目录插到第二位，会再次导入 cv2 包并触发递归保护。
# 该官方加载器开关要求它替换首项，确保实际加载 cv2.abi3 扩展。
if getattr(sys, "frozen", False):
    sys.OpenCV_REPLACE_SYS_PATH_0 = True

# 确保项目根目录在 sys.path（支持任意工作目录启动）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 提前加载语言偏好（必须在 import gui_qt.app / utils.config 之前执行——
# config 等模块的模块级 tr() 需要正确的语言；延迟到 MainWindow 构造会拿到中文）
try:
    import json as _json
    from gui_qt.i18n import set_language
    from utils.config import get_user_data_dir
    _prefs_path = os.path.join(get_user_data_dir(), "user_prefs.json")
    _lang = "zh"
    if os.path.isfile(_prefs_path):
        with open(_prefs_path, encoding="utf-8") as _f:
            _lang = _json.load(_f).get("language", "zh")
    set_language(_lang)
except Exception:  # noqa: BLE001 - 语言加载失败不影响启动（默认中文）
    pass


def _setup_high_dpi():
    """在 QApplication 创建前配置高 DPI 行为。

    Qt 6 默认以 DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 创建进程
    DPI 感知，无需（也不应）手动调用 SetProcessDpiAwareness——手动设置
    会锁定感知级别，阻止 Qt 使用 V2 上下文并触发 '拒绝访问' 警告。

    这里只做 Qt 提供的纯缩放策略配置：PassThrough 让 125%/150% 等
    非整数缩放按真实比例渲染，避免取整导致的模糊。
    """
    try:
        from PySide6.QtCore import Qt
        Qt.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass


def _print_crypto_hint():
    venv_python = ("venv\\Scripts\\python.exe"
                   if os.name == "nt" else "venv/bin/python")
    print("=" * 60)
    print("[提示] 当前 Python 环境缺少 cryptography 模块，文件安全工具（加密/解密）将不可用。")
    print(f"        请改用项目虚拟环境运行：{venv_python} main_qt.py")
    print("        或执行：pip install cryptography")
    print("=" * 60)


def _ensure_crypto_env():
    """缺失 cryptography 时自动改用项目 venv 重启（无论用哪个 Python 启动）。

    - 当前环境有 cryptography → 正常继续
    - 缺 cryptography 且存在项目 venv → 静默用 venv 的 python 拉起本程序后退出
      （CREATE_NO_WINDOW 避免 Windows 下闪现黑框；仅 venv 也缺时打印指引）
    - venv 也缺 → 控制台打印明确指引（FM_NO_REEXEC 防止无限重启）
    """
    if os.environ.get("FM_NO_REEXEC"):
        return
    try:
        import cryptography  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    root = os.path.dirname(os.path.abspath(__file__))
    venv_rel = (os.path.join("venv", "Scripts", "python.exe")
                if os.name == "nt" else os.path.join("venv", "bin", "python"))
    venv_py = os.path.join(root, venv_rel)
    if not os.path.isfile(venv_py):
        _print_crypto_hint()
        return
    try:
        import subprocess
        env = dict(os.environ)
        env["FM_NO_REEXEC"] = "1"
        # CREATE_NO_WINDOW：静默拉起 venv，不闪现控制台黑框
        flags = 0x08000000 if os.name == "nt" else 0
        subprocess.Popen(
            [venv_py, os.path.abspath(__file__)] + sys.argv[1:],
            cwd=root, env=env, creationflags=flags)
        sys.exit(0)
    except Exception:  # noqa: BLE001
        _print_crypto_hint()


def _run_package_self_test():
    """在已打包程序内验证动态依赖、声明文件和媒体工具均可实际加载。"""
    import importlib
    import subprocess

    if not getattr(sys, "frozen", False):
        raise RuntimeError("--self-test-package 仅用于 PyInstaller 发布产物")

    required_modules = (
        "PySide6", "cryptography", "onnxruntime", "pymupdf",
        "qfluentwidgets", "rapidocr_onnxruntime", "rapid_table", "yt_dlp",
    )
    for module in required_modules:
        importlib.import_module(module)

    from utils.config import APP_VERSION, get_resource_path

    for notice in ("LICENSE", "NOTICE", "PRIVACY.md", "SECURITY.md",
                   "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.md"):
        if not os.path.isfile(get_resource_path(notice)):
            raise RuntimeError(f"发布包缺少声明文件：{notice}")

    tool_names = ("ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe") \
        if os.name == "nt" else ("ffmpeg", "ffprobe", "yt-dlp")
    for tool in tool_names:
        path = get_resource_path(os.path.join("bin", tool))
        if not os.path.isfile(path):
            raise RuntimeError(f"发布包缺少工具：{tool}")
        version_arg = "--version" if tool.startswith("yt-dlp") else "-version"
        result = subprocess.run(
            [path, version_arg], capture_output=True, timeout=20,
            text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"发布包工具无法运行：{tool}")
    print(f"PACKAGE_SELF_TEST_OK version={APP_VERSION}")


if __name__ == "__main__":
    _ensure_crypto_env()

    # ── CLI 命令（无需 GUI）─────────────────
    args = sys.argv[1:]
    if "--self-test-package" in args:
        try:
            _run_package_self_test()
        except Exception as ex:  # noqa: BLE001 - 发布门禁必须输出具体失败项
            print(f"PACKAGE_SELF_TEST_ERROR={type(ex).__name__}: {ex}")
            sys.exit(1)
        sys.exit(0)

    if "--api-server" in args:
        from api_server import main as api_main
        api_args = []
        for option in ("--host", "--port"):
            if option in args:
                i = args.index(option)
                if i + 1 < len(args):
                    api_args.extend([option, args[i + 1]])
        api_main(api_args)
        sys.exit(0)

    if "--install-shell" in args:
        _setup_high_dpi()
        from utils.shell_integration import install_context_menu, is_installed
        ok = install_context_menu()
        print("右键菜单注册成功" if ok else "右键菜单注册失败（请以管理员权限重试）")
        sys.exit(0 if ok else 1)

    if "--remove-shell" in args:
        from utils.shell_integration import remove_context_menu
        remove_context_menu()
        print("已移除右键菜单")
        sys.exit(0)

    if "--quick-convert" in args:
        i = args.index("--quick-convert")
        if i + 1 < len(args):
            path = args[i + 1]
            if not os.path.isfile(path):
                print(f"[错误] 文件不存在: {path}")
                sys.exit(1)
            from core.cli_bridge import auto_convert
            print(f"[格式大师] 正在后台转换: {os.path.basename(path)}")
            ok, out = auto_convert(path)
            if ok:
                print(f"[完成] → {out}")
                sys.exit(0)
            else:
                print("[失败] 转换未完成，请检查文件是否损坏或格式是否支持")
                sys.exit(1)
        sys.exit(1)

    if "--self-test-ocr" in args:
        # 发布包验收入口：直接在冻结环境内执行一次真实 OCR，避免出现
        # 开发环境可用、PyInstaller 漏收动态依赖却仍发布的情况。
        i = args.index("--self-test-ocr")
        if i + 2 >= len(args):
            print("用法: --self-test-ocr <输入图片> <输出CSV>")
            sys.exit(2)
        from core.table_recognizer import recognize_table
        try:
            ok = recognize_table(args[i + 1], args[i + 2])
        except Exception as ex:  # noqa: BLE001 - 自检需要输出完整根因
            import traceback
            traceback.print_exc()
            print(f"OCR_SELF_TEST_ERROR={type(ex).__name__}: {ex}")
            sys.exit(1)
        print("OCR_SELF_TEST_OK" if ok else "OCR_SELF_TEST_EMPTY")
        sys.exit(0 if ok else 1)

    # ── GUI 模式 ───────────────────────────
    _setup_high_dpi()
    from gui_qt.app import run  # noqa: E402
    # --convert <path>：右键菜单集成入口
    convert_path = None
    if "--convert" in args:
        i = args.index("--convert")
        if i + 1 < len(args):
            convert_path = args[i + 1]
    elif sys.platform == "darwin":
        # Finder「打开方式」会把文件作为裸参数传入；忽略 macOS 可能附带的
        # -psn_* 参数，只接收实际存在的文件，保持 --quick-convert 优先级不变。
        for arg in args:
            if not arg.startswith("-") and os.path.isfile(arg):
                convert_path = os.path.abspath(arg)
                break
    run(convert_path=convert_path)
