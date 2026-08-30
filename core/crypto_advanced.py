"""crypto_advanced — 高级加密工具（RSA/ECC 非对称 · 数字签名 · AES-GCM · X.509 证书）。

- generate_keypair(): RSA/ECC 密钥对生成
- encrypt/decrypt_asymmetric(): 混合加密（AES-GCM 会话密钥 + RSA-OAEP / ECDH）
- sign_file()/verify_signature(): 数字签名（RSA-PSS / ECDSA）
- encrypt/decrypt_file_gcm(): 纯 AES-GCM 认证加密（比 Fernet 更快）
- generate_self_signed_cert(): X.509 自签名证书

依赖 cryptography；缺失时 _CRYPTO_OK=False，各函数给出明确提示。
"""
import base64
import os
import secrets
import tempfile

try:
    import cryptography  # noqa: F401
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    import datetime
    _CRYPTO_OK = True
except Exception:  # noqa: BLE001
    _CRYPTO_OK = False

_MISSING_MSG = ("缺少 cryptography 模块：请执行 pip install cryptography")
MAGIC = b"FMCRY01\0"          # 高级加密格式魔数
_BLOCK = 16 * 1024 * 1024     # 分块 16MB
_MAX_CIPHER_BLOCK = _BLOCK + 1024
_MAX_ENCRYPTED_KEY = 1024 * 1024
_NEW_RSA = b"\x02"
_NEW_ECC = b"\x03"
_NEW_GCM = b"2"


def _same_path(first, second):
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second))


def _stage_output(output_path):
    output_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(output_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".fm_crypto_", dir=output_dir)
    os.close(fd)
    return path


def _cleanup_stage(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _read_exact(stream, size, label):
    data = stream.read(size)
    if len(data) != size:
        raise ValueError(f"加密文件已截断（缺少{label}）")
    return data


def _chunk_nonce(seed, index):
    """保留随机 32-bit 前缀，后 64-bit 作单调块计数器。"""
    return seed[:4] + int(index).to_bytes(8, "big")


def _chunk_aad(marker, plaintext_size, index):
    return marker + int(plaintext_size).to_bytes(8, "big") + int(index).to_bytes(8, "big")


def _read_cipher_block(stream):
    prefix = stream.read(4)
    if not prefix:
        return None
    if len(prefix) != 4:
        raise ValueError("加密文件已截断（块长度不完整）")
    size = int.from_bytes(prefix, "big")
    if size < 16 or size > _MAX_CIPHER_BLOCK:
        raise ValueError("加密文件块长度无效")
    return _read_exact(stream, size, "密文块")


# ── 密钥对生成 ──────────────────────────────────

def generate_keypair(algorithm="rsa", key_size=2048, curve="SECP256R1"):
    """生成密钥对，返回 (private_pem, public_pem) 字节。

    algorithm: "rsa"（默认 2048） / "ecc"（默认 SECP256R1）。
    """
    if not _CRYPTO_OK:
        return None, None
    try:
        if algorithm == "ecc":
            cname = getattr(ec, curve, ec.SECP256R1)
            private = ec.generate_private_key(cname())
        else:
            private = rsa.generate_private_key(
                public_exponent=65537, key_size=int(key_size))
        private_pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption())
        public_pem = private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo)
        return private_pem, public_pem
    except Exception:  # noqa: BLE001
        return None, None


def _load_private(pem: bytes, password=None):
    password_bytes = password.encode("utf-8") if isinstance(password, str) else password
    return serialization.load_pem_private_key(pem, password=password_bytes)


def _load_public(pem: bytes):
    return serialization.load_pem_public_key(pem)


# ── 混合加密（非对称 + AES-GCM 会话密钥）────────

