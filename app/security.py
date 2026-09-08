import os
import logging
from cryptography.fernet import Fernet
from app.config import DATA_DIR

logger = logging.getLogger("fnos.security")

KEY_FILE_PATH = os.path.join(DATA_DIR, ".secret_key")

_fernet_instance = None

def get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    if not os.path.exists(KEY_FILE_PATH):
        key = Fernet.generate_key()
        try:
            with open(KEY_FILE_PATH, "wb") as f:
                f.write(key)
            # 在类 Unix 系统中保护密钥文件权限
            try:
                os.chmod(KEY_FILE_PATH, 0o600)
            except Exception:
                pass
            logger.info(f"[Security] Generated new encryption secret key at {KEY_FILE_PATH}")
        except Exception as e:
            logger.error(f"[Security] Failed to write secret key: {e}")
            return Fernet(key)
    else:
        with open(KEY_FILE_PATH, "rb") as f:
            key = f.read().strip()

    _fernet_instance = Fernet(key)
    return _fernet_instance

def encrypt_credential(plain_text: str) -> str:
    if not plain_text:
        return ""
    if plain_text.startswith("enc:"):
        return plain_text  # 已经是密文
    fernet = get_fernet()
    token = fernet.encrypt(plain_text.encode("utf-8")).decode("utf-8")
    return f"enc:{token}"

def decrypt_credential(cipher_text: str) -> str:
    if not cipher_text:
        return ""
    if not cipher_text.startswith("enc:"):
        return cipher_text  # 兼容未加密的历史纯文本
    raw_token = cipher_text[4:]
    fernet = get_fernet()
    try:
        return fernet.decrypt(raw_token.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"[Security] Failed to decrypt credential: {e}")
        return ""

def mask_credential(text: str) -> str:
    if not text:
        return ""
    return "******"
