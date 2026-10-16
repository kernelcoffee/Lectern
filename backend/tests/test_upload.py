"""Streaming multipart helper (``api/upload.py``) driven through a tiny app:
field collection in any order, caps (early and streamed), malformed bodies,
and the guarantee that no ``.part`` survives anything but an adoption."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from lectern.api.upload import MULTIPART_SLACK, multipart_body, receive_upload

BOUNDARY = "uploadtestboundary"
CTYPE = f"multipart/form-data; boundary={BOUNDARY}"


def _body(parts: list[tuple[str, str | None, bytes]]) -> bytes:
    out = b""
    for name, filename, data in parts:
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += f"--{BOUNDARY}\r\nContent-Disposition: {disp}\r\n\r\n".encode() + data + b"\r\n"
    return out + f"--{BOUNDARY}--\r\n".encode()


@pytest.fixture
def staging(tmp_path) -> Path:
    return tmp_path / "staging"


@pytest.fixture
def app_client(tmp_path, staging):
    app = FastAPI()
    seen: dict = {}

    @app.post("/up", openapi_extra=multipart_body(["path"]))
    async def up(request: Request, max_bytes: int = 1024 * 1024, mode: str = "keep") -> dict:
        async with receive_upload(
            request, staging, max_bytes=max_bytes, too_large_detail="Too big"
        ) as upload:
            seen["path"] = upload.path
            assert upload.path.parent == staging and upload.path.suffix == ".part"
            if mode == "raise":
                raise HTTPException(418, "caller failed")
            if mode == "adopt":
                upload.path.replace(tmp_path / "adopted.bin")
            return {
                "size": upload.size,
                "filename": upload.filename,
                "fields": upload.fields,
                "content": upload.path.read_bytes().decode() if mode == "keep" else None,
            }

    client = TestClient(app)
    client.seen = seen  # type: ignore[attr-defined]
    return client


def _parts(staging: Path) -> list[Path]:
    return list(staging.glob("*")) if staging.exists() else []


@pytest.mark.parametrize("file_first", [True, False], ids=["file-first", "field-first"])
def test_file_and_field_any_order(app_client, staging, file_first):
    parts = [("file", "a.txt", b"hello"), ("path", None, b"config/sub")]
    if not file_first:
        parts.reverse()
    resp = app_client.post("/up", content=_body(parts), headers={"content-type": CTYPE})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "size": 5, "filename": "a.txt", "fields": {"path": "config/sub"}, "content": "hello",
    }
    assert _parts(staging) == []  # not adopted → removed on exit


def test_extra_fields_kept_and_second_file_ignored(app_client, staging):
    body = _body([
        ("note", None, "hé".encode()),
        ("file", "first.txt", b"ONE"),
        ("file", "second.txt", b"TWO-IGNORED"),
        ("other", "x.bin", b"also ignored"),
    ])
    resp = app_client.post("/up", content=body, headers={"content-type": CTYPE})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["filename"] == "first.txt" and data["content"] == "ONE" and data["size"] == 3
    assert data["fields"] == {"note": "hé"}


def test_over_cap_early_creates_nothing(app_client, staging):
    body = _body([("file", "big.bin", b"x" * (MULTIPART_SLACK + 4096))])
    resp = app_client.post("/up?max_bytes=1024", content=body, headers={"content-type": CTYPE})
    assert resp.status_code == 413 and resp.json()["detail"] == "Too big"
    assert not staging.exists()
    assert "path" not in app_client.seen


def test_over_cap_streamed_removes_temp(app_client, staging):
    body = _body([("file", "big.bin", b"x" * 1500)])
    resp = app_client.post("/up?max_bytes=1024", content=body, headers={"content-type": CTYPE})
    assert resp.status_code == 413
    assert staging.exists() and _parts(staging) == []


def test_missing_file_field_is_400(app_client, staging):
    body = _body([("path", None, b"x")])
    resp = app_client.post("/up", content=body, headers={"content-type": CTYPE})
    assert resp.status_code == 400 and "'file'" in resp.json()["detail"]
    assert _parts(staging) == []


def test_not_multipart_is_400(app_client, staging):
    resp = app_client.post("/up", content=b"raw", headers={"content-type": "application/zip"})
    assert resp.status_code == 400 and "multipart" in resp.json()["detail"]
    resp = app_client.post("/up", content=b"raw", headers={"content-type": "multipart/form-data"})
    assert resp.status_code == 400  # no boundary
    assert not staging.exists()


def test_malformed_body_is_400(app_client, staging):
    resp = app_client.post(
        "/up", content=b"garbage, no boundary here\r\n" * 4, headers={"content-type": CTYPE}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("Malformed upload")
    assert _parts(staging) == []


def test_oversized_text_field_is_400(app_client, staging):
    body = _body([("note", None, b"n" * (1024 * 1024 + 1)), ("file", "a", b"A")])
    resp = app_client.post("/up?max_bytes=4194304", content=body, headers={"content-type": CTYPE})
    assert resp.status_code == 400 and "too large" in resp.json()["detail"]
    assert _parts(staging) == []


def test_caller_raising_removes_temp(app_client, staging):
    body = _body([("file", "a.txt", b"A")])
    resp = app_client.post("/up?mode=raise", content=body, headers={"content-type": CTYPE})
    assert resp.status_code == 418
    assert not app_client.seen["path"].exists()
    assert _parts(staging) == []


def test_caller_adopting_keeps_file(app_client, staging, tmp_path):
    body = _body([("file", "a.txt", b"ADOPTED")])
    resp = app_client.post("/up?mode=adopt", content=body, headers={"content-type": CTYPE})
    assert resp.status_code == 200, resp.text
    assert (tmp_path / "adopted.bin").read_bytes() == b"ADOPTED"
    assert _parts(staging) == []


def test_openapi_documents_fields():
    schema = multipart_body(["path"])["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert schema["required"] == ["file"]
    assert schema["properties"]["file"]["format"] == "binary"
    assert schema["properties"]["path"] == {"type": "string"}
