"""
gcshttpx: Minimal async Google Cloud Storage + Auth client on httpx (HTTP/2).
"""

from .auth import AioSession, IamClient, OffloadLoop, Token, Type, decode, encode
from .storage import Blob, Bucket, Storage, StreamResponse

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
    "OffloadLoop",
]

__version__ = "0.2.0"
