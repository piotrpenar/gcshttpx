import asyncio
import json
from typing import Callable, Dict, Any

import httpx
import pytest


class TransportBuilder:
    def __init__(self) -> None:
        self.routes: Dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def route(self, method: str, url: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes[f"{method.upper()} {url}"] = handler

    def build(self) -> httpx.MockTransport:
        def handler(req: httpx.Request) -> httpx.Response:
            key = f"{req.method} {str(req.url)}"
            if key not in self.routes:
                return httpx.Response(404, json={"error": "not found"})
            return self.routes[key](req)

        return httpx.MockTransport(handler)


@pytest.fixture
def mock_transport() -> TransportBuilder:
    return TransportBuilder()