def encrypt_asymmetric(input_path, output_path, public_key_pem, progress_cb=None):
    """公钥加密文件（混合加密）。

    输出格式：魔数 + 1字节算法(0=RSA/1=ECC) + 4字节会话密钥长度 +
    会话密钥密文 + 12字节 nonce + 密文分块（块长 4 字节前缀）。
    """
    if not _CRYPTO_OK:
        if progress_cb: progress_cb(-1, _MISSING_MSG)
        return False
    staged_path = ""
    try:
        if _same_path(input_path, output_path):
            raise ValueError("输出文件不能覆盖源文件")
        pub = _load_public(public_key_pem)
        # 生成 AES-256 会话密钥
        session_key = secrets.token_bytes(32)
        if isinstance(pub, rsa.RSAPublicKey):
            algo = _NEW_RSA
            enc_key = pub.encrypt(
                session_key,
                padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                             algorithm=hashes.SHA256(), label=None))
        else:
            algo = _NEW_ECC
            # ECC: ECDH 派生共享密钥
            eph = ec.generate_private_key(pub.curve)
            shared = eph.exchange(ec.ECDH(), pub)
            _h = hashes.Hash(hashes.SHA256())
            _h.update(shared)
            session_key = _h.finalize()
            enc_key = eph.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo)
        nonce_seed = secrets.token_bytes(12)
        aes = AESGCM(session_key)
        size = os.path.getsize(input_path)
        staged_path = _stage_output(output_path)
        done = 0
        with open(input_path, "rb") as fin, open(staged_path, "wb") as fout:
            fout.write(MAGIC + algo)
            fout.write(len(enc_key).to_bytes(4, "big") + enc_key)
            fout.write(nonce_seed + size.to_bytes(8, "big"))
            index = 0
            while True:
                chunk = fin.read(_BLOCK)
                if not chunk and (size or index):
                    break
                ct = aes.encrypt(
                    _chunk_nonce(nonce_seed, index), chunk,
                    _chunk_aad(algo, size, index))
                fout.write(len(ct).to_bytes(4, "big") + ct)
                done += len(chunk)
                index += 1
                if progress_cb and size:
                    progress_cb(int(done * 90 / size), f"加密中 {done // 1048576}MB…")
                if not chunk:
                    break
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(staged_path, output_path)
        staged_path = ""
        if progress_cb:
            progress_cb(100, "完成")
        return True
    except Exception as e:  # noqa: BLE001
        if progress_cb: progress_cb(-1, f"加密失败：{e}")
        return False
    finally:
        _cleanup_stage(staged_path)


def decrypt_asymmetric(input_path, output_path, private_key_pem, progress_cb=None,
                       private_key_password=None):
    """私钥解密文件（encrypt_asymmetric 的逆过程）。"""
    if not _CRYPTO_OK:
        if progress_cb: progress_cb(-1, _MISSING_MSG)
        return False
    staged_path = ""
    try:
        if _same_path(input_path, output_path):
            raise ValueError("输出文件不能覆盖源文件")
        priv = _load_private(private_key_pem, private_key_password)
        with open(input_path, "rb") as fh:
            head = _read_exact(fh, len(MAGIC), "文件头")
            if head != MAGIC:
                if progress_cb: progress_cb(-1, "不是本工具非对称加密的文件")
                return False
            algo = _read_exact(fh, 1, "算法标记")
            if algo not in (b"\x00", b"\x01", _NEW_RSA, _NEW_ECC):
                raise ValueError("不支持的非对称加密格式")
            key_size = int.from_bytes(_read_exact(fh, 4, "密钥长度"), "big")
            if key_size <= 0 or key_size > _MAX_ENCRYPTED_KEY:
                raise ValueError("加密会话密钥长度无效")
            enc_key = _read_exact(fh, key_size, "加密会话密钥")
            nonce_seed = _read_exact(fh, 12, "nonce")
            modern = algo in (_NEW_RSA, _NEW_ECC)
            expected_size = (int.from_bytes(
                _read_exact(fh, 8, "原文长度"), "big")
                if modern else None)
            if algo in (b"\x00", _NEW_RSA):
                session_key = priv.decrypt(
                    enc_key,
                    padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                 algorithm=hashes.SHA256(), label=None))
            else:
                eph_pub = _load_public(enc_key)
                shared = priv.exchange(ec.ECDH(), eph_pub)
                digest = hashes.Hash(hashes.SHA256())
                digest.update(shared)
                session_key = digest.finalize()
            aes = AESGCM(session_key)
            staged_path = _stage_output(output_path)
            written = 0
            index = 0
            encrypted_size = os.path.getsize(input_path)
            with open(staged_path, "wb") as fout:
                while True:
                    token = _read_cipher_block(fh)
                    if token is None:
                        break
                    nonce = (_chunk_nonce(nonce_seed, index)
                             if modern else nonce_seed)
                    aad = (_chunk_aad(algo, expected_size, index)
                           if modern else None)
                    plaintext = aes.decrypt(nonce, token, aad)
                    fout.write(plaintext)
                    written += len(plaintext)
                    index += 1
                    if progress_cb:
                        progress_cb(
                            min(int(fh.tell() * 90 / max(encrypted_size, 1)), 95),
                            "解密中…")
                if expected_size is not None and written != expected_size:
                    raise ValueError("加密文件已截断（原文长度不匹配）")
                fout.flush()
                os.fsync(fout.fileno())
        os.replace(staged_path, output_path)
        staged_path = ""
        if progress_cb:
            progress_cb(100, "完成")
        return True
    except Exception as e:  # noqa: BLE001
        if progress_cb: progress_cb(-1, f"解密失败：{e}")
        return False
    finally:
        _cleanup_stage(staged_path)


# ── 数字签名 ────────────────────────────────────

def _hash_file_sha256(path):
    digest = hashes.Hash(hashes.SHA256())
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.finalize()


