"""TLS trust for the Youli endpoints, including private-CA deployments."""

from __future__ import annotations

import os
import ssl
import threading

_PARTIAL_CHAIN_FLAG = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0x80000)

_TLS_CACHE_LOCK = threading.Lock()
_TLS_CACHE: dict[tuple[str, int, int], ssl.SSLContext] = {}


def build_verify(ca_file: str) -> ssl.SSLContext | bool:
    """Translate ``YouliConfig.ca_file`` into an httpx ``verify`` value.

    A deployment can hand us the exact public certificate of an internal
    HTTPS endpoint. Treating that leaf as the trust anchor needs OpenSSL's
    partial-chain flag; otherwise the default store cannot verify it. Without
    a file we keep httpx's default verification on.
    """
    if not ca_file:
        return True
    try:
        stat = os.stat(ca_file)
        cache_key = (ca_file, stat.st_mtime_ns, stat.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None:
        with _TLS_CACHE_LOCK:
            cached = _TLS_CACHE.get(cache_key)
            if cached is not None:
                return cached
    context = ssl.create_default_context(cafile=ca_file)
    context.verify_flags |= _PARTIAL_CHAIN_FLAG
    if cache_key is not None:
        with _TLS_CACHE_LOCK:
            if len(_TLS_CACHE) >= 32:
                _TLS_CACHE.clear()
            _TLS_CACHE[cache_key] = context
    return context
