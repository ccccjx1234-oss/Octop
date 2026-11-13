"""TLS trust for the Youli endpoints, including private-CA deployments."""

from __future__ import annotations

import ssl

_PARTIAL_CHAIN_FLAG = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0x80000)


def build_verify(ca_file: str) -> ssl.SSLContext | bool:
    """Translate ``YouliConfig.ca_file`` into an httpx ``verify`` value.

    A deployment can hand us the exact public certificate of an internal
    HTTPS endpoint. Treating that leaf as the trust anchor needs OpenSSL's
    partial-chain flag; otherwise the default store cannot verify it. Without
    a file we keep httpx's default verification on.
    """
    if not ca_file:
        return True
    context = ssl.create_default_context(cafile=ca_file)
    context.verify_flags |= _PARTIAL_CHAIN_FLAG
    return context
