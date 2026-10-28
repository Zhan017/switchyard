"""Breaker state and health counts shared between replicas through Redis (spec 7.4, 7.9).

The request path never talks to Redis. Each replica decides from its in-process copy and
a background task syncs every ``shared_sync_s`` (0.5 s):

1. Health counts recorded since the last sync are added to per-bucket Redis hashes, and
   the merged buckets for the window are read back, in one pipelined round trip.
2. Breaker records are read. A newer record written by another replica is adopted. A
   local change (a trip, a forced state) is written with a compare-and-set Lua script on
   the record's version; if another replica got there first, the newer change wins.
3. Open breakers are probed by one replica at a time: a probe slot is claimed with
   ``SET NX``, and the probe result is applied by a Lua script, so the streak of good
   probes survives a change of prober.

If Redis is unreachable, every replica keeps deciding from its own copy (fail open) and
keeps its unsent counts; they are sent when Redis returns. The consecutive-failure rule
always counts per replica. Cooldowns (429 and bad keys) are per replica too: each replica
gets its own 429s. Redis Cluster isn't supported: the scripts touch keys they derive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import replace
from typing import Any, cast

import redis.asyncio as redis
from redis.exceptions import RedisError

from switchyard.observability.metrics import Metrics
from switchyard.resilience.breaker import BreakerRecord, Breakers
from switchyard.resilience.health import Bucket

log = logging.getLogger("switchyard.shared")

PREFIX = "sy"
BUCKET_TTL_S = 300

# KEYS[1]: breaker hash. ARGV: expected version, state, forced, reason, changed_at,
# streak, bucket key prefix, current bucket, buckets kept.
TRANSITION = """
local current = tonumber(redis.call('HGET', KEYS[1], 'version') or '0')
if current ~= tonumber(ARGV[1]) then
  return redis.call('HGETALL', KEYS[1])
end
redis.call('HSET', KEYS[1], 'state', ARGV[2], 'forced', ARGV[3], 'reason', ARGV[4],
  'changed_at', ARGV[5], 'streak', ARGV[6], 'version', current + 1)
if ARGV[2] == 'closed' and ARGV[3] == '' then
  local now = tonumber(ARGV[8])
  for i = now - tonumber(ARGV[9]) + 1, now do redis.call('DEL', ARGV[7] .. i) end
end
return redis.call('HGETALL', KEYS[1])
"""

# KEYS[1]: breaker hash. ARGV: 1 or 0 for the probe result, successes needed, changed_at,
# bucket key prefix, current bucket, buckets kept.
PROBE = """
if redis.call('HGET', KEYS[1], 'state') ~= 'open' then
  return redis.call('HGETALL', KEYS[1])
end
local forced = redis.call('HGET', KEYS[1], 'forced')
if forced and forced ~= '' then
  return redis.call('HGETALL', KEYS[1])
end
if ARGV[1] == '1' then
  local streak = redis.call('HINCRBY', KEYS[1], 'streak', 1)
  if streak >= tonumber(ARGV[2]) then
    redis.call('HSET', KEYS[1], 'state', 'closed', 'streak', 0, 'changed_at', ARGV[3],
      'reason', streak .. ' probes in a row succeeded')
    redis.call('HINCRBY', KEYS[1], 'version', 1)
    local now = tonumber(ARGV[5])
    for i = now - tonumber(ARGV[6]) + 1, now do redis.call('DEL', ARGV[4] .. i) end
  end
else
  redis.call('HSET', KEYS[1], 'streak', 0)
