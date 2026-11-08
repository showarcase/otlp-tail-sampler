# API

The gateway serves six routes from one Starlette application in `api.py`. Errors from the application are JSON objects with an `error` field. All timestamps are Unix nanoseconds, as in OTLP.

| Method and path | Purpose |
| --- | --- |
| `POST /v1/traces` | accept an OTLP/HTTP JSON export request |
| `GET /v1/traces` | list stored traces by root span |
| `GET /v1/traces/{trace_id}` | return every stored span of one trace |
| `GET /v1/estimates` | estimated request and failure counts per route |
| `GET /metrics` | counters and gauges in the Prometheus text format |
| `GET /healthz` | liveness check |

## POST /v1/traces

The body is an OTLP `ExportTraceServiceRequest` in its JSON encoding, sent with `Content-Type: application/json`. A `Content-Encoding` of `gzip` is accepted. The body may not exceed `MAX_BODY_BYTES` in `api.py`, and the limit applies again after decompression.

The decoder reads `resourceSpans`, `scopeSpans` and `spans`. The service of a span is the `service.name` resource attribute, with `unknown_service` as the fallback. Identifiers are hexadecimal text, 32 digits for a trace and 16 for a span, and an empty `parentSpanId` marks a root span. Timestamps and integer attribute values may be numbers or decimal strings. Attribute values of the string, boolean, integer and double kinds are kept, and other kinds are dropped, as are span events and links.

| Status | Cause |
| --- | --- |
| 200 | accepted, the body is `{}` |
| 400 | invalid JSON, a missing or malformed field, or an invalid gzip body |
| 413 | body larger than the limit |
| 415 | a content type other than `application/json`, or a content encoding other than gzip or identity |

A 400 from the decoder carries the path of the offending field:

```json
{"error": "resourceSpans[0].scopeSpans[0].spans[3].traceId: expected [0-9a-fA-F]{32} as hexadecimal text", "field": "resourceSpans[0].scopeSpans[0].spans[3].traceId"}
```

A 200 means the spans were buffered or counted, not that they were stored. Whether the trace is kept is decided after `decision_wait_seconds`.

## GET /v1/traces

Lists kept traces by their root span, longest first. A trace appears here only after it was decided and kept.

| Parameter | Meaning |
| --- | --- |
| `limit` | number of traces, from 1 to 200, default 20 |
| `service` | only root spans of this service |
| `min_duration_ms` | only root spans at least this long, default 0 |
| `errors_only` | `true`, `yes` or `1` keeps traces with any failed span |

The response has this shape:

```json
{
  "traces": [
    {
      "trace_id": "<32 hexadecimal characters>",
      "service": "<string>",
      "operation": "<string>",
      "start_unix_nano": "<integer>",
      "duration_ms": "<number>",
      "weight": "<number>",
      "reason": "<error, latency, baseline>",
      "failed": "<boolean>"
    }
  ]
}
```

A value outside the allowed range returns 400 with a message naming the parameter.

## GET /v1/traces/{trace_id}

Returns the stored spans of one trace, ordered by start time. The trace id is 32 hexadecimal digits, upper or lower case. Anything else returns 400. A trace that is not stored returns 404, with a message that it may have been dropped by sampling.

```json
{
  "trace_id": "<32 hexadecimal characters>",
  "reason": "<error, latency, baseline>",
  "weight": "<number>",
  "spans": [
    {
      "span_id": "<16 hexadecimal characters>",
      "parent_span_id": "<16 hexadecimal characters, empty for a root span>",
      "service": "<string>",
      "name": "<string>",
      "kind": "<integer>",
      "start_unix_nano": "<integer>",
      "end_unix_nano": "<integer>",
      "status_code": "<0, 1 or 2>",
      "status_message": "<string>",
      "attributes": "<object>"
    }
  ]
}
```

`weight` is the number of requests the trace stands for. It is 1 for traces kept because of an error or high latency, and 1 divided by the baseline rate for baseline traces.

## GET /v1/estimates

Sums the weights of stored root spans for each service and operation. `since` and `until` are optional Unix nanosecond bounds on the root span start time, inclusive and exclusive.

```json
{
  "since_unix_nano": "<integer or null>",
  "until_unix_nano": "<integer or null>",
  "total_estimated_requests": "<number>",
  "routes": [
    {
      "service": "<string>",
      "operation": "<string>",
      "estimated_requests": "<number>",
      "estimated_failed_requests": "<number>",
      "stored_traces": "<integer>"
    }
  ]
}
```

The failed count uses the weight of traces that contain any span with status code 2. Sampling error applies to `estimated_requests`, and `docs/evaluation.md` reports how large it was on the generated traffic.

## GET /metrics

Plain text in the Prometheus exposition format. The counters are `tailsampler_spans_received_total`, `tailsampler_spans_kept_total`, `tailsampler_spans_dropped_total`, `tailsampler_traces_kept_total`, `tailsampler_traces_dropped_total`, `tailsampler_late_spans_kept_total`, `tailsampler_late_spans_dropped_total` and `tailsampler_forced_decisions_total`, plus `tailsampler_traces_kept_by_reason_total` with a `reason` label. The gauges are `tailsampler_buffered_traces`, `tailsampler_buffered_spans`, `tailsampler_peak_buffered_traces`, `tailsampler_peak_buffered_spans` and `tailsampler_stored_spans`.

Watch `tailsampler_forced_decisions_total` and the late span counters. A rising forced decision count means the buffer limit is too low for the incoming rate, and late spans dropped mean the decision wait is shorter than the time spans take to arrive.

## GET /healthz

Returns `{"status":"ok"}` while the process can answer requests. It does not check the database.
