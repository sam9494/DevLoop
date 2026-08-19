"""UUIDv7 主鍵 —— 時間有序、索引友善（抄 JobRadar 的慣例）。"""

import uuid

import uuid_utils


def uuid7() -> uuid.UUID:
    return uuid.UUID(bytes=uuid_utils.uuid7().bytes)
