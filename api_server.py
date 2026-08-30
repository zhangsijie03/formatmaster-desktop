"""FormatMaster 本地 REST API 服务。

默认仅监听 127.0.0.1:5000，提供健康检查、视频转换和文档转换。
可直接运行 ``python api_server.py``，也可用打包后的主程序
``FormatMaster --api-server`` 启动。
"""
from __future__ import annotations

import argparse
import os
import threading
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import logger
from core.doc_converter import DocumentConverter
from core.video_converter import VideoConverter
from core.video_formats import video_codec_supported
from utils.output_paths import path_key, unique_output_path


class ApiCode(str, Enum):
    OK = "OK"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    CONVERSION_FAILED = "CONVERSION_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class VideoFormat(str, Enum):
    MP4 = "mp4"
    AVI = "avi"
    MKV = "mkv"
    WMV = "wmv"
    MOV = "mov"
    FLV = "flv"
    WEBM = "webm"
    TS = "ts"
    MPEG = "mpeg"
    THREE_GP = "3gp"


class VideoCodec(str, Enum):
    DEFAULT = "default"
    H264 = "h264"
    H265 = "h265"
    VP9 = "vp9"
    MPEG4 = "mpeg4"


class QualityPreset(str, Enum):
    ORIGINAL = "original"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    MOBILE = "mobile"
    WEB = "web"


class ResolutionPreset(str, Enum):
    ORIGINAL = "original"
    UHD_4K = "4k"
    QHD_2K = "2k"
    P1080 = "1080p"
    P720 = "720p"
    P480 = "480p"
    P360 = "360p"


class BitratePreset(str, Enum):
    AUTO = "auto"
    M1 = "1M"
    M2 = "2M"
    M5 = "5M"
    M8 = "8M"
    M10 = "10M"
    M20 = "20M"


_CODECS = {
    VideoCodec.DEFAULT: None,
    VideoCodec.H264: "libx264",
    VideoCodec.H265: "libx265",
    VideoCodec.VP9: "libvpx-vp9",
    VideoCodec.MPEG4: "mpeg4",
}
_QUALITIES = {
    QualityPreset.ORIGINAL: None,
    QualityPreset.HIGH: "high",
    QualityPreset.MEDIUM: "medium",
    QualityPreset.LOW: "low",
    QualityPreset.MOBILE: "mobile",
    QualityPreset.WEB: "web",
}
_RESOLUTIONS = {
    ResolutionPreset.ORIGINAL: None,
    ResolutionPreset.UHD_4K: (3840, 2160),
    ResolutionPreset.QHD_2K: (2560, 1440),
    ResolutionPreset.P1080: (1920, 1080),
    ResolutionPreset.P720: (1280, 720),
    ResolutionPreset.P480: (854, 480),
    ResolutionPreset.P360: (640, 360),
}


class VideoConvertRequest(BaseModel):
    input_path: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    format: VideoFormat = VideoFormat.MP4
    codec: VideoCodec = VideoCodec.DEFAULT
    quality: QualityPreset = QualityPreset.ORIGINAL
    resolution: ResolutionPreset = ResolutionPreset.ORIGINAL
    frame_rate: int | None = Field(default=None)
    bitrate: BitratePreset = BitratePreset.AUTO
    stream_copy: bool = False
    selected_streams: dict[int, bool] | None = None


class DocumentConvertRequest(BaseModel):
    input_path: str = Field(min_length=1)
    output_path: str = Field(min_length=1)


def _response(code: ApiCode, message: str, data=None):
    return {
        "success": code == ApiCode.OK,
        "code": code.value,
        "message": message,
        "data": data,
    }


def _prepare_paths(input_path: str, output_path: str, reserved=()) -> tuple[str, str]:
    source = os.path.abspath(os.path.expanduser(input_path))
    target = os.path.abspath(os.path.expanduser(output_path))
    if not os.path.isfile(source):
        raise HTTPException(status_code=404, detail={
            "code": ApiCode.NOT_FOUND.value,
            "message": f"输入文件不存在: {source}",
        })
    target = unique_output_path(target, source=source, reserved=reserved)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    return source, target


