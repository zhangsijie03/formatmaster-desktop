"""视频下载"""
import json
import logging
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)
AUDIO_FORMATS = {"mp3", "m4a", "flac", "wav", "opus"}
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _creation_flags():
    """按当前平台返回无窗口标志，便于跨平台运行与平台模拟测试。"""
    return 0x08000000 if sys.platform == "win32" else 0


SUPPORTED_PLATFORMS = (
    "YouTube · B站(bilibili) · 微博(weibo) · Instagram · Twitter/X · "
    "Facebook · 快手(kuaishou) · 小红书 · 知乎 · 网易云音乐 · 腾讯视频 · 优酷 · 爱奇艺"
)

# known short-domain redirect patterns that need special handling
_SHORT_DOMAINS = {
    "v.douyin.com": "douyin",
    "douyin.com": "douyin",
    "www.douyin.com": "douyin",
    "b23.tv": "bilibili",
}


def _find_ytdlp_exe():
    """查找当前平台 yt-dlp：用户可写 bin → 内置 bin（打包资源）→ PATH。"""
    # 与工具状态卡/更新器共用同一查找链，避免 macOS 误选残留的
    # yt-dlp.exe，或出现“状态显示已安装、下载面板却提示缺失”的分叉。
    from core.tool_updater import _ytdlp_exe_path
    return _ytdlp_exe_path()


def validate_http_url(url):
    """下载器只接受具备主机名的 HTTP(S) URL。"""
    value = str(url or "").strip()
    if len(value) > 4096:
        raise ValueError("链接过长（最多 4096 个字符）")
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        parsed.port
    except (ValueError, UnicodeError):
        raise ValueError("仅支持有效的 HTTP/HTTPS 链接") from None
    return value


def _validated_headers(headers):
    """拒绝非法请求头和换行，避免请求头注入。"""
    result = {}
    for key, value in dict(headers or {}).items():
        name = str(key).strip()
        text = str(value).strip()
        if (not name or not _HEADER_NAME_RE.fullmatch(name)
                or any(char in text for char in "\r\n")):
            raise ValueError(f"无效请求头：{name or '?'}")
        result[name] = text
    return result


def _apply_network_options(opts, cookie=None, proxy=None, headers=None):
    """统一应用解析/下载网络参数；Cookie 既支持文件也支持原始字符串。"""
    merged = _validated_headers(headers)
    cookie_value = str(cookie or "").strip()
    if cookie_value:
        if any(char in cookie_value for char in "\r\n"):
            raise ValueError("Cookie 不能包含换行")
        cookie_path = os.path.abspath(os.path.expanduser(cookie_value))
        if os.path.isfile(cookie_path):
            opts["cookiefile"] = cookie_path
        else:
            merged["Cookie"] = cookie_value
    if merged:
        opts["http_headers"] = merged
    if proxy:
        opts["proxy"] = str(proxy).strip()
    return opts


def _output_template(output_path, custom_template=None):
    """模板只允许文件名，不允许绕过面板所选保存目录。"""
    if not custom_template:
        return os.path.splitext(output_path)[0] + ".%(ext)s"
    template = str(custom_template).strip()
    if (not template or template in {".", ".."}
            or os.path.isabs(template) or "/" in template or "\\" in template
            or re.search(r'[<>:"|?*]', template)
            or any(ord(char) < 32 for char in template)
            or template.endswith((".", " "))):
        raise ValueError("文件名模板不能包含路径、父目录或控制字符")
    return os.path.join(os.path.dirname(os.path.abspath(output_path)), template)


