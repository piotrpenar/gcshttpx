import asyncio
import gzip
import io
import threading

import httpx
import pytest

from gcshttpx import OffloadLoop, ShiftedStreamResponse, Storage
from gcshttpx.auth import AioSession, ShiftedAioSession

OFFLOAD_THREAD = "gcshttpx-offload"


def install_mock_client(monkeypatch, handler):
    """Route every lazily-created AioSession client through a MockTransport,
    recording which thread constructed each client."""
    mock = httpx.MockTransport(handler)
    created_on: list[int] = []

    def _lazy_session(self: AioSession) -> httpx.AsyncClient:
        if not self._session:
            created_on.append(threading.get_ident())
            self._session = httpx.AsyncClient(transport=mock)
        return self._session

    monkeypatch.setattr(AioSession, "session", property(_lazy_session))
    return created_on


@pytest.mark.asyncio
async def test_verbs_and_error_contract(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/boom":
            return httpx.Response(503, text="unavailable")
        return httpx.Response(
            200, json={"method": req.method, "body": req.content.decode() or None}
        )

    install_mock_client(monkeypatch, handler)
    offload = OffloadLoop()
    session = ShiftedAioSession(offload)
    try:
        resp = await session.get("http://t/x", params={"a": "1"})
        assert resp.json()["method"] == "GET"

        resp = await session.post("http://t/x", data=b"payload")
        assert resp.json() == {"method": "POST", "body": "payload"}

        # File-likes are materialized exactly like on the loop path.
        resp = await session.put("http://t/x", data=io.BytesIO(b"streamed"))
        assert resp.json() == {"method": "PUT", "body": "streamed"}

        resp = await session.patch("http://t/x", data=b"delta")
        assert resp.json() == {"method": "PATCH", "body": "delta"}

        resp = await session.delete("http://t/x")
        assert resp.json()["method"] == "DELETE"

        with pytest.raises(httpx.HTTPStatusError) as err:
            await session.get("http://t/boom")
        assert err.value.response.status_code == 503
        assert "unavailable" in str(err.value)
    finally:
        await offload.close()


@pytest.mark.asyncio
async def test_requests_and_lazy_client_land_on_offload_thread(monkeypatch):
    handled_on: list[tuple[int, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        thread = threading.current_thread()
        handled_on.append((thread.ident, thread.name))
        return httpx.Response(200, json={})

    created_on = install_mock_client(monkeypatch, handler)
    offload = OffloadLoop()
    session = ShiftedAioSession(offload)
    try:
        await session.get("http://t/x")
        await session.get("http://t/x")
    finally:
        await offload.close()
    idents = {ident for ident, _ in handled_on}
    assert len(idents) == 1
    assert threading.get_ident() not in idents
    assert all(name == OFFLOAD_THREAD for _, name in handled_on)
    # The httpx.AsyncClient was built lazily INSIDE the side loop, once.
    assert created_on == [next(iter(idents))]


@pytest.mark.asyncio
async def test_cancellation_aborts_side_loop_request(monkeypatch):
    request_started = threading.Event()
    cancelled_seen = threading.Event()
    responded = threading.Event()

    async def handler(req: httpx.Request) -> httpx.Response:
        request_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled_seen.set()
            raise
        responded.set()
        return httpx.Response(200, json={})

    install_mock_client(monkeypatch, handler)
    offload = OffloadLoop()
    session = ShiftedAioSession(offload)
    try:
        task = asyncio.ensure_future(session.get("http://t/slow"))
        assert await asyncio.to_thread(request_started.wait, 2)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, timeout=0.1)
        # Cancelling the awaiting task aborted the request ON the side loop.
        assert await asyncio.to_thread(cancelled_seen.wait, 2)
        assert not responded.is_set()
        assert task.cancelled()
    finally:
        await offload.close()


@pytest.mark.asyncio
async def test_explicit_session_bypasses_offload_including_resumable_initiation(
    monkeypatch,
):
    offload_hits: list[str] = []

    def offload_handler(req: httpx.Request) -> httpx.Response:
        offload_hits.append(req.method)
        return httpx.Response(500, text="wrong path")

    install_mock_client(monkeypatch, offload_handler)

    caller_hits: list[str] = []

    def caller_handler(req: httpx.Request) -> httpx.Response:
        caller_hits.append(f"{req.method} {req.url.path}")
        if req.url.params.get("uploadType") == "resumable":
            return httpx.Response(
                200, headers={"Location": "http://test/upload-session"}
            )
        return httpx.Response(200, json={"name": "obj"})

    storage = Storage(api_root="http://test", offload=True)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(caller_handler)
        ) as loop_client:
            result = await storage.upload(
                "bkt",
                "obj",
                b"x" * 16,
                force_resumable_upload=True,
                session=loop_client,
            )
        assert result == {"name": "obj"}
        # Both the initiation POST and the PUT hit the caller's session.
        assert caller_hits == [
            "POST /upload/storage/v1/b/bkt/o",
            "PUT /upload-session",
        ]
        assert offload_hits == []
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_precondition_412_fails_immediately_without_retries(monkeypatch):
    puts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, headers={"Location": "http://test/u1"})
        puts.append(req.method)
        return httpx.Response(412, text="conditionNotMet")

    install_mock_client(monkeypatch, handler)
    storage = Storage(api_root="http://test")
    try:
        with pytest.raises(httpx.HTTPStatusError) as err:
            await storage.upload(
                "bkt", "obj", b"data", force_resumable_upload=True, if_generation_match=0
            )
        assert err.value.response.status_code == 412
        assert puts == ["PUT"]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_resumable_upload_still_retries_server_errors(monkeypatch):
    attempts = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, headers={"Location": "http://test/u1"})
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"name": "obj"})

    install_mock_client(monkeypatch, handler)
    storage = Storage(api_root="http://test")
    try:
        result = await storage.upload("bkt", "obj", b"data", force_resumable_upload=True)
        assert result == {"name": "obj"}
        assert attempts["n"] == 3
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_list_buckets_sends_project_query_param(monkeypatch):
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"items": [{"id": "b1"}]})

    install_mock_client(monkeypatch, handler)
    storage = Storage(api_root="http://test")
    try:
        buckets = await storage.list_buckets("my-proj")
        assert [b.name for b in buckets] == ["b1"]
        assert seen["path"] == "/storage/v1/b"
        assert seen["params"]["project"] == "my-proj"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_shared_offload_survives_first_member_close(monkeypatch):
    names: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        names.append(threading.current_thread().name)
        return httpx.Response(200, json={"items": []})

    install_mock_client(monkeypatch, handler)
    shared = OffloadLoop()
    s1 = Storage(api_root="http://test", offload=shared)
    s2 = Storage(api_root="http://test", offload=shared)
    try:
        assert await s1.list_objects("bkt") == {"items": []}
        assert s1._owns_offload is False
        await s1.close()
        # The shared loop keeps serving other members after one closes.
        assert await s2.list_objects("bkt") == {"items": []}
        assert names == [OFFLOAD_THREAD, OFFLOAD_THREAD]
    finally:
        await s2.close()
        await shared.close()


