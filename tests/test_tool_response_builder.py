"""Unit tests for `_build_tool_response` and `_redact_attachments`.

When the desktop side returns a result containing screenshot
attachments, the shim must split the single text content block into:
  * one text block with attachments[*].data redacted, AND
  * one MCP `image` content block per screenshot attachment.

The text block keeps selection metadata; only binary screenshot
payloads are redacted (their bytes ride in the dedicated image
block instead).
"""

from __future__ import annotations

import json


from prodius_mcp import _build_tool_response, _redact_attachments


# ---------------------------------------------------------------------------
# _redact_attachments
# ---------------------------------------------------------------------------


def test_redact_attachments_strips_data_from_screenshot_entries():
    result = {
        "answer": "yes",
        "attachments": [
            {"kind": "screenshot", "data": "BASE64BLOB", "mime_type": "image/jpeg"},
        ],
    }
    out = _redact_attachments(result)
    assert "data" not in out["attachments"][0]
    assert out["attachments"][0]["kind"] == "screenshot"
    assert out["attachments"][0]["mime_type"] == "image/jpeg"


def test_redact_attachments_preserves_selection_text():
    """Selection attachments have no `data` key, but have `text`. The
    text must survive — it's useful signal for the recipient."""
    result = {
        "answer": "yes",
        "attachments": [
            {"kind": "selection", "text": "user highlighted this"},
        ],
    }
    out = _redact_attachments(result)
    assert out["attachments"][0] == {
        "kind": "selection", "text": "user highlighted this",
    }


def test_redact_attachments_handles_mixed_kinds():
    result = {
        "answer": "ok",
        "attachments": [
            {"kind": "selection", "text": "snippet"},
            {"kind": "screenshot", "data": "B64", "mime_type": "image/jpeg"},
        ],
    }
    out = _redact_attachments(result)
    assert out["attachments"][0] == {"kind": "selection", "text": "snippet"}
    assert out["attachments"][1] == {"kind": "screenshot", "mime_type": "image/jpeg"}


def test_redact_attachments_returns_input_unchanged_when_no_attachments():
    result = {"answer": "yes"}
    assert _redact_attachments(result) == {"answer": "yes"}


def test_redact_attachments_does_not_mutate_input():
    """Sanity: the redacted dict must be a fresh shallow copy, not a
    mutation of the caller's data."""
    original = {
        "answer": "ok",
        "attachments": [
            {"kind": "screenshot", "data": "B64", "mime_type": "image/jpeg"},
        ],
    }
    out = _redact_attachments(original)
    assert "data" in original["attachments"][0], (
        "input must be untouched"
    )
    assert "data" not in out["attachments"][0]


def test_redact_attachments_safe_with_non_dict_entries():
    """Defensive: malformed attachments[*] entries must not crash."""
    result = {
        "answer": "ok",
        "attachments": ["not a dict", 42],
    }
    out = _redact_attachments(result)
    assert out["attachments"] == ["not a dict", 42]


# ---------------------------------------------------------------------------
# _build_tool_response — content block layout
# ---------------------------------------------------------------------------


def test_text_only_response_when_no_attachments():
    result = {"answer": "yes", "answered_at": 1.0}
    resp = _build_tool_response(result)
    assert resp["isError"] is False
    assert len(resp["content"]) == 1
    assert resp["content"][0]["type"] == "text"
    parsed = json.loads(resp["content"][0]["text"])
    assert parsed == result


def test_selection_only_attachments_emit_text_only_no_image():
    result = {
        "answer": "ok",
        "attachments": [{"kind": "selection", "text": "hello"}],
    }
    resp = _build_tool_response(result)
    assert len(resp["content"]) == 1
    assert resp["content"][0]["type"] == "text"
    parsed = json.loads(resp["content"][0]["text"])
    # Selection metadata survives in the redacted text.
    assert parsed["attachments"][0]["text"] == "hello"


