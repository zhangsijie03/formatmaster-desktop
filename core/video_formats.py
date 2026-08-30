"""视频输出容器的编码约束，供界面和转换入口共用。"""
from enum import Enum


class VideoOutputFormat(str, Enum):
    WEBM = ".webm"
    GIF = ".gif"


DEFAULT_VIDEO_CODEC = "libx264"
FORMAT_CODECS = {
    VideoOutputFormat.WEBM: ("libvpx-vp9", "libvpx"),
    VideoOutputFormat.GIF: ("gif",),
}


def default_video_codec(extension: str) -> str:
    return FORMAT_CODECS.get(extension.lower(), (DEFAULT_VIDEO_CODEC,))[0]


def video_codec_supported(extension: str, codec: str | None) -> bool:
    allowed = FORMAT_CODECS.get(extension.lower())
    return codec is None or allowed is None or codec in allowed
