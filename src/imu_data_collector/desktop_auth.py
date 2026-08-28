"""桌面端 Google OAuth 2.0 Authorization Code + PKCE 登录。

客户端 ID 是公开配置；安装包不携带客户端密钥或服务账号密钥。长期 refresh token
只进入操作系统凭据库，短期 ID token 只保留在进程内存中。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import keyring
import requests

from imu_data_collector.config import DesktopCloudSettings


class OAuthLoginRequired(RuntimeError):
    """本机长期凭据缺失或已被 Google 撤销，需要重新交互登录。"""


@dataclass(frozen=True, slots=True)
class OAuthPending:
    verifier: str
    redirect_uri: str
    created_monotonic: float


def _jwt_claims_unverified(token: str) -> dict[str, Any]:
    """仅用于 UI 显示邮箱；所有授权决定都由代理端完成签名校验。"""

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload))
        return value if isinstance(value, dict) else {}
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


class DesktopOAuthManager:
    def __init__(self, settings: DesktopCloudSettings) -> None:
        self.settings = settings
        self._pending: dict[str, OAuthPending] = {}
        self._id_token: str | None = None
        self._expires_monotonic = 0.0
        self._display_email: str | None = None
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.broker_url
            and self.settings.google_oauth_client_id
        )

    @property
    def _credential_name(self) -> str:
        client = self.settings.google_oauth_client_id or "unconfigured"
        digest = hashlib.sha256(client.encode()).hexdigest()[:16]
        return f"google-refresh-token-{digest}"

    @property
    def _email_name(self) -> str:
        return f"{self._credential_name}-email"

    @property
    def logged_in(self) -> bool:
        return bool(
            self.configured
            and keyring.get_password(
                self.settings.keyring_service,
                self._credential_name,
            )
        )

    def begin(self, redirect_uri: str) -> str:
        if not self.configured:
            raise RuntimeError("云端发布尚未配置 broker_url 和 Google OAuth client ID")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(32)
        with self._lock:
            cutoff = time.monotonic() - 600.0
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if value.created_monotonic >= cutoff
            }
            self._pending[state] = OAuthPending(verifier, redirect_uri, time.monotonic())
        query = urlencode(
            {
                "client_id": self.settings.google_oauth_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.settings.scopes),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return f"{self.settings.authorization_endpoint}?{query}"

    def complete(self, *, state: str, code: str) -> dict[str, Any]:
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or time.monotonic() - pending.created_monotonic > 600.0:
            raise RuntimeError("OAuth state 无效或已经过期，请重新登录")
        try:
            payload = self._token_request(
                {
                    "client_id": self.settings.google_oauth_client_id,
                    "code": code,
                    "code_verifier": pending.verifier,
                    "redirect_uri": pending.redirect_uri,
                    "grant_type": "authorization_code",
                }
            )
        except requests.RequestException as error:
            raise RuntimeError("无法向 Google 完成登录，请检查网络后重试") from error
        refresh_token = str(payload.get("refresh_token") or "")
        if refresh_token:
            keyring.set_password(
                self.settings.keyring_service,
                self._credential_name,
                refresh_token,
            )
        elif not keyring.get_password(
            self.settings.keyring_service, self._credential_name
        ):
            raise RuntimeError("Google 未返回 refresh token，请撤销旧授权后重新登录")
        self._remember_id_token(payload)
        if self._display_email:
            keyring.set_password(
                self.settings.keyring_service,
                self._email_name,
                self._display_email,
            )
        return self.status()

    def _token_request(self, form: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(self.settings.token_endpoint, data=form, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Google token endpoint 返回了无效响应")
        return payload

    def _remember_id_token(self, payload: dict[str, Any]) -> str:
        token = str(payload.get("id_token") or "")
        if not token:
            raise RuntimeError("Google token 响应缺少 id_token")
        expires_in = max(60, int(payload.get("expires_in", 3600)))
        with self._lock:
            self._id_token = token
            self._expires_monotonic = time.monotonic() + expires_in
            email = _jwt_claims_unverified(token).get("email")
            self._display_email = str(email).lower() if email else None
        return token

    def id_token(self) -> str:
        if not self.configured:
            raise RuntimeError("云端发布尚未配置")
        with self._lock:
            if self._id_token and time.monotonic() < self._expires_monotonic - 120.0:
                return self._id_token
        refresh_token = keyring.get_password(
            self.settings.keyring_service, self._credential_name
        )
        if not refresh_token:
            raise RuntimeError("尚未登录 Google，无法发布到团队云端")
        try:
            payload = self._token_request(
                {
                    "client_id": self.settings.google_oauth_client_id,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
            )
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 0
            if status in {400, 401}:
                self._forget_credentials()
                raise OAuthLoginRequired("Google 登录已失效，请重新登录") from error
            raise
        return self._remember_id_token(payload)

    def _forget_credentials(self) -> None:
        try:
            keyring.delete_password(
                self.settings.keyring_service,
                self._credential_name,
            )
        except keyring.errors.PasswordDeleteError:
            pass
        try:
            keyring.delete_password(
                self.settings.keyring_service,
                self._email_name,
            )
        except keyring.errors.PasswordDeleteError:
            pass
        with self._lock:
            self._id_token = None
            self._expires_monotonic = 0.0
            self._display_email = None
            self._pending.clear()

    def logout(self) -> None:
        refresh_token = keyring.get_password(
            self.settings.keyring_service,
            self._credential_name,
        )
        if refresh_token:
            try:
                requests.post(
                    self.settings.revocation_endpoint,
                    data={"token": refresh_token},
                    timeout=10,
                ).raise_for_status()
            except requests.RequestException:
                # 注销必须优先删除本机长期凭据；Google 撤销失败不能把秘密留在电脑上。
                pass
        self._forget_credentials()

    def status(self) -> dict[str, Any]:
        stored = self.logged_in
        display_email = self._display_email
        if stored and not display_email:
            display_email = keyring.get_password(
                self.settings.keyring_service,
                self._email_name,
            )
        return {
            "configured": self.configured,
            "logged_in": stored,
            "email": display_email,
            "broker_url": self.settings.broker_url if self.configured else None,
        }
