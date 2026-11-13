"""Pinned-CA trust settings shared by the Youli and MCN clients."""

from __future__ import annotations

import ssl

import certifi

from octop.infra.auth.youli.tls import build_verify


def test_missing_ca_keeps_default_verification() -> None:
    assert build_verify("") is True


def test_ca_file_becomes_a_partial_chain_context() -> None:
    context = build_verify(certifi.where())

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    assert context.verify_mode is ssl.CERT_REQUIRED
