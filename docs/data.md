# Data

The repository contains no external dataset. All traffic is produced by `tailsampler generate` (run through `make data`) from `configs/simulation.toml`, so the files can be rebuilt exactly and nothing is downloaded. The generated files are written to `data/generated/`, which `.gitignore` excludes.

## Intended use

The data exists to test a sampling policy against known ground truth. It is not a model of any real system, and a result measured on it says how the sampler behaves under the assumptions below, not how it would behave on production traffic.

## Generation

The fleet is a set of operations in `configs/simulation.toml`. Each operation belongs to a service, has a log-normal self time with its own median and spread, an error rate, and a list of calls to other operations, each with a probability. A call can be marked as background, in which case the caller does not wait for it. Operations with an entrypoint weight start a request, and the generator picks one by that weight.

`traffic.py` draws request arrivals from a Poisson process, walks the call tree for each request and records one span per operation call. A failing child makes its parent fail with a fixed probability, set by `propagate_error_probability`. Every trace gets a random 128-bit trace id from the seeded generator, and the generator records the ground truth for it: whether any span failed, the duration of the root, the span count, whether the rare path was taken, and which incident touched it.

Spans reach the gateway the way an exporter would send them. Each service flushes on its own interval with a random phase and sends the batch after a network delay drawn from a configured range. One service, notifications, flushes on a much longer interval than the rest, which is why some spans of a trace arrive long after the root span. Those are the late spans in the evaluation.

Two plans are generated with different seeds. The calibration plan has no incidents and is used only by `make train`. The evaluation plan has incidents and is used by `make evaluate`.

## Incidents

| Name | Operation | Kind | Effect |
| --- | --- | --- | --- |
| search-subtle-shift | search.index | latency | a small added delay on every call during a long window |
| payments-latency-regression | payments.charge | latency | a large added delay on part of the calls during a short window |
| inventory-error-burst | inventory.reserve | errors | part of the calls fail during a short window |

The exact windows, probabilities and delays are in `configs/simulation.toml`. The evaluation reports recall for the traces each incident touched.

## Files

| File | Content |
| --- | --- |
| `calibration.requests`, `evaluation.requests` | one line per export request: the send time in Unix nanoseconds, a tab, and the OTLP JSON body |
| `calibration.truth.jsonl`, `evaluation.truth.jsonl` | one JSON object per trace with `trace_id`, `root_operation`, `start_unix_nano`, `duration_nano`, `span_count`, `error`, `incident` and `rare_path` |
| `manifest.json` | the dataset summary used in the table below |

The request file is what a replay sends to the gateway, and the truth file is read only by the scoring code. The gateway never sees it.

## Sizes

The table is written by `scripts/render_results.py` from `results/metrics.json`.

<!-- results:begin dataset -->

| Dataset | Seed | Seconds | Traces | Spans | Export requests | Request file (MiB) | Error traces | Rare path traces |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| calibration | 11 | 450 | 27,073 | 121,384 | 9,716 | 32.1 | 122 | 26 |
| evaluation | 7 | 900 | 53,961 | 241,206 | 19,443 | 63.9 | 338 | 63 |

<!-- results:end dataset -->

## Trace categories

Recall is reported per category, and a trace can belong to more than one.

| Category | Definition |
| --- | --- |
| `error` | any span of the trace has status ERROR |
| `slow` | the root span lasts at least as long as the `slow_quantile` quantile of its route in the evaluation set |
| `rare_path` | the trace called an operation marked `rare` |
| one per incident | the trace was touched by that incident |
| `typical` | none of the above |

The slow cutoff is computed from the evaluation set itself, while the sampler's latency thresholds come from the calibration set. The two differ on purpose, so the evaluation does not hand the sampler its own cutoffs.

## Limits and risks

Latencies are log-normal and independent between operations, with no daily pattern, no queueing and no correlation between services. Real latency distributions have heavier tails and shift together, so a fitted quantile threshold will behave differently on them.

The fleet is small and each incident happens once. Recall per incident describes one occurrence of each, so it says little about how the sampler handles other incidents.

Trace ids come from a seeded generator. They are uniformly distributed, which the baseline hash relies on, and real ids may not be.

The simulator and the sampler were written together. The sampler has no access to the truth files, but I wrote both, and the simulator's export model reflects the behaviour I designed the buffer for. The results show the sampler working as designed on that model and do not validate the model.