class VideoDownloader:
    def __init__(self):
        self._cancel = False
        self._process = None
        self._last_error = ""

    def cancel(self):
        self._cancel = True
        if self._process:
            try:
                self._process.terminate()
            except OSError:
                logger.exception("终止 yt-dlp 进程失败")

    def _make_ydl_opts(self, **extra):
        opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "source_address": "0.0.0.0",
            "socket_timeout": 10,
            "extractor_args": {"youtube": {"skip": ["dash"]}},
        }
        opts.update(extra)
        return opts

    def _detect_site(self, url):
        for domain, site in _SHORT_DOMAINS.items():
            if domain in url:
                return site
        return None

    def get_formats(self, url, cookie=None, proxy=None, headers=None):
        """获取格式信息，返回 (formats, title, thumbnail, playlist)"""
        url = validate_http_url(url)
        # 优先尝试 Python 模块
        try:
            return self._get_formats_module(url, cookie, proxy, headers)
        except ImportError:
            pass
        # 降级到命令行
        return self._get_formats_cli(url, cookie, proxy, headers)

    def _get_formats_module(self, url, cookie=None, proxy=None, headers=None):
        from yt_dlp import YoutubeDL
        try:
            ydl_opts = _apply_network_options(
                self._make_ydl_opts(), cookie, proxy, headers)
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info is None:
                    raise RuntimeError("无法解析该链接，请检查URL是否正确")
                formats = []
                for f in info.get("formats", []):
                    fmt_id = f.get("format_id", "")
                    ext = f.get("ext", "")
                    resolution = f.get("resolution", "") or f.get("format_note", "")
                    filesize = f.get("filesize", 0) or f.get("filesize_approx", 0)
                    vcodec = f.get("vcodec", "none")
                    acodec = f.get("acodec", "none")
                    if ext in ("mhtml", "json"):
                        continue
                    formats.append({
                        "format_id": fmt_id,
                        "ext": ext,
                        "resolution": resolution,
                        "filesize": filesize,
                        "vcodec": vcodec,
                        "acodec": acodec,
                    })
                if not formats:
                    raise RuntimeError("未找到可下载的格式，该视频可能受DRM保护或需要登录")
                title = info.get("title", "") or info.get("fulltitle", "") or "untitled"
                # 从同一次 extract_info 结果中提取播放列表信息，避免重复请求
                playlist = None
                entries = info.get("entries")
                if entries and len(entries) > 1:
                    items = []
                    for e in entries:
                        if e:
                            items.append({
                                "url": e.get("url") or e.get("webpage_url", ""),
                                "title": e.get("title", ""),
                                "duration": e.get("duration", 0),
                            })
                    playlist = {"title": info.get("title", ""), "count": len(items), "items": items}
                return formats, title, info.get("thumbnail", ""), playlist
        except RuntimeError:
            raise
        except Exception as e:
            err_str = str(e)
            # douyin/tiktok 需要 cookies
            if "cookies" in err_str.lower() and ("douyin" in err_str.lower() or "tiktok" in err_str.lower()):
                # 尝试从浏览器自动获取 cookies
                for browser in ("chrome", "edge", "firefox", "opera"):
                    try:
                        ydl_opts = _apply_network_options(
                            self._make_ydl_opts(cookies_from_browser=browser),
                            cookie, proxy, headers)
                        with YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(url, download=False)
                            if info and info.get("formats"):
                                formats = []
                                for f in info["formats"]:
                                    if f.get("ext") in ("mhtml", "json"):
                                        continue
                                    formats.append({
                                        "format_id": f.get("format_id", ""),
                                        "ext": f.get("ext", ""),
                                        "resolution": f.get("resolution", "") or f.get("format_note", ""),
                                        "filesize": f.get("filesize", 0) or f.get("filesize_approx", 0),
                                        "vcodec": f.get("vcodec", "none"),
                                        "acodec": f.get("acodec", "none"),
                                    })
                                if formats:
                                    title = info.get("title", "") or "untitled"
                                    return formats, title, info.get("thumbnail", ""), None
                    except Exception:  # noqa: BLE001 - 依次尝试下一个浏览器
                        logger.debug("浏览器 Cookie 读取失败：%s", browser,
                                     exc_info=True)
                        continue
                raise RuntimeError(
                    "抖音/TikTok 链接解析失败：平台强制要求有效的 Cookie 认证。\n\n"
                    "【原因】抖音网页版已强制开启登录态校验，匿名请求会被拦截；\n"
                    "        Cookies 有效期极短（通常仅数小时），需频繁刷新。\n\n"
                    "【解决方案】\n"
                    "1. 浏览器打开抖音网页版 → 随便浏览/播放一个视频 → 刷新页面（刷新 Cookie）\n"
                    "2. 在程序的「Cookie」输入框粘贴完整 Cookie 字符串（F12 → Network → 任意请求 → Request Headers → Cookie）\n"
                    "3. 或命令行启动自动读取浏览器 Cookie：\n"
                    "   python main_qt.py --cookies-from-browser chrome\n"
                    "   （支持 chrome / edge / firefox / opera）\n\n"
                    "【替代建议】优先使用 YouTube、Bilibili、Twitter/X 等开放平台链接，成功率更高。"
                )
            raise RuntimeError(f"解析失败：{err_str}")

    def _get_formats_cli(self, url, cookie=None, proxy=None, headers=None):
        """使用 yt-dlp 命令行解析格式。"""
        exe = _find_ytdlp_exe()
        if not exe:
            raise RuntimeError("未找到 yt-dlp，请安装: pip install yt-dlp")
        cmd = [exe, "--dump-single-json", "--no-download", "--no-warnings"]
        self._extend_cli_network(cmd, cookie, proxy, headers)
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding='utf-8', errors='ignore', timeout=30,
                                creationflags=_creation_flags())
        if result.returncode != 0 and not result.stdout.strip():
            raise RuntimeError(f"解析失败: {result.stderr[:200]}")
        info = json.loads(result.stdout)
        formats = []
        for f in info.get("formats", []):
            ext = f.get("ext", "")
            if ext in ("mhtml", "json"):
                continue
            formats.append({
                "format_id": f.get("format_id", ""),
                "ext": ext,
                "resolution": f.get("resolution", "") or f.get("format_note", ""),
                "filesize": f.get("filesize", 0) or f.get("filesize_approx", 0),
                "vcodec": f.get("vcodec", "none"),
                "acodec": f.get("acodec", "none"),
            })
        if not formats:
            raise RuntimeError("未找到可下载的格式")
        title = info.get("title", "") or "untitled"
        # 从同一次解析结果中提取播放列表信息
        playlist = None
        entries = info.get("entries")
        if entries and len(entries) > 1:
            items = []
            for e in entries:
                if e:
                    items.append({
                        "url": e.get("url") or e.get("webpage_url", ""),
                        "title": e.get("title", ""),
                        "duration": e.get("duration", 0),
                    })
            playlist = {"title": info.get("title", ""), "count": len(items), "items": items}
        return formats, title, info.get("thumbnail", ""), playlist

    def download(self, url, output_path, format_id=None, progress_callback=None,
                 cookie=None, headers=None, proxy=None, speed_limit=0,
                 audio_only=False, audio_format="mp3", subtitles=False,
                 output_template=None, thumbnail=False, video_only=False):
        self._cancel = False
        self._last_error = ""
        url = validate_http_url(url)
        if audio_only and video_only:
            raise ValueError("“仅音频”和“仅视频”不能同时启用")
        if audio_format not in AUDIO_FORMATS:
            raise ValueError(f"不支持的音频格式：{audio_format}")
        from utils.config import get_ffmpeg_path
        ffmpeg = get_ffmpeg_path()
        tmpl = _output_template(output_path, output_template)
        ydl_opts = _apply_network_options(self._make_ydl_opts(
            outtmpl=tmpl,
            progress_hooks=[],
        ), cookie, proxy, headers)
        if ffmpeg:
            ydl_opts["ffmpeg_location"] = ffmpeg
        if speed_limit > 0:
            ydl_opts["ratelimit"] = speed_limit * 1024 * 1024
        if audio_only:
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
            }]
        elif video_only:
            # 仅视频画面（无音频）：只取最佳视频流，不做合并
            ydl_opts["format"] = "bestvideo/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegVideoRemuxer",
                "preferedformat": "mp4",
            }]
        elif format_id:
            ydl_opts["format"] = format_id
        else:
            ydl_opts["format"] = "bestvideo+bestaudio/best"
        if subtitles:
            ydl_opts["writesubtitles"] = True
            ydl_opts["writeautomaticsub"] = True
            ydl_opts["subtitleslangs"] = ["all"]
            ydl_opts["embedsubs"] = False
        if thumbnail:
            ydl_opts["writethumbnail"] = True
            ydl_opts["skip_download"] = False

        start = time.time()

        def progress_hook(d):
            if self._cancel:
                raise InterruptedError("已取消")
            if d["status"] == "downloading":
                pct = 0
                if "total_bytes" in d and d["total_bytes"]:
                    pct = int(d.get("downloaded_bytes", 0) * 100 / d["total_bytes"])
                elif "total_bytes_estimate" in d and d["total_bytes_estimate"]:
                    pct = int(d.get("downloaded_bytes", 0) * 100 / d["total_bytes_estimate"])
                speed = d.get("speed", 0) or 0
                speed_str = f"{speed/1024/1024:.1f}MB/s" if speed > 1024*1024 else f"{speed/1024:.0f}KB/s" if speed else ""
                eta = d.get("eta", 0) or 0
                if progress_callback:
                    progress_callback(pct, f"下载中 {pct}%  {speed_str}  剩余 {eta}s")
            elif d["status"] == "finished":
                if progress_callback:
                    progress_callback(95, "正在合并…")

        ydl_opts["progress_hooks"] = [progress_hook]
        # 优先尝试 Python 模块
        try:
            from yt_dlp import YoutubeDL
            with YoutubeDL(ydl_opts) as ydl:
                result = ydl.download([url])
            if result:
                self._last_error = f"yt-dlp 返回错误码 {result}"
                if progress_callback:
                    progress_callback(-1, self._last_error)
                return False
            elapsed = time.time() - start
            if progress_callback:
                progress_callback(100, f"下载完成  耗时{int(elapsed)}s")
            return True
        except ImportError:
            pass
        # 降级到命令行
        return self._download_cli(url, output_path, format_id, progress_callback,
                                  cookie, proxy, headers, speed_limit,
                                  audio_only, audio_format, subtitles,
                                  output_template, thumbnail, video_only)

    def _download_cli(self, url, output_path, format_id=None, progress_callback=None,
                      cookie=None, proxy=None, headers=None, speed_limit=0,
                      audio_only=False, audio_format="mp3", subtitles=False,
                      output_template=None, thumbnail=False, video_only=False):
        """使用 yt-dlp 命令行下载。"""
        exe = _find_ytdlp_exe()
        if not exe:
            if progress_callback:
                progress_callback(-1, "未找到 yt-dlp")
            return False
        from utils.config import get_ffmpeg_path
        ffmpeg = get_ffmpeg_path()
        cmd = [exe, "-o", _output_template(output_path, output_template)]
        if ffmpeg:
            cmd.extend(["--ffmpeg-location", os.path.dirname(ffmpeg)])
        try:
            self._extend_cli_network(cmd, cookie, proxy, headers)
        except ValueError as exc:
            if progress_callback:
                progress_callback(-1, str(exc))
            return False
        if speed_limit > 0:
            cmd.extend(["--limit-rate", f"{speed_limit}M"])
        if audio_only:
            cmd.extend(["-x", "--audio-format", audio_format])
        elif video_only:
            cmd.extend(["-f", "bestvideo/best", "--remux-video", "mp4"])
        elif format_id:
            cmd.extend(["-f", format_id])
        if subtitles:
            cmd.extend(["--write-subs", "--write-auto-subs", "--sub-langs", "all"])
        if thumbnail:
            cmd.append("--write-thumbnail")
        cmd.append(url)

        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='ignore',
                creationflags=_creation_flags())
            for line in self._process.stdout:
                if self._cancel:
                    self._process.terminate()
                    if progress_callback:
                        progress_callback(-1, "已取消")
                    return False
                # 解析进度
                if "%" in line:
                    try:
                        pct_str = line.split("%")[0].split()[-1]
                        pct = int(float(pct_str))
                        if progress_callback:
                            progress_callback(pct, f"下载中 {pct}%")
                    except (ValueError, IndexError):
                        logger.debug("无法解析 yt-dlp 进度行：%s", line.rstrip())
            self._process.wait()
            if self._process.returncode == 0:
                if progress_callback:
                    progress_callback(100, "下载完成")
                return True
            else:
                if progress_callback:
                    progress_callback(-1, "下载失败")
                return False
        except (OSError, subprocess.SubprocessError) as e:
            if progress_callback:
                progress_callback(-1, f"下载失败: {str(e)[:60]}")
            return False
        finally:
            self._process = None

    @staticmethod
    def _extend_cli_network(cmd, cookie=None, proxy=None, headers=None):
        """CLI 网络参数；敏感原始 Cookie/鉴权头禁止暴露到进程参数。"""
        cookie_value = str(cookie or "").strip()
        if cookie_value:
            cookie_path = os.path.abspath(os.path.expanduser(cookie_value))
            if not os.path.isfile(cookie_path):
                raise ValueError("命令行模式仅支持 Cookie 文件路径，请安装 Python yt-dlp")
            cmd.extend(["--cookies", cookie_path])
        if proxy:
            cmd.extend(["--proxy", str(proxy).strip()])
        for key, value in _validated_headers(headers).items():
            if key.casefold() in {"authorization", "cookie", "proxy-authorization"}:
                raise ValueError("命令行模式不接收敏感请求头，请安装 Python yt-dlp")
            cmd.extend(["--add-header", f"{key}:{value}"])

    def _resolve_douyin_short_url(self, url):
        """尝试解析抖音短链接 (v.douyin.com) 为重定向后的长链接"""
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.geturl()
        except Exception:  # noqa: BLE001 - 短链解析失败时保留原 URL
            logger.debug("抖音短链重定向解析失败：%s", url, exc_info=True)
            return url

    def get_playlist_info(self, url):
        from yt_dlp import YoutubeDL
        ydl_opts = self._make_ydl_opts(extract_flat=True, force_generic_extractor=False)
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info is None:
            return None
        entries = info.get("entries", [])
        if entries:
            items = []
            for e in entries:
                if e:
                    items.append({
                        "url": e.get("url") or e.get("webpage_url", ""),
                        "title": e.get("title", ""),
                        "duration": e.get("duration", 0),
                    })
            return {"title": info.get("title", ""), "count": len(items), "items": items}
        return None

    @staticmethod
    def update_ytdlp(progress_cb=None):
        if progress_cb: progress_cb("正在更新 yt-dlp…")
        try:
            # 下载器实际调用的是外部可执行文件，必须和状态卡共用同一
            # 跨平台更新链；pip 更新 Python 包不会更新这个可执行文件。
            from core.tool_updater import download_ytdlp
            ok, message = download_ytdlp()
            if progress_cb:
                progress_cb("yt-dlp 已更新到最新版" if ok
                            else f"更新失败：{message}")
            return ok
        except Exception as exc:  # noqa: BLE001
            if progress_cb: progress_cb(f"更新失败：{exc}")
            return False
