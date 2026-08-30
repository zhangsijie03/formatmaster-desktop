# -*- coding: utf-8 -*-
"""lan_service — 局域网统一服务（FastAPI 单端口承载两能力）。

把一个 HTTP 端口同时提供：
1. 文件分享（原 lan_sender：下载列表页 / 单文件 / 全部打包 / 选中打包 / 预览）
2. 文件接收（原 lan_receiver：上传页 / 流式上传 / 分类 / 冲突策略）

设计要点：
- 复用 core/lan_transfer.py 的 HTML 模板、_parse_multipart_stream、make_zip、
  分类表、_unique_path 等既有实现，不重复造轮子；
- 回调（on_received / on_downloaded / on_progress / on_all_done）经面板信号回主线程；
- 防火墙规则 / IP 探测 / 空闲超时：复用 core/lan_transfer 工具；
- 端口占用自动 +1。
"""
import hashlib
import html
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
import zipfile

# 注意：lan_transfer ↔ lan_receiver/lan_sender 存在循环导入，
# 本模块必须延迟 import（见 _lt() / _lr() / _ls()），不能在模块顶层引。

# 默认端口
DEFAULT_PORT = 8765
MAX_UPLOAD_BYTES = 20 * 1024 * 1024 * 1024
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_SESSIONS = 32
MAX_CHAT_MESSAGES = 2000
MAX_TEXT_MESSAGE = 64 * 1024


async def _spool_request(request, max_bytes=MAX_UPLOAD_BYTES):
    """把请求流落到匿名临时文件；限制声明值和实际值，避免内存/磁盘失控。"""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared < 0 or declared > max_bytes:
        raise ValueError("upload too large")
    stream = tempfile.TemporaryFile(prefix="fm_lan_upload_")
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("upload too large")
            stream.write(chunk)
        stream.seek(0)
        return stream, total
    except Exception:
        stream.close()
        raise


async def _json_request(request, max_bytes=128 * 1024):
    stream, _size = await _spool_request(request, max_bytes)
    try:
        return json.load(stream)
    finally:
        stream.close()


def _multipart_boundary(content_type):
    """提取并约束 multipart boundary，拒绝控制字符和异常超长值。"""
    if "boundary=" not in content_type:
        return b""
    value = content_type.split("boundary=", 1)[1].split(";", 1)[0]
    value = value.strip().strip('"')
    if not value or len(value) > 200 or any(ord(char) < 33 for char in value):
        return b""
    return value.encode("ascii", errors="ignore")