def create_app() -> FastAPI:
    app = FastAPI(title="FormatMaster REST API", version="1.0.0")
    output_lock = threading.Lock()
    reserved_outputs: set[str] = set()

    @contextmanager
    def reserve_paths(input_path, output_path):
        # 锁只保护命名与预留，不覆盖耗时转换；失败/取消请求也会释放占用。
        with output_lock:
            source, target = _prepare_paths(input_path, output_path, reserved_outputs)
            key = path_key(target)
            reserved_outputs.add(key)
        try:
            yield source, target
        finally:
            with output_lock:
                reserved_outputs.discard(key)

    @app.exception_handler(HTTPException)
    async def handle_http_exception(_request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code", ApiCode.INVALID_ARGUMENT.value)
        message = detail.get("message", str(exc.detail))
        return JSONResponse(
            status_code=exc.status_code,
            content=_response(ApiCode(code), message),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_response(
                ApiCode.INVALID_ARGUMENT, "请求参数不合法", exc.errors()),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception):
        logger.error("REST API 未处理异常", exc)
        return JSONResponse(
            status_code=500,
            content=_response(ApiCode.INTERNAL_ERROR, "服务内部错误"),
        )

    @app.get("/")
    @app.get("/api/health")
    async def health():
        return _response(ApiCode.OK, "FormatMaster API is ready", {
            "service": "FormatMaster",
            "version": app.version,
        })

    @app.post("/api/video/convert")
    async def convert_video(body: VideoConvertRequest):
        if body.frame_rate not in (None, 24, 25, 30, 60):
            raise HTTPException(status_code=422, detail={
                "code": ApiCode.INVALID_ARGUMENT.value,
                "message": "frame_rate 仅支持 24/25/30/60 或 null",
            })
        expected_ext = "." + body.format.value
        if not body.stream_copy and not video_codec_supported(expected_ext, _CODECS[body.codec]):
            raise HTTPException(status_code=422, detail={
                "code": ApiCode.INVALID_ARGUMENT.value,
                "message": "目标格式与编码器不兼容",
            })
        if os.path.splitext(body.output_path)[1].lower() != expected_ext:
            raise HTTPException(status_code=422, detail={
                "code": ApiCode.INVALID_ARGUMENT.value,
                "message": f"output_path 扩展名必须为 {expected_ext}",
            })
        with reserve_paths(body.input_path, body.output_path) as (source, target):
            converter = VideoConverter()
            ok = await run_in_threadpool(
                converter.convert,
                source,
                target,
                expected_ext,
                _CODECS[body.codec],
                _QUALITIES[body.quality],
                _RESOLUTIONS[body.resolution],
                None if body.bitrate == BitratePreset.AUTO else body.bitrate.value,
                body.frame_rate,
                None,
                body.stream_copy,
                body.selected_streams,
            )
        if not ok:
            raise HTTPException(status_code=500, detail={
                "code": ApiCode.CONVERSION_FAILED.value,
                "message": "视频转换失败",
            })
        return _response(ApiCode.OK, "视频转换完成", {"output_path": target})

    @app.post("/api/document/convert")
    async def convert_document(body: DocumentConvertRequest):
        with reserve_paths(body.input_path, body.output_path) as (source, target):
            ok = await run_in_threadpool(
                DocumentConverter().convert, source, target)
        if not ok:
            raise HTTPException(status_code=500, detail={
                "code": ApiCode.CONVERSION_FAILED.value,
                "message": "文档转换失败或该组合不受支持",
            })
        return _response(ApiCode.OK, "文档转换完成", {"output_path": target})

    return app


app = create_app()


def run_server(host: str = "127.0.0.1", port: int = 5000):
    """启动本地 API；显式传入非回环 host 时由调用者承担网络暴露风险。"""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv=None):
    parser = argparse.ArgumentParser(description="FormatMaster REST API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