def sign_file(input_path, private_key_pem, signature_path=None, progress_cb=None,
              private_key_password=None):
    """私钥签名文件，返回签名字节（RSA-PSS / ECDSA 自动适配）。"""
    if not _CRYPTO_OK:
        if progress_cb: progress_cb(-1, _MISSING_MSG)
        return None
    staged_path = ""
    try:
        priv = _load_private(private_key_pem, private_key_password)
        h = _hash_file_sha256(input_path)
        if isinstance(priv, rsa.RSAPrivateKey):
            sig = priv.sign(
                h, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                               salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256())
        else:
            sig = priv.sign(h, ec.ECDSA(hashes.SHA256()))
        if signature_path:
            if _same_path(input_path, signature_path):
                raise ValueError("签名文件不能覆盖源文件")
            staged_path = _stage_output(signature_path)
            with open(staged_path, "wb") as stream:
                stream.write(sig)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged_path, signature_path)
            staged_path = ""
        if progress_cb:
            progress_cb(100, "签名完成")
        return sig
    except Exception as e:  # noqa: BLE001
        if progress_cb: progress_cb(-1, f"签名失败：{e}")
        return None
    finally:
        _cleanup_stage(staged_path)


def verify_signature(input_path, public_key_pem, signature, progress_cb=None):
    """公钥验签，返回 (ok, msg)。signature 为字节或签名文件路径。"""
    if not _CRYPTO_OK:
        if progress_cb: progress_cb(-1, _MISSING_MSG)
        return False, _MISSING_MSG
    try:
        if isinstance(signature, (bytes, bytearray)):
            sig = bytes(signature)
        else:
            with open(signature, "rb") as f:
                sig = f.read()
        pub = _load_public(public_key_pem)
        h = _hash_file_sha256(input_path)
        if isinstance(pub, rsa.RSAPublicKey):
            pub.verify(
                sig, h,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256())
        else:
            pub.verify(sig, h, ec.ECDSA(hashes.SHA256()))
        if progress_cb:
            progress_cb(100, "验签通过")
        return True, "签名有效"
    except Exception:  # noqa: BLE001 - 验签失败统一返回 False
        if progress_cb:
            progress_cb(100, "验签失败")
        return False, "签名无效或文件被篡改"


# ── 纯 AES-GCM 认证加密（比 Fernet 快）──────────

def _gcm_derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    return kdf.derive(password.encode("utf-8"))


def encrypt_file_gcm(input_path, output_path, password, progress_cb=None):
    """AES-GCM 加密（scrypt 派生密钥，认证加密，比 Fernet 快）。
    输出：魔数/版本 + salt + nonce 种子 + 原文长度 + 认证密文块。
    """
    if not _CRYPTO_OK:
        if progress_cb: progress_cb(-1, _MISSING_MSG)
        return False
    staged_path = ""
    try:
        if not password:
            raise ValueError("密码不能为空")
        if _same_path(input_path, output_path):
            raise ValueError("输出文件不能覆盖源文件")
        salt = secrets.token_bytes(16)
        nonce_seed = secrets.token_bytes(12)
        key = _gcm_derive_key(password, salt)
        aes = AESGCM(key)
        size = os.path.getsize(input_path)
        staged_path = _stage_output(output_path)
        done = 0
        with open(input_path, "rb") as fin, open(staged_path, "wb") as fout:
            fout.write(MAGIC + _NEW_GCM + salt + nonce_seed
                       + size.to_bytes(8, "big"))
            index = 0
            while True:
                chunk = fin.read(_BLOCK)
                if not chunk and (size or index):
                    break
                ct = aes.encrypt(
                    _chunk_nonce(nonce_seed, index), chunk,
                    _chunk_aad(_NEW_GCM, size, index))
                fout.write(len(ct).to_bytes(4, "big") + ct)
                done += len(chunk)
                index += 1
                if progress_cb and size:
                    progress_cb(int(done * 90 / size), f"加密中 {done // 1048576}MB…")
                if not chunk:
                    break
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(staged_path, output_path)
        staged_path = ""
        if progress_cb:
            progress_cb(100, "完成")
        return True
    except Exception as e:  # noqa: BLE001
        if progress_cb: progress_cb(-1, f"加密失败：{e}")
        return False
    finally:
        _cleanup_stage(staged_path)


