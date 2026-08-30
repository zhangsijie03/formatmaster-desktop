"""file_security — 文件安全工具。

- 加密/解密：密码经 scrypt 派生密钥，Fernet（AES-128-CBC + HMAC-SHA256）加密。
  输出格式：8 字节魔数 `FMSEC01\\0` + 16 字节 salt +
  若干块 [4 字节块长 + Fernet token]，分块流式处理支持大文件。
- 粉碎：多次随机数据覆写 + fsync 落盘后删除，防止数据恢复。
纯函数，无 UI 依赖。
"""

import os
import secrets
import tempfile

try:
    import cryptography  # noqa: F401 - 启动即检查，缺失时给出明确提示
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_OK = True
except Exception:  # noqa: BLE001
    _CRYPTO_OK = False
    Fernet = None          # type: ignore[assignment]
    InvalidToken = Exception

_MISSING_MSG = ("缺少 cryptography 模块：打包运行请重新打包（build.py 已含收集配置）；"
                "源码运行请执行 pip install cryptography")

LEGACY_MAGIC = b"FMSEC01\0"
MAGIC = b"FMSEC02\0"
_SALT_LEN = 16
_DEFAULT_PASSES = 3
_BLOCK = 16 * 1024 * 1024   # 分块大小 16MB，避免大文件整读入内存

# Fernet token 首字节必是 url-safe base64 字符（用于区分新旧格式）
_B64URL_CHARS = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_MAX_TOKEN = _BLOCK * 2


def _stage_output(output_path):
    """在目标目录建立临时文件，验证全部成功后再原子替换。"""
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".fm_security_", dir=output_dir)
    os.close(fd)
    return path


def _cleanup_stage(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _same_path(first, second):
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second))


def _read_exact(stream, size, label):
    data = stream.read(size)
    if len(data) != size:
        raise ValueError(f"加密文件已截断（缺少{label}）")
    return data


def _derive_key(password: str, salt: bytes) -> bytes:
    """scrypt 派生 Fernet key（base64 编码，拖慢暴力破解）。"""
    import base64
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def encrypt_file(input_path, output_path, password, progress_cb=None):
    """加密文件 → 输出加密文件（含魔数+salt，分块流式）。成功返回 True。"""
    if not _CRYPTO_OK:
        if progress_cb:
            progress_cb(-1, _MISSING_MSG)
        return False
    staged_path = ""
    try:
        if not password:
            raise ValueError("密码不能为空")
        if _same_path(input_path, output_path):
            raise ValueError("输出文件不能覆盖源文件")
        salt = secrets.token_bytes(_SALT_LEN)
        key = _derive_key(password, salt)
        f = Fernet(key)
        size = os.path.getsize(input_path)
        staged_path = _stage_output(output_path)
        if progress_cb:
            progress_cb(5, "读取文件…")
        done = 0
        with open(input_path, "rb") as fin, open(staged_path, "wb") as fout:
            fout.write(MAGIC + salt + size.to_bytes(8, "big"))
            while True:
                chunk = fin.read(_BLOCK)
                if not chunk and (size or done):
                    break
                tok = f.encrypt(chunk)
                fout.write(len(tok).to_bytes(4, "big") + tok)
                done += len(chunk)
                if progress_cb:
                    pct = int(done * 90 / size) if size else 90
                    progress_cb(pct, f"加密中 {done // 1048576}/{size // 1048576} MB…")
                if not chunk:
                    break
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(staged_path, output_path)
        staged_path = ""
        if progress_cb:
            progress_cb(100, "完成")
        return True
    except OSError as e:
        if progress_cb:
            progress_cb(-1, f"错误：{e}")
        return False
    except Exception as e:  # noqa: BLE001
        if progress_cb:
            progress_cb(-1, f"加密失败：{e}")
        return False
    finally:
        _cleanup_stage(staged_path)