end
return redis.call('HGETALL', KEYS[1])
"""


def breaker_key(model: str) -> str:
    return f"{PREFIX}:br:{model}"


def bucket_prefix(model: str) -> str:
    return f"{PREFIX}:h:{model}:"


def probe_key(model: str) -> str:
    return f"{PREFIX}:probe:{model}"


def parse_record(raw: Any) -> BreakerRecord | None:
    """A breaker record from HGETALL (a dict) or from a script (a flat list)."""
    if isinstance(raw, list):
        raw = dict(zip(raw[::2], raw[1::2], strict=True))
    if not raw:
        return None
    data = cast(dict[str, str], raw)
    state = data.get("state", "closed")
    forced = data.get("forced") or None
    return BreakerRecord(
        state="open" if state == "open" else "closed",
        forced=forced if forced in ("open", "closed") else None,  # type: ignore[arg-type]
        reason=data.get("reason", ""),
        version=int(data.get("version", "0")),
        changed_at=float(data.get("changed_at", "0")),
        streak=int(data.get("streak", "0")),
    )


class SharedState:
    def __init__(self, client: redis.Redis, breakers: Breakers, metrics: Metrics) -> None:
        self.client = client
        self.breakers = breakers
        self.metrics = metrics
        self.replica = uuid.uuid4().hex[:8]
        self.up = False
        self.task: asyncio.Task[None] | None = None
        self.wake = asyncio.Event()
        self._transition = client.register_script(TRANSITION)
        self._probe = client.register_script(PROBE)

    @classmethod
    def from_url(cls, url: str, breakers: Breakers, metrics: Metrics) -> SharedState:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=0.25,
            socket_connect_timeout=0.25,
            health_check_interval=0,
        )
        return cls(client, breakers, metrics)

    async def start(self) -> None:
        await self.sync()  # adopt the shared state before serving, so a restart keeps it
        self.task = asyncio.create_task(self._run(), name="switchyard-shared-state")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        with contextlib.suppress(RedisError, OSError):
            await self.sync()
        await self.client.aclose()

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=self.breakers.policy.shared_sync_s)
            self.wake.clear()
            await self.sync()

    def _set_up(self, up: bool, error: BaseException | None = None) -> None:
        if up != self.up:
            if up:
                log.info("Redis reachable: breakers and health are shared again")
            else:
                log.warning("Redis unreachable, deciding from local state: %s", error)
        self.up = up
        self.metrics.shared_state_up.set(1 if up else 0)

    async def sync(self) -> bool:
        breakers = self.breakers
        keys = list(breakers.models)
        now = breakers.index()
        keep = breakers.buckets_kept()
        sent: dict[str, dict[int, Bucket]] = {}
        for key in keys:
            sent[key] = breakers.model(key).health.take_pending()
        try:
            async with asyncio.timeout(1.0):
                pipe = self.client.pipeline(transaction=False)
                for key, buckets in sent.items():
                    for index, bucket in buckets.items():
                        name = bucket_prefix(key) + str(index)
                        for field_name, value in bucket.fields().items():
                            pipe.hincrby(name, field_name, value)
                        pipe.expire(name, BUCKET_TTL_S)
                for key in keys:
                    for index in range(now - keep + 1, now + 1):
                        pipe.hgetall(bucket_prefix(key) + str(index))
                    pipe.hgetall(breaker_key(key))
                results = await pipe.execute()
                tail = results[len(results) - len(keys) * (keep + 1) :]
                for i, key in enumerate(keys):
                    if key not in breakers.models:
                        continue  # removed by a config reload during the round trip
                    chunk = tail[i * (keep + 1) : (i + 1) * (keep + 1)]
                    merged = {}
                    for offset, raw in enumerate(chunk[:keep]):
                        if raw:
                            merged[now - keep + 1 + offset] = Bucket.from_fields(
                                {k: int(v) for k, v in raw.items()}
                            )
                    state = breakers.model(key)
                    state.health.sync_done(merged, now)
                    await self._reconcile(key, parse_record(chunk[keep]))
        except (RedisError, OSError, TimeoutError) as error:
            for key in keys:
                if key in breakers.models:
                    breakers.model(key).health.sync_failed()
            self.metrics.shared_state_errors.inc()
            self._set_up(False, error)
            return False
        self._set_up(True)
        return True

    async def _reconcile(self, key: str, remote: BreakerRecord | None) -> None:
        local = self.breakers.model(key).breaker
        if not local.dirty:
            if remote is not None and remote.version > local.version:
                self.breakers.adopt(key, remote)
            return
        expected = local.version
        if remote is not None and remote.version != local.version:
            if remote.changed_at > local.changed_at:
                self.breakers.adopt(key, remote)  # another replica's change is newer
                return
            expected = remote.version  # ours is newer, for example made while Redis was down
        # The request path and the admin API keep changing the local record while the
        # write is in flight, so write a snapshot and never adopt over a newer local change.
        sent = replace(local)
        written = parse_record(
            await self._transition(
                keys=[breaker_key(key)],
                args=[
                    expected,
                    sent.state,
                    sent.forced or "",
                    sent.reason,
                    repr(sent.changed_at),
                    sent.streak,
                    bucket_prefix(key),
                    self.breakers.index(),
                    self.breakers.buckets_kept(),
                ],
            )
        )
        if written is None or key not in self.breakers.models:
            return
        current = self.breakers.model(key).breaker
        changed_meanwhile = current.changed_at != sent.changed_at
        if written.version == expected + 1 and written.changed_at == sent.changed_at:
            current.version = written.version
            if not changed_meanwhile:
                current.dirty = False  # otherwise the newer change goes out next sync
        elif not changed_meanwhile:
            self.breakers.adopt(key, written)

    # Probes

    async def claim_probe(self, key: str) -> bool:
        """True if this replica should send the next probe for ``key``."""
        if not self.up:
            return True
        interval_ms = int(self.breakers.policy.probe_interval_s * 1000)
        try:
            claimed = await self.client.set(
                probe_key(key), self.replica, nx=True, px=max(100, interval_ms - 500)
            )
        except (RedisError, OSError) as error:
            self._set_up(False, error)
            return True
        return bool(claimed)

    async def report_probe(self, key: str, ok: bool) -> None:
        local = self.breakers.model(key).breaker
        before = local.changed_at
        if self.up and not local.dirty:
            try:
                raw = await self._probe(
                    keys=[breaker_key(key)],
                    args=[
                        1 if ok else 0,
                        self.breakers.policy.probe_successes,
                        repr(time.time()),
                        bucket_prefix(key),
                        self.breakers.index(),
                        self.breakers.buckets_kept(),
                    ],
                )
                record = parse_record(raw)
                current = self.breakers.model(key).breaker
                if record is not None:
                    if current.changed_at != before:
                        return  # changed locally meanwhile (forced, say); the next sync decides
                    current.streak = record.streak
                    if record.version > current.version:
                        self.breakers.adopt(key, record, origin="local")
                    return
            except (RedisError, OSError) as error:
                self._set_up(False, error)
        self.breakers.probe_result(key, ok)
        self.wake.set()
