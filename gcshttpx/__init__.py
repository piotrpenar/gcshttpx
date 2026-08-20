"""
gcshttpx: Minimal async Google Cloud Storage + Auth client on httpx (HTTP/2).
"""

from .auth import AioSession, IamClient, OffloadLoop, Token, Type, decode, encode
from .storage import Blob, Bucket, ShiftedStreamResponse, Storage, StreamResponse
from .transport import (
    StreamAwareHTTP2Connection,
    StreamAwareHTTPConnection,
    StreamAwarePool,
    StreamAwareTransport,
)

__all__ = [
    "AioSession",
    "IamClient",
    "Token",
    "Type",
    "encode",
    "decode",
    "Storage",
    "Bucket",
    "Blob",
    "StreamResponse",
    "ShiftedStreamResponse",
    "OffloadLoop",
    "StreamAwareHTTP2Connection",
    "StreamAwareHTTPConnection",
    "StreamAwarePool",
    "StreamAwareTransport",
]

__version__ = "0.2.3"
