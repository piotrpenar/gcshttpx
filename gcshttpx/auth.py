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
        data: bytes | str | dict | IO[AnyStr] | None = None,
        params: Mapping[str, int | str] | None = None,
        timeout: Timeout = 10,
    ) -> Response:
        if not isinstance(timeout, httpx.Timeout):
            timeout = httpx.Timeout(timeout)
        # Use 'data' for form data (dict), 'content' for raw bytes/str
        if isinstance(data, dict):
            return await self.request(
                "POST", url, headers=headers, params=params, data=data, timeout=timeout
            )
        # Convert IO objects to bytes for httpx AsyncClient compatibility
        if hasattr(data, "read"):
            data = data.read()  # type: ignore
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
        # Convert IO objects to bytes for httpx AsyncClient compatibility
        if hasattr(data, "read"):
            data = data.read()  # type: ignore
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
    IMPERSONATED_SERVICE_ACCOUNT = "impersonated_service_account"


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
GCE_ENDPOINT_EMAIL = f"{GCE_METADATA_BASE}/instance/service-accounts/default/email"
GCE_ENDPOINT_ID_TOKEN = (
    f"{GCE_METADATA_BASE}/instance/service-accounts/default/identity?audience={{audience}}"
)
GCLOUD_ENDPOINT_GENERATE_ACCESS_TOKEN = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:generateAccessToken"
GCLOUD_ENDPOINT_GENERATE_ID_TOKEN = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:generateIdToken"


def decode(payload: str) -> bytes:
    return base64.b64decode(payload, altchars=b"-_")


