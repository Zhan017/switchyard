# 6. No Redis call on the request path

Date: 2026-10-28 · Status: accepted

## Context

Breaker state has to be shared between replicas, or each replica would keep sending traffic to a provider the others already know is down. Redis is the shared store (spec decision table). The obvious design reads the breaker from Redis before each attempt and writes the outcome after it. That adds two round trips to every request, puts Redis latency inside the 10 ms overhead target, and makes Redis a dependency of every request, so a Redis outage would have to be handled on the request path too.

## Decision

The request path only touches in-process state. Each replica decides from its own copy of the breakers and health windows, and a background task syncs with Redis every 0.5 s:

- Health counts recorded since the last sync are added to per-bucket Redis hashes, and the merged window comes back, in one pipelined round trip. Buckets are numbered from the wall clock so replicas agree on them.
- A breaker change made locally (a trip, a forced state) is written with a compare-and-set Lua script on the record's version. A newer record from another replica is adopted. When two replicas changed the same breaker while apart, the newer change wins.
- One replica probes an open breaker per round, claimed with `SET NX`. A second Lua script applies each probe result in Redis, so a streak of good probes survives a change of prober, and closing clears the shared window.

When Redis is unreachable, nothing changes for requests: every replica keeps deciding from its own copy (fail open), keeps its unsent counts, and sends them when Redis returns.

## Consequences

- Zero Redis calls per request, and the same code path with or without Redis.
- Replicas agree within about half a second, not instantly. A replica can send a few requests to a provider another replica has just tripped on, and they fail over as usual.
- Two things stay per replica: the five-in-a-row rule (sharing it would need a call per attempt) and cooldowns (each replica gets its own 429s).
- Redis Cluster isn't supported: the scripts touch keys they derive rather than keys they are given.
- The scripts are tested on fakeredis and on a real Redis 7 in CI, including the race where a local change lands while a write is in flight.
