"""
Stream-aware HTTP/2 transport for httpx/httpcore.

Fixes two httpcore defects that poison HTTP/2 connections under concurrent
load against GCS, which caps one connection at 100 concurrent streams:

1. An abandoned request (timeout, cancellation, early close) releases
   httpcore's stream slot without sending RST_STREAM, so h2 keeps counting the
   stream as open. These phantom streams accumulate until the connection
   rejects every new request with "Max outbound streams is 100, 100 open".
   Mirrors the open upstream fix https://github.com/encode/httpcore/pull/1098.

2. The connection pool never dials another connection while one is merely
   stream-saturated: ``is_available()`` ignores the open-stream count, so all
   requests pile onto a single connection and queue behind its stream ceiling.
   Slim variant of the open upstream fix
   https://github.com/encode/httpcore/pull/1088. Limitation: it only helps
   requests that arrive while streams are actually open — a burst assigned in
   a single pool pass still lands entirely on one connection, because no
   stream opens until each request's headers are later sent. Fix 1 is the
   load-bearing production fix; this spreads sustained load, not burst skew.

Delete this module and drop the httpcore pin once both fixes ship in an
httpcore release.
"""

from __future__ import annotations

import contextlib

import h2.exceptions
import httpcore
import httpx

# Public exports
__all__ = [
    "StreamAwareHTTP2Connection",
    "StreamAwareHTTPConnection",
    "StreamAwarePool",
    "StreamAwareTransport",
]

_DEFAULT_LIMITS = httpx.Limits(
    max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0
)


class StreamAwareHTTP2Connection(httpcore.AsyncHTTP2Connection):
    """AsyncHTTP2Connection that resets abandoned streams and reports itself
    unavailable at the peer's concurrent-stream ceiling."""

    def is_available(self) -> bool:
        if not super().is_available():
            return False
        if not self._sent_connection_init:
            # _max_streams only exists after the connection init handshake.
            return True
        return self._h2_state.open_outbound_streams < self._max_streams

    async def _response_closed(self, stream_id: int) -> None:
        # httpcore#1098: an early teardown (cancelled or abandoned request)
        # must reset the stream, or h2 counts it as open forever while the
        # stream semaphore slot is already free -- the drift ends in
        # TooManyStreamsError once phantoms + live streams reach the ceiling.
        # The RST_STREAM frame is queued, not flushed: this can run inside a
        # cancellation shield, and taking the write lock here risks deadlock
        # against the request currently holding it. The next
        # _write_outgoing_data() call sends the queued frame.
        h2_stream = self._h2_state.streams.get(stream_id)
        if h2_stream is not None and not h2_stream.closed:
            # ProtocolError means the remote end already reset the stream.
            with contextlib.suppress(h2.exceptions.ProtocolError):
                self._h2_state.reset_stream(stream_id)
        await super()._response_closed(stream_id)


class StreamAwareHTTPConnection(httpcore.AsyncHTTPConnection):
    """AsyncHTTPConnection whose HTTP/2 connections are stream-aware.

    ``handle_async_request`` is copied verbatim from httpcore 1.0.9
    (httpcore/_async/connection.py) with one change: the HTTP/2 connection
    class is StreamAwareHTTP2Connection. httpcore hardcodes the class inside
    this method, so there is no narrower override point.
    """

    async def handle_async_request(self, request: httpcore.Request) -> httpcore.Response:
        if not self.can_handle_request(request.url.origin):
            raise RuntimeError(
                f"Attempted to send request to {request.url.origin} "
                f"on connection to {self._origin}"
            )

        try:
            async with self._request_lock:
                if self._connection is None:
                    stream = await self._connect(request)

                    ssl_object = stream.get_extra_info("ssl_object")
                    http2_negotiated = (
                        ssl_object is not None
                        and ssl_object.selected_alpn_protocol() == "h2"
                    )
                    if http2_negotiated or (self._http2 and not self._http1):
                        self._connection = StreamAwareHTTP2Connection(
                            origin=self._origin,
                            stream=stream,
                            keepalive_expiry=self._keepalive_expiry,
                        )
                    else:
                        self._connection = httpcore.AsyncHTTP11Connection(
                            origin=self._origin,
                            stream=stream,
                            keepalive_expiry=self._keepalive_expiry,
                        )
        except BaseException as exc:
            self._connect_failed = True
            raise exc

        return await self._connection.handle_async_request(request)


class StreamAwarePool(httpcore.AsyncConnectionPool):
    """AsyncConnectionPool that dials stream-aware connections.

    With StreamAwareHTTP2Connection reporting unavailable at its stream
    ceiling, the pool's normal assignment logic dials the next connection
    (up to ``max_connections``) instead of queueing everything on one.
    """

    def create_connection(self, origin: httpcore.Origin) -> httpcore.AsyncConnectionInterface:
        if self._proxy is not None:
            return super().create_connection(origin)
        return StreamAwareHTTPConnection(
            origin=origin,
            ssl_context=self._ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            retries=self._retries,
            local_address=self._local_address,
            uds=self._uds,
            network_backend=self._network_backend,
            socket_options=self._socket_options,
        )


class StreamAwareTransport(httpx.AsyncHTTPTransport):
    """httpx transport backed by a StreamAwarePool.

    httpx.AsyncHTTPTransport only ever touches ``self._pool``, so replacing
    the pool is the entire integration surface. Unsupported vs the stock
    transport: cert, trust_env, proxy, uds, local_address, socket_options —
    callers needing those must pass their own preconfigured session instead.
    Note that passing any explicit transport disables httpx's environment
    proxy mounts (HTTP_PROXY/HTTPS_PROXY/NO_PROXY).
    """

    def __init__(
        self,
        *,
        verify: bool = True,
        limits: httpx.Limits | None = None,
        retries: int = 0,
    ) -> None:
        limits = limits if limits is not None else _DEFAULT_LIMITS
        self._pool = StreamAwarePool(
            ssl_context=httpx.create_ssl_context(verify=verify),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=True,
            retries=retries,
        )