def encode(payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return base64.b64encode(payload, altchars=b"-_")


def _get_adc_path() -> str:
    """Get the well-known ADC credentials file path."""
    if os.name != "nt":
        return os.path.join(
            os.path.expanduser("~"), ".config", "gcloud", "application_default_credentials.json"
        )
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return os.path.join(appdata, "gcloud", "application_default_credentials.json")
    return os.path.join(
        os.environ.get("SYSTEMDRIVE", "C:"), "gcloud", "application_default_credentials.json"
    )


def _load_credentials_file(source: str | IO[AnyStr]) -> dict[str, Any]:
    """Load and validate credentials from file path or file-like object."""
    try:
        with open(source, encoding="utf-8") as f:  # type: ignore[arg-type]
            data = orjson.loads(f.read())  # type: ignore[attr-defined]
            return data if isinstance(data, dict) else {}
    except (TypeError, AttributeError):
        try:
            content = source.read()  # type: ignore[union-attr]
            if isinstance(content, bytes):
                data = orjson.loads(content)  # type: ignore[attr-defined]
            else:
                data = orjson.loads(content.encode("utf-8"))  # type: ignore[attr-defined]
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    except Exception:
        return {}


def get_service_data(
    service: str | IO[AnyStr] | None = None,
    *,
    use_adc: bool = True,
) -> dict[str, Any]:
    """
    Load credentials following ADC search order:
    1. Explicitly provided file path or file-like object
    2. GOOGLE_APPLICATION_CREDENTIALS environment variable
    3. Well-known ADC file (if use_adc=True)
    4. Returns {} to trigger metadata server fallback
    """
    if service is not None:
        return _load_credentials_file(service)

    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path:
        return _load_credentials_file(env_path)

    if use_adc:
        adc_path = _get_adc_path()
        if os.path.exists(adc_path):
            return _load_credentials_file(adc_path)

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
        use_adc: bool = True,
        background_refresh_after: float = 0.5,
        force_refresh_after: float = 0.95,
    ) -> None:
        if not (0 < background_refresh_after <= 1):
            raise ValueError("background_refresh_after must be between 0 and 1")
        if not (0 < force_refresh_after <= 1):
            raise ValueError("force_refresh_after must be between 0 and 1")
        if background_refresh_after >= force_refresh_after:
            raise ValueError(
                "background_refresh_after must be less than force_refresh_after"
            )

        self.background_refresh_after = background_refresh_after
        self.force_refresh_after = force_refresh_after

        self.service_data = get_service_data(service_file, use_adc=use_adc)
        # For impersonated credentials, store additional fields
        self._impersonation_url: str | None = None
        self._impersonated_email: str | None = None
        self._source_credentials: dict[str, Any] | None = None

        if self.service_data:
            # Validate required fields for service account
            if "type" not in self.service_data:
                raise ValueError("Invalid service account JSON: missing 'type' field")
            try:
                self.token_type = Type(self.service_data["type"])
            except ValueError as e:
                raise ValueError(
                    f"Invalid service account type: {self.service_data['type']}"
                ) from e

            # Handle impersonated_service_account type
            if self.token_type == Type.IMPERSONATED_SERVICE_ACCOUNT:
                self._init_impersonated_credentials()
            else:
                self.token_uri = self.service_data.get(
                    "token_uri", "https://oauth2.googleapis.com/token"
                )

            # Validate token_uri is HTTPS
            if not self.token_uri.startswith("https://"):
                raise ValueError(
                    f"token_uri must use HTTPS protocol, got: {self.token_uri}"
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

    def _init_impersonated_credentials(self) -> None:
        """Initialize fields for impersonated_service_account credentials."""
        self._impersonation_url = self.service_data.get("service_account_impersonation_url")
        if not self._impersonation_url:
            raise ValueError("Missing service_account_impersonation_url for impersonated credentials")
        # Extract service account email from URL
        # URL format: .../serviceAccounts/EMAIL:generateAccessToken
        try:
            self._impersonated_email = self._impersonation_url.split("/serviceAccounts/")[1].split(":")[0]
        except (IndexError, AttributeError) as e:
            raise ValueError(f"Cannot extract service account email from URL: {self._impersonation_url}") from e
        self._source_credentials = self.service_data.get("source_credentials")
        if not self._source_credentials:
            raise ValueError("Missing source_credentials for impersonated credentials")
        # Use source credentials' token_uri
        self.token_uri = self._source_credentials.get("token_uri", "https://oauth2.googleapis.com/token")

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

    async def get_service_account_email(self) -> str | None:
        """Get service account email from credentials or metadata server."""
        # For impersonated credentials, return the target service account email
        if self.token_type == Type.IMPERSONATED_SERVICE_ACCOUNT:
            return self._impersonated_email
        if self.service_data:
            return self.service_data.get("client_email")
        if self.token_type == Type.GCE_METADATA:
            resp = await self.session.get(GCE_ENDPOINT_EMAIL, headers=GCE_METADATA_HEADERS)
            return resp.text.strip()
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
        *,
        use_adc: bool = True,
    ) -> None:
        super().__init__(service_file=service_file, session=session, use_adc=use_adc)
        self.scopes = " ".join(scopes or []) if scopes else ""

    async def _refresh_authorized_user(self, timeout: int) -> TokenResponse:
        assert self.service_data
        # Validate required fields
        required = ["client_id", "client_secret", "refresh_token"]
        missing = [f for f in required if f not in self.service_data]
        if missing:
            raise ValueError(
                f"Invalid authorized_user credentials: missing {', '.join(missing)}"
            )

        form_data = {
            "grant_type": "refresh_token",
            "client_id": self.service_data["client_id"],
            "client_secret": self.service_data["client_secret"],
            "refresh_token": self.service_data["refresh_token"],
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await self.session.post(
            self.token_uri, data=form_data, headers=headers, timeout=timeout
        )
        data = resp.json()
        if "access_token" not in data or "expires_in" not in data:
            raise ValueError("Invalid token response: missing required fields")
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
        # Validate required fields
        required = ["client_email", "private_key"]
        missing = [f for f in required if f not in self.service_data]
        if missing:
            raise ValueError(
                f"Invalid service_account credentials: missing {', '.join(missing)}"
            )

        # Validate private key format
        private_key = self.service_data["private_key"]
        if not isinstance(private_key, str) or not private_key.strip():
            raise ValueError("Invalid private_key: must be a non-empty string")
        if "BEGIN PRIVATE KEY" not in private_key:
            raise ValueError(
                "Invalid private_key format: must be PEM-encoded private key"
            )

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
        try:
            assertion = jwt.encode(payload, private_key, algorithm="RS256")
        except Exception as e:
            raise ValueError(f"Failed to sign JWT assertion: {e}") from e

        form = {
            "assertion": assertion,
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await self.session.post(
            self.token_uri, data=form, headers=headers, timeout=timeout
        )
        data = resp.json()
        token_value = str(data.get("access_token") or data.get("id_token") or "")
        if not token_value:
            raise ValueError("Token response missing access_token or id_token")
        expires = int(data.get("expires_in", "0") or self.default_token_ttl)
        return TokenResponse(value=token_value, expires_in=expires)

    async def get_id_token(self, audience: str, *, timeout: int = 10) -> str:
        """
        Get an ID token for service-to-service authentication.

        Args:
            audience: The target service URL (e.g., "https://my-service.run.app")

        Returns:
            JWT ID token for the specified audience
        """
        if self.token_type == Type.GCE_METADATA:
            url = GCE_ENDPOINT_ID_TOKEN.format(audience=audience)
            resp = await self.session.get(url, headers=GCE_METADATA_HEADERS, timeout=timeout)
            return resp.text.strip()

        if self.token_type == Type.SERVICE_ACCOUNT:
            return await self._generate_id_token_iam(audience, timeout)

        raise RuntimeError(f"ID tokens not supported for {self.token_type}")

    async def _generate_id_token_iam(self, audience: str, timeout: int) -> str:
        """Generate ID token using IAM credentials API."""
        assert self.service_data
        email = self.service_data["client_email"]
        url = GCLOUD_ENDPOINT_GENERATE_ID_TOKEN.format(service_account=email)
        access_token = await self.get()
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        body = orjson.dumps({"audience": audience, "includeEmail": True})
        resp = await self.session.post(url, data=body, headers=headers, timeout=timeout)
        return resp.json()["token"]

    async def _refresh_impersonated(self, timeout: int) -> TokenResponse:
        """Refresh token using service account impersonation.

        Flow:
        1. Get source token using source_credentials (authorized_user)
        2. Use source token to call IAM Credentials API to get impersonated token
        """
        assert self._source_credentials
        assert self._impersonation_url

        # Step 1: Get source token from authorized_user credentials
        source_creds = self._source_credentials
        required = ["client_id", "client_secret", "refresh_token"]
        missing = [f for f in required if f not in source_creds]
        if missing:
            raise ValueError(f"Invalid source_credentials: missing {', '.join(missing)}")

        form_data = {
            "grant_type": "refresh_token",
            "client_id": source_creds["client_id"],
            "client_secret": source_creds["client_secret"],
            "refresh_token": source_creds["refresh_token"],
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        source_token_uri = source_creds.get("token_uri", "https://oauth2.googleapis.com/token")
        resp = await self.session.post(source_token_uri, data=form_data, headers=headers, timeout=timeout)
        source_data = resp.json()
        if "access_token" not in source_data:
            raise ValueError("Failed to get source access token")
        source_token = source_data["access_token"]

        # Step 2: Use source token to get impersonated token via IAM Credentials API
        impersonate_headers = {
            "Authorization": f"Bearer {source_token}",
            "Content-Type": "application/json",
        }
        # Build request body with scopes if available
        body: dict[str, Any] = {"lifetime": "3600s"}
        if self.scopes:
            body["scope"] = self.scopes.split()

        body_bytes = orjson.dumps(body)
        resp = await self.session.post(
            self._impersonation_url,
            data=body_bytes,
            headers=impersonate_headers,
            timeout=timeout,
        )
        data = resp.json()
        if "accessToken" not in data:
            raise ValueError(f"Failed to get impersonated token: {data}")

        # Parse expireTime to calculate expires_in
        # Format: "2024-01-21T12:00:00Z"
        expires_in = 3600  # default
        if "expireTime" in data:
            try:
                expire_time = datetime.datetime.fromisoformat(data["expireTime"].replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                expires_in = int((expire_time - now).total_seconds())
            except Exception:
                pass

        return TokenResponse(value=str(data["accessToken"]), expires_in=expires_in)

    async def refresh(self, *, timeout: int) -> TokenResponse:  # type: ignore[override]
        if self.token_type == Type.AUTHORIZED_USER:
            return await self._refresh_authorized_user(timeout)
        if self.token_type == Type.GCE_METADATA:
            return await self._refresh_gce_metadata(timeout)
        if self.token_type == Type.SERVICE_ACCOUNT:
            return await self._refresh_service_account(timeout)
        if self.token_type == Type.IMPERSONATED_SERVICE_ACCOUNT:
            return await self._refresh_impersonated(timeout)
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
        """Get email from credentials (sync). Use get_service_account_email() for metadata."""
        return self.token.service_data.get("client_email")

    async def get_service_account_email(self) -> str | None:
        """Get service account email, fetching from metadata if needed."""
        return await self.token.get_service_account_email()

    async def sign_blob(
        self,
        payload: str | bytes | None,
        *,
        service_account_email: str | None = None,
        delegates: list[str] | None = None,
        timeout: int = 10,
    ) -> dict[str, str]:
        sa_email = service_account_email or await self.get_service_account_email()
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
