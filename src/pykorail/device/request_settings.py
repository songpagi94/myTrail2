"""클라이언트 인스턴스에만 적용하는 요청 신원 설정."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RequestSettings:
    """미지정 항목은 기존 프로파일과 상수를 그대로 사용합니다."""

    device_model: str | None = None
    os_version: str | None = None
    os_type: str | None = None
    sdk_version: str | None = None
    app_version: str | None = None
    sid_key: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value in (self.device_model, self.os_version, self.os_type, self.sdk_version, self.app_version):
            if value is not None and (not value or len(value) > 32 or not value.isascii() or not value.isprintable()):
                raise ValueError("설정값은 출력 가능한 ASCII 문자 1~32자리여야 합니다")
        # Sid는 AES 키를 IV로도 사용하므로 IV 길이인 16바이트만 허용합니다.
        if self.sid_key is not None and len(self.sid_key) != 16:
            raise ValueError("Sid key는 정확히 16바이트여야 합니다")
        if self.sdk_version is not None and re.fullmatch(r"[A-Za-z0-9._-]{1,16}", self.sdk_version) is None:
            raise ValueError("SDK version은 영숫자와 . _ -로 된 1~16자리여야 합니다")
