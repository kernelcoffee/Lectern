"""Streaming multipart upload, shared by the backups and file-manager uploads.

Not a router: a request-layer helper beside the routers it serves. The body is
parsed straight off the request stream rather than via ``UploadFile``:
Starlette would first spool the whole (multi-GB) file to the system temp dir —
outside the data volume in Docker — and only then let the size cap fire. Here
the file part is written once, into a caller-chosen staging dir on its final
filesystem (so adopting it is an atomic rename), and an oversized body is
refused from ``Content-Length`` before any of it lands.

Text fields are collected too, whatever their position in the body: browsers
send ``FormData`` in append order, so a field such as the file manager's
``path`` may arrive after the file bytes.

Follow-up: chunks are written synchronously on the event loop (fine on a LAN
with local disks); ``api/servers.py::import_world`` shows the ``anyio.open_file``
pattern if that ever matters.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from fastapi import HTTPException, Request, status
from python_multipart.multipart import MultipartParser, parse_options_header

# Multipart framing (boundaries, part headers) on top of the file bytes.
MULTIPART_SLACK = 1024 * 1024
# The one 413 constant (``HTTP_413_REQUEST_ENTITY_TOO_LARGE`` is deprecated).
TOO_LARGE = status.HTTP_413_CONTENT_TOO_LARGE
# Cap on each non-file (text) part, as Starlette's ``max_part_size``.
MAX_FIELD_BYTES = 1024 * 1024


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


class _PartWriter:
    """python-multipart callbacks: write the first ``file_field`` part's bytes to
    ``out`` (stop writing once past ``max_bytes``), collect every non-file part
    as a text field, ignore later file parts."""

    def __init__(self, out: IO[bytes], max_bytes: int, file_field: str) -> None:
        self.out = out
        self.max_bytes = max_bytes
        self.file_field = file_field.encode()
        self.size = 0
        self.found = False
        self.too_large = False
        self.filename: str | None = None
        self.fields: dict[str, str] = {}
        self._in_file = False
        self._field_name: str | None = None
        self._field_value = bytearray()
        self._headers: dict[bytes, bytes] = {}
        self._hfield = b""
        self._hvalue = b""

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self) -> None:
        self._headers.clear()

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._hfield += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._hvalue += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._hfield.lower()] = self._hvalue
        self._hfield = self._hvalue = b""

    def _headers_finished(self) -> None:
        _, opts = parse_options_header(self._headers.get(b"content-disposition"))
        name = opts.get(b"name")
        is_file = b"filename" in opts or name == self.file_field
        if name == self.file_field and not self.found:
            self._in_file = self.found = True
            filename = opts.get(b"filename")
            self.filename = _decode(filename) if filename is not None else None
        elif not is_file and name is not None:
            self._field_name = _decode(name)
            self._field_value = bytearray()

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._in_file:
            if self.too_large:
                return
            self.size += end - start
            if self.size > self.max_bytes:
                self.too_large = True
                return
            self.out.write(data[start:end])
        elif self._field_name is not None:
            self._field_value += data[start:end]
            if len(self._field_value) > MAX_FIELD_BYTES:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Form field {self._field_name!r} is too large",
                )

    def _part_end(self) -> None:
        if self._field_name is not None:
            self.fields[self._field_name] = _decode(bytes(self._field_value))
        self._in_file = False
        self._field_name = None


def multipart_body(text_fields: Sequence[str] = ()) -> dict:
    """``openapi_extra`` describing a hand-parsed upload: a required binary
    ``file`` plus optional string ``text_fields``."""
    properties: dict = {"file": {"type": "string", "format": "binary"}}
    properties.update({name: {"type": "string"} for name in text_fields})
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {"type": "object", "required": ["file"], "properties": properties}
                }
            },
        }
    }


@dataclass
class Upload:
    path: Path  # the staged file; removed on exit unless the caller moved it
    size: int
    filename: str | None
    fields: dict[str, str]


@asynccontextmanager
async def receive_upload(
    request: Request,
    staging_dir: Path,
    *,
    max_bytes: int,
    too_large_detail: str,
    file_field: str = "file",
) -> AsyncIterator[Upload]:
    """Stream the request's multipart ``file_field`` into a ``.part`` file in
    ``staging_dir`` and yield it. The caller adopts it by ``replace()``-ing
    ``Upload.path``; whatever is still there on exit (failure, or the caller
    raising) is deleted. An over-cap ``Content-Length`` or a non-multipart body
    is refused before anything is created on disk."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > max_bytes + MULTIPART_SLACK:
        raise HTTPException(TOO_LARGE, too_large_detail)  # refused before a byte is stored
    ctype, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected a multipart/form-data upload")

    staging_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=staging_dir, delete=False, suffix=".part")  # noqa: SIM115
    path = Path(tmp.name)
    try:
        with tmp:
            writer = _PartWriter(tmp, max_bytes, file_field)
            try:
                parser = MultipartParser(boundary, writer.callbacks())
                async for chunk in request.stream():
                    parser.write(chunk)
                    if writer.too_large:
                        raise HTTPException(TOO_LARGE, too_large_detail)
                parser.finalize()
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"Malformed upload: {exc}"
                ) from exc
            if not writer.found:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"No {file_field!r} field in the upload"
                )
        yield Upload(path=path, size=writer.size, filename=writer.filename, fields=writer.fields)
    finally:
        path.unlink(missing_ok=True)
