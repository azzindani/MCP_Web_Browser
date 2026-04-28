"""TLS fingerprint helpers. Python equivalent of workers/tls.ts.

Provides make_chrome_client() which tries curl_cffi first for a full
Chrome-120 JA3/JA4 fingerprint, then falls back to httpx with a
manually ordered cipher suite. No new required dependency is added;
curl_cffi is used only when already present in the environment.
"""
from __future__ import annotations

import ssl
from typing import Any

# Chrome 120 cipher preference order (mirrors tls.ts)
CHROME_CIPHERS = ":".join([
    "TLS_AES_128_GCM_SHA256",
    "TLS_AES_256_GCM_SHA384",
    "TLS_CHACHA20_POLY1305_SHA256",
    "ECDHE-ECDSA-AES128-GCM-SHA256",
    "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-ECDSA-AES256-GCM-SHA384",
    "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-CHACHA20-POLY1305",
    "ECDHE-RSA-CHACHA20-POLY1305",
    "ECDHE-RSA-AES128-SHA",
    "ECDHE-RSA-AES256-SHA",
    "AES128-GCM-SHA256",
    "AES256-GCM-SHA384",
    "AES128-SHA",
    "AES256-SHA",
])


def make_chrome_ssl_context() -> ssl.SSLContext:
    """Build an SSLContext with Chrome-120 cipher preference order."""
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers(CHROME_CIPHERS)
    except ssl.SSLError:
        pass  # some platforms don't support all ciphers; fall back silently
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def make_chrome_client(**kwargs: Any) -> Any:
    """Return an async HTTP client with Chrome-like TLS fingerprint.

    Uses curl_cffi (Chrome impersonation) when available, otherwise
    falls back to httpx with a manually ordered cipher suite.
    """
    try:
        import curl_cffi.requests as cf  # type: ignore[import]
        return cf.AsyncSession(impersonate="chrome120", **kwargs)
    except ImportError:
        pass

    import httpx
    return httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        verify=make_chrome_ssl_context(),
        **kwargs,
    )
