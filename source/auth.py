"""
Auth primitives for gcshttpx: Token acquisition, IAM signing, and HTTP session.
All APIs are async and built on httpx with HTTP/2 enabled.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import enum
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Any, AnyStr

import httpx
import jwt
import orjson

# Public exports
__all__ = [
    "AioSession",
    "Token",
    "IamClient",
    "Type",
    "encode",
    "decode",
]


# Session wrapper
Response = httpx.Response
Session = httpx.AsyncClient
Timeout = httpx.Timeout | float


async def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        body = resp.text
        raise httpx.HTTPStatusError(
            f"{resp.reason_phrase}: {body}", request=resp.request, response=resp
        )


class AioSession:
    def __init__(
        self,
        session: Session | None = None,
        *,
        timeout: Timeout = 10,
        verify_ssl: bool = True,
    ) -> None:
        self._shared_session = bool(session)
        self._session = session
        self._timeout = timeout
        self._ssl = verify_ssl

    @property
    def session(self) -> Session:
        if not self._session:
            timeout = (
                self._timeout
                if isinstance(self._timeout, httpx.Timeout)
                else httpx.Timeout(self._timeout)
            )
            self._session = httpx.AsyncClient(
                timeout=timeout, verify=self._ssl, http2=True
            )
        return self._session

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        resp = await self.session.request(method, url, **kwargs)
        await _raise_for_status(resp)
        return resp

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, int | str] | None = None,
        timeout: Timeout = 10,
    ) -> Response:
        if not isinstance(timeout, httpx.Timeout):
            timeout = httpx.Timeout(timeout)
        return await self.request(
            "GET", url, headers=headers, params=params, timeout=timeout
        )

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | str | IO[AnyStr] | None = None,
        params: Mapping[str, int | str] | None = None,
        timeout: Timeout = 10,
    ) -> Response:
        if not isinstance(timeout, httpx.Timeout):
            timeout = httpx.Timeout(timeout)
        return await self.request(
            "POST", url, headers=headers, params=params, content=data, timeout=timeout
        )

    async def patch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | str | None = None,
        params: Mapping[str, int | str] | None = None,
        timeout: Timeout = 10,
    ) -> Response:
        if not isinstance(timeout, httpx.Timeout):
            timeout = httpx.Timeout(timeout)
        return await self.request(
            "PATCH", url, headers=headers, params=params, content=data, timeout=timeout
        )

    async def put(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | str | IO[Any],
        timeout: Timeout = 10,
    ) -> Response:
        if not isinstance(timeout, httpx.Timeout):
            timeout = httpx.Timeout(timeout)
        return await self.request(
            "PUT", url, headers=headers, content=data, timeout=timeout
        )

    async def delete(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, int | str] | None = None,
        timeout: Timeout = 10,
    ) -> Response:
        if not isinstance(timeout, httpx.Timeout):
            timeout = httpx.Timeout(timeout)
        return await self.request(
            "DELETE", url, headers=headers, params=params, timeout=timeout
        )

    async def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, int | str] | None = None,
        timeout: Timeout = 10,
        allow_redirects: bool = False,
    ) -> Response:
        if not isinstance(timeout, httpx.Timeout):
            timeout = httpx.Timeout(timeout)
        return await self.request(
            "HEAD",
            url,
            headers=headers,
            params=params,
            timeout=timeout,
            follow_redirects=allow_redirects,
        )

    async def close(self) -> None:
        if not self._shared_session and self._session:
            await self._session.aclose()


# Token logic
class Type(enum.Enum):
    AUTHORIZED_USER = "authorized_user"
    GCE_METADATA = "gce_metadata"
    SERVICE_ACCOUNT = "service_account"


# Environment and endpoints
_GCE_METADATA_HOST = os.environ.get("GCE_METADATA_HOST") or os.environ.get(
    "GCE_METADATA_ROOT", "metadata.google.internal"
)
GCE_METADATA_BASE = f"http://{_GCE_METADATA_HOST}/computeMetadata/v1"
GCE_METADATA_HEADERS = {"metadata-flavor": "Google"}
GCE_ENDPOINT_PROJECT = f"{GCE_METADATA_BASE}/project/project-id"
GCE_ENDPOINT_TOKEN = (
    f"{GCE_METADATA_BASE}/instance/service-accounts/default/token?recursive=true"
)
GCLOUD_ENDPOINT_GENERATE_ACCESS_TOKEN = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:generateAccessToken"
GCLOUD_ENDPOINT_GENERATE_ID_TOKEN = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:generateIdToken"


def decode(payload: str) -> bytes:
    return base64.b64decode(payload, altchars=b"-_")


def encode(payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return base64.b64encode(payload, altchars=b"-_")


def get_service_data(service: str | IO[AnyStr] | None) -> dict[str, Any]:
    service = service or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not service:
        # Try gcloud ADC path on disk
        if os.name != "nt":
            sdkpath = os.path.join(os.path.expanduser("~"), ".config", "gcloud")
        else:
            sdkpath = (
                os.path.join(os.environ.get("APPDATA", ""), "gcloud")
                if os.environ.get("APPDATA")
                else os.path.join(os.environ.get("SYSTEMDRIVE", "C:"), "\\", "gcloud")
            )
        cred_path = os.path.join(sdkpath, "application_default_credentials.json")
        try:
            with open(cred_path, encoding="utf-8") as f:
                return orjson.load(f)
        except Exception:
            return {}

    try:
        # Attempt reading string path first
        with open(service, encoding="utf-8") as f:  # type: ignore[arg-type]
            return orjson.load(f)
    except TypeError:
        # file-like object
        try:
            return orjson.loads(service.read())  # type: ignore[union-attr]
        except Exception:
            return {}
    except Exception:
        return {}


@dataclass
class TokenResponse:
    value: str
    expires_in: int


class BaseToken:
    def __init__(
        self,
        service_file: str | IO[AnyStr] | None = None,
        session: Session | None = None,
        *,
        background_refresh_after: float = 0.5,
        force_refresh_after: float = 0.95,
    ) -> None:
        if not (0 < background_refresh_after <= 1):
            raise ValueError("background_refresh_after must be between 0 and 1")
        if not (0 < force_refresh_after <= 1):
            raise ValueError("force_refresh_after must be between 0 and 1")

        self.background_refresh_after = background_refresh_after
        self.force_refresh_after = force_refresh_after

        self.service_data = get_service_data(service_file)
        if self.service_data:
            self.token_type = Type(self.service_data["type"])
            self.token_uri = self.service_data.get(
                "token_uri", "https://oauth2.googleapis.com/token"
            )
        else:
            self.token_type = Type.GCE_METADATA
            self.token_uri = GCE_ENDPOINT_TOKEN

        self.session = AioSession(session)
        self.access_token: str | None = None
        self.access_token_duration = 0
        self.access_token_acquired_at = datetime.datetime(
            1970, 1, 1, tzinfo=datetime.timezone.utc
        )
        self.access_token_preempt_after = 0
        self.access_token_refresh_after = 0
        self.acquiring: asyncio.Task[None] | None = None

    async def get_project(self) -> str | None:
        project = (
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
            or os.environ.get("APPLICATION_ID")
        )
        if project:
            return project
        if self.token_type == Type.GCE_METADATA:
            await self.ensure_token()
            resp = await self.session.get(
                GCE_ENDPOINT_PROJECT, headers=GCE_METADATA_HEADERS
            )
            try:
                return resp.text  # type: ignore[return-value]
            except Exception:
                return str(resp.text)
        if self.token_type == Type.SERVICE_ACCOUNT:
            return self.service_data.get("project_id")
        return None

    async def get(self) -> str | None:
        await self.ensure_token()
        return self.access_token

    async def ensure_token(self) -> None:
        if self.access_token:
            now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            if now_ts <= self.access_token_refresh_after:
                if now_ts <= self.access_token_preempt_after:
                    return
                # preemptive refresh in background
                if not self.acquiring or self.acquiring.done():
                    self.acquiring = asyncio.create_task(self.acquire_access_token())
                return

        if not self.acquiring or self.acquiring.done():
            self.acquiring = asyncio.create_task(self.acquire_access_token())
        await self.acquiring

    async def acquire_access_token(self, timeout: int = 10) -> None:
        resp = await self.refresh(timeout=timeout)
        self.access_token = resp.value
        self.access_token_duration = resp.expires_in
        self.access_token_acquired_at = datetime.datetime.now(datetime.timezone.utc)
        base_ts = self.access_token_acquired_at.timestamp()
        self.access_token_preempt_after = int(
            base_ts + (resp.expires_in * self.background_refresh_after)
        )
        self.access_token_refresh_after = int(
            base_ts + (resp.expires_in * self.force_refresh_after)
        )
        self.acquiring = None

    async def refresh(
        self, *, timeout: int
    ) -> TokenResponse:  # pragma: no cover - abstract
        raise NotImplementedError

    async def close(self) -> None:
        await self.session.close()

    async def __aenter__(self) -> BaseToken:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


class Token(BaseToken):
    default_token_ttl = 3600

    def __init__(
        self,
        service_file: str | IO[AnyStr] | None = None,
        session: Session | None = None,
        scopes: list[str] | None = None,
    ) -> None:
        super().__init__(service_file=service_file, session=session)
        self.scopes = " ".join(scopes or []) if scopes else ""

    async def _refresh_authorized_user(self, timeout: int) -> TokenResponse:
        assert self.service_data
        payload = httpx.QueryParams(
            {
                "grant_type": "refresh_token",
                "client_id": self.service_data["client_id"],
                "client_secret": self.service_data["client_secret"],
                "refresh_token": self.service_data["refresh_token"],
            }
        ).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await self.session.post(
            self.token_uri, data=payload, headers=headers, timeout=timeout
        )
        data = resp.json()
        return TokenResponse(
            value=str(data["access_token"]), expires_in=int(data["expires_in"])
        )

    async def _refresh_gce_metadata(self, timeout: int) -> TokenResponse:
        resp = await self.session.get(
            self.token_uri, headers=GCE_METADATA_HEADERS, timeout=timeout
        )
        data = resp.json()
        return TokenResponse(
            value=str(data["access_token"]), expires_in=int(data["expires_in"])
        )

    async def _refresh_service_account(self, timeout: int) -> TokenResponse:
        assert self.service_data
        now = int(time.time())
        payload = {
            "iss": self.service_data["client_email"],
            "scope": self.scopes,
            "aud": self.service_data.get(
                "token_uri", "https://oauth2.googleapis.com/token"
            ),
            "iat": now,
            "exp": now + self.default_token_ttl,
        }
        assertion = jwt.encode(
            payload, self.service_data["private_key"], algorithm="RS256"
        )
        form = httpx.QueryParams(
            {
                "assertion": assertion,
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            }
        ).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await self.session.post(
            self.token_uri, data=form, headers=headers, timeout=timeout
        )
        data = resp.json()
        token_value = str(data.get("access_token") or data.get("id_token"))
        expires = int(data.get("expires_in", "0") or self.default_token_ttl)
        return TokenResponse(value=token_value, expires_in=expires)

    async def refresh(self, *, timeout: int) -> TokenResponse:  # type: ignore[override]
        if self.token_type == Type.AUTHORIZED_USER:
            return await self._refresh_authorized_user(timeout)
        if self.token_type == Type.GCE_METADATA:
            return await self._refresh_gce_metadata(timeout)
        if self.token_type == Type.SERVICE_ACCOUNT:
            return await self._refresh_service_account(timeout)
        raise RuntimeError(f"unsupported token type: {self.token_type}")


class IamClient:
    API_ROOT_IAM_CREDENTIALS = "https://iamcredentials.googleapis.com/v1"

    def __init__(
        self,
        *,
        service_file: str | IO[AnyStr] | None = None,
        session: Session | None = None,
        token: Token | None = None,
    ) -> None:
        self.session = AioSession(session)
        self.token = token or Token(
            service_file=service_file,
            session=self.session.session,
            scopes=["https://www.googleapis.com/auth/iam"],
        )

    async def _headers(self) -> dict[str, str]:
        tok = await self.token.get()
        return {"Authorization": f"Bearer {tok}"}

    @property
    def service_account_email(self) -> str | None:
        return self.token.service_data.get("client_email")

    async def sign_blob(
        self,
        payload: str | bytes | None,
        *,
        service_account_email: str | None = None,
        delegates: list[str] | None = None,
        timeout: int = 10,
    ) -> dict[str, str]:
        sa_email = service_account_email or self.service_account_email
        if not sa_email:
            raise TypeError("service_account_email is required for sign_blob")
        resource_name = f"projects/-/serviceAccounts/{sa_email}"
        url = f"{self.API_ROOT_IAM_CREDENTIALS}/{resource_name}:signBlob"
        body = orjson.dumps(
            {
                "delegates": delegates or [resource_name],
                "payload": encode(payload or b"").decode("utf-8"),
            }
        )
        headers = await self._headers()
        headers.update(
            {"Content-Type": "application/json", "Content-Length": str(len(body))}
        )
        resp = await self.session.post(url, data=body, headers=headers, timeout=timeout)
        return resp.json()

    async def close(self) -> None:
        await self.session.close()

    async def __aenter__(self) -> IamClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
