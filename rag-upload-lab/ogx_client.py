import httpx
from settings import OGX


def client():
    return httpx.Client(base_url=OGX, timeout=180)


def call(c, method, path, body=None):
    response = c.request(method, path, **({"json": body} if body is not None else {}))
    if response.is_error:
        raise RuntimeError(f"OGX {method} {path}: HTTP {response.status_code}\n{response.text[:3000]}")
    return response.json() if response.content else None