@pytest.mark.asyncio
async def test_owned_offload_closes_with_its_storage(monkeypatch):
    install_mock_client(monkeypatch, lambda req: httpx.Response(200, json={}))
    storage = Storage(api_root="http://test", offload=True)
    offload = storage._offload
    assert storage._owns_offload is True
    await storage.list_objects("bkt")
    await storage.close()
    with pytest.raises(RuntimeError):
        await offload.submit(asyncio.sleep(0))


@pytest.mark.asyncio
async def test_env_opt_in_matrix(monkeypatch):
    monkeypatch.setenv("GCSHTTPX_OFFLOAD", "1")
    s = Storage(api_root="http://test")
    assert s._offload is not None
    assert s._owns_offload is True
    await s.close()

    # A caller-provided shared session disables the env opt-in.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    ) as shared_session:
        s2 = Storage(api_root="http://test", session=shared_session)
        assert s2._offload is None
        await s2.close()

    monkeypatch.delenv("GCSHTTPX_OFFLOAD")
    s3 = Storage(api_root="http://test")
    assert s3._offload is None
    await s3.close()


@pytest.mark.asyncio
async def test_upload_zipped_through_offload(monkeypatch):
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["params"] = dict(req.url.params)
        captured["body"] = req.content
        return httpx.Response(200, json={"name": "obj", "bucket": "bkt"})

    install_mock_client(monkeypatch, handler)
    storage = Storage(api_root="http://test", offload=True)
    try:
        await storage.upload("bkt", "obj", b"a" * 1024, zipped=True, if_generation_match=0)
        assert captured["params"]["contentEncoding"] == "gzip"
        assert captured["params"]["ifGenerationMatch"] == "0"
        assert gzip.decompress(captured["body"]) == b"a" * 1024
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_download_stream_rides_the_offload_loop_and_decodes_there(monkeypatch):
    names: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        names.append(threading.current_thread().name)
        return httpx.Response(
            200,
            content=gzip.compress(b"payload"),
            headers={"content-encoding": "gzip"},
        )

    install_mock_client(monkeypatch, handler)
    storage = Storage(api_root="http://test", offload=True)
    try:
        async with await storage.download_stream("bkt", "obj") as stream:
            assert isinstance(stream, ShiftedStreamResponse)
            assert stream.content_encoding == "gzip"
            # The request, and therefore transport + decoding, ran on the side loop.
            assert names == [OFFLOAD_THREAD]
            got = b""
            while True:
                chunk = await stream.read(4)
                if not chunk:
                    break
                got += chunk
            assert got == b"payload"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_shifted_stream_read_sync_from_worker_thread(monkeypatch):
    payload = gzip.compress(b"worker-payload")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload, headers={"content-encoding": "gzip"}
        )

    install_mock_client(monkeypatch, handler)
    storage = Storage(api_root="http://test", offload=True)
    try:
        stream = await storage.download_stream("bkt", "obj")
        assert isinstance(stream, ShiftedStreamResponse)

        chunks: list[bytes] = []

        def drain() -> None:
            while True:
                chunk = stream.read_sync(4)
                if not chunk:
                    return
                chunks.append(chunk)

        await asyncio.to_thread(drain)
        assert b"".join(chunks) == b"worker-payload"
        await stream.aclose()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_download_stream_with_explicit_session_stays_on_caller_loop(monkeypatch):
    names: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        names.append(threading.current_thread().name)
        return httpx.Response(200, content=b"payload")

    install_mock_client(monkeypatch, handler)
    offload = OffloadLoop()
    caller_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    storage = Storage(api_root="http://test", offload=offload)
    try:
        stream = await storage.download_stream("bkt", "obj", session=caller_client)
        assert not isinstance(stream, ShiftedStreamResponse)
        assert names == [threading.current_thread().name]
        assert await stream.read() == b"payload"
        await stream.aclose()
    finally:
        await caller_client.aclose()
        await storage.close()
        await offload.close()


@pytest.mark.asyncio
async def test_offload_limits_shape_the_side_loop_client():
    offload = OffloadLoop()
    storage = Storage(
        api_root="http://test",
        offload=offload,
        offload_limits=httpx.Limits(max_connections=7, keepalive_expiry=3.0),
    )
    try:
        shifted = storage._shifted
        assert shifted is not None
        client = await offload.submit(_client_of(shifted))
        pool = client._transport._pool
        assert pool._max_connections == 7
        assert pool._keepalive_expiry == 3.0
    finally:
        await storage.close()
        await offload.close()


async def _client_of(session: AioSession) -> httpx.AsyncClient:
    return session.session
