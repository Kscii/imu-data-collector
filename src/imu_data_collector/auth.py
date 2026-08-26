"""标注服务的可信操作者身份解析。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import cachecontrol
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token

from imu_data_collector.config import Settings

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"


class AuthenticationError(ValueError):
    """请求没有可验证的登录身份。"""


class AuthorizationError(ValueError):
    """身份有效，但不在应用白名单内。"""


@dataclass(frozen=True, slots=True)
class Actor:
    """由服务器验证后得到的当前操作者。"""

    unikey: str
    email: str | None
    subject: str | None
    is_admin: bool
    auth_mode: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "unikey": self.unikey,
            "email": self.email,
            "is_admin": self.is_admin,
            "auth_mode": self.auth_mode,
        }


TokenVerifier = Callable[[str, str], Mapping[str, Any]]


class IapTokenVerifier:
    """使用带 HTTP 缓存的 Google 公钥验证 IAP JWT。"""

    def __init__(self) -> None:
        session = cachecontrol.CacheControl(requests.Session())
        self._request = GoogleAuthRequest(session=session)

    def __call__(self, token: str, audience: str) -> Mapping[str, Any]:
        claims = id_token.verify_token(
            token,
            self._request,
            audience=audience,
            certs_url=IAP_CERTS_URL,
            clock_skew_in_seconds=30,
        )
        if claims.get("iss") != IAP_ISSUER:
            raise AuthenticationError("IAP JWT issuer 无效")
        return claims


class Authenticator:
    """解析 local 或 IAP 模式，并且只返回白名单成员。"""

    def __init__(
        self,
        settings: Settings,
        token_verifier: TokenVerifier | None = None,
    ) -> None:
        self.settings = settings
        self.mode = settings.auth.mode
        if self.mode not in {"local", "iap"}:
            raise ValueError("auth.mode 必须为 local 或 iap")
        if self.mode == "iap" and not settings.auth.iap_audience:
            raise ValueError("IAP 模式必须配置 auth.iap_audience")
        self.token_verifier = token_verifier or IapTokenVerifier()

    def authenticate(self, iap_assertion: str | None) -> Actor:
        if self.mode == "local":
            return self._actor(
                self.settings.auth.local_actor_id,
                email=None,
                subject=None,
            )
        if not iap_assertion:
            raise AuthenticationError("缺少 IAP 身份断言")
        try:
            claims = self.token_verifier(
                iap_assertion,
                str(self.settings.auth.iap_audience),
            )
        except AuthenticationError:
            raise
        except Exception as error:
            raise AuthenticationError("IAP 身份断言无效") from error
        email_claim = claims.get("email")
        if not isinstance(email_claim, str) or not email_claim.strip():
            raise AuthenticationError("IAP JWT 缺少 email")
        email = email_claim.strip().lower()
        unikey = self.settings.identity.email_to_unikey.get(email)
        if not unikey:
            raise AuthorizationError("该 Google 账号尚未映射到团队 UniKey")
        subject = claims.get("sub")
        return self._actor(
            unikey,
            email=email,
            subject=str(subject) if subject is not None else None,
        )

    def _actor(
        self,
        unikey: str,
        *,
        email: str | None,
        subject: str | None,
    ) -> Actor:
        if unikey not in self.settings.identity.allowed_unikeys:
            raise AuthorizationError("当前 UniKey 不在应用允许名单")
        return Actor(
            unikey=unikey,
            email=email,
            subject=subject,
            is_admin=unikey in self.settings.identity.admins,
            auth_mode=self.mode,
        )
