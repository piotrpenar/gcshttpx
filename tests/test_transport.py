import asyncio

import hpack
import httpcore
import hyperframe.frame
import pytest

from gcshttpx.auth import AioSession, OffloadLoop, ShiftedAioSession
from gcshttpx.transport import (
    StreamAwareHTTP2Connection,
    StreamAwarePool,
    StreamAwareTransport,
)

ORIGIN = httpcore.Origin(b"http", b"example.com", 80)

RST_STREAM_FRAME_TYPE = 0x3


def frame_types(raw: bytes) -> list[int]:
    """Frame type byte of every HTTP/2 frame in a raw byte sequence."""
    types = []
    index = 0
    while index + 9 <= len(raw):
        length = int.from_bytes(raw[index : index + 3], "big")
        types.append(raw[index + 3])
        index += 9 + length
    return types


def h2_server_bytes(max_concurrent_streams: int | None = None) -> list[bytes]:
    """Server-side byte script: SETTINGS, then response HEADERS for stream 1
    left open (no END_STREAM), so the client-side stream stays in flight."""
    settings = {}
    if max_concurrent_streams is not None:
        settings[hyperframe.frame.SettingsFrame.MAX_CONCURRENT_STREAMS] = max_concurrent_streams
    return [
        hyperframe.frame.SettingsFrame(settings=settings).serialize(),
        hyperframe.frame.HeadersFrame(
            stream_id=1,
            data=hpack.Encoder().encode([(b":status", b"200")]),
            flags=["END_HEADERS"],
        ).serialize(),
    ]


class HandshakeThenBlockedWriteStream(httpcore.AsyncNetworkStream):
    """Lets the HTTP/2 connection init write pass, then blocks every later
    write, signalling when it has — so a request can be cancelled exactly
    after its headers were handed to h2 but before they reached the network."""

    def __init__(self) -> None:
        self._handshake_complete = False
        self.blocked = asyncio.Event()

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not self._handshake_complete:
            self._handshake_complete = True
            return
        self.blocked.set()
        await asyncio.sleep(999)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        await asyncio.sleep(999)  # pragma: no cover
        return b""  # pragma: no cover

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_cancelled_request_resets_h2_stream():
    """A cancelled request must reset its h2 stream; otherwise h2 keeps
    counting it as open ('phantom' stream) while the semaphore slot is free,
    and the drift ends in TooManyStreamsError (encode/httpcore#1022)."""
    stream = HandshakeThenBlockedWriteStream()
    async with StreamAwareHTTP2Connection(ORIGIN, stream) as conn:
        task = asyncio.create_task(conn.request("GET", "http://example.com/"))
        await asyncio.wait_for(stream.blocked.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert conn._h2_state.open_outbound_streams == 0


@pytest.mark.asyncio
async def test_abandoned_stream_body_resets_h2_stream():
    """Closing a streamed response before END_STREAM must reset the h2 stream
    and make the connection available again."""
    stream = httpcore.AsyncMockStream(h2_server_bytes())
    async with StreamAwareHTTP2Connection(ORIGIN, stream) as conn:
        async with conn.stream("GET", "http://example.com/") as response:
            assert response.status == 200
        # The body was never read to END_STREAM; exit abandons the stream.
        assert conn._h2_state.open_outbound_streams == 0
        assert conn.is_available()
        # The abandonment queued an actual RST_STREAM frame for the wire.
        assert RST_STREAM_FRAME_TYPE in frame_types(conn._h2_state.data_to_send())


@pytest.mark.asyncio
async def test_normal_completion_skips_reset():
    """A body read to END_STREAM completes untouched: no reset, no phantom."""
    server_bytes = h2_server_bytes()
    server_bytes.append(
        hyperframe.frame.DataFrame(
            stream_id=1, data=b"hello", flags=["END_STREAM"]
        ).serialize()
    )
    stream = httpcore.AsyncMockStream(server_bytes)
    async with StreamAwareHTTP2Connection(ORIGIN, stream) as conn:
        response = await conn.request("GET", "http://example.com/")
        assert response.status == 200
        assert response.content == b"hello"
        assert conn._h2_state.open_outbound_streams == 0
        assert conn.is_available()
        assert RST_STREAM_FRAME_TYPE not in frame_types(conn._h2_state.data_to_send())


@pytest.mark.asyncio
async def test_is_available_false_at_stream_ceiling():
    """At the peer's concurrent-stream ceiling the connection reports itself
    unavailable, so a pool dials past it instead of queueing on it."""
    stream = httpcore.AsyncMockStream(h2_server_bytes(max_concurrent_streams=1))
    async with StreamAwareHTTP2Connection(ORIGIN, stream) as conn:
        async with conn.stream("GET", "http://example.com/") as response:
            assert response.status == 200
            assert not conn.is_available()
        assert conn.is_available()


@pytest.mark.asyncio
async def test_pool_dials_second_connection_when_first_saturated():
    """With stream-aware connections, a saturated connection makes the pool
    open a second one instead of piling every request onto the first."""
    backend = httpcore.AsyncMockBackend(h2_server_bytes(max_concurrent_streams=1), http2=True)
    async with StreamAwarePool(network_backend=backend, http2=True) as pool:
        cm1 = pool.stream("GET", "https://example.com/")
        response1 = await cm1.__aenter__()
        assert response1.status == 200
        cm2 = pool.stream("GET", "https://example.com/")
        response2 = await cm2.__aenter__()
        assert response2.status == 200

        assert len(pool.connections) == 2

        await cm2.__aexit__(None, None, None)
        await cm1.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_aiosession_lazy_client_uses_stream_aware_transport():
    session = AioSession()
    try:
        assert isinstance(session.session._transport, StreamAwareTransport)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_shifted_session_side_loop_client_uses_stream_aware_transport():
    """The production data path builds its client on the offload side loop;
    that client must carry the stream-aware transport too."""
    offload = OffloadLoop()
    shifted = ShiftedAioSession(offload)

    async def side_loop_client() -> object:
        return shifted.session._transport

    try:
        transport = await offload.submit(side_loop_client())
        assert isinstance(transport, StreamAwareTransport)
    finally:
        await offload.close()
