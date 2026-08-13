"""Durable storage for the module-level dicts that pin the API to one node.

The workbench keeps its application state in module dicts — `_SAVED_WORKFLOWS`,
`_DATASETS`, `_RUNS`, `_MODELS`, the auth token store. That is why a restart
loses every dataset, run and token, and why a second replica would serve
different data from the first. It is a data-design problem, not a deployment
one: no amount of load balancing fixes it.

This is the replacement. One DynamoDB table, partitioned by entity type and
sorted by id:

    pk = "workflow"        sk = "sw-abc123"
    pk = "dataset"         sk = "ds-abc123"
    pk = "model"           sk = "mdl-abc123"
    pk = "run"             sk = "run-abc123"

so fetching one item and listing all of a type are both a single call, and
neither is a scan.

Opt-in, like everything else here: without CMA_STATE_TABLE it falls back to an
in-process dict, so a developer with no AWS account sees exactly the old
behaviour. The fallback is also what makes the tests below meaningful — the
same code path is exercised either way.

Pydantic models go in as plain dicts and come back as dicts; converting back to
a model is the caller's job, because this layer should not import schemas.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

log = logging.getLogger("cma.entity_store")

# Used when no table is configured. Module-level on purpose: it is the very
# thing being replaced, kept only as a local-development fallback.
_MEMORY: dict[str, dict[str, dict]] = {}


# ── per-request memo ──────────────────────────────────────────────────────
# Moving the registries off module dicts turned what used to be a dict lookup
# into a network call, and some callers do that lookup per graph node: chat
# validation walks the canvas twice (once to label nodes, once to check
# features), and a workflow run resolves a model per step. Twenty nodes was
# twenty GetItems for maybe three distinct ids.
#
# So: memoize reads for the span of one request. A ContextVar rather than a
# module dict because that is what makes it *per request* — asyncio copies the
# context per task, so concurrent requests cannot see each other's entries and
# nothing survives to the next request. Default None means "no memo active",
# which is the old behaviour, so anything running outside a request (workers,
# startup) is unaffected.
#
# Correctness rests on two things. Writes go through this module, so `put` and
# `delete` refresh the entry rather than leaving it stale. And every hit is a
# deep copy, so a caller that mutates what it got back — `reintrospect_model`
# does exactly that — cannot corrupt what the next caller reads.
#
# The deliberate limit: within one request the registry is a snapshot. A long
# workflow run will not observe a model another request updates midway. For a
# run that is arguably the property you want; it is not a general-purpose
# cache, and it must never grow into one.
_CACHE: ContextVar[dict[tuple[str, str], dict | None] | None] = ContextVar(
    "entity_store_cache", default=None
)

_MISS = object()

# ── the secondary index ───────────────────────────────────────────────────
# One GSI, declared in infra/cdk/async_stack.py, keyed on these two
# attributes. Entities that want a time-ordered collection pass `index=` to
# `put`; everything else omits it and stays out of the index entirely, since a
# GSI only contains items that carry its key attributes.
INDEX_NAME = "gsi1"
_GSI_PK = "gsi1pk"
_GSI_SK = "gsi1sk"

# Attributes that belong to the storage layer, not the caller's model.
_KEY_ATTRS = ("pk", "sk", _GSI_PK, _GSI_SK)


def _strip(record: dict) -> dict:
    """Drop the key attributes so the caller gets back only its own fields."""
    return {k: v for k, v in record.items() if k not in _KEY_ATTRS}


def _encode_cursor(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, default=str).encode()).decode()


def _decode_cursor(cursor: str) -> dict | None:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        # An unreadable cursor is a client problem, not a server error: start
        # from the top rather than 500.
        log.warning("ignoring unreadable cursor")
        return None


@contextmanager
def request_cache():
    """Memoize `get` for the duration of the block.

    Re-entrant: if a memo is already active (the HTTP middleware opens one per
    request), the inner block joins it rather than starting a cold one.
    """
    if _CACHE.get() is not None:
        yield
        return
    token = _CACHE.set({})
    try:
        yield
    finally:
        _CACHE.reset(token)


def _cache_get(entity: str, item_id: str) -> Any:
    cache = _CACHE.get()
    if cache is None:
        return _MISS
    found = cache.get((entity, item_id), _MISS)
    return copy.deepcopy(found) if isinstance(found, dict) else found


def _cache_put(entity: str, item_id: str, value: dict | None) -> None:
    cache = _CACHE.get()
    if cache is not None:
        cache[(entity, item_id)] = copy.deepcopy(value) if value is not None else None


def table_name() -> str:
    return os.getenv("CMA_STATE_TABLE", "").strip()


def enabled() -> bool:
    return bool(table_name())


def _table():
    import boto3

    from cof.llm_config import bedrock_region

    region = os.getenv("CMA_STATE_REGION", "").strip() or bedrock_region()
    return boto3.resource("dynamodb", region_name=region).Table(table_name())


def _clean(value: Any) -> Any:
    """Make a value DynamoDB-safe.

    DynamoDB has no float type and rejects empty strings in key attributes, so
    the pragmatic path is a JSON round-trip through Decimal. Cheaper to reason
    about than a bespoke type walker, and these items are small.
    """
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def _restore(value: Any) -> Any:
    """Turn Decimals back into ints/floats on the way out."""
    if isinstance(value, list):
        return [_restore(v) for v in value]
    if isinstance(value, dict):
        return {k: _restore(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if as_int == value else float(value)
    return value


# ── operations ────────────────────────────────────────────────────────────
def put(
    entity: str,
    item_id: str,
    item: dict,
    index: tuple[str, str] | None = None,
) -> dict:
    """Write an item. `index` is an optional (gsi1pk, gsi1sk) pair.

    Passing `index` puts the item in the secondary index; omitting it keeps
    the item out, which is what every entity except runs wants.
    """
    record = dict(item)
    if index is not None:
        record[_GSI_PK], record[_GSI_SK] = index

    if not enabled():
        # The fallback keeps the index attributes so `query_index` has
        # something to emulate against, and strips them on the way out.
        _MEMORY.setdefault(entity, {})[item_id] = record
        _cache_put(entity, item_id, _strip(record))
        return item

    cleaned = _clean(record)
    record = dict(cleaned)
    record["pk"] = entity
    record["sk"] = item_id
    _table().put_item(Item=record)
    # Refresh the memo so a read-modify-write inside one request sees its own
    # change. Memoize what a subsequent `get` would return rather than what we
    # were handed: the Decimal round-trip is not value-preserving (a whole
    # float in an untyped field comes back an int), and a read-after-write in
    # this request must not disagree with the same read in the next one.
    _cache_put(entity, item_id, _strip(_restore(cleaned)))
    return item


def get(entity: str, item_id: str) -> dict | None:
    memoized = _cache_get(entity, item_id)
    if memoized is not _MISS:
        return memoized

    if not enabled():
        found = _MEMORY.get(entity, {}).get(item_id)
        # Copy on the way out: without one the caller holds the fallback's own
        # object, and the memo would hand later callers that same aliased dict.
        found = _strip(copy.deepcopy(found)) if found is not None else None
        _cache_put(entity, item_id, found)
        return found

    response = _table().get_item(Key={"pk": entity, "sk": item_id})
    found = response.get("Item")
    if not found:
        # Memoize the miss too — validation asks about the same dangling
        # ref_id on every pass over the canvas.
        _cache_put(entity, item_id, None)
        return None
    found = _strip(_restore(found))
    _cache_put(entity, item_id, found)
    return found


def delete(entity: str, item_id: str) -> None:
    _cache_put(entity, item_id, None)
    if not enabled():
        _MEMORY.get(entity, {}).pop(item_id, None)
        return
    _table().delete_item(Key={"pk": entity, "sk": item_id})


def list_all(entity: str) -> list[dict]:
    """Every item of a type. A Query on the partition, never a table scan.

    Never served from the memo — a listing has to be authoritative and the
    memo has no way to know it holds every id. It does *populate* the memo
    though, which is the payoff for endpoints that list and then look up:
    the per-item reads that follow are free.
    """
    if not enabled():
        items = [_strip(copy.deepcopy(i)) for i in _MEMORY.get(entity, {}).values()]
        for found in items:
            _cache_put(entity, str(found.get("id", "")), found)
        return items

    from boto3.dynamodb.conditions import Key

    items = []
    kwargs: dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(entity)}
    while True:
        response = _table().query(**kwargs)
        for found in response.get("Items", []):
            found = _restore(found)
            item_id = str(found.get("sk") or found.get("id", ""))
            found = _strip(found)
            _cache_put(entity, item_id, found)
            items.append(found)
        token = response.get("LastEvaluatedKey")
        if not token:
            return items
        kwargs["ExclusiveStartKey"] = token


class IndexUnavailable(RuntimeError):
    """The secondary index is not deployed yet.

    Raised so a caller can fall back to the slow path rather than 500. It is
    the expected state between shipping code that queries the index and
    running `cdk deploy` to create it.
    """


def query_index(
    index_pk: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
    descending: bool = True,
) -> tuple[list[dict], str | None]:
    """Read one page of a time-ordered collection, newest first by default.

    This is the whole point of the index: the read stops after `limit` items
    instead of walking the entity's entire partition, so the cost of a page
    does not depend on how many items exist.

    Returns (items, next_cursor). `next_cursor` is None on the last page, and
    is an opaque token — its contents differ between the real table and the
    in-memory fallback, so it is not portable between them.
    """
    if not enabled():
        return _query_index_memory(index_pk, limit, cursor, descending)

    from boto3.dynamodb.conditions import Key

    kwargs: dict[str, Any] = {
        "IndexName": INDEX_NAME,
        "KeyConditionExpression": Key(_GSI_PK).eq(index_pk),
        "ScanIndexForward": not descending,
        "Limit": limit,
    }
    start = _decode_cursor(cursor) if cursor else None
    if start:
        kwargs["ExclusiveStartKey"] = start

    try:
        response = _table().query(**kwargs)
    except Exception as e:
        if "ValidationException" in f"{type(e).__name__}: {e}" and INDEX_NAME in str(e):
            raise IndexUnavailable(str(e)) from e
        raise

    items = []
    for found in response.get("Items", []):
        found = _restore(found)
        item_id = str(found.get("sk") or found.get("id", ""))
        found = _strip(found)
        # Same warm-up as list_all: a page of runs makes the per-run reads
        # that follow it free.
        _cache_put(_entity_of(index_pk), item_id, found)
        items.append(found)

    token = response.get("LastEvaluatedKey")
    return items, (_encode_cursor(_restore(token)) if token else None)


def _entity_of(index_pk: str) -> str:
    """`run#capital_planning` -> `run`, so index reads warm the right memo."""
    return index_pk.split("#", 1)[0]


def _query_index_memory(
    index_pk: str, limit: int, cursor: str | None, descending: bool,
) -> tuple[list[dict], str | None]:
    """Emulate the index over `_MEMORY`, so local development behaves the same.

    Deliberately does the expensive thing — walk everything, then sort. The
    point of the fallback is matching behaviour, not matching performance.
    """
    matches: list[tuple[str, str, dict]] = []
    for items in _MEMORY.values():
        for record in items.values():
            if record.get(_GSI_PK) == index_pk:
                matches.append(
                    (str(record.get(_GSI_SK, "")), str(record.get("id", "")), record)
                )
    matches.sort(key=lambda m: (m[0], m[1]), reverse=descending)

    start = _decode_cursor(cursor) if cursor else None
    if start:
        after = (str(start.get("sk", "")), str(start.get("id", "")))
        keep = False
        remaining = []
        for m in matches:
            if keep:
                remaining.append(m)
            elif (m[0], m[1]) == after:
                keep = True
        matches = remaining

    page = matches[:limit]
    items = [_strip(copy.deepcopy(m[2])) for m in page]
    for found in items:
        _cache_put(_entity_of(index_pk), str(found.get("id", "")), found)

    next_cursor = None
    if len(matches) > limit and page:
        next_cursor = _encode_cursor({"sk": page[-1][0], "id": page[-1][1]})
    return items, next_cursor


def status() -> dict:
    info = {"enabled": enabled(), "table": table_name() or "(unset, using memory)"}
    if enabled():
        try:
            info["item_count_estimate"] = _table().item_count
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"
    return info
