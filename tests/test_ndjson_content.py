import pytest

from frankensurf.runtime import WebFailure, _parse_builtin_content

URL = "https://example.com/api/search"


def test_newline_delimited_json_becomes_records():
    body = '{"_type":"PageErrorModule"}\n\n{"_type":"SearchResultsModule","results":[{"id":1}]}\n'
    parsed = _parse_builtin_content(body, "application/json", URL, None)
    assert parsed["structured"]["format"] == "ndjson"
    assert [r["_type"] for r in parsed["structured"]["records"]] == ["PageErrorModule", "SearchResultsModule"]


def test_broken_json_is_still_schema_changed():
    with pytest.raises(WebFailure) as error:
        _parse_builtin_content('{"a": 1}\n{not json', "application/json", URL, None)
    assert error.value.code == "SCHEMA_CHANGED"


def test_single_line_invalid_json_is_not_ndjson():
    with pytest.raises(WebFailure):
        _parse_builtin_content("{oops", "application/json", URL, None)