def _port_in_use(host: str, port: int) -> bool:
    """探测端口是否被占用（bind 探测，不加 SO_REUSEADDR 避免 Windows
    双绑定干扰）。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def _get_lan_ip():
    return _lt().get_lan_ip()


def detect_wifi_ssid(timeout=2.0):
    """探测本机当前活跃网络名称。

    Windows 走 netsh（项目主目标平台）；其它平台尽力兜底；任一异常静默。
    返回：WiFi SSID（连接中）、"有线 · <接口名>"（以太网）、或空串。

    中文 Windows 下 netsh 的 State 字段输出是「已连接/已断开」、列名是「状态」
    （非英文 connected），所以不能按英文关键字定位——改为取第一个非空 SSID
    （已连接 WiFi 必有 SSID，未连接为空或「无」），并兜底检查有线接口。
    """
    import sys, subprocess, re
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            # 第一个非空 SSID 即为已连接 WiFi（断开接口 SSID 字段为空或"无"）
            for m in re.finditer(r"^\s*SSID\s*:\s*(.+?)\s*$", out, re.M):
                v = m.group(1).strip()
                if v and v not in ("", "无", "N/A"):
                    return v
        elif sys.platform == "darwin":
            # 新版 macOS 可能已移除旧 airport 工具，优先使用 networksetup
            # 获取实际 Wi-Fi 设备名，再读取当前网络名称。
            ports = subprocess.run(
                ["networksetup", "-listallhardwareports"],
                capture_output=True, text=True, timeout=timeout).stdout
            port = re.search(
                r"Hardware Port:\s*(?:Wi-Fi|AirPort).*?Device:\s*(\S+)",
                ports, re.S | re.I)
            device = port.group(1) if port else "en0"
            out = subprocess.run(
                ["networksetup", "-getairportnetwork", device],
                capture_output=True, text=True, timeout=timeout).stdout
            m = re.search(r"Current Wi-Fi Network:\s*(.+?)\s*$", out, re.M | re.I)
            if m and m.group(1).strip().lower() not in ("you are not associated", "none"):
                return m.group(1).strip()
        else:
            out = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=timeout).stdout.strip()
            if out: return out
    except Exception:
        pass
    # 有线兜底：找已连接的有线接口名（避开表头和分隔符行）
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(
                ["netsh", "interface", "show", "interface"],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in out.splitlines():
                if ("已连接" in line or " Connected " in line) and \
                   ("已断开" not in line and "Disconnected" not in line) and \
                   "---" not in line and "接口名称" not in line and "Interface Name" not in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        return "有线 · " + parts[-1]
    except Exception:
        pass
    return ""


def detect_pc_name():
    """本机电脑名（COMPUTERNAME / HOSTNAME），失败兜底空串。"""
    import os
    return (os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "").strip()


def _make_zip(paths, dest, name, progress_cb=None):
    return _ls().make_zip(paths, dest, name, progress_cb=progress_cb)


def _upload_html():
    return _lr()._UPLOAD_HTML


def _list_html():
    return _ls()._LIST_HTML


def _file_icon(ext):
    return _ls()._get_file_icon(ext)


def _human_size(n):
    return _ls()._human_size(n)


def _preview_exts():
    return _lt()._PREVIEW_EXTS


def _parse_multipart(src, boundary, save_dir, conflict, t0,
                     on_file=None, ip="", classify=False):
    return _lr()._parse_multipart_stream(
        src, boundary, save_dir, conflict, t0,
        on_file=on_file, ip=ip, classify=classify)


def _unique_path(path):
    return _lt()._unique_path(path)


def _rmtree(path):
    """安全删除目录（忽略错误）。"""
    try:
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _lt():
    """延迟导入 lan_transfer（先加载顶层定义）。"""
    import core.lan_transfer
    return core.lan_transfer


def _lr():
    """延迟导入 lan_receiver（依赖 lan_transfer 已加载）。"""
    import core.lan_transfer  # noqa: F401 确保顶层先加载
    import core.lan_receiver
    return core.lan_receiver


def _ls():
    import core.lan_transfer  # noqa: F401
    import core.lan_sender
    return core.lan_sender


class _BytesReader:
    """把整段 bytes 包成类文件对象，供同步的 _parse_multipart_stream 读取。"""

    def __init__(self, data):
        self._data = data
        self._pos = 0

    def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        if n is None or n < 0:
            end = len(self._data)
        else:
            end = min(self._pos + n, len(self._data))
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk


class ChatSession:
    """聊天式互传的共享会话：内存消息列表 + 自增 id + 增量查询。

    消息结构：{id, ts, side, type, text?, name?, size?, from?, local_path?}
    - side: "pc" | "phone"（谁发的）
    - type: "text" | "file"
    - local_path: 服务端落盘绝对路径（仅服务端用，不序列化给客户端）
    - 客户端通过 /chat/dl/<id> 按 id 取回文件，绕开旧的 /share 列表复杂度
    """

    _PUBLIC = ("id", "ts", "side", "type", "text", "name", "size", "from", "device")

    def __init__(self):
        self._lock = threading.Lock()
        self._msgs = []
        self._next = 1
        # 在线设备心跳表：device -> {"nick", "side", "last"}（last=最近活动时间）
        self._devices = {}

    def add(self, side, mtype, **kw):
        with self._lock:
            mid = self._next
            self._next += 1
            msg = {"id": mid, "ts": time.time(), "side": side, "type": mtype}
            msg.update(kw)
            self._msgs.append(msg)
            if len(self._msgs) > MAX_CHAT_MESSAGES:
                del self._msgs[:-MAX_CHAT_MESSAGES]
            return msg

    def since(self, after):
        with self._lock:
            return [self._public(m) for m in self._msgs if m["id"] > after]

    def get(self, mid):
        with self._lock:
            for m in self._msgs:
                if m["id"] == mid:
                    return m
            return None

    def heartbeat(self, device, nick="", side=""):
        """设备心跳：聊天页轮询/发消息时上报，刷新 last_seen。

        device 为空则忽略（旧客户端/无身份请求不污染设备表）。
        """
        if not device:
            return
        now = time.time()
        with self._lock:
            # 长时间离线设备及时剔除，避免伪造 device id 无限占用内存。
            stale = [key for key, value in self._devices.items()
                     if now - value["last"] > 300]
            for key in stale:
                self._devices.pop(key, None)
            if device not in self._devices and len(self._devices) >= 200:
                oldest = min(self._devices,
                             key=lambda key: self._devices[key]["last"])
                self._devices.pop(oldest, None)
            d = self._devices.get(device)
            if d is None:
                d = {"nick": "", "side": side, "last": 0}
                self._devices[device] = d
            if nick:
                d["nick"] = nick[:40]
            if side in ("pc", "phone"):
                d["side"] = side
            d["last"] = now

    def devices(self, alive=12):
        """返回 alive 秒内有活动的在线设备列表（按最近活动倒序）。"""
        now = time.time()
        with self._lock:
            out = [{"device": k, "nick": v["nick"], "side": v["side"]}
                   for k, v in self._devices.items()
                   if now - v["last"] <= alive]
        out.sort(key=lambda x: x["side"] == "pc", reverse=True)
        return out

    @staticmethod
    def _public(m):
        return {k: m[k] for k in ChatSession._PUBLIC if k in m}


class LanService:
    """统一局域网服务：分享 + 接收 + 转换 API。

    用法::
        srv = LanService(port=8765)
        srv.set_callbacks(on_received=..., on_downloaded=..., on_progress=...)
        srv.start()      # 后台 daemon 线程
        srv.stop()
    """

    def __init__(self, port=DEFAULT_PORT, host="0.0.0.0"):
        self.host = host
        self.port = port
        self._server = None
        self._uvicorn = None
        self._thread = None
        # 运行时状态
        self.share_dir = None          # 分享目录（start_share 设置）
        self.recv_dir = None           # 接收目录
        self.recv_conflict = "rename"  # rename/overwrite/skip
        self.recv_classify = False
        # 回调（面板注入，工作线程调用 → 信号回主线程）
        self.on_received = None
        self.on_downloaded = None
        self.on_progress = None
        self.on_all_done = None
        self.on_share_done = None      # 分享目录清空/全部下载完时触发
        # 会话统计
        self.session = {"files": 0, "bytes": 0}
        self._lock = threading.Lock()
        self._downloaded_names = set()
        self._share_total = frozenset()
        # 聊天式互传共享会话（内存存储）
        self.chat = ChatSession()
        self.chat_dir = None
        # 分片上传会话：sid -> {dir, meta, chunks:set, lock, status}
        self._uploads = {}
        self._upload_lock = threading.Lock()
        # Office 预览管理器（懒加载，绑定 share_dir）
        self._office_preview = None
        # 聊天 Office 预览管理器 + 服务级预览缓存目录（stop 清理）
        self._office_chat = None
        self._preview_cache = None
        # 可选访问密码：None=不保护；设置后所有页面/API 需 token 或 cookie
        self.access_token = None
        self._cookie_secret = secrets.token_urlsafe(32)
        self._login_failures = {}
        # 主机信息（启动时探测一次，缓存供聊天页注入）
        self._wifi_ssid = None   # 当前连接的 WiFi 名称（SSID）
        self._pc_name = None     # 本机电脑名
        self._build_app()

    # ── 生命周期 ──────────────────────────────
    def start(self, max_retry=10):
        """启动服务（端口占用自动 +1）。返回 True 表示就绪。"""
        if self._thread and self._thread.is_alive():
            return True
        import uvicorn
        port = self.port
        # 聊天互传目录：服务运行期间始终存在，供双向文件落地
        if self.chat_dir is None:
            self.chat_dir = tempfile.mkdtemp(prefix="fm_chat_")
        for attempt in range(max_retry + 1):
            if not _port_in_use(self.host, port):
                self.port = port
                self._uvicorn = uvicorn.Server(uvicorn.Config(
                    self._app, host=self.host, port=port, log_level="warning"))
                self._thread = threading.Thread(
                    target=self._uvicorn.run, daemon=True, name="lan-service")
                self._thread.start()
                for _ in range(50):
                    if self.is_running():
                        return True
                    time.sleep(0.1)
                self.stop()
                return False
            if attempt >= max_retry:
                self.stop()
                return False
            port += 1
        return False

    def stop(self):
        if self._uvicorn:
            try:
                self._uvicorn.should_exit = True
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        if self._thread and self._thread.is_alive():
            return False
        with self._upload_lock:
            uploads, self._uploads = list(self._uploads.values()), {}
        for upload in uploads:
            _rmtree(upload.get("dir"))
        if self.chat_dir and os.path.isdir(self.chat_dir):
            import shutil
            shutil.rmtree(self.chat_dir, ignore_errors=True)
            self.chat_dir = None
        if self._preview_cache and os.path.isdir(self._preview_cache):
            import shutil
            shutil.rmtree(self._preview_cache, ignore_errors=True)
            self._preview_cache = None
        self._server = None
        self._uvicorn = None
        self._thread = None
        return True

    def is_running(self):
        import socket
        target = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            try:
                s.connect((target, self.port))
                return True
            except OSError:
                return False

    @property
    def url(self):
        return f"http://{_get_lan_ip()}:{self.port}/"

    def set_access_token(self, token):
        """更新访问密码并撤销既有登录 Cookie。"""
        normalized = str(token or "")
        if normalized != (self.access_token or ""):
            self.access_token = normalized or None
            self._cookie_secret = secrets.token_urlsafe(32)
            self._login_failures.clear()

    # ── 分享/接收状态设置 ──────────────────────
    def start_share(self, paths, on_progress=None):
        """设置分享内容（文件/文件夹自动 zip）。返回分享目录。"""
        if not paths:
            return None
        self.clear_share()
        import tempfile
        share_dir = tempfile.mkdtemp(prefix="fm_share_")
        files = []
        try:
            for p in paths:
                if os.path.islink(p):
                    continue
                if os.path.isdir(p):
                    z = _make_zip([p], share_dir,
                                  os.path.basename(p.rstrip("\\/")),
                                  progress_cb=on_progress)
                    files.append(z)
                elif os.path.isfile(p) and not os.path.islink(p):
                    dst = _unique_path(
                        os.path.join(share_dir, os.path.basename(p)))
                    if os.path.abspath(dst).lower() != os.path.abspath(p).lower():
                        import shutil
                        shutil.copy2(p, dst)
                    files.append(dst)
        except Exception:
            shutil.rmtree(share_dir, ignore_errors=True)
            raise
        if not files:
            import shutil
            shutil.rmtree(share_dir, ignore_errors=True)
            return None
        self.share_dir = share_dir
        with self._lock:
            self._downloaded_names = set()
            self._share_total = frozenset(
                n for n in os.listdir(share_dir)
                if os.path.isfile(os.path.join(share_dir, n))
                and not n.startswith("."))
        return share_dir

    def clear_share(self):
        """停止分享并清理临时目录。"""
        if self.share_dir:
            import shutil
            shutil.rmtree(self.share_dir, ignore_errors=True)
            self.share_dir = None

    def set_recv(self, save_dir, conflict="rename", classify=False):
        if conflict not in ("rename", "overwrite", "skip"):
            raise ValueError("不支持的文件冲突策略")
        if not save_dir:
            raise ValueError("接收目录不能为空")
        self.recv_dir = os.path.abspath(save_dir)
        self.recv_conflict = conflict
        self.recv_classify = classify
        os.makedirs(self.recv_dir, exist_ok=True)

    def _note_download(self, name):
        """记录一次下载；全部下载完触发 on_all_done（仅一次）。"""
        if not self._share_total or not name:
            return
        with self._lock:
            self._downloaded_names.add(name)
            done = self._share_total <= self._downloaded_names
        if done and self.on_all_done:
            cb, self.on_all_done = self.on_all_done, None
            try:
                cb()
            except Exception:
                pass

    def _ingest_chat_upload(self, source, boundary, side, frm, bundle,
                            client_ip, device=""):
        """把一次聊天文件上传（HTTP multipart）解析落盘并记一条聊天消息。

        pc 端 → chat_dir；手机端 → recv_dir（并触发 on_received 给电脑弹通知）。
        多文件/文件夹自动打成 zip 一条（相对路径作归档名，保留结构）。
        """
        target = self.chat_dir if side == "pc" else self.recv_dir
        if not target:
            return []
        conflict = "rename" if side == "pc" else self.recv_conflict
        classify = False if side == "pc" else self.recv_classify
        saved = []

        def on_file(rel, size, sec, ip, renamed_from):
            saved.append((os.path.join(target, rel), rel, size))

        t0 = time.time()
        reader = _BytesReader(source) if isinstance(source, bytes) else source
        _parse_multipart(
            reader, boundary, target, conflict, t0,
            on_file=on_file, ip=client_ip, classify=classify)

        if not saved:
            return []
        return self._chat_finalize(target, saved, side, frm, t0, bundle,
                                   client_ip, device)

    def chat_send_local(self, paths, side="pc", frm="", bundle=False):
        """PC 端把本机文件/文件夹直接投入聊天会话（不经 HTTP 上传）。

        - 文件复制进 chat_dir（pc）/ recv_dir（手机），聊天下载校验才能通过；
        - 多文件/文件夹自动打成 zip 一条（bundle 或 len>1 时）；
        - **绝不删除用户原文件**（复制而非移动）；单文件即原样副本一条。
        返回新增消息 id 列表。
        """
        target = self.chat_dir if side == "pc" else self.recv_dir
        if not target or not paths:
            return []
        import shutil
        saved = []
        t0 = time.time()
        for p in paths:
            if os.path.islink(p):
                continue
            if os.path.isdir(p):
                for fp, rel in _lt().iter_safe_files(p):
                    self._chat_copy_one(
                        fp, target, rel.replace(os.sep, "/"), saved)
            elif os.path.isfile(p) and not os.path.islink(p):
                self._chat_copy_one(p, target, os.path.basename(p), saved)
        if not saved:
            return []
        if not bundle and len(saved) > 1:
            bundle = True
        return self._chat_finalize(target, saved, side, frm, t0, bundle)

    def _chat_copy_one(self, src, target, rel, saved):
        """把单个本机文件复制到 target（保留 rel 子目录），记录 (副本路径, rel, 大小)。"""
        dst = _unique_path(os.path.join(target, rel))
        d = os.path.dirname(dst)
        if d and not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:  # noqa: BLE001
                return
        try:
            import shutil
            shutil.copy2(src, dst)
        except OSError:  # noqa: BLE001
            return
        try:
            saved.append((dst, rel, os.path.getsize(dst)))
        except OSError:  # noqa: BLE001
            pass

    def _chat_finalize(self, target, saved, side, frm, t0, bundle,
                       client_ip="", device=""):
        """把已落盘到 target 的 saved 列表合成聊天消息（单文件直出 / 多文件打包）。

        saved: [(副本绝对路径, rel, size), ...]。打包时删除副本（副本安全），
        原文件不受影响（调用方对原文件只复制不删）。
        """
        if not saved:
            return []
        if len(saved) == 1 and not bundle:
            local = saved[0][0]
            name = os.path.basename(saved[0][1])
            size = saved[0][2]
        else:
            folder_base = None
            for _, rel, _ in saved:
                d = os.path.dirname(rel)
                if d:
                    folder_base = d.split("/")[0]
                    break
            base = folder_base or "批量文件"
            zip_path = _unique_path(os.path.join(target, base + ".zip"))
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for abs_path, rel, _ in saved:
                    zf.write(abs_path, rel)
            name = os.path.basename(zip_path)
            local = zip_path
            size = os.path.getsize(zip_path)
            for abs_path, _, _ in saved:
                try:
                    os.remove(abs_path)
                except OSError:  # noqa: BLE001
                    pass

        msg = self.chat.add(side, "file", name=name, size=size,
                            **({"from": frm} if frm else {}),
                            **({"device": device} if device else {}),
                            local_path=local)
        if side != "pc" and self.on_received:
            try:
                self.on_received(name, size, time.time() - t0, client_ip, None)
            except Exception:  # noqa: BLE001
                pass
        return [msg]

    # ── App 构建 ──────────────────────────────
    def _build_app(self):
        from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
        from fastapi.responses import (HTMLResponse, FileResponse,
                                       JSONResponse, RedirectResponse)
        from starlette.background import BackgroundTask

        app = FastAPI(title="FormatMaster 局域网服务", version="2.0.0")
        self._app = app

        # ── 可选访问密码守卫（access_token 非空时启用）─────────
        @app.middleware("http")
        async def access_guard(request, call_next):
            if not self.access_token or request.url.path == "/chat/login":
                return await call_next(request)
            supplied = request.query_params.get("token", "")
            cookie = request.cookies.get("fm_session", "")
            # compare_digest 的 str 输入仅支持 ASCII；密码允许中文，统一按 UTF-8 比较。
            ok = (secrets.compare_digest(supplied.encode("utf-8"), self.access_token.encode("utf-8"))
                  or secrets.compare_digest(cookie.encode("utf-8"), self._cookie_secret.encode("utf-8")))
            if ok:
                return await call_next(request)
            # 未授权：页面 → 登录页；API → 401 JSON
            if request.method == "GET" and request.url.path == "/chat":
                return HTMLResponse(_LOGIN_HTML, status_code=200)
            return JSONResponse({"ok": False, "err": "auth"},
                                status_code=401)

        @app.post("/chat/login")
        async def chat_login(request: Request):
            """登录页表单提交：token 正确 → 种 cookie 后跳回 /chat。

            手动解析 urlencoded（环境无 python-multipart，request.form() 会抛异常）。
            """
            token = ""
            try:
                stream, _size = await _spool_request(request, 4096)
                try:
                    raw = stream.read().decode("utf-8", errors="ignore")
                finally:
                    stream.close()
                token = (urllib.parse.parse_qs(raw).get("token")
                         or [""])[0].strip()
            except Exception:  # noqa: BLE001
                pass
            client_ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            self._login_failures = {
                ip: [stamp for stamp in stamps if now - stamp < 60]
                for ip, stamps in self._login_failures.items()
                if any(now - stamp < 60 for stamp in stamps)
            }
            attempts = [stamp for stamp in self._login_failures.get(client_ip, [])
                        if now - stamp < 60]
            if len(attempts) >= 10:
                return HTMLResponse(_LOGIN_HTML.replace(
                    '<div class="err" id="err"></div>',
                    '<div class="err" id="err">尝试次数过多，请稍后再试</div>'),
                    status_code=429)
            if (self.access_token and
                    secrets.compare_digest(token.encode("utf-8"), self.access_token.encode("utf-8"))):
                self._login_failures.pop(client_ip, None)
                # Cookie 只保存随机会话凭据，不暴露用户输入的访问密码。
                resp = RedirectResponse("/chat", status_code=302)
                resp.set_cookie("fm_session", self._cookie_secret,
                                httponly=True, samesite="strict")
                return resp
            attempts.append(now)
            self._login_failures[client_ip] = attempts
            return HTMLResponse(_LOGIN_HTML.replace(
                '<div class="err" id="err" role="status" aria-live="polite"></div>',
                '<div class="err" id="err" role="status" aria-live="polite">密码错误，请重试</div>'),
                status_code=401)

        # ── 首页导航 ─────────────────────────────
        @app.get("/", response_class=HTMLResponse)
        def index():
            # 根路径渲染导航页（聚合聊天互传 / 文件分享），二维码与面板默认
            # 指向 /chat。保留旧的「文件分享」入口供手机浏览下载。
            return _home_html(has_share=bool(self.share_dir))

        # ── 文件分享 ─────────────────────────────
        @app.get("/share/", response_class=HTMLResponse)
        def share_list():
            if not self.share_dir:
                # 与 /recv/ 未开启一致：返回带 charset 的可读中文提示页，
                # 而非 404 JSON（手机按 GBK 解码会乱码）
                return """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>格式大师 · 暂无分享</title></head><body style="font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;background:#f4f6fb;color:#1e293b;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0"><div style="text-align:center;max-width:420px;padding:24px"><div style="font-size:46px">📤</div><h1 style="font-size:20px;margin:12px 0 6px">暂无分享内容</h1><p style="color:#64748b;font-size:14px;line-height:1.6">请在电脑端「文件传输」中添加要分享的文件后重新启动服务，<br>即可在手机上浏览下载。</p></div></body></html>"""
            entries = []
            try:
                entries = [e for e in os.scandir(self.share_dir)
                           if not e.name.startswith(".")]
            except OSError:
                pass
            entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()))
            rows = []
            for e in entries:
                ext = os.path.splitext(e.name)[1].lower().lstrip(".")
                icon = "📁" if e.is_dir() else _file_icon(ext)
                size_txt = "—"
                if not e.is_dir() and os.path.isfile(e.path):
                    try:
                        size_txt = _human_size(os.path.getsize(e.path))
                    except OSError:
                        size_txt = "?"
                href = urllib.parse.quote(e.name)
                link = f"/share/{href}/" if e.is_dir() else f"/share/{href}"
                is_preview = (not e.is_dir()) and ext in _preview_exts()
                escaped_name = html.escape(e.name, quote=True)
                pv = (f"<button type='button' class='pv' "
                      f"aria-label='预览 {escaped_name}' "
                      f"onclick=\"preview(this.closest('.row'))\">👁</button>"
                      if is_preview else "")
                rows.append(
                    f'<div class="row" data-fn="{escaped_name}" '
                    f'data-dir="{1 if e.is_dir() else 0}">'
                    f'<input class="cb" type="checkbox" '
                    f'aria-label="选择 {escaped_name}" onchange="onSel(this)">'
                    f'<span class="ic" aria-hidden="true">{icon}</span>'
                    f'<a class="nm" href="{link}" '
                    f'onclick="return onNm(this)">{escaped_name}</a>{pv}'
                    f'<span class="sz">{size_txt}</span></div>')
            rows_html = "\n".join(rows) if rows else '<div class="empty">📭 目录为空</div>'
            return _list_html().replace("__ROWS__", rows_html)

        @app.get("/share/{path:path}")
        def share_file(path: str, request: Request):
            if not self.share_dir:
                raise HTTPException(404)
            # 列表页用 urllib.parse.quote 编码文件名（中文→%E6%95%99...），
            # 但 FastAPI/Starlette 的 path 参数不做 percent-decode —— 必须手动
            # unquote，否则中文文件名下载全部 404（历史 bug）。
            path = urllib.parse.unquote(path)
            base = os.path.realpath(self.share_dir)
            full = os.path.realpath(os.path.join(base, path))
            if os.path.commonpath([base, full]) != base:
                raise HTTPException(403, "非法路径")
            if os.path.isdir(full):
                # 子目录：继续渲染列表页（简化：直接 404，分享层通常扁平）
                raise HTTPException(404)
            if not os.path.isfile(full):
                raise HTTPException(404)
            # 记录下载
            name = os.path.basename(full)
            if self.on_downloaded and path != "/":
                t0 = time.time()
                try:
                    size = os.path.getsize(full)
                    self.on_downloaded(name, size, time.time() - t0,
                                       request.client.host if request.client else "")
                    self._note_download(name)
                except Exception:
                    pass
            return FileResponse(full, filename=name)

        def _archive_response(entries, download_name, *, all_done=False):
            """在磁盘构建受限 ZIP，并在响应发送完成后清理与通知。"""
            fd, archive = tempfile.mkstemp(prefix="fm_share_", suffix=".zip")
            os.close(fd)
            count = 0
            total = 0
            try:
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                    for path, arcname in entries:
                        count += 1
                        total += os.path.getsize(path)
                        if (count > _lt().MAX_SHARE_ARCHIVE_FILES
                                or total > _lt().MAX_SHARE_ARCHIVE_BYTES):
                            raise ValueError("共享压缩包超过安全限制")
                        zf.write(path, arcname)
            except Exception:
                try:
                    os.remove(archive)
                except OSError:
                    pass
                raise

            archive_size = os.path.getsize(archive)

            def _finish():
                try:
                    os.remove(archive)
                except OSError:
                    pass
                if self.on_downloaded:
                    try:
                        self.on_downloaded(download_name, archive_size, 0, "")
                    except Exception:
                        pass
                if all_done:
                    with self._lock:
                        cb, self.on_all_done = self.on_all_done, None
                    if cb:
                        try:
                            cb()
                        except Exception:
                            pass

            return FileResponse(
                archive, media_type="application/zip", filename=download_name,
                background=BackgroundTask(_finish))

        @app.get("/all.zip")
        @app.get("/share-all.zip")
        def share_all_zip():
            """打包下载全部。"""
            if not self.share_dir:
                raise HTTPException(404)
            try:
                return _archive_response(
                    _lt().iter_safe_files(self.share_dir),
                    "all.zip", all_done=True)
            except ValueError as error:
                raise HTTPException(413, str(error))
            except OSError:
                raise HTTPException(500)

        @app.get("/selected.zip")
        @app.get("/share-selected.zip")
        def share_selected_zip(request: Request):
            """打包选中文件；只接受分享目录内的非链接普通文件。"""
            if not self.share_dir:
                raise HTTPException(404)
            base = os.path.realpath(self.share_dir)
            entries = []
            seen = set()
            for name in request.query_params.getlist("files"):
                relative = urllib.parse.unquote(name)
                candidate = os.path.join(base, relative)
                if os.path.islink(candidate):
                    continue
                path = os.path.realpath(candidate)
                try:
                    if (os.path.commonpath([base, path]) != base
                            or not os.path.isfile(path) or path in seen):
                        continue
                except ValueError:
                    continue
                seen.add(path)
                entries.append((path, os.path.basename(path)))
            if not entries:
                raise HTTPException(400, "未选择有效文件")
            try:
                return _archive_response(entries, "selected.zip")
            except ValueError as error:
                raise HTTPException(413, str(error))
            except OSError:
                raise HTTPException(500)

        # ── 文件接收 ─────────────────────────────
        @app.get("/recv/", response_class=HTMLResponse)
        def recv_page():
            if not self.recv_dir:
                # 接收未开启：返回带 charset 的中文提示页，避免手机端
                # 把 JSON 错误当成 GBK 解码出现乱码（原 HTTPException 404）
                return """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>格式大师 · 接收未开启</title></head><body style="font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;background:#f4f6fb;color:#1e293b;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0"><div style="text-align:center;max-width:420px;padding:24px"><div style="font-size:46px">📥</div><h1 style="font-size:20px;margin:12px 0 6px">接收功能未开启</h1><p style="color:#64748b;font-size:14px;line-height:1.6">请在电脑端「格式大师」的「局域网服务 → 接收文件」中<br>选择接收目录并启动服务后，再扫码访问。</p></div></body></html>"""
            page = _upload_html()
            page = page.replace("__SAVE_DIR__", html.escape(self.recv_dir) or "-")
            page = page.replace("__SESSION_ID__", str(self.port))
            # 统一服务挂在 /recv/，上传端点为 /recv/upload（占位符覆盖默认 /）
            page = page.replace("__UPLOAD_URL__", "/recv/upload")
            return page

        @app.post("/recv/upload")
        async def recv_upload(request: Request):
            """流式接收 multipart 上传（复用 lan_receiver 的解析器）。"""
            if not self.recv_dir:
                raise HTTPException(404, "接收未开启")
            ctype = request.headers.get("content-type", "")
            boundary = _multipart_boundary(ctype)
            if not boundary:
                raise HTTPException(400, "非 multipart 请求")
            saved = []
            client_ip = request.client.host if request.client else ""

            def on_file(name, size, sec, ip, renamed_from=None):
                saved.append(name)
                if self.on_received:
                    try:
                        self.on_received(name, size, sec, ip, renamed_from)
                    except Exception:
                        pass
                with self._lock:
                    self.session["files"] += 1
                    self.session["bytes"] += size

            t0 = time.time()
            try:
                stream, total = await _spool_request(request)
            except ValueError:
                raise HTTPException(413, "上传文件过大")
            try:
                _parse_multipart(
                    stream, boundary, self.recv_dir,
                    self.recv_conflict, t0,
                    on_file=on_file, ip=client_ip,
                    classify=self.recv_classify)
                if self.on_progress:
                    self.on_progress(total, total)
            finally:
                stream.close()
            return HTMLResponse(
                f"✅ 上传成功：{len(saved)} 个文件<br>" + "<br>".join(
                    html.escape(s) for s in saved))

        # ── 聊天式互传 ─────────────────────────────
        @app.get("/chat", response_class=HTMLResponse)
        def chat_page():
            from core.lan_chat_page import chat_html
            # 首次访问时探测并缓存主机信息（活跃网络 + 电脑名）供聊天页注入
            if self._wifi_ssid is None:
                self._wifi_ssid = detect_wifi_ssid() or ""
            if self._pc_name is None:
                self._pc_name = detect_pc_name() or ""
            return HTMLResponse(chat_html(
                wifi_ssid=self._wifi_ssid, pc_name=self._pc_name))

        @app.get("/chat/history")
        def chat_history(after: int = 0, device: str = "",
                         frm: str = "", side: str = ""):
            # 设备心跳：轮询即在线（12s 内无请求算离线）
            if device:
                self.chat.heartbeat(device, frm, side)
            return JSONResponse(self.chat.since(after))

        @app.get("/chat/devices")
        def chat_devices():
            """在线设备列表：谁扫码/输 IP 进来、还活着，一目了然。"""
            return JSONResponse(self.chat.devices())

        @app.post("/chat/message")
        async def chat_message(request: Request):
            try:
                data = await _json_request(request)
            except Exception:  # noqa: BLE001
                data = {}
            text = (data.get("text") or "").strip()
            side = data.get("side") if data.get("side") in ("pc", "phone") else "phone"
            frm = (data.get("from") or "").strip()[:40]
            device = (data.get("device") or "").strip()[:40]
            mtype = data.get("type") if data.get("type") in ("text", "clip") else "text"
            if not text:
                return JSONResponse({"ok": False, "err": "empty"},
                                    status_code=400)
            if len(text.encode("utf-8")) > MAX_TEXT_MESSAGE:
                return JSONResponse({"ok": False, "err": "too_large"},
                                    status_code=413)
            msg = self.chat.add(side, mtype, text=text,
                                **({"from": frm} if frm else {}),
                                **({"device": device} if device else {}))
            if device:
                self.chat.heartbeat(device, frm, side)  # 发消息也算在线
            return JSONResponse({"ok": True, "id": msg["id"]})

        @app.post("/chat/file")
        async def chat_file(request: Request):
            side = request.query_params.get("side")
            side = side if side in ("pc", "phone") else "phone"
            frm = (request.query_params.get("from") or "").strip()[:40]
            device = (request.query_params.get("device") or "").strip()[:40]
            bundle = request.query_params.get("bundle") == "1"
            ctype = request.headers.get("content-type", "")
            boundary = _multipart_boundary(ctype)
            if not boundary:
                return JSONResponse({"ok": False, "err": "no boundary"},
                                    status_code=400)
            try:
                stream, _total = await _spool_request(request)
            except ValueError:
                return JSONResponse({"ok": False, "err": "too_large"},
                                    status_code=413)
            client_ip = request.client.host if request.client else ""
            try:
                msgs = self._ingest_chat_upload(
                    stream, boundary, side, frm, bundle, client_ip, device)
            finally:
                stream.close()
            return JSONResponse({"ok": True, "count": len(msgs),
                                "ids": [m["id"] for m in msgs]})

        @app.get("/chat/dl/{mid:int}")
        def chat_dl(mid: int):
            msg = self.chat.get(mid)
            if not msg or not msg.get("local_path"):
                raise HTTPException(404)
            lp = msg["local_path"]
            rp = os.path.realpath(lp)
            ok = False
            for root in (self.chat_dir, self.recv_dir):
                if root and os.path.commonpath(
                        [os.path.realpath(root), rp]) == os.path.realpath(root):
                    ok = True
                    break
            if not ok or not os.path.isfile(lp):
                raise HTTPException(403)
            return FileResponse(lp, filename=msg.get("name")
                                or os.path.basename(lp))

        @app.get("/chat/preview/{mid:int}")
        def chat_preview(mid: int):
            """聊天文件离线预览（文件不出本机）。

            - Office（doc/xls/ppt…）→ 服务端转 PDF 内嵌（复用 office_preview）；
            - PDF → 原样内联（浏览器内置查看器）；
            - 文本类（txt/md/code/log…）→ 读取并以转义文本页展示；
            - 其余格式 → 404（前端不显示预览按钮，预览不了的就算了）。
            """
            msg = self.chat.get(mid)
            if not msg or msg.get("type") != "file" or not msg.get("local_path"):
                raise HTTPException(404)
            lp = msg["local_path"]
            rp = os.path.realpath(lp)
            ok = False
            for root in (self.chat_dir, self.recv_dir):
                if root and os.path.commonpath(
                        [os.path.realpath(root), rp]) == os.path.realpath(root):
                    ok = True
                    break
            if not ok or not os.path.isfile(lp):
                raise HTTPException(403)
            ext = os.path.splitext(lp)[1].lower()
            if ext in _OFFICE_EXTS:
                if self._office_chat is None:
                    if self._preview_cache is None:
                        self._preview_cache = tempfile.mkdtemp(prefix="fm_preview_")
                    from core import office_preview as _op
                    self._office_chat = _op.OfficePreviewManager(self._preview_cache)
                status, pdf, _eng = self._office_chat.request(lp)
                if status == "ready" and pdf and os.path.isfile(pdf):
                    return FileResponse(pdf, media_type="application/pdf",
                                        filename=os.path.basename(pdf),
                                        content_disposition_type="inline")
                if status == "error":
                    return HTMLResponse(_OFFICE_ERR_HTML)
                return HTMLResponse(_OFFICE_PENDING_HTML)
            if ext == ".pdf":
                return FileResponse(lp, media_type="application/pdf",
                                    filename=msg.get("name") or os.path.basename(lp),
                                    content_disposition_type="inline")
            if ext in _TEXT_EXTS:
                if os.path.getsize(lp) > _TEXT_PREVIEW_MAX:
                    return HTMLResponse(_text_preview_html(
                        f"文件过大（{_human_size(os.path.getsize(lp))}），"
                        "仅支持预览 2MB 以内的文本。"))
                return HTMLResponse(_text_preview_html(_read_text(lp)))
            raise HTTPException(404)


        # ── Office 文档离线预览 ───────────────────
        @app.get("/office-pdf")
        def office_pdf(file: str = ""):
            """把分享目录里的 Office 文档转 PDF 后内嵌预览（离线、不出本机）。

            - ready：返回 PDF（inline），灯箱 iframe 直接渲染；
            - pending：返回带自动刷新的「转换中」页（iframe 轮询直至 ready）；
            - error：返回无法预览提示（本机无 Office/LibreOffice）。
            """
            if not self.share_dir:
                raise HTTPException(404)
            if not file:
                raise HTTPException(400)
            file = urllib.parse.unquote(file)
            base = os.path.realpath(self.share_dir)
            full = os.path.realpath(os.path.join(base, file))
            if os.path.commonpath([base, full]) != base:
                raise HTTPException(403)
            if not os.path.isfile(full):
                raise HTTPException(404)
            if self._office_preview is None:
                from core import office_preview as _op
                self._office_preview = _op.OfficePreviewManager(base)
            status, pdf, _engine = self._office_preview.request(full)
            if status == "ready" and pdf and os.path.isfile(pdf):
                return FileResponse(
                    pdf, media_type="application/pdf",
                    filename=os.path.basename(pdf),
                    content_disposition_type="inline")
            if status == "error":
                return HTMLResponse(_OFFICE_ERR_HTML)
            return HTMLResponse(_OFFICE_PENDING_HTML)

        # ── 分片上传（大文件 / 断点续传 / 哈希校验）─────────
        @app.post("/chat/upload/init")
        async def upload_init(request: Request):
            """初始化分片会话：返回 sid 与已收分片（用于断点续传）。"""
            try:
                data = await _json_request(request)
            except Exception:  # noqa: BLE001
                data = {}
            raw_name = (str(data.get("name") or "file")).strip() or "file"
            safe_rel = _lr()._safe_rel(raw_name)
            name = os.path.basename(safe_rel) if safe_rel else ""
            try:
                size = int(data.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            try:
                total = int(data.get("total_chunks") or 0)
            except (TypeError, ValueError):
                total = 0
            sha = (str(data.get("sha256") or "")).strip()
            side = str(data.get("side") or "phone")
            side = side if side in ("pc", "phone") else "phone"
            frm = (str(data.get("from") or "")).strip()[:40]
            device = (str(data.get("device") or "")).strip()[:40]
            bundle = bool(data.get("bundle"))
            if (not name or total <= 0 or total > MAX_UPLOAD_BYTES or
                    size < 0 or size > MAX_UPLOAD_BYTES or
                    total > (MAX_UPLOAD_BYTES // (2 * 1024 * 1024) + 1) or
                    (sha and (len(sha) > 128 or
                              any(char not in "0123456789abcdefABCDEF" for char in sha)))):
                return JSONResponse({"ok": False, "err": "bad"},
                                    status_code=400)
            stale_uploads = []
            with self._upload_lock:
                now = time.time()
                for old_sid, upload in list(self._uploads.items()):
                    if now - upload.get("created", now) > 3600:
                        stale_uploads.append(self._uploads.pop(old_sid))
                if len(self._uploads) >= MAX_UPLOAD_SESSIONS:
                    return JSONResponse({"ok": False, "err": "busy"},
                                        status_code=429)
            for upload in stale_uploads:
                _rmtree(upload.get("dir"))
            sid = secrets.token_hex(8)
            root = self.chat_dir or tempfile.mkdtemp(prefix="fm_up_")
            udir = os.path.join(root, ".uploads", sid)
            os.makedirs(os.path.join(udir, "chunks"), exist_ok=True)
            meta = {"name": name, "size": size, "total": total,
                    "sha256": sha, "side": side, "from": frm,
                    "bundle": bundle, "device": device}
            try:
                with open(os.path.join(udir, "meta.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(meta, f)
            except OSError:  # noqa: BLE001
                return JSONResponse({"ok": False, "err": "io"},
                                    status_code=500)
            with self._upload_lock:
                self._uploads[sid] = {
                    "dir": udir, "meta": meta, "chunks": set(),
                    "chunk_sizes": {}, "lock": threading.Lock(),
                    "status": "uploading", "created": time.time()}
            return JSONResponse({"ok": True, "sid": sid, "received": []})

        @app.post("/chat/upload/chunk")
        async def upload_chunk(request: Request, sid: str = "",
                               index: int = -1):
            """上传单个分片（raw body）；index 重发即覆盖（支持断点续传）。"""
            with self._upload_lock:
                up = self._uploads.get(sid)
            if up is None:
                return JSONResponse({"ok": False, "err": "nosid"},
                                    status_code=404)
            if index < 0 or index >= up["meta"]["total"]:
                return JSONResponse({"ok": False, "err": "idx"},
                                    status_code=400)
            try:
                stream, chunk_size = await _spool_request(
                    request, MAX_CHUNK_BYTES)
            except ValueError:
                return JSONResponse({"ok": False, "err": "too_large"},
                                    status_code=413)
            if not chunk_size:
                stream.close()
                return JSONResponse({"ok": False, "err": "empty"},
                                    status_code=400)
            with up["lock"]:
                old_size = up["chunk_sizes"].get(index, 0)
                projected = sum(up["chunk_sizes"].values()) - old_size + chunk_size
                declared_size = up["meta"].get("size") or 0
                if (projected > MAX_UPLOAD_BYTES or
                        (declared_size and projected > declared_size)):
                    stream.close()
                    return JSONResponse({"ok": False, "err": "too_large"},
                                        status_code=413)
                # 先预留字节配额，避免并发分片同时绕过总量检查。
                up["chunk_sizes"][index] = chunk_size
            cpath = os.path.join(up["dir"], "chunks", str(index))
            staged = cpath + ".part"
            try:
                with open(staged, "wb") as output:
                    shutil.copyfileobj(stream, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(staged, cpath)
            except OSError:  # noqa: BLE001
                with up["lock"]:
                    if old_size:
                        up["chunk_sizes"][index] = old_size
                    else:
                        up["chunk_sizes"].pop(index, None)
                try:
                    os.remove(staged)
                except OSError:
                    pass
                return JSONResponse({"ok": False, "err": "io"},
                                    status_code=500)
            finally:
                stream.close()
            with up["lock"]:
                up["chunks"].add(index)
            return JSONResponse({"ok": True, "index": index})

        @app.get("/chat/upload/status")
        def upload_status(sid: str = ""):
            """查询已收分片列表（断点续传：客户端只补传缺失分片）。"""
            with self._upload_lock:
                up = self._uploads.get(sid)
            if up is None:
                return JSONResponse({"ok": False, "err": "nosid"},
                                    status_code=404)
            with up["lock"]:
                received = sorted(up["chunks"])
                done = len(received) >= up["meta"]["total"]
            return JSONResponse({"ok": True, "sid": sid,
                                 "received": received,
                                 "total": up["meta"]["total"], "done": done})

        @app.post("/chat/upload/commit")
        async def upload_commit(request: Request, sid: str = ""):
            """合并分片 → SHA256 校验 → 落入聊天会话。缺失分片/校验失败均报错。"""
            with self._upload_lock:
                up = self._uploads.pop(sid, None)
            if up is None:
                return JSONResponse({"ok": False, "err": "nosid"},
                                    status_code=404)
            meta = up["meta"]
            total = meta["total"]
            with up["lock"]:
                missing = [i for i in range(total)
                           if i not in up["chunks"]]
            if missing:
                # 放回会话，允许客户端补传后重试
                with self._upload_lock:
                    self._uploads[sid] = up
                return JSONResponse({"ok": False, "err": "missing",
                                    "missing": missing}, status_code=400)
            target = (self.chat_dir if meta["side"] == "pc"
                      else self.recv_dir)
            if not target:
                _rmtree(up["dir"])
                return JSONResponse({"ok": False, "err": "notarget"},
                                    status_code=400)
            cdir = os.path.join(up["dir"], "chunks")
            t0 = time.time()
            h = hashlib.sha256()
            fd, staged = tempfile.mkstemp(prefix=".fm_lan_", dir=target)
            os.close(fd)
            try:
                with open(staged, "wb") as out:
                    for i in range(total):
                        cp = os.path.join(cdir, str(i))
                        with open(cp, "rb") as f:
                            while True:
                                b = f.read(1024 * 1024)
                                if not b:
                                    break
                                out.write(b)
                                h.update(b)
                    out.flush()
                    os.fsync(out.fileno())
            except OSError:  # noqa: BLE001
                try:
                    os.remove(staged)
                except OSError:
                    pass
                _rmtree(up["dir"])
                return JSONResponse({"ok": False, "err": "io"},
                                    status_code=500)
            digest = h.hexdigest()
            if meta.get("sha256") and digest.lower() != meta["sha256"].lower():
                try:
                    os.remove(staged)
                except OSError:  # noqa: BLE001
                    pass
                _rmtree(up["dir"])
                return JSONResponse({"ok": False, "err": "sha",
                                    "got": digest}, status_code=400)
            size = os.path.getsize(staged)
            # 兜底完整性校验：声明大小 != 合并后大小 → 截断/丢分片，拒绝入库
            if meta.get("size") and size != meta.get("size"):
                try:
                    os.remove(staged)
                except OSError:  # noqa: BLE001
                    pass
                _rmtree(up["dir"])
                return JSONResponse({"ok": False, "err": "size",
                                    "got": size, "want": meta.get("size")},
                                    status_code=400)
            safe_target = _lr()._safe_target(target, meta["name"])
            if not safe_target:
                os.remove(staged)
                _rmtree(up["dir"])
                return JSONResponse({"ok": False, "err": "name"},
                                    status_code=400)
            unique = _unique_path(safe_target)
            try:
                os.replace(staged, unique)
            except OSError:
                try:
                    os.remove(staged)
                except OSError:
                    pass
                _rmtree(up["dir"])
                return JSONResponse({"ok": False, "err": "io"},
                                    status_code=500)
            msgs = self._chat_finalize(target,
                                      [(unique, meta["name"], size)],
                                      meta["side"], meta["from"], t0,
                                      meta["bundle"], device=meta.get("device", ""))
            _rmtree(up["dir"])
            return JSONResponse({"ok": True, "count": len(msgs),
                                "ids": [m["id"] for m in msgs]})

        @app.post("/chat/upload/cancel")
        def upload_cancel(sid: str = ""):
            """取消并清理分片会话。"""
            with self._upload_lock:
                up = self._uploads.pop(sid, None)
            if up is not None:
                _rmtree(up["dir"])
            return JSONResponse({"ok": True})

        # 首页导航卡片见模块底部 _nav_card（模块级函数）


_OFFICE_EXTS = {".doc", ".docx", ".wps", ".ppt", ".pptx", ".dps",
                ".xls", ".xlsx", ".xlsm"}

# 文本类可预览扩展名（浏览器/服务端直接读取展示）
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".log", ".json", ".py", ".js", ".ts", ".jsx",
    ".tsx", ".css", ".scss", ".less", ".html", ".htm", ".xml", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv", ".sql", ".sh", ".bat",
    ".ps1", ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".php",
    ".lua", ".pl", ".r", ".kt", ".swift", ".m", ".mm", ".dart", ".vue",
    ".srt", ".vtt", ".ass", ".lrc", ".nfo", ".env", ".gitignore", ".properties",
    ".m3u", ".m3u8", ".lock", ".txt",
}
# 文本预览大小上限（2MB，防大文件撑爆内存/页面）
_TEXT_PREVIEW_MAX = 2 * 1024 * 1024


def _read_text(path):
    """读取文本文件内容：UTF-8 → GBK → latin-1 逐级降级，绝不抛异常。"""
    data = open(path, "rb").read()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return data.decode("utf-8", errors="replace")


def _text_preview_html(content):
    """文本/提示内容 → 转义后的预览页（文件不出本机，纯服务端渲染）。"""
    return """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>文本预览</title><style>body{margin:0;background:#0b1220;color:#cbd5e1;
font-family:ui-monospace,Consolas,'Courier New',monospace;font-size:13px;line-height:1.6;
padding:18px 20px;white-space:pre-wrap;word-break:break-word}</style></head>
<body>__BODY__</body></html>""".replace("__BODY__", html.escape(content))

_OFFICE_PENDING_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="1.5">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>转换中</title><style>body{margin:0;height:100vh;display:flex;flex-direction:column;
gap:14px;align-items:center;justify-content:center;background:#0b1220;color:#cbd5e1;
font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif}
.sp{width:36px;height:36px;border:4px solid #334155;border-top-color:#6366f1;
border-radius:50%;animation:r 1s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}.t{font-size:13px}</style></head>
<body><div class="sp"></div><div class="t">正在转换文档，请稍候…</div></body></html>"""

