import copy
import json

import pytest

from tailsampler.model import StatusCode
from tailsampler.otlp import (
    OtlpFormatError,
    build_export_request,
    decode_export_request,
    parse_export_request,
)


def minimal_request() -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "cart"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "aa" * 16,
                                "spanId": "bb" * 8,
                                "name": "GET /cart",
                                "startTimeUnixNano": "100",
                                "endTimeUnixNano": "250",
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_round_trip_preserves_every_field(make_span):
    spans = [
        make_span(span_id="1111111111111111", attributes={"http.route": "/checkout"}),
        make_span(
            span_id="2222222222222222",
            parent_span_id="1111111111111111",
            service="payments",
            name="charge",
            kind=3,
            status_code=StatusCode.ERROR,
            status_message="card declined",
            attributes={"retry": 2, "ratio": 0.5, "cached": False, "big": 2**60},
        ),
    ]
    body = json.dumps(build_export_request(spans)).encode()
    assert decode_export_request(body) == spans


def test_build_groups_spans_by_service_in_first_seen_order(make_span):
    spans = [
        make_span(service="b", span_id="1" * 16),
        make_span(service="a", span_id="2" * 16),
        make_span(service="b", span_id="3" * 16),
    ]
    request = build_export_request(spans)
    names = [
        resource["resource"]["attributes"][0]["value"]["stringValue"]
        for resource in request["resourceSpans"]
    ]
    assert names == ["b", "a"]
    assert len(request["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 2


def test_minimal_request_gets_defaults():
    (span,) = parse_export_request(minimal_request())
    assert span.service == "cart"
    assert span.is_root
    assert span.status_code == StatusCode.UNSET
    assert span.duration_nano == 150
    assert span.attributes == {}


def test_missing_service_name_falls_back_to_unknown_service():
    request = minimal_request()
    del request["resourceSpans"][0]["resource"]
    (span,) = parse_export_request(request)
    assert span.service == "unknown_service"


def test_uppercase_hex_identifiers_are_normalised():
    request = minimal_request()
    request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "AB" * 16
    (span,) = parse_export_request(request)
    assert span.trace_id == "ab" * 16


def test_error_status_is_reported_as_error():
    request = minimal_request()
    request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["status"] = {
        "code": 2,
        "message": "boom",
    }
    (span,) = parse_export_request(request)
    assert span.is_error
    assert span.status_message == "boom"


def test_unsupported_attribute_types_are_dropped():
    request = minimal_request()
    request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
        {"key": "kept", "value": {"stringValue": "yes"}},
        {"key": "dropped", "value": {"arrayValue": {"values": []}}},
    ]
    (span,) = parse_export_request(request)
    assert span.attributes == {"kept": "yes"}


def test_empty_request_yields_no_spans():
    assert parse_export_request({}) == []


@pytest.mark.parametrize(
    ("path", "bad_value", "expected_field"),
    [
        ("traceId", "abc", "traceId"),
        ("traceId", "zz" * 16, "traceId"),
        ("traceId", 7, "traceId"),
        ("spanId", "bb" * 7, "spanId"),
        ("parentSpanId", "xyz", "parentSpanId"),
        ("startTimeUnixNano", "soon", "startTimeUnixNano"),
        ("endTimeUnixNano", True, "endTimeUnixNano"),
        ("kind", "server", "kind"),
        ("status", {"code": 9}, "status.code"),
    ],
)
def test_malformed_span_fields_name_the_offending_path(path, bad_value, expected_field):
    request = copy.deepcopy(minimal_request())
    request["resourceSpans"][0]["scopeSpans"][0]["spans"][0][path] = bad_value
    with pytest.raises(OtlpFormatError) as raised:
        parse_export_request(request)
    assert raised.value.field_path.endswith(expected_field)
    assert "resourceSpans[0].scopeSpans[0].spans[0]" in raised.value.field_path


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"', b"\xff\xfe"])
def test_bodies_that_are_not_json_objects_are_rejected(body):
    with pytest.raises(OtlpFormatError) as raised:
        decode_export_request(body)
    assert raised.value.field_path == "body"


def test_resource_spans_must_be_a_list():
    with pytest.raises(OtlpFormatError):
        parse_export_request({"resourceSpans": {"not": "a list"}})
