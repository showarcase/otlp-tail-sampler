import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from tailsampler.model import AttributeValue, SpanRecord, StatusCode

SERVICE_NAME_KEY = "service.name"
UNKNOWN_SERVICE = "unknown_service"
DEFAULT_SCOPE_NAME = "tailsampler.fleet"

TRACE_ID_PATTERN = re.compile(r"[0-9a-fA-F]{32}")
SPAN_ID_PATTERN = re.compile(r"[0-9a-fA-F]{16}")


class OtlpFormatError(ValueError):
    def __init__(self, field_path: str, reason: str) -> None:
        super().__init__(f"{field_path}: {reason}")
        self.field_path = field_path


def decode_export_request(body: bytes) -> list[SpanRecord]:
    try:
        document = json.loads(body)
    except ValueError as error:
        raise OtlpFormatError("body", "not valid JSON") from error
    if not isinstance(document, dict):
        raise OtlpFormatError("body", "expected a JSON object")
    return parse_export_request(document)


def parse_export_request(document: Mapping[str, Any]) -> list[SpanRecord]:
    spans: list[SpanRecord] = []
    for resource_index, resource_spans in enumerate(_items(document, "resourceSpans", "request")):
        resource_path = f"resourceSpans[{resource_index}]"
        service = _service_name(resource_spans, resource_path)
        for scope_index, scope_spans in enumerate(
            _items(resource_spans, "scopeSpans", resource_path)
        ):
            scope_path = f"{resource_path}.scopeSpans[{scope_index}]"
            for span_index, raw_span in enumerate(_items(scope_spans, "spans", scope_path)):
                spans.append(_parse_span(raw_span, service, f"{scope_path}.spans[{span_index}]"))
    return spans


def build_export_request(
    spans: Sequence[SpanRecord], scope_name: str = DEFAULT_SCOPE_NAME
) -> dict[str, Any]:
    documents_by_service: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        documents_by_service.setdefault(span.service, []).append(_span_document(span))
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": SERVICE_NAME_KEY, "value": {"stringValue": service}}]
                },
                "scopeSpans": [{"scope": {"name": scope_name}, "spans": documents}],
            }
            for service, documents in documents_by_service.items()
        ]
    }


def _items(parent: Mapping[str, Any], key: str, parent_path: str) -> list[Any]:
    found = parent.get(key)
    if found is None:
        return []
    if not isinstance(found, list):
        raise OtlpFormatError(f"{parent_path}.{key}", "expected a list")
    return found


def _service_name(resource_spans: Mapping[str, Any], path: str) -> str:
    resource = resource_spans.get("resource") or {}
    for attribute in _items(resource, "attributes", f"{path}.resource"):
        if attribute.get("key") == SERVICE_NAME_KEY:
            value = _attribute_value(attribute.get("value") or {}, f"{path}.resource.attributes")
            if isinstance(value, str) and value:
                return value
    return UNKNOWN_SERVICE


def _parse_span(raw: Mapping[str, Any], service: str, path: str) -> SpanRecord:
    status = raw.get("status") or {}
    return SpanRecord(
        trace_id=_hex_identifier(raw.get("traceId"), TRACE_ID_PATTERN, f"{path}.traceId"),
        span_id=_hex_identifier(raw.get("spanId"), SPAN_ID_PATTERN, f"{path}.spanId"),
        parent_span_id=_parent_identifier(raw.get("parentSpanId"), f"{path}.parentSpanId"),
        service=service,
        name=str(raw.get("name", "")),
        kind=_integer(raw.get("kind", 0), f"{path}.kind"),
        start_unix_nano=_integer(raw.get("startTimeUnixNano"), f"{path}.startTimeUnixNano"),
        end_unix_nano=_integer(raw.get("endTimeUnixNano"), f"{path}.endTimeUnixNano"),
        status_code=_status_code(status.get("code", 0), f"{path}.status.code"),
        status_message=str(status.get("message", "")),
        attributes=_attributes(raw, path),
    )


def _hex_identifier(value: Any, pattern: re.Pattern[str], path: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise OtlpFormatError(path, f"expected {pattern.pattern} as hexadecimal text")
    return value.lower()


def _parent_identifier(value: Any, path: str) -> str:
    if value is None or value == "":
        return ""
    return _hex_identifier(value, SPAN_ID_PATTERN, path)


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise OtlpFormatError(path, "expected an integer or a decimal string")
    try:
        return int(value)
    except ValueError as error:
        raise OtlpFormatError(path, "not a decimal integer") from error


def _status_code(value: Any, path: str) -> StatusCode:
    try:
        return StatusCode(_integer(value, path))
    except ValueError as error:
        raise OtlpFormatError(path, "expected 0, 1 or 2") from error


def _attributes(raw_span: Mapping[str, Any], path: str) -> dict[str, AttributeValue]:
    attributes: dict[str, AttributeValue] = {}
    for attribute in _items(raw_span, "attributes", path):
        value = _attribute_value(attribute.get("value") or {}, f"{path}.attributes")
        if value is not None:
            attributes[str(attribute.get("key", ""))] = value
    return attributes


def _attribute_value(wrapper: Mapping[str, Any], path: str) -> AttributeValue | None:
    if "stringValue" in wrapper:
        return str(wrapper["stringValue"])
    if "boolValue" in wrapper:
        return bool(wrapper["boolValue"])
    if "intValue" in wrapper:
        return _integer(wrapper["intValue"], f"{path}.intValue")
    if "doubleValue" in wrapper:
        try:
            return float(wrapper["doubleValue"])
        except (TypeError, ValueError) as error:
            raise OtlpFormatError(f"{path}.doubleValue", "not a number") from error
    return None


def _span_document(span: SpanRecord) -> dict[str, Any]:
    document: dict[str, Any] = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "parentSpanId": span.parent_span_id,
        "name": span.name,
        "kind": span.kind,
        "startTimeUnixNano": str(span.start_unix_nano),
        "endTimeUnixNano": str(span.end_unix_nano),
        "attributes": [
            {"key": key, "value": _attribute_document(value)}
            for key, value in span.attributes.items()
        ],
        "status": {"code": int(span.status_code)},
    }
    if span.status_message:
        document["status"]["message"] = span.status_message
    return document


def _attribute_document(value: AttributeValue) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": value}
