"""
Google Cloud Storage client using httpx (async, HTTP/2). Feature-parity surface
with a minimal, DRY design. Bucket/Blob helpers are provided for ergonomics.
"""

from __future__ import annotations

import asyncio as _asyncio
import base64
import binascii
import datetime
import gzip
import io
import json
import logging
import mimetypes
import os
from collections.abc import Iterator
from typing import IO, Any, AnyStr
from urllib.parse import quote

import httpx
import orjson
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .auth import AioSession, IamClient, Token

log = logging.getLogger(__name__)

Session = httpx.AsyncClient
ResponseError = httpx.HTTPStatusError

DEFAULT_TIMEOUT = 10
MAX_CONTENT_LENGTH_SIMPLE_UPLOAD = 5 * 1024 * 1024  # 5 MB
SCOPES = [
    "https://www.googleapis.com/auth/devstorage.read_write",
    "https://www.googleapis.com/auth/iam",  # Required for SignBlob (signed URLs)
]


def _init_api_root(api_root: str | None) -> tuple[bool, str]:
    if api_root:
        return True, api_root
    host = os.environ.get("STORAGE_EMULATOR_HOST")
    if host:
        if not host.startswith("http"):
            host = f"http://{host}"
        return True, host
    return False, "https://www.googleapis.com"


def _choose_boundary() -> str:
    return binascii.hexlify(os.urandom(16)).decode("ascii")


def _encode_multipart_formdata(
    fields: list[tuple[dict[str, str], bytes]], boundary: str
) -> tuple[bytes, str]:
    body: list[bytes] = []
    for headers, data in fields:
        body.append(f"--{boundary}\r\n".encode())
        for k in ["Content-Disposition", "Content-Type", "Content-Location"]:
            v = headers.pop(k, None)
            if v:
                body.append(f"{k}: {v}\r\n".encode())
        for k, v in headers.items():
            if v:
                body.append(f"{k}: {v}\r\n".encode())
        body.append(b"\r\n")
        body.append(data)
        body.append(b"\r\n")
    body.append(f"--{boundary}--\r\n".encode())
    return b"".join(body), f"multipart/related; boundary={boundary}"


class StreamResponse:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._iter: Iterator[bytes] | None = None
        self._stream_iter = None

    @property
    def content_length(self) -> int:
        return int(self._response.headers.get("content-length", 0))

    async def read(self, size: int = -1) -> bytes:
        if self._stream_iter is None:
            if size > 0:
                self._stream_iter = self._response.aiter_bytes(chunk_size=size)
            else:
                return await self._response.aread()
        try:
            return await self._stream_iter.__anext__()
        except StopAsyncIteration:
            return b""

    async def __aenter__(self) -> Any:
        return await self._response.__aenter__()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._response.__aexit__(*exc_info)


