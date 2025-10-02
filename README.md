gcs-httpx
==========

Minimal async Google Cloud Storage + Auth client built on httpx with HTTP/2.

Install
-------

```
pip install gcs-httpx
```

Quick start
-----------

```python
import httpx
from gcs_httpx import Storage

async with httpx.AsyncClient(http2=True) as client:
    storage = Storage(session=client)
    await storage.upload("my-bucket", "path/object.txt", b"hello")
```

License
-------
MIT


