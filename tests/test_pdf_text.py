import io

import httpx
from pypdf import PdfWriter
from pypdf.generic import NameObject, TextStringObject

from frankensurf.runtime import Runtime, WebPolicy

URL = "https://docs.example.com/paper.pdf"


def pdf_bytes(text: str, title: str | None = "Example Paper") -> bytes:
    """A one-page PDF with real text, built with pypdf's own annotations-free writer."""
    from pypdf import PageObject
    from pypdf.generic import DecodedStreamObject, DictionaryObject, ArrayObject, FloatObject
    writer = PdfWriter()
    page = PageObject.create_blank_page(width=300, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(("BT /F1 12 Tf 20 150 Td (" + text + ") Tj ET").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_page(page)
    if title:
        writer.add_metadata({"/Title": title})
    out = io.BytesIO(); writer.write(out)
    return out.getvalue()


async def test_pdf_text_is_extracted_not_returned_as_bytes(tmp_path):
    body = pdf_bytes("Dummy PDF file")
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=body, headers={"content-type": "application/pdf"}))
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.read(URL, policy_overrides={"provider": "http"})
    assert result["receipt"]["status"] == "observed"
    assert "Dummy PDF file" in result["text"] and "%PDF" not in result["text"]
    assert result["title"] == "Example Paper"
    assert result["structured"] == {"format": "pdf", "pages": 1}


async def test_untitled_pdf_uses_its_first_line(tmp_path):
    body = pdf_bytes("First line of the document", title=None)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=body, headers={"content-type": "application/pdf"}))
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.read(URL, policy_overrides={"provider": "http"})
    assert result["title"] == "First line of the document"


async def test_broken_pdf_is_a_typed_failure(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=b"%PDF-1.4 truncated garbage", headers={"content-type": "application/pdf"}))
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.read(URL, policy_overrides={"provider": "http"})
    assert result["receipt"]["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"


def test_pdf_page_cap_is_validated():
    import pytest
    assert WebPolicy().pdf_max_pages == 50
    with pytest.raises(ValueError):
        WebPolicy(pdf_max_pages=0)