def test_screenshot_attachment_emits_text_plus_image_block():
    result = {
        "answer": "ok",
        "attachments": [
            {"kind": "screenshot", "data": "BASE64", "mime_type": "image/jpeg"},
        ],
    }
    resp = _build_tool_response(result)
    assert len(resp["content"]) == 2

    # Block 0: text with redacted attachments
    assert resp["content"][0]["type"] == "text"
    parsed = json.loads(resp["content"][0]["text"])
    assert "data" not in parsed["attachments"][0], (
        "binary payload must NOT be duplicated inside the text block"
    )

    # Block 1: image with camelCase mimeType
    assert resp["content"][1] == {
        "type": "image",
        "data": "BASE64",
        "mimeType": "image/jpeg",
    }


def test_mixed_selection_and_screenshot_emits_two_blocks():
    result = {
        "answer": "ok",
        "attachments": [
            {"kind": "selection", "text": "code snippet"},
            {"kind": "screenshot", "data": "B64", "mime_type": "image/jpeg"},
        ],
    }
    resp = _build_tool_response(result)
    assert len(resp["content"]) == 2
    parsed = json.loads(resp["content"][0]["text"])
    assert parsed["attachments"][0]["text"] == "code snippet"
    assert resp["content"][1]["type"] == "image"
    assert resp["content"][1]["data"] == "B64"


def test_screenshot_without_mime_type_defaults_to_jpeg():
    result = {
        "answer": "ok",
        "attachments": [
            {"kind": "screenshot", "data": "B64"},  # no mime_type
        ],
    }
    resp = _build_tool_response(result)
    assert resp["content"][1]["mimeType"] == "image/jpeg"


def test_screenshot_camelcase_mime_type_mapping():
    """Desktop side uses snake_case `mime_type` (Python dataclass
    convention); MCP spec expects camelCase `mimeType` on the wire.
    The mapping must happen exactly once, at the shim boundary."""
    result = {
        "answer": "ok",
        "attachments": [
            {"kind": "screenshot", "data": "B64", "mime_type": "image/png"},
        ],
    }
    resp = _build_tool_response(result)
    assert "mimeType" in resp["content"][1]
    assert "mime_type" not in resp["content"][1]
    assert resp["content"][1]["mimeType"] == "image/png"


def test_multiple_screenshot_attachments_emit_multiple_image_blocks():
    """Forward-compatible: a future result with N screenshots becomes
    N image content blocks."""
    result = {
        "answer": "ok",
        "attachments": [
            {"kind": "screenshot", "data": "A", "mime_type": "image/jpeg"},
            {"kind": "screenshot", "data": "B", "mime_type": "image/png"},
        ],
    }
    resp = _build_tool_response(result)
    image_blocks = [b for b in resp["content"] if b["type"] == "image"]
    assert len(image_blocks) == 2
    assert image_blocks[0]["data"] == "A"
    assert image_blocks[1]["data"] == "B"


def test_screenshot_with_empty_data_skipped():
    """A screenshot entry with no `data` is malformed; skip it rather
    than emit an empty image block."""
    result = {
        "answer": "ok",
        "attachments": [
            {"kind": "screenshot", "data": "", "mime_type": "image/jpeg"},
        ],
    }
    resp = _build_tool_response(result)
    image_blocks = [b for b in resp["content"] if b["type"] == "image"]
    assert image_blocks == []


def test_error_result_sets_isError_true():
    result = {"error": "timeout", "message": "no response"}
    resp = _build_tool_response(result)
    assert resp["isError"] is True


def test_success_result_sets_isError_false():
    resp = _build_tool_response({"answer": "yes"})
    assert resp["isError"] is False


def test_unknown_attachment_kind_ignored():
    """Future-proof: an unrecognised attachment `kind` must not produce
    an image content block (only `screenshot` does)."""
    result = {
        "answer": "ok",
        "attachments": [
            {"kind": "future_thing", "blob": "xyz"},
        ],
    }
    resp = _build_tool_response(result)
    image_blocks = [b for b in resp["content"] if b["type"] == "image"]
    assert image_blocks == []
