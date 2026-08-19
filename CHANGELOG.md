# Changelog

All notable changes to this project will be documented in this file.

## [0.2.1] - 2026-08-19

### Fixed
- `download_stream` no longer buffers the whole (decoded) object body at open. It previously issued a plain request, so httpx read and content-decoded the entire body on the caller's loop before the first `read()`; it now opens the response with `stream=True` and chunks arrive lazily through `StreamResponse.read()`.
- `StreamResponse.__aenter__`/`__aexit__` no longer call the nonexistent `httpx.Response.__aenter__`; the context manager now returns the stream and closes the response via the new `StreamResponse.aclose()`.

### Added
- Offloaded streaming downloads: when a `Storage` runs with an `OffloadLoop`, `download_stream` opens the response on the side loop and returns a `ShiftedStreamResponse` whose `read()`/`aclose()` are submitted there, so TLS, HTTP framing and content decoding (including gzip Content-Encoding) never run on the caller's loop — only decoded chunks cross back. Explicit per-call `session=` arguments still stream on the caller's loop.
- `AioSession.stream_request()` — open a response without reading its body; `ShiftedAioSession` runs it on its side loop and exposes the loop via the new `offload` property.
- `ShiftedStreamResponse.read_sync()` and `OffloadLoop.submit_sync()` — blocking variants for plain worker threads, so a synchronous pipeline can consume an offloaded stream without touching any event loop.

## [0.2.0] - 2026-08-19

### Added
- Opt-in offloaded request execution: `Storage(offload=True)`, a shared `OffloadLoop` instance passed as `Storage(offload=loop)`, or the `GCSHTTPX_OFFLOAD=1` environment variable run the existing `AioSession` request logic on a dedicated side event loop in a single daemon thread, keeping the caller's event loop free of request-body, TLS and HTTP/2 framing work. The exact same code path serves both modes, so behavior, errors and timeouts match the normal path, and cancelling an awaiting task cancels the in-flight side-loop request. One `OffloadLoop` can be shared by any number of `Storage` instances; `Storage.close()` only shuts down an offload loop it created itself. Explicit per-call `session=` arguments and `download_stream` always stay on the caller's event loop, and the environment opt-in never activates for a `Storage` built on a caller-provided shared session.
- `if_generation_match` parameter on `Storage.upload()` and `Storage.delete()`. `if_generation_match=0` makes an upload create-if-absent (HTTP 412 = object already exists), removing the need for a preflight existence check.
- `StreamResponse.content_encoding` property exposing the response's Content-Encoding header, so stream consumers can tell whether a body fetched with gzip accept-encoding is compressed.

### Changed
- `upload(zipped=True)` gzip compression runs off the event loop via `asyncio.to_thread` when offload is enabled, instead of on the event loop.

### Fixed
- Resumable uploads forward the caller's `session` and `timeout` to the upload-initiation request; previously the initiation POST silently used the default session even when a per-call session was passed.
- The resumable upload retry loop no longer retries 4xx responses; only 5xx responses are retried. A failed `ifGenerationMatch` precondition now surfaces immediately instead of re-sending the full body up to five times.
- `list_buckets` sends `project` as a real query parameter. Previously it was embedded in the URL string and then dropped when request-level params replaced the URL query, failing every call.

## [0.1.6]

### Fixed
- `Blob.get_signed_url()` now correctly retrieves service account email for impersonated credentials using `token.get_service_account_email()` instead of accessing `service_data["client_email"]` directly, which was returning `None` for `IMPERSONATED_SERVICE_ACCOUNT` credential type

## [0.1.5]

### Added
- Application Default Credentials (ADC) well-known file discovery (`~/.config/gcloud/application_default_credentials.json`)
- `Token.get_id_token(audience)` for service-to-service authentication on Cloud Run
- `Token.get_service_account_email()` to fetch email from credentials or metadata server
- `IamClient.get_service_account_email()` async method for metadata-based email lookup
- `use_adc` parameter on `Token` to control ADC discovery (default `True`)
- New metadata endpoints: `GCE_ENDPOINT_EMAIL`, `GCE_ENDPOINT_ID_TOKEN`

### Changed
- `IamClient.sign_blob()` now auto-fetches service account email from metadata when not provided

## [0.1.0] - 2024-10-02

Initial release of **gcshttpx** - a minimal, secure async Google Cloud Storage client built on httpx with native HTTP/2 support.

### Features

- **Authentication**:
  - Service account authentication with JWT signing
  - Authorized user authentication
  - GCE metadata server authentication
  - Explicit credential handling (no automatic filesystem searches)
  - HTTPS-only token endpoints
  - Comprehensive input validation

- **Storage Operations**:
  - Upload/download with streaming support
  - Resumable uploads for large files
  - List objects with pagination
  - Bucket operations
  - Object composition
  - Metadata operations
  - Signed URLs via IAM

- **Developer Experience**:
  - Full async/await support
  - Native HTTP/2 with httpx
  - Complete type hints (py.typed)
  - Python 3.10+ with modern syntax
  - Comprehensive documentation
  - 76% test coverage

- **Security**:
  - Explicit credential sources only
  - Private key validation (PEM format)
  - Required field validation
  - No sensitive data in error messages
  - Detailed security documentation


