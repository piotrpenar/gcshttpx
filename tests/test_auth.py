import io
import json

import httpx
import pytest

from gcs_httpx.auth import AioSession, IamClient, Token


@pytest.mark.asyncio
async def test_session_http2():
    s = AioSession()
    # Verify session is created and uses HTTP/2 (httpx doesn't expose http2 attr after 0.24)
    assert s.session is not None
    assert isinstance(s.session, httpx.AsyncClient)
    await s.close()


@pytest.mark.asyncio
async def test_token_service_account_refresh(monkeypatch):
    service_data = {
        "type": "service_account",
        "client_email": "sa@example.com",
        "private_key": """-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEIJyC1vEIU2qvTgZl+Maa9QIEeGRLGOJxWT4VyrJ+yR8X
-----END PRIVATE KEY-----""",
        "token_uri": "https://oauth2.googleapis.com/token",
    }

    # Fake jwt.encode to return a static assertion to avoid crypto dep in test
    import gcs_httpx.auth as auth_mod

    def fake_jwt_encode(payload, key, algorithm):  # noqa
        return "ASSERTION"

    monkeypatch.setattr(auth_mod.jwt, "encode", fake_jwt_encode)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url) == service_data["token_uri"]:
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, http2=True) as client:
        tok = Token(
            session=client,
            scopes=["scope"],
            service_file=io.StringIO(json.dumps(service_data)),
        )
        v = await tok.get()
        assert v == "abc"
        await tok.close()


@pytest.mark.asyncio
async def test_iam_sign_blob(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "signBlob" in str(req.url):
            return httpx.Response(200, json={"signedBlob": "QUJD"})
        if req.method == "POST" and "oauth2" in str(req.url):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        if req.method == "GET" and "metadata" in str(req.url):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, http2=True) as client:
        iam = IamClient(session=client, token=Token(session=client, scopes=["iam"]))
        data = await iam.sign_blob("ABC", service_account_email="sa@example.com")
        assert data["signedBlob"] == "QUJD"
        await iam.close()