_OFFICE_ERR_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>无法预览</title><style>body{margin:0;height:100vh;display:flex;flex-direction:column;
gap:12px;align-items:center;justify-content:center;text-align:center;padding:24px;
background:#0b1220;color:#cbd5e1;font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;line-height:1.7}
.e{font-size:40px}.t{font-size:14px;max-width:320px}</style></head>
<body><div class="e">📄</div><div class="t">本机未安装 Office / WPS / LibreOffice，
无法离线预览此文档。<br>可在电脑端打开后另存为 PDF 再分享。</div></body></html>"""


_LOGIN_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>格式大师 · 访问验证</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0b1220;color:#e2e8f0;font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif}
.card{width:100%;max-width:340px;padding:28px 24px;border:1px solid rgba(255,255,255,.09);
border-radius:18px;background:rgba(22,30,44,.6);text-align:center;margin:0 20px}
.logo{font-size:40px}h1{font-size:18px;margin:10px 0 4px}
p{font-size:13px;color:#94a3b8;margin:0 0 18px;line-height:1.6}
.pw{position:relative;margin-bottom:14px}
.pw input{width:100%;padding:14px 54px 14px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.14);
background:rgba(255,255,255,.06);color:#fff;font-size:18px;text-align:center;
letter-spacing:.3em;outline:none;box-sizing:border-box}
.pw input:focus{border-color:#34d399}
.eye{position:absolute;right:8px;top:50%;transform:translateY(-50%);width:42px;height:42px;
border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.06);
color:#cbd5e1;font-size:20px;cursor:pointer;border-radius:10px;
display:flex;align-items:center;justify-content:center;
transition:background-color .16s,color .16s,border-color .16s,transform .16s}
.eye:hover{background:rgba(255,255,255,.12);color:#fff;border-color:rgba(255,255,255,.28)}
.eye:active{transform:translateY(-50%) scale(.94)}
button[type=submit]{width:100%;padding:12px;border:0;border-radius:12px;font-size:15px;font-weight:600;
background:linear-gradient(135deg,#10b981,#06b6d4);color:#fff;cursor:pointer}
.err{color:#f87171;font-size:12px;min-height:18px;margin-top:10px}
button:focus-visible,input:focus-visible{outline:3px solid #34d399;outline-offset:2px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}</style></head>
<body><div class="card"><div class="logo">🔒</div><h1>格式大师 · 互传</h1>
<p>此服务已开启访问保护<br>请输入电脑端显示的访问密码</p>
<form action="/chat/login" method="post" autocomplete="off">
<div class="pw"><input type="password" name="token" id="tok" maxlength="64"
  placeholder="访问密码…" aria-label="访问密码" autocomplete="current-password" required>
<button type="button" class="eye" id="eye" title="显示/隐藏密码" aria-label="显示或隐藏密码">👁</button></div>
<button type="submit">进入</button>
<div class="err" id="err" role="status" aria-live="polite"></div>
</form></div>
<script>
var t=document.getElementById('tok'),e=document.getElementById('eye');
e.onclick=function(){
  if(t.type==='password'){t.type='text';e.textContent='🙈';e.title='隐藏密码';}
  else{t.type='password';e.textContent='👁';e.title='显示密码';}
  t.focus();var p=t.value.length;t.setSelectionRange(p,p);
};
</script></body></html>"""


def _nav_card(icon, title, href, desc):
    return (f'<a class="card" href="{href}">'
            f'<span class="ic">{icon}</span>'
            f'<div><div class="t">{html.escape(title)}</div>'
            f'<div class="d">{html.escape(desc)}</div></div>'
            f'<span style="margin-left:auto;color:#94a3b8">→</span></a>')


def _home_html(has_share=False):
    """局域网首页：聊天始终可用，仅在已有共享内容时显示文件分享。"""
    share_card = ""
    if has_share:
        share_card = """<a class="card" href="/share/"><span class="ic" aria-hidden="true">📤</span>
      <div><div class="t">文件分享</div>
      <div class="d">浏览电脑已分享的文件并下载</div></div>
      <span style="margin-left:auto;color:#94a3b8" aria-hidden="true">→</span></a>"""
    return """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#f4f6fb">
<title>格式大师 · 局域网服务</title>
<style>
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; background:#f4f6fb;
  color:#1e293b; font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;
  display:flex; align-items:center; justify-content:center; padding:24px; }
.wrap { width:100%; max-width:480px; }
.hd { text-align:center; margin-bottom:22px; }
.hd .logo { font-size:40px; }
.hd h1 { font-size:20px; margin:8px 0 4px; }
.hd p { margin:0; color:#64748b; font-size:13px; }
.cards { display:flex; flex-direction:column; gap:12px; }
.card { display:flex; align-items:center; gap:14px; text-decoration:none;
  background:#fff; border:1px solid #e6e9f2; border-radius:14px; padding:16px 18px;
  color:#1e293b; box-shadow:0 1px 3px rgba(15,23,42,.05);
  transition:border-color .15s,transform .15s,box-shadow .15s; }
.card:hover { border-color:#3b82f6; transform:translateY(-1px);
  box-shadow:0 6px 18px rgba(59,130,246,.16); }
.card:focus-visible { outline:3px solid #2563eb; outline-offset:3px; }
@media (prefers-reduced-motion:reduce) {
  *,*::before,*::after { animation:none!important; transition:none!important; }
}
.card .ic { font-size:26px; width:34px; text-align:center; }
.card .t { font-size:15px; font-weight:600; }
.card .d { font-size:12px; color:#64748b; margin-top:2px; }
</style></head>
<body><div class="wrap">
  <div class="hd">
    <div class="logo" aria-hidden="true">📡</div>
    <h1>格式大师 · 局域网服务</h1>
    <p>同一 WiFi 下，手机与电脑互传文件</p>
  </div>
  <div class="cards">
    <a class="card" href="/chat"><span class="ic" aria-hidden="true">💬</span>
      <div><div class="t">聊天互传</div>
      <div class="d">扫码即用：发消息、传图片/文件/文件夹，支持粘贴与拖拽</div></div>
      <span style="margin-left:auto;color:#94a3b8" aria-hidden="true">→</span></a>
    __SHARE_CARD__
  </div>
</div></body></html>""".replace("__SHARE_CARD__", share_card)
