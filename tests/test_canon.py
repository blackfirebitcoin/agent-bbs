"""Tests for canonicalization — record_hash and content_fingerprint.

Spec reference: Section 5 of agent-bbs-v2-technical-proposal.md
"""

import hashlib
import json
import unicodedata

from agent_bbs.canon import compute_content_fingerprint, compute_record_hash


# ---------------------------------------------------------------------------
# Determinism: same inputs → same hash every time
# ---------------------------------------------------------------------------

class TestRecordHashDeterminism:
    """record_hash must be fully deterministic."""

    FIELDS = dict(
        author_id="agent-alpha",
        created_at="2026-03-30T12:00:00Z",
        entry_type="finding",
        performative="inform",
        content="The quick brown fox.",
    )

    def test_identical_calls_produce_same_hash(self):
        h1 = compute_record_hash(**self.FIELDS)
        h2 = compute_record_hash(**self.FIELDS)
        assert h1 == h2

    def test_hash_is_64_hex_chars(self):
        """SHA-256 → 64-character lowercase hex string."""
        h = compute_record_hash(**self.FIELDS)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# NFC normalization
# ---------------------------------------------------------------------------

class TestNFCNormalization:
    """All string values MUST be NFC-normalized before hashing."""

    def test_nfc_equivalence(self):
        """e + combining-acute (NFD) must hash identically to é (NFC)."""
        nfd_content = "caf\u0065\u0301"   # e + combining acute accent
        nfc_content = "caf\u00e9"          # precomposed é
        assert nfd_content != nfc_content  # different byte sequences

        h1 = compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", nfd_content)
        h2 = compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", nfc_content)
        assert h1 == h2

    def test_fingerprint_nfc_equivalence(self):
        nfd = "caf\u0065\u0301"
        nfc = "caf\u00e9"
        assert compute_content_fingerprint(nfd) == compute_content_fingerprint(nfc)


# ---------------------------------------------------------------------------
# Newline handling
# ---------------------------------------------------------------------------

class TestNewlineNormalization:
    """Newlines normalized to \\n; trailing whitespace stripped."""

    def test_crlf_normalized_to_lf(self):
        h1 = compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", "line1\r\nline2")
        h2 = compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", "line1\nline2")
        assert h1 == h2

    def test_cr_normalized_to_lf(self):
        h1 = compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", "line1\rline2")
        h2 = compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", "line1\nline2")
        assert h1 == h2

    def test_trailing_whitespace_stripped(self):
        h1 = compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", "hello   ")
        h2 = compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", "hello")
        assert h1 == h2

    def test_fingerprint_crlf(self):
        assert compute_content_fingerprint("a\r\nb") == compute_content_fingerprint("a\nb")


# ---------------------------------------------------------------------------
# Sorted keys & schema version in preimage
# ---------------------------------------------------------------------------

class TestPreimageStructure:
    """Hash preimage must contain sorted keys and schema_version='v1'."""

    def test_schema_version_in_preimage(self):
        """Changing the schema version changes the hash — proves it's in the preimage."""
        # We compute manually WITHOUT schema_version and compare to the function.
        # They must differ, proving schema_version is included.
        h_func = compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", "x")

        # Manual hash WITHOUT schema_version
        preimage_no_ver = json.dumps({
            "author_id": "a", "content": "x", "created_at": "2026-01-01T00:00:00Z",
            "entry_type": "finding", "performative": "inform",
        }, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
        h_no_ver = hashlib.sha256(preimage_no_ver.encode('utf-8')).hexdigest()

        assert h_func != h_no_ver

    def test_sorted_keys(self):
        """Manually construct with sorted keys — must match function output."""
        nc = unicodedata.normalize('NFC', "hello").rstrip().replace('\r\n', '\n').replace('\r', '\n')
        preimage = json.dumps({
            "author_id": "a", "content": nc, "created_at": "2026-01-01T00:00:00Z",
            "entry_type": "finding", "performative": "inform",
            "schema_version": "v1",
        }, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
        expected = hashlib.sha256(preimage.encode('utf-8')).hexdigest()

        assert compute_record_hash("a", "2026-01-01T00:00:00Z", "finding", "inform", "hello") == expected


# ---------------------------------------------------------------------------
# Two independent implementations must agree
# ---------------------------------------------------------------------------

class TestCrossImplementation:
    """Two different implementations of the same spec produce identical hashes."""

    @staticmethod
    def _alt_record_hash(author_id, created_at, entry_type, performative, content):
        """Alternative implementation — different code path, same spec."""
        import io
        buf = io.StringIO()
        c = unicodedata.normalize('NFC', content)
        c = c.replace('\r\n', '\n').replace('\r', '\n').rstrip()
        fields = [
            ("author_id", author_id),
            ("content", c),
            ("created_at", created_at),
            ("entry_type", entry_type),
            ("performative", performative),
            ("schema_version", "v1"),
        ]
        buf.write("{")
        parts = []
        for k, v in sorted(fields):
            parts.append(json.dumps(k, ensure_ascii=True) + ":" + json.dumps(v, ensure_ascii=True))
        buf.write(",".join(parts))
        buf.write("}")
        return hashlib.sha256(buf.getvalue().encode('utf-8')).hexdigest()

    def test_same_output_as_canonical(self):
        cases = [
            ("agent-x", "2026-03-30T12:00:00Z", "finding", "inform", "hello world"),
            ("agent-y", "2025-01-01T00:00:00Z", "question", "query", "Why?\r\nBecause."),
            ("z", "2026-06-15T08:30:00Z", "synthesis", "propose", "caf\u0065\u0301 data   "),
        ]
        for args in cases:
            assert compute_record_hash(*args) == self._alt_record_hash(*args), f"Mismatch for {args}"


# ---------------------------------------------------------------------------
# Content fingerprint
# ---------------------------------------------------------------------------

class TestContentFingerprint:
    """content_fingerprint is a SHA-256 of NFC-normalized, stripped content."""

    def test_deterministic(self):
        assert compute_content_fingerprint("abc") == compute_content_fingerprint("abc")

    def test_strips_leading_and_trailing_whitespace(self):
        assert compute_content_fingerprint("  abc  ") == compute_content_fingerprint("abc")

    def test_different_content_different_fingerprint(self):
        assert compute_content_fingerprint("abc") != compute_content_fingerprint("xyz")
