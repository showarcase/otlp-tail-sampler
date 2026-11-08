# 0002 SQLite as the span store

## Context

Kept spans have to be read back three ways: all spans of one trace, root spans by duration with a failure filter, and the sum of root weights per route. Stores designed for trace volumes exist, and a columnar database would suit the aggregate. This project has to run from a clean checkout on a laptop or a free CI runner with no service beyond the process itself.

## Decision

`SpanStore` uses the SQLite module of the Python standard library. The database runs in WAL mode with `synchronous=NORMAL`. One `spans` table holds a row per span, and each row repeats the weight and reason of its trace so that the queries are plain SQL over one table. The primary key is `(trace_id, span_id)` on a `WITHOUT ROWID` table, which makes `INSERT OR IGNORE` idempotent and lets a late span add its row. A secondary index covers `(service, start_unix_nano)`. One connection is shared and guarded by a lock.

## Consequences

The store needs no extra install, and the evaluation computes its estimates in an in-memory database. In the benchmarks it is the stage with the lowest throughput, and its per-call tail is the longest of any stage. Only kept spans reach it, so its cost applies to a small share of the traffic, but a sampler that kept more would reach the store's limit first.

There is a single writer and no retention. The database grows until it is deleted, and nothing compacts it. Repeating the weight and reason on every row costs space in exchange for simpler queries.

`search` orders root rows by duration and has no index for it. I did not measure how it behaves on a large database.

Moving to another store means reimplementing the methods of `SpanStore`, which are the only code that issues SQL. The estimate query is the one that would gain most from a columnar engine.
