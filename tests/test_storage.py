from pathlib import Path

import httpx
import pytest

from gcs_httpx.storage import Blob, Storage


def make_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, http2=True)


@pytest.mark.asyncio
async def test_list_buckets_pagination():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/storage/v1/b":
            page = req.url.params.get("pageToken", "")
            if page == "":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"id": "b1"}],
                        "nextPageToken": "NXT",
                    },
                )
            return httpx.Response(200, json={"items": [{"id": "b2"}]})
        return httpx.Response(404)

    async with make_client(httpx.MockTransport(handler)) as client:
        s = Storage(session=client, api_root="http://test")
        buckets = await s.list_buckets("p1")
        assert [b.name for b in buckets] == ["b1", "b2"]
        await s.close()


@pytest.mark.asyncio
async def test_get_bucket_and_metadata_and_exists_false(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        # download metadata for non-existent blob
        if req.method == "GET" and req.url.path.startswith("/storage/v1/b/bkt/o/"):
            return httpx.Response(404, json={"error": "not found"})
        # bucket metadata
        if req.method == "GET" and req.url.path == "/storage/v1/b/bkt":
            return httpx.Response(200, json={"name": "bkt"})
        return httpx.Response(404)

    async with make_client(httpx.MockTransport(handler)) as client:
        s = Storage(session=client, api_root="http://test")
        b = s.get_bucket("bkt")
        md = await b.get_metadata()
        assert md["name"] == "bkt"
        exists = await b.blob_exists("obj")
        assert exists is False
        await s.close()


@pytest.mark.asyncio
async def test_download_and_delete_and_metadata(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/o/obj"):
            if req.url.params.get("alt") == "media":
                return httpx.Response(200, content=b"hello-world")
            return httpx.Response(200, content=b'{"size": 5}')
        if req.method == "DELETE" and req.url.path.endswith("/o/obj"):
            return httpx.Response(200, text="OK")
        return httpx.Response(404)

    async with make_client(httpx.MockTransport(handler)) as client:
        s = Storage(session=client, api_root="http://test")
        data = await s.download("bkt", "obj")
        assert data == b"hello-world"
        md = await s.download_metadata("bkt", "obj")
        assert md["size"] == 5
        text = await s.delete("bkt", "obj")
        assert text == "OK"
        # download_to_filename
        out = tmp_path / "d.bin"
        await s.download_to_filename("bkt", "obj", str(out))
        assert out.read_bytes() == b"hello-world"
        await s.close()


@pytest.mark.asyncio
async def test_download_stream_reads_in_chunks():
    payload = b"0123456789"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/o/obj"):
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    async with make_client(httpx.MockTransport(handler)) as client:
        s = Storage(session=client, api_root="http://test")
        stream = await s.download_stream("bkt", "obj")
        got = b""
        while True:
            chunk = await stream.read(4)
            if not chunk:
                break
            got += chunk
        assert got == payload
        await s.close()


@pytest.mark.asyncio
async def test_list_objects_with_prefixes():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/storage/v1/b/bkt/o":
            page = req.url.params.get("pageToken", "")
            if page == "":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"name": "a/1"}],
                        "prefixes": ["a/"],
                        "nextPageToken": "NXT",
                    },
                )
            return httpx.Response(200, json={"items": [{"name": "b/2"}]})
        return httpx.Response(404)

    async with make_client(httpx.MockTransport(handler)) as client:
        s = Storage(session=client, api_root="http://test")
        items = await s.list_objects("bkt", params={"delimiter": "/"})
        assert items["items"][0]["name"] == "a/1"
        await s.close()


@pytest.mark.asyncio
async def test_upload_simple_and_multipart_and_resumable(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/upload/storage/v1/b/bkt/o":
            # init resumable
            if req.url.params.get("uploadType") == "resumable":
                return httpx.Response(
                    200, headers={"Location": "http://session.upload/xyz"}
                )
            # simple/multipart result
            return httpx.Response(200, json={"bucket": "bkt", "name": "obj"})
        if req.method == "PUT" and req.url.host == "session.upload":
            return httpx.Response(200, json={"bucket": "bkt", "name": "obj"})
        return httpx.Response(404)

    async with make_client(httpx.MockTransport(handler)) as client:
        s = Storage(session=client, api_root="http://test")
        # simple
        res = await s.upload("bkt", "obj", b"data", content_type="text/plain")
        assert res["name"] == "obj"
        # multipart
        res = await s.upload(
            "bkt",
            "obj",
            b"data",
            content_type="text/plain",
            metadata={"cache-control": "no-cache"},
        )
        assert res["name"] == "obj"
        # resumable
        res = await s.upload("bkt", "obj", b"data", force_resumable_upload=True)
        assert res["name"] == "obj"
        await s.close()


@pytest.mark.asyncio
async def test_compose_and_patch_bucket_metadata():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/storage/v1/b/bkt/o/obj/compose":
            return httpx.Response(200, json={"name": "obj"})
        if req.method == "PATCH" and req.url.path == "/storage/v1/b/bkt/o/obj":
            return httpx.Response(200, json={"updated": True})
        if req.method == "GET" and req.url.path == "/storage/v1/b/bkt":
            return httpx.Response(200, json={"name": "bkt"})
        return httpx.Response(404)

    async with make_client(httpx.MockTransport(handler)) as client:
        s = Storage(session=client, api_root="http://test")
        out = await s.compose("bkt", "obj", ["a", "b"], content_type="text/plain")
        assert out["name"] == "obj"
        out = await s.patch_metadata("bkt", "obj", {"cacheControl": "no-cache"})
        assert out["updated"] is True
        out = await s.get_bucket_metadata("bkt")
        assert out["name"] == "bkt"
        await s.close()


@pytest.mark.asyncio
async def test_blob_helpers_and_signed_url_iam_path():
    def handler(req: httpx.Request) -> httpx.Response:
        # IAM sign blob
        if req.method == "POST" and "signBlob" in str(req.url):
            return httpx.Response(200, json={"signedBlob": "QUJD"})
        # token refresh for IAM client using metadata
        if req.method == "GET" and "computeMetadata" in str(req.url):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        # generic metadata fetch for blob
        if req.method == "GET" and req.url.path == "/storage/v1/b/bkt/o/obj":
            return httpx.Response(200, content=b'{"size": 3}')
        # uploads
        if req.method == "POST" and req.url.path == "/upload/storage/v1/b/bkt/o":
            return httpx.Response(200, json={"bucket": "bkt", "name": "obj"})
        return httpx.Response(404)

    async with make_client(httpx.MockTransport(handler)) as client:
        s = Storage(session=client, api_root="http://test")
        b = s.get_bucket("bkt")
        blob = await b.get_blob("obj")
        assert isinstance(blob, Blob)
        # upload through Blob
        md = await blob.upload(b"data", content_type="application/octet-stream")
        assert md["name"] == "obj"
        # signed url (IAM path)
        url = await blob.get_signed_url(
            60, iam_client=None, service_account_email="sa@example.com", session=client
        )
        assert "X-Goog-Signature=414243" in url
        await s.close()
