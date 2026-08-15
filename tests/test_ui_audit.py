"""Contracts for bounded, privacy-preserving UI evidence."""
from __future__ import annotations

from repo_ops.ui_audit import _fingerprint, _same_origin_path


def test_same_origin_paths_drop_queries_fragments_and_external_origins() -> None:
    origin = "http://sandbox-agent-zero"

    assert _same_origin_path("http://sandbox-agent-zero/project?token=secret#section", origin) == "http://sandbox-agent-zero/project"
    assert _same_origin_path("https://example.test/private", origin) is None
    assert _same_origin_path("javascript:alert(1)", origin) is None


def test_ui_evidence_uses_fingerprints_instead_of_text() -> None:
    private_error = "Bearer private-token was rejected"
    fingerprint = _fingerprint(private_error)

    assert fingerprint["characters"] == len(private_error)
    assert fingerprint["sha256"] != private_error
