"""憑證加密。

Jira API token 是 bearer 憑證 —— 拿到就等於拿到那個人的 Jira 帳號。
所以資料庫裡只放密文，金鑰走環境變數，兩者分開才有意義。
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from devloop.core.config import get_settings


class SecretKeyMissingError(RuntimeError):
    """沒設金鑰就寧可整個起不來，也不要退化成明文存放。"""


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise SecretKeyMissingError(
            "DEVLOOP_SECRET_KEY 沒設定。產生一把：\n"
            '  uv run python -c "from cryptography.fernet import Fernet;'
            ' print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("憑證解不開 —— 金鑰換過了，請重新輸入 token") from exc


def mask(secret: str, keep: int = 4) -> str:
    """回顯用。永遠不要把完整 token 送回瀏覽器。"""
    if len(secret) <= keep:
        return "•" * 8
    return "•" * 8 + secret[-keep:]