class Storage:
    _api_root: str
    _api_is_dev: bool
    _api_root_read: str
    _api_root_write: str

    def __init__(
        self,
        *,
        service_file: str | IO[AnyStr] | None = None,
        token: Token | None = None,
        session: Session | None = None,
        api_root: str | None = None,
    ) -> None:
        self._api_is_dev, self._api_root = _init_api_root(api_root)
        self._api_root_read = f"{self._api_root}/storage/v1/b"
        self._api_root_write = f"{self._api_root}/upload/storage/v1/b"

        self.session = AioSession(session, verify_ssl=not self._api_is_dev)
        self.token = token or Token(
            service_file=service_file, scopes=SCOPES, session=self.session.session
        )

    async def _headers(self) -> dict[str, str]:
        if self._api_is_dev:
            return {}
        tok = await self.token.get()
        return {"Authorization": f"Bearer {tok}"}

    async def list_buckets(
        self,
        project: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, Any] | None = None,
        session: Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> list[Bucket]:
        url = f"{self._api_root_read}?project={project}"
        headers = {**(headers or {}), **(await self._headers())}
        params = dict(params or {})
        if not params.get("pageToken"):
            params["pageToken"] = ""
        s = AioSession(session) if session else self.session
        buckets: list[Bucket] = []
        while True:
            resp = await s.get(url, headers=headers, params=params, timeout=timeout)
            data = resp.json()
            for item in data.get("items", []):
                buckets.append(Bucket(self, item["id"]))
            params["pageToken"] = data.get("nextPageToken", "")
            if not params["pageToken"]:
                break
        return buckets

    def get_bucket(self, bucket_name: str) -> Bucket:
        return Bucket(self, bucket_name)

    async def copy(
        self,
        bucket: str,
        object_name: str,
        destination_bucket: str,
        *,
        new_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        session: Session | None = None,
    ) -> dict[str, Any]:
        new_name = new_name or object_name
        url = (
            f"{self._api_root_read}/{bucket}/o/"
            f"{quote(object_name, safe='')}/rewriteTo/b/"
            f"{destination_bucket}/o/{quote(new_name, safe='')}"
        )
        metadict = {
            self._format_metadata_key(k): v for k, v in dict(metadata or {}).items()
        }
        if "metadata" in metadict:
            metadict["metadata"] = {
                str(k): (str(v) if v is not None else None)
                for k, v in metadict["metadata"].items()
            }
        body = orjson.dumps(metadict)
        headers = {**(headers or {}), **(await self._headers())}
        headers.update(
            {
                "Content-Type": "application/json; charset=UTF-8",
                "Content-Length": str(len(body)),
            }
        )
        s = AioSession(session) if session else self.session
        params = params or {}
        resp = await s.post(
            url, headers=headers, params=params, timeout=timeout, data=body
        )
        data: dict[str, Any] = resp.json()
        while not data.get("done") and data.get("rewriteToken"):
            params["rewriteToken"] = data["rewriteToken"]
            resp = await s.post(
                url, headers=headers, params=params, timeout=timeout, data=body
            )
            data = resp.json()
        return data

    async def delete(
        self,
        bucket: str,
        object_name: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        session: Session | None = None,
    ) -> str:
        encoded = quote(object_name, safe="")
        url = f"{self._api_root_read}/{bucket}/o/{encoded}"
        headers = {**(headers or {}), **(await self._headers())}
        s = AioSession(session) if session else self.session
        resp = await s.delete(
            url, headers=headers, params=params or {}, timeout=timeout
        )
        try:
            return resp.text  # type: ignore[return-value]
        except Exception:
            return str(resp.text)

    async def download(
        self,
        bucket: str,
        object_name: str,
        *,
        headers: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        session: Session | None = None,
    ) -> bytes:
        return await self._download(
            bucket,
            object_name,
            headers=headers,
            timeout=timeout,
            params={"alt": "media"},
            session=session,
        )

    async def download_to_filename(
        self, bucket: str, object_name: str, filename: str, **kwargs: Any
    ) -> None:
        data = await self.download(bucket, object_name, **kwargs)

        def _write() -> None:
            with open(filename, "wb+") as f:
                f.write(data)

        await _asyncio.to_thread(_write)

    async def download_metadata(
        self,
        bucket: str,
        object_name: str,
        *,
        headers: dict[str, Any] | None = None,
        session: Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        data = await self._download(
            bucket,
            object_name,
            headers=headers,
            timeout=timeout,
            params={"alt": "json"},
            session=session,
        )
        return json.loads(data.decode())

    async def download_stream(
        self,
        bucket: str,
        object_name: str,
        *,
        headers: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        session: Session | None = None,
    ) -> StreamResponse:
        return await self._download_stream(
            bucket,
            object_name,
            headers=headers,
            timeout=timeout,
            params={"alt": "media"},
            session=session,
        )

    async def list_objects(
        self,
        bucket: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, Any] | None = None,
        session: Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        url = f"{self._api_root_read}/{bucket}/o"
        headers = {**(headers or {}), **(await self._headers())}
        s = AioSession(session) if session else self.session
        resp = await s.get(url, headers=headers, params=params or {}, timeout=timeout)
        return resp.json()

    async def upload(
        self,
        bucket: str,
        object_name: str,
        file_data: Any,
        *,
        content_type: str | None = None,
        parameters: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        metadata: dict[str, Any] | None = None,
        session: Session | None = None,
        force_resumable_upload: bool | None = None,
        zipped: bool = False,
        timeout: int = 30,
    ) -> dict[str, Any]:
        url = f"{self._api_root_write}/{bucket}/o"
        stream = self._preprocess_data(file_data)
        params = dict(parameters or {})
        if zipped:
            stream = self._compress_file_in_chunks(stream)
            params["contentEncoding"] = "gzip"
        content_length = self._get_stream_len(stream)
        content_type = content_type or mimetypes.guess_type(object_name)[0] or ""
        headers = {**(headers or {}), **(await self._headers())}
        headers.update(
            {"Content-Length": str(content_length), "Content-Type": content_type}
        )
        upload_type = self._decide_upload_type(force_resumable_upload, content_length)
        if upload_type == "resumable":
            return await self._upload_resumable(
                url,
                object_name,
                stream,
                params,
                headers,
                metadata=metadata,
                session=session,
                timeout=timeout,
            )
        if upload_type == "simple":
            if metadata:
                return await self._upload_multipart(
                    url,
                    object_name,
                    stream,
                    params,
                    headers,
                    metadata or {},
                    session=session,
                    timeout=timeout,
                )
            return await self._upload_simple(
                url,
                object_name,
                stream,
                params,
                headers,
                session=session,
                timeout=timeout,
            )
        raise TypeError("unsupported upload type")

    async def upload_from_filename(
        self, bucket: str, object_name: str, filename: str, **kwargs: Any
    ) -> dict[str, Any]:
        def _read() -> bytes:
            with open(filename, "rb") as f:
                return f.read()

        contents = await _asyncio.to_thread(_read)
        return await self.upload(bucket, object_name, contents, **kwargs)

    async def compose(
        self,
        bucket: str,
        object_name: str,
        source_object_names: list[str],
        *,
        content_type: str | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, Any] | None = None,
        session: Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        url = f"{self._api_root_read}/{bucket}/o/{quote(object_name, safe='')}/compose"
        headers = {**(headers or {}), **(await self._headers())}
        payload: dict[str, Any] = {
            "sourceObjects": [{"name": n} for n in source_object_names]
        }
        if content_type:
            payload["destination"] = {"contentType": content_type}
        body = orjson.dumps(payload)
        headers.update(
            {
                "Content-Type": "application/json; charset=UTF-8",
                "Content-Length": str(len(body)),
            }
        )
        s = AioSession(session) if session else self.session
        resp = await s.post(
            url, headers=headers, params=params or {}, timeout=timeout, data=body
        )
        return resp.json()

    @staticmethod
    def _get_stream_len(stream: IO[AnyStr]) -> int:
        current = stream.tell()
        try:
            return stream.seek(0, os.SEEK_END)
        finally:
            stream.seek(current)

    @staticmethod
    def _preprocess_data(data: Any) -> IO[Any]:
        if data is None:
            return io.StringIO("")
        if isinstance(data, bytes):
            return io.BytesIO(data)
        if isinstance(data, str):
            return io.StringIO(data)
        if isinstance(data, io.IOBase):
            return data  # type: ignore[return-value]
        raise TypeError(f"unsupported upload type: {type(data)!r}")

    @staticmethod
    def _compress_file_in_chunks(
        input_stream: IO[AnyStr], chunk_size: int = 8192
    ) -> IO[bytes]:
        out = io.BytesIO()
        with gzip.open(out, "wb") as gz:
            while True:
                chunk = input_stream.read(chunk_size)
                if not chunk:
                    break
                chunk_b = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                gz.write(chunk_b)
        out.seek(0)
        return out

    @staticmethod
    def _decide_upload_type(
        force_resumable_upload: bool | None, content_length: int
    ) -> str:
        if force_resumable_upload is True:
            return "resumable"
        if force_resumable_upload is False:
            return "simple"
        return (
            "resumable"
            if content_length > MAX_CONTENT_LENGTH_SIMPLE_UPLOAD
            else "simple"
        )

    @staticmethod
    def _split_content_type(content_type: str) -> tuple[str, str | None]:
        parts = content_type.split(";")
        ctype = parts[0].lower().strip()
        encoding = None
        if len(parts) > 1:
            encoding_str = parts[1].lower().strip()
            encoding = encoding_str.split("=")[-1]
        return ctype, encoding

    @staticmethod
    def _format_metadata_key(key: str) -> str:
        parts = key.split("-")
        parts = [parts[0].lower()] + [p.capitalize() for p in parts[1:]]
        return "".join(parts)

    async def _download(
        self,
        bucket: str,
        object_name: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        session: Session | None = None,
    ) -> bytes:
        url = f"{self._api_root_read}/{bucket}/o/{quote(object_name, safe='')}"
        headers = {**(headers or {}), **(await self._headers())}
        s = AioSession(session) if session else self.session
        resp = await s.get(url, headers=headers, params=params or {}, timeout=timeout)
        return resp.content

    async def _download_stream(
        self,
        bucket: str,
        object_name: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        session: Session | None = None,
    ) -> StreamResponse:
        url = f"{self._api_root_read}/{bucket}/o/{quote(object_name, safe='')}"
        headers = {**(headers or {}), **(await self._headers())}
        s = AioSession(session) if session else self.session
        resp = await s.get(url, headers=headers, params=params or {}, timeout=timeout)
        return StreamResponse(resp)

    async def _upload_simple(
        self,
        url: str,
        object_name: str,
        stream: IO[AnyStr],
        params: dict[str, str],
        headers: dict[str, str],
        *,
        session: Session | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        params = dict(params)
        params["name"] = object_name
        params["uploadType"] = "media"
        s = self.session if not session else AioSession(session)
        resp = await s.post(
            url, data=stream, headers=headers, params=params, timeout=timeout
        )
        return resp.json()

    async def _upload_multipart(
        self,
        url: str,
        object_name: str,
        stream: IO[AnyStr],
        params: dict[str, str],
        headers: dict[str, str],
        metadata: dict[str, Any],
        *,
        session: Session | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        params = dict(params)
        params["uploadType"] = "multipart"
        metadata_headers = {"Content-Type": "application/json; charset=UTF-8"}
        metadata = {self._format_metadata_key(k): v for k, v in metadata.items()}
        if "metadata" in metadata:
            metadata["metadata"] = {
                str(k): (str(v) if v is not None else None)
                for k, v in metadata["metadata"].items()
            }
        metadata["name"] = object_name
        raw_body: AnyStr = stream.read()
        bytes_body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
        parts = [
            (metadata_headers, orjson.dumps(metadata)),
            (
                {
                    "Content-Type": headers.get(
                        "Content-Type", "application/octet-stream"
                    )
                },
                bytes_body,
            ),
        ]
        boundary = _choose_boundary()
        body, content_type = _encode_multipart_formdata(parts, boundary)
        headers = dict(headers)
        headers.update(
            {
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Accept": "application/json",
            }
        )
        s = self.session if not session else AioSession(session)
        resp = await s.post(
            url, data=body, headers=headers, params=params, timeout=timeout
        )
        return resp.json()

    async def _upload_resumable(
        self,
        url: str,
        object_name: str,
        stream: IO[AnyStr],
        params: dict[str, str],
        headers: dict[str, str],
        *,
        metadata: dict[str, Any] | None = None,
        session: Session | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        session_uri = await self._initiate_upload(
            url, object_name, params, headers, metadata=metadata
        )
        return await self._do_upload(
            session_uri, stream, headers=headers, session=session, timeout=timeout
        )

    async def _initiate_upload(
        self,
        url: str,
        object_name: str,
        params: dict[str, str],
        headers: dict[str, str],
        *,
        metadata: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        session: Session | None = None,
    ) -> str:
        params = dict(params)
        params["uploadType"] = "resumable"
        metadict = {
            self._format_metadata_key(k): v for k, v in dict(metadata or {}).items()
        }
        if "metadata" in metadict:
            metadict["metadata"] = {
                str(k): (str(v) if v is not None else None)
                for k, v in metadict["metadata"].items()
            }
        metadict.update({"name": object_name})
        body = orjson.dumps(metadict)
        post_headers = dict(headers)
        post_headers.update(
            {
                "Content-Type": "application/json; charset=UTF-8",
                "Content-Length": str(len(body)),
                "X-Upload-Content-Type": headers.get(
                    "Content-Type", "application/octet-stream"
                ),
                "X-Upload-Content-Length": headers.get("Content-Length", "0"),
            }
        )
        s = self.session if not session else AioSession(session)
        resp = await s.post(
            url, headers=post_headers, params=params, data=body, timeout=timeout
        )
        return resp.headers["Location"]

    async def _do_upload(
        self,
        session_uri: str,
        stream: IO[AnyStr],
        headers: dict[str, str],
        *,
        retries: int = 5,
        session: Session | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        s = self.session if not session else AioSession(session)
        original_close = stream.close
        original_position = stream.tell()
        stream.close = lambda: None  # type: ignore[assignment]
        resp: httpx.Response | None = None
        try:
            for attempt in range(retries):
                try:
                    stream.seek(original_position)
                    content = stream.read()
                    resp = await s.put(
                        session_uri, headers=headers, data=content, timeout=timeout
                    )
                except ResponseError:
                    if attempt == retries - 1:
                        raise
                else:
                    break
        finally:
            original_close()
        if resp is None:
            raise RuntimeError("upload failed with no response")
        return resp.json()

    async def patch_metadata(
        self,
        bucket: str,
        object_name: str,
        metadata: dict[str, Any],
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        session: Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        encoded = quote(object_name, safe="")
        url = f"{self._api_root_read}/{bucket}/o/{encoded}"
        params = params or {}
        headers = {**(headers or {}), **(await self._headers())}
        headers["Content-Type"] = "application/json"
        body = orjson.dumps(metadata)
        s = AioSession(session) if session else self.session
        resp = await s.patch(
            url, data=body, headers=headers, params=params, timeout=timeout
        )
        return resp.json()

    async def get_bucket_metadata(
        self,
        bucket: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        session: Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        url = f"{self._api_root_read}/{bucket}"
        headers = {**(headers or {}), **(await self._headers())}
        s = AioSession(session) if session else self.session
        resp = await s.get(url, headers=headers, params=params or {}, timeout=timeout)
        return resp.json()

    async def close(self) -> None:
        await self.session.close()

    async def __aenter__(self) -> Storage:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


class Bucket:
    def __init__(self, storage: Storage, name: str) -> None:
        self.storage = storage
        self.name = name

    async def get_blob(
        self,
        blob_name: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        session: Session | None = None,
    ) -> Blob:
        metadata = await self.storage.download_metadata(
            self.name, blob_name, timeout=timeout, session=session
        )
        return Blob(self, blob_name, metadata)

    async def blob_exists(
        self, blob_name: str, *, session: Session | None = None
    ) -> bool:
        try:
            await self.get_blob(blob_name, session=session)
            return True
        except ResponseError as e:  # type: ignore[misc]
            status = (
                getattr(e, "response", None).status_code
                if hasattr(e, "response")
                else None
            )
            code = getattr(e, "status", None) or getattr(e, "code", None)
            if status in {404, 410} or code in {404, 410}:  # type: ignore[operator]
                return False
            raise

    async def list_blobs(
        self,
        prefix: str = "",
        match_glob: str = "",
        delimiter: str = "",
        *,
        session: Session | None = None,
    ) -> list[str]:
        params = {
            "delimiter": delimiter,
            "matchGlob": match_glob,
            "pageToken": "",
            "prefix": prefix,
        }
        items: list[str] = []
        while True:
            content = await self.storage.list_objects(
                self.name, params=params, session=session
            )
            items.extend([x["name"] for x in content.get("items", [])])
            if delimiter:
                items.extend(content.get("prefixes", []))
            params["pageToken"] = content.get("nextPageToken", "")
            if not params["pageToken"]:
                break
        return items

    def new_blob(self, blob_name: str) -> Blob:
        return Blob(self, blob_name, {"size": 0})

    async def get_metadata(
        self, *, params: dict[str, Any] | None = None, session: Session | None = None
    ) -> dict[str, Any]:
        return await self.storage.get_bucket_metadata(
            self.name, params=params, session=session
        )


class Blob:
    def __init__(self, bucket: Bucket, name: str, metadata: dict[str, Any]) -> None:
        metadata = dict(metadata)
        metadata["bucket_name"] = metadata.pop("bucket", "")
        self.__dict__.update(**metadata)
        self.bucket = bucket
        self.name = name
        try:
            self.size = int(self.size)  # type: ignore[attr-defined]
        except Exception:
            self.size = 0

    @property
    def chunk_size(self) -> int:
        # Next multiple of 256KiB
        return self.size + (262144 - (self.size % 262144)) if self.size else 262144

    async def download(
        self,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        session: Session | None = None,
        auto_decompress: bool = True,
    ) -> Any:
        headers = None if auto_decompress else {"accept-encoding": "gzip"}
        return await self.bucket.storage.download(
            self.bucket.name,
            self.name,
            timeout=timeout,
            session=session,
            headers=headers,
        )

    async def upload(
        self,
        data: Any,
        *,
        content_type: str | None = None,
        session: Session | None = None,
    ) -> dict[str, Any]:
        metadata = await self.bucket.storage.upload(
            self.bucket.name,
            self.name,
            data,
            content_type=content_type,
            session=session,
        )
        metadata["bucket_name"] = metadata.pop("bucket", "")
        self.__dict__.update(metadata)
        return metadata

    async def get_signed_url(
        self,
        expiration: int,
        *,
        headers: dict[str, str] | None = None,
        query_params: dict[str, Any] | None = None,
        http_method: str = "GET",
        iam_client: IamClient | None = None,
        service_account_email: str | None = None,
        token: Token | None = None,
        session: Session | None = None,
    ) -> str:
        if expiration > 604800:
            raise ValueError(
                "expiration time can't be longer than 604800 seconds (7 days)"
            )
        quoted_name = quote(self.name, safe=b"/~")
        canonical_uri = f"/{self.bucket.name}/{quoted_name}"
        datetime_now = datetime.datetime.now(datetime.timezone.utc)
        request_timestamp = datetime_now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = datetime_now.strftime("%Y%m%d")
        token = token or self.bucket.storage.token
        # Get service account email - must handle impersonated credentials properly
        client_email = service_account_email or await token.get_service_account_email()
        if not client_email:
            raise ValueError(
                "Cannot determine service account email for signing. "
                "For impersonated credentials, ensure service_account_impersonation_url is set. "
                "For metadata credentials, ensure the metadata server is accessible."
            )
        private_key = (
            token.service_data.get("private_key") if token.service_data else None
        )
        credential_scope = f"{datestamp}/auto/storage/goog4_request"
        credential = f"{client_email}/{credential_scope}"
        host = os.environ.get("STORAGE_EMULATOR_HOST", "storage.googleapis.com")
        headers = dict(headers or {})
        headers["host"] = host
        ordered_headers = {
            k.lower(): str(v).lower()
            for k, v in sorted(headers.items(), key=lambda x: x[0].lower())
        }
        canonical_headers = "".join(f"{k}:{v}\n" for k, v in ordered_headers.items())
        signed_headers = ";".join(ordered_headers.keys())
        query_params = dict(query_params or {})
        query_params.update(
            {
                "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
                "X-Goog-Credential": credential,
                "X-Goog-Date": request_timestamp,
                "X-Goog-Expires": expiration,
                "X-Goog-SignedHeaders": signed_headers,
            }
        )
        ordered_q = "&".join(
            f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
            for k, v in sorted(query_params.items())
        )
        canonical_req = "\n".join(
            [
                http_method,
                canonical_uri,
                ordered_q,
                canonical_headers,
                signed_headers,
                "UNSIGNED-PAYLOAD",
            ]
        )
        canonical_req_hash = (
            __import__("hashlib").sha256(canonical_req.encode()).hexdigest()
        )
        string_to_sign = "\n".join(
            [
                "GOOG4-RSA-SHA256",
                request_timestamp,
                credential_scope,
                canonical_req_hash,
            ]
        )

        signature_hex: str
        if private_key:
            # Local PKCS8 key signing using cryptography
            key = load_pem_private_key(private_key.encode("utf-8"), password=None)
            signed = key.sign(
                string_to_sign.encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            signature_hex = binascii.hexlify(signed).decode()
        else:
            provided_session = bool(iam_client or session)
            iam_client = iam_client or IamClient(token=token, session=session)
            signed_resp = await iam_client.sign_blob(
                string_to_sign, service_account_email=client_email
            )
            signature_hex = binascii.hexlify(
                base64.urlsafe_b64decode(signed_resp["signedBlob"])
            ).decode()
            if not provided_session:
                await iam_client.close()

        return f"https://{host}{canonical_uri}?{ordered_q}&X-Goog-Signature={signature_hex}"