def decrypt_file_gcm(input_path, output_path, password, progress_cb=None):
    """AES-GCM 解密（encrypt_file_gcm 逆过程）。"""
    if not _CRYPTO_OK:
        if progress_cb: progress_cb(-1, _MISSING_MSG)
        return False
    staged_path = ""
    try:
        if not password:
            raise ValueError("密码不能为空")
        if _same_path(input_path, output_path):
            raise ValueError("输出文件不能覆盖源文件")
        with open(input_path, "rb") as fh:
            head = _read_exact(fh, len(MAGIC) + 1, "文件头")
            marker = head[-1:]
            if head[:len(MAGIC)] != MAGIC or marker not in (b"G", _NEW_GCM):
                if progress_cb: progress_cb(-1, "不是本工具 GCM 加密的文件")
                return False
            salt = _read_exact(fh, 16, "salt")
            nonce_seed = _read_exact(fh, 12, "nonce")
            modern = marker == _NEW_GCM
            expected_size = (int.from_bytes(
                _read_exact(fh, 8, "原文长度"), "big")
                if modern else None)
            key = _gcm_derive_key(password, salt)
            aes = AESGCM(key)
            staged_path = _stage_output(output_path)
            written = 0
            index = 0
            encrypted_size = os.path.getsize(input_path)
            with open(staged_path, "wb") as fout:
                while True:
                    token = _read_cipher_block(fh)
                    if token is None:
                        break
                    nonce = (_chunk_nonce(nonce_seed, index)
                             if modern else nonce_seed)
                    aad = (_chunk_aad(marker, expected_size, index)
                           if modern else None)
                    plaintext = aes.decrypt(nonce, token, aad)
                    fout.write(plaintext)
                    written += len(plaintext)
                    index += 1
                    if progress_cb:
                        progress_cb(
                            min(int(fh.tell() * 90 / max(encrypted_size, 1)), 95),
                            "解密中…")
                if expected_size is not None and written != expected_size:
                    raise ValueError("加密文件已截断（原文长度不匹配）")
                fout.flush()
                os.fsync(fout.fileno())
        os.replace(staged_path, output_path)
        staged_path = ""
        if progress_cb:
            progress_cb(100, "完成")
        return True
    except Exception as e:  # noqa: BLE001
        if progress_cb: progress_cb(-1, f"解密失败：{e}")
        return False
    finally:
        _cleanup_stage(staged_path)


# ── X.509 自签名证书 ────────────────────────────

def generate_self_signed_cert(common_name, output_path, private_key_path=None,
                              days=365, key_size=2048, progress_cb=None,
                              private_key_password=None):
    """生成 X.509 自签名证书。

    返回 (cert_path, private_key_path)；密钥未指定时生成 RSA 密钥对并
    一并保存（证书与私钥分离，私钥供服务器/签名使用）。
    """
    if not _CRYPTO_OK:
        if progress_cb: progress_cb(-1, _MISSING_MSG)
        return None, None
    cert_stage = ""
    key_stage = ""
    try:
        common_name = str(common_name).strip()
        if not common_name or len(common_name) > 253 or any(
                ord(char) < 32 for char in common_name):
            raise ValueError("证书名称不能为空、含控制字符或超过 253 字符")
        days = int(days)
        if days < 1 or days > 3650:
            raise ValueError("证书有效期必须在 1–3650 天之间")
        key_size = int(key_size)
        if key_size < 2048:
            raise ValueError("RSA 密钥长度不能低于 2048 位")
        password_bytes = (private_key_password.encode("utf-8")
                          if private_key_password else None)
        if private_key_path and os.path.isfile(private_key_path):
            with open(private_key_path, "rb") as f:
                private = serialization.load_pem_private_key(
                    f.read(), password=password_bytes)
            write_private_key = False
        else:
            private = rsa.generate_private_key(
                public_exponent=65537, key_size=key_size)
            private_key_path = private_key_path or (output_path + ".key")
            write_private_key = True
        if _same_path(output_path, private_key_path):
            raise ValueError("证书和私钥不能使用同一路径")
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=days))
            .sign(private, hashes.SHA256())
        )
        # 两个文件均先在各自目标目录完整写入，避免生成失败留下半成品。
        cert_stage = _stage_output(output_path)
        with open(cert_stage, "wb") as stream:
            stream.write(cert.public_bytes(serialization.Encoding.PEM))
            stream.flush()
            os.fsync(stream.fileno())
        if write_private_key:
            key_stage = _stage_output(private_key_path)
            encryption = (serialization.BestAvailableEncryption(password_bytes)
                          if password_bytes
                          else serialization.NoEncryption())
            with open(key_stage, "wb") as stream:
                stream.write(private.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=encryption))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(key_stage, 0o600)
            os.replace(key_stage, private_key_path)
            key_stage = ""
        os.replace(cert_stage, output_path)
        cert_stage = ""
        if progress_cb:
            progress_cb(100, "证书已生成")
        return output_path, private_key_path
    except Exception as e:  # noqa: BLE001
        if progress_cb: progress_cb(-1, f"证书生成失败：{e}")
        return None, None
    finally:
        _cleanup_stage(cert_stage)
        _cleanup_stage(key_stage)
