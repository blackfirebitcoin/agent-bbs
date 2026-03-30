"""Canonicalization — deterministic hashing per Section 5 of the spec.

Rules:
  - UTF-8, NFC normalization on all string values
  - JSON: sorted keys, compact separators, ensure_ascii
  - Newlines normalised to \\n; trailing whitespace stripped
  - Schema version "v1" included in record_hash preimage
"""

import hashlib
import json
import unicodedata

SCHEMA_VERSION = "v1"


def _normalize_content(content: str) -> str:
    """NFC-normalize, normalize newlines to \\n, strip trailing whitespace."""
    s = unicodedata.normalize("NFC", content)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.rstrip()
    return s


def compute_record_hash(
    author_id: str,
    created_at: str,
    entry_type: str,
    performative: str,
    content: str,
) -> str:
    """Compute the canonical record hash (SHA-256 hex).

    Preimage fields (sorted):
        author_id, content, created_at, entry_type, performative, schema_version
    """
    nc = _normalize_content(content)
    preimage = {
        "author_id": author_id,
        "content": nc,
        "created_at": created_at,
        "entry_type": entry_type,
        "performative": performative,
        "schema_version": SCHEMA_VERSION,
    }
    canonical = json.dumps(
        preimage, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_content_fingerprint(content: str) -> str:
    """Compute a content-only fingerprint for dedup (SHA-256 hex).

    NFC-normalized, fully stripped (leading + trailing), newlines normalised.
    """
    s = unicodedata.normalize("NFC", content)
    s = s.strip()
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
