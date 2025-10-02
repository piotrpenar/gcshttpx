# gcshttpx

**Minimal, secure async Google Cloud Storage client built on httpx with native HTTP/2 support.**

[![CI](https://github.com/piotrpenar/gcshttpx/workflows/CI/badge.svg)](https://github.com/piotrpenar/gcshttpx/actions)
[![PyPI](https://img.shields.io/pypi/v/gcshttpx.svg)](https://pypi.org/project/gcshttpx/)
[![Python](https://img.shields.io/pypi/pyversions/gcshttpx.svg)](https://pypi.org/project/gcshttpx/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Why gcshttpx?

- **🚀 Fast**: Built on httpx with HTTP/2 support for connection multiplexing
- **🔒 Secure**: Explicit credential handling, no automatic filesystem searches
- **🪶 Lightweight**: Minimal dependencies (httpx, orjson, PyJWT, cryptography)
- **⚡ Async-first**: Full async/await support with modern Python
- **🎯 Simple**: Clean, intuitive API for common operations
- **📦 Type-safe**: Fully typed with py.typed marker

## Installation

```bash
# Using uv (recommended)
uv add gcshttpx

# Using pip
pip install gcshttpx
```

## Quick Start

```python
import asyncio
from gcshttpx import Storage, Token

async def main():
    # Create authenticated token
    token = Token(
        service_file="path/to/service-account.json",
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )

    # Create storage client
    storage = Storage(token=token)

    # Upload a file
    await storage.upload("my-bucket", "hello.txt", b"Hello, World!")

    # Download a file
    data = await storage.download("my-bucket", "hello.txt")
    print(data)  # b"Hello, World!"

    # List objects
    items = await storage.list_objects("my-bucket", params={"prefix": "logs/"})
    for item in items["items"]:
        print(item["name"])

    # Clean up
    await storage.close()

asyncio.run(main())
```

## Authentication

### Service Account (Recommended for Production)

```python
from gcshttpx import Token

# From file path
token = Token(
    service_file="/path/to/service-account.json",
    scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
)

# From environment variable
# Set GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
token = Token(scopes=["https://www.googleapis.com/auth/cloud-platform"])

# From file-like object
import io
import json

# Do not use this pattern in production
credentials = {
    "type": "service_account",
    "project_id": "my-project",
    "private_key_id": "key-id",
    "private_key": "-----BEGIN PRIVATE KEY-----\n...",
    "client_email": "service@my-project.iam.gserviceaccount.com",
    "client_id": "123456789",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}

token = Token(
    service_file=io.StringIO(json.dumps(credentials)),
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
```

### GCE Metadata Server (For GCP Environments)

```python
from gcshttpx import Token

# Automatically uses GCE metadata service if no credentials provided
token = Token(scopes=["https://www.googleapis.com/auth/cloud-platform"])
```

### Security Notes

⚠️ **gcshttpx takes security seriously:**

- **HTTPS-only token endpoints**: Prevents credential leakage
- **Input validation**: All credentials are validated before use
- **No sensitive data in logs**: Errors don't expose credentials

## Storage Operations

### Upload

```python
# Simple upload (bytes or str)
await storage.upload("bucket", "file.txt", b"content")

# With metadata
await storage.upload(
    "bucket",
    "file.json",
    b'{"key": "value"}',
    content_type="application/json",
    metadata={"cacheControl": "no-cache", "contentLanguage": "en"}
)

# From file
await storage.upload_from_filename("bucket", "remote.txt", "local.txt")

# Gzip compression
await storage.upload("bucket", "file.txt", b"content", zipped=True)

# Force resumable upload (for large files)
await storage.upload(
    "bucket",
    "large-file.bin",
    large_data,
    force_resumable_upload=True
)
```

### Download

```python
# Download to bytes
data = await storage.download("bucket", "file.txt")

# Download to file
await storage.download_to_filename("bucket", "remote.txt", "local.txt")

# Download metadata only
metadata = await storage.download_metadata("bucket", "file.txt")
print(metadata["size"], metadata["contentType"])

# Stream large files
stream = await storage.download_stream("bucket", "large-file.bin")
while chunk := await stream.read(8192):
    process(chunk)
```

### List & Search

```python
# List all objects
result = await storage.list_objects("bucket")
for item in result["items"]:
    print(item["name"], item["size"])

# With prefix filter
result = await storage.list_objects("bucket", params={"prefix": "logs/2024/"})

# List with delimiter (folder-like)
result = await storage.list_objects("bucket", params={"delimiter": "/"})
print("Prefixes:", result.get("prefixes"))  # 'folders'
print("Items:", result.get("items"))        # files in current 'folder'

# List buckets
buckets = await storage.list_buckets("my-project-id")
for bucket in buckets:
    print(bucket.name)
```

### Delete

```python
# Delete object
await storage.delete("bucket", "file.txt")

# Delete returns status text
status = await storage.delete("bucket", "file.txt")
print(status)  # "OK" or error message
```

### Object Operations

```python
# Check if object exists
exists = await storage.blob_exists("bucket", "file.txt")

# Compose multiple objects into one
await storage.compose(
    "bucket",
    "merged.txt",
    ["part1.txt", "part2.txt", "part3.txt"],
    content_type="text/plain"
)

# Update metadata
await storage.patch_metadata(
    "bucket",
    "file.txt",
    {"cacheControl": "public, max-age=3600"}
)

# Get bucket metadata
metadata = await storage.get_bucket_metadata("bucket")
print(metadata["location"], metadata["storageClass"])
```

### Signed URLs

```python
from gcshttpx import IamClient

# Create IAM client for signing
iam = IamClient(token=token)

# Generate signed URL (valid for 1 hour)
bucket = storage.get_bucket("my-bucket")
blob = await bucket.get_blob("file.txt")
signed_url = await blob.get_signed_url(
    expiration=3600,
    iam_client=iam
)

# Share the URL
print(f"Download link: {signed_url}")
```

### Bucket and Blob Objects

```python
# Get bucket object
bucket = storage.get_bucket("my-bucket")
metadata = await bucket.get_metadata()
exists = await bucket.blob_exists("file.txt")

# Get blob object
blob = await bucket.get_blob("file.txt")
await blob.upload(b"new content", content_type="text/plain")
data = await blob.download()
await blob.delete()
```

## Advanced Usage

### Custom HTTP Client

```python
import httpx
from gcshttpx import Storage, Token

# Use custom httpx client with specific settings
async with httpx.AsyncClient(
    http2=True,
    timeout=httpx.Timeout(30.0),
    limits=httpx.Limits(max_keepalive_connections=5)
) as client:
    token = Token(service_file="credentials.json")
    storage = Storage(session=client, token=token)

    await storage.upload("bucket", "file.txt", b"content")
```

### Token Management

```python
from gcshttpx import Token

token = Token(
    service_file="credentials.json",
    scopes=["https://www.googleapis.com/auth/cloud-platform"],
    # Refresh token when 50% of lifetime has passed
    background_refresh_after=0.5,
    # Force refresh when 95% of lifetime has passed
    force_refresh_after=0.95
)

# Manually get current token
access_token = await token.get()

# Get project ID
project_id = await token.get_project()

# Close token session
await token.close()
```

### Error Handling

```python
import httpx
from gcshttpx import Storage

try:
    data = await storage.download("bucket", "nonexistent.txt")
except httpx.HTTPStatusError as e:
    print(f"HTTP {e.response.status_code}: {e.response.text}")
except httpx.TimeoutException:
    print("Request timed out")
except ValueError as e:
    print(f"Validation error: {e}")
```

## Common OAuth Scopes

```python
# Full control
"https://www.googleapis.com/auth/cloud-platform"

# Read/write access to storage
"https://www.googleapis.com/auth/devstorage.read_write"

# Read-only access
"https://www.googleapis.com/auth/devstorage.read_only"

# Write-only access
"https://www.googleapis.com/auth/devstorage.write_only"
```

## Development

```bash
# Clone repository
git clone https://github.com/piotrpenar/gcshttpx.git
cd gcshttpx

# Install with uv
uv sync

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=gcshttpx

# Lint code
uv run ruff check .

# Format code
uv run ruff format .

# Build package
uv build
```

## Requirements

- Python 3.10+
- httpx[http2] >= 0.27.0
- orjson >= 3.10.0
- PyJWT >= 2.9.0
- cryptography >= 43.0.0

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Links

- **Documentation**: [README.md](https://github.com/piotrpenar/gcshttpx)
- **Source Code**: [GitHub](https://github.com/piotrpenar/gcshttpx)
- **Issue Tracker**: [GitHub Issues](https://github.com/piotrpenar/gcshttpx/issues)
- **PyPI**: [gcshttpx](https://pypi.org/project/gcshttpx/)


