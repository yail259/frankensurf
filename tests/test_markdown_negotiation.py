import httpx

from frankensurf.runtime import Runtime, WebPolicy, _parse_builtin_content

URL = "https://docs.example.com/guide"
MARKDOWN = "# Guide title\n\nSome text.\n\n![diagram](/img/a.png \"Diagram\")\n"


def test_markdown_responses_parse_to_text_title_and_images():
    parsed = _parse_builtin_content(MARKDOWN, "text/markdown; charset=utf-8", URL, None)
    assert parsed["title"] == "Guide title"
    assert "Some text." in parsed["text"]
    assert parsed["structured"] == {"format": "markdown"}
    assert parsed["image_urls"] == ["https://docs.example.com/img/a.png"]


async def test_prefer_markdown_sends_accept_header_and_reads_markdown(tmp_path):
    seen = []
    def handle(request):
        seen.append(request.headers.get("accept"))
        if "text/markdown" in request.headers.get("accept", ""):
            return httpx.Response(200, text=MARKDOWN, headers={"content-type": "text/markdown"})
        return httpx.Response(200, text="<html><title>HTML</title><body>" + "x" * 200 + "</body></html>",
                              headers={"content-type": "text/html"})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        markdown = await web.read(URL, policy_overrides={"provider": "http", "prefer_markdown": True})
        html = await web.read(URL, policy_overrides={"provider": "http"})
    assert markdown["title"] == "Guide title"
    assert html["title"] == "HTML"
    assert "text/markdown" in seen[0] and "text/markdown" not in (seen[1] or "")


def test_prefer_markdown_is_off_by_default_and_validated():
    assert WebPolicy().prefer_markdown is False
    import pytest
    with pytest.raises(ValueError):
        WebPolicy(prefer_markdown="yes")