def decrypt_file(input_path, output_path, password, progress_cb=None):
    """解密文件。兼容两种格式：
    - 新格式（分块流式）：salt 后是 [4 字节块长 + token] × N
    - 旧格式（整体 token）：salt 后直接是单个 Fernet token
    密码错误/文件损坏返回 False。
    """
    if not _CRYPTO_OK:
        if progress_cb:
            progress_cb(-1, _MISSING_MSG)
        return False
    staged_path = ""
    try:
        if not password:
            raise ValueError("密码不能为空")
        if _same_path(input_path, output_path):
            raise ValueError("输出文件不能覆盖源文件")
        with open(input_path, "rb") as fh:
            head = _read_exact(fh, len(MAGIC), "文件头")
            if head not in (MAGIC, LEGACY_MAGIC):
                if progress_cb:
                    progress_cb(-1, "错误：不是本工具加密的文件")
                return False
            salt = _read_exact(fh, _SALT_LEN, "salt")
            expected_size = (int.from_bytes(
                _read_exact(fh, 8, "原文长度"), "big")
                if head == MAGIC else None)
            key = _derive_key(password, salt)
            fernet = Fernet(key)
            size = os.path.getsize(input_path)
            staged_path = _stage_output(output_path)
            written = 0
            with open(staged_path, "wb") as fout:
                first = fh.read(1)
                if head == LEGACY_MAGIC and first and first[0] in _B64URL_CHARS:
                    # 最早版整文 Fernet token 仅为兼容而整体读取。
                    plaintext = fernet.decrypt(first + fh.read())
                    fout.write(plaintext)
                    written = len(plaintext)
                else:
                    prefix = first + fh.read(3) if first else b""
                    while prefix:
                        if len(prefix) != 4:
                            raise ValueError("加密文件已截断（块长度不完整）")
                        token_size = int.from_bytes(prefix, "big")
                        if token_size <= 0 or token_size > _MAX_TOKEN:
                            raise ValueError("加密文件块长度无效")
                        token = _read_exact(fh, token_size, "密文块")
                        plaintext = fernet.decrypt(token)
                        fout.write(plaintext)
                        written += len(plaintext)
                        if progress_cb:
                            progress_cb(
                                min(int(fh.tell() * 90 / max(size, 1)), 95),
                                "解密中…")
                        prefix = fh.read(4)
                if expected_size is not None and written != expected_size:
                    raise ValueError("加密文件已截断（原文长度不匹配）")
                fout.flush()
                os.fsync(fout.fileno())
        os.replace(staged_path, output_path)
        staged_path = ""
        if progress_cb:
            progress_cb(100, "完成")
        return True
    except InvalidToken:
        if progress_cb:
            progress_cb(-1, "错误：密码不正确")
        return False
    except OSError as e:
        if progress_cb:
            progress_cb(-1, f"错误：{e}")
        return False
    except Exception as e:  # noqa: BLE001
        if progress_cb:
            progress_cb(-1, f"解密失败：{e}")
        return False
    finally:
        _cleanup_stage(staged_path)


def _is_encrypted(path):
    """判断文件是否为本工具加密产物（读魔数）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) in (MAGIC, LEGACY_MAGIC)
    except OSError:
        return False


def shred_file(path, passes=_DEFAULT_PASSES, progress_cb=None):
    """粉碎单个文件：随机覆写 N 遍 + fsync 后删除。"""
    try:
        if os.path.islink(path):
            raise OSError("不支持粉碎符号链接")
        passes = int(passes)
        if passes < 1 or passes > 20:
            raise ValueError("覆写遍数必须在 1–20 之间")
        size = os.path.getsize(path)
        with open(path, "r+b") as fh:
            chunk = 1 << 20
            for i in range(passes):
                fh.seek(0)
                remaining = size
                while remaining > 0:
                    n = min(chunk, remaining)
                    fh.write(secrets.token_bytes(n))
                    remaining -= n
                fh.flush()
                os.fsync(fh.fileno())
                if progress_cb:
                    progress_cb(int((i + 1) * 90 / passes), f"覆写第 {i + 1}/{passes} 遍…")
        os.remove(path)
        if progress_cb:
            progress_cb(100, "已粉碎删除")
        return True
    except (OSError, ValueError) as e:
        if progress_cb:
            progress_cb(-1, f"粉碎失败：{e}")
        return False


def shred_paths(paths, passes=_DEFAULT_PASSES, progress_cb=None):
    """批量粉碎文件。返回 (成功数, 失败列表)。"""
    total = len(paths)
    ok = 0
    failed = []
    for i, p in enumerate(paths):
        if progress_cb:
            progress_cb(int(i * 100 / max(total, 1)),
                        f"粉碎 {os.path.basename(p)} ({i + 1}/{total})…")
        if shred_file(p, passes):
            ok += 1
        else:
            failed.append(p)
    return ok, failed
