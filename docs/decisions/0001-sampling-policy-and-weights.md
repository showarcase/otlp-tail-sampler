# 0001 Sampling policy and weights

## Context

An operator searching traces wants the failed and slow ones, and also wants to read request counts from the stored sample. Keeping only the interesting traces breaks the counts, because the stored set is no longer a sample of the traffic. A head sampler with a fixed rate keeps the counts honest but throws away most failed and slow traces, since it decides before it knows how a request ended.

## Decision

`decide` in `policies.py` applies three rules to a buffered trace in order. A trace with a failed span is kept with weight 1. A trace whose root span is at least as long as the threshold for its route is kept with weight 1. Any other trace is kept when the hash of its trace id falls under the baseline rate, with weight 1 divided by that rate, and dropped otherwise.

Thresholds are per route and come from a quantile of calibration traffic, because routes differ widely in their usual latency and a single threshold would flag one route constantly and miss another. The baseline choice hashes the trace id instead of drawing a random number, so it is reproducible and any process that sees the same trace makes the same choice.

Estimates sum the weights of root spans. A span budget is matched by giving the head sampler the span share that the tail sampler kept.

## Consequences

Failed requests are counted exactly, as long as the failing span reaches the buffer before the trace is decided. Request counts are unbiased in expectation, but at a matched span budget they are not more accurate than a head sampler's, because part of the budget goes to error and slow traces and the baseline rate that remains is lower. `docs/evaluation.md` shows the numbers.

Two kinds of trace the policy was not built for are visible in the evaluation. A trace that takes an unusual path but runs fast and does not fail has no rule of its own. A small, steady latency shift that stays under the route threshold is mostly missed.

The error rule has no cap. During an error burst every failing trace is kept, so the kept volume rises with the failure rate and the span budget holds only while failures are rare.

Thresholds are a snapshot of the calibration traffic. When the latency profile of a route changes, `make train` has to be run again, and a route with too few calibration traces has no latency rule at all.
