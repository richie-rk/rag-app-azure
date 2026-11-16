"""An in-memory Cosmos DB container for unit tests (`84-testing.md` sections 1.1 and 1.2).

`FakeContainer` serves the calls the API's stores make on an `azure.cosmos.aio.ContainerProxy` —
`read_item`, `create_item`, `replace_item`, `upsert_item`, `patch_item`, `delete_item`,
`execute_item_batch` and `query_items` — over a dictionary keyed by partition key and `id`, with
the service's semantics the stores rely on: an `_etag` that changes on every write, `404`, `409`
and `412` raised as the SDK's own exceptions, ETag conditions on replace, patch and delete and on
batch operations (`if_match_etag`), all-or-nothing transactional batches that fail with
`CosmosBatchOperationError` naming the failed operation, patch operations (`set`, `incr`, `add`,
`replace`, `remove`), paged parameterised queries over a small `SELECT` subset (comparisons and
`ARRAY_CONTAINS` joined by `AND` and `OR` with parentheses, `TOP`, and an `ORDER BY` over one or
more paths), and the `response_hook` with a request charge.

Every call is recorded with its keyword arguments, so a test checks the `timeout` and the
conditions a call carried. `fail` injects a failure into the next matching calls: an exception
raised before the operation, one raised after it committed (a response lost after commit), or a
call that never answers. A never-answering call honours the per-operation `timeout` keyword as
the SDK does: it moves the attached `FakeClock` by the timeout and raises
`CosmosClientTimeoutError`; without a `timeout` it waits forever, so code that forgets its
timeout hangs its test. `before` runs a callback before the next matching call, for a concurrent
write between a read and a conditioned write. The Cosmos DB emulator stays the authority: the
integration tests run the same code against it.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import itertools
import operator
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosBatchOperationError,
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from docchat_core.cosmos.containers import ContainerDefinition
from docchat_testing.clock import FakeClock

REQUEST_CHARGE_HEADER: Final = "x-ms-request-charge"
FAILED_DEPENDENCY: Final = 424
"""The status of every other operation of a failed transactional batch."""

type Key = tuple[str, ...]
type Callback = Callable[[], object]

_SELECT_RE: Final = re.compile(
    r"^SELECT (?:TOP (?P<top>@?\w+) )?(?P<fields>.+?) FROM c"
    r"(?: WHERE (?P<where>.+?))?"
    r"(?: ORDER BY (?P<order>c\.[\w.]+(?: (?:ASC|DESC))?(?:, c\.[\w.]+(?: (?:ASC|DESC))?)*))?$",
    re.IGNORECASE,
)
_ORDER_TERM_RE: Final = re.compile(r"^c\.(?P<path>[\w.]+)(?: (?P<direction>ASC|DESC))?$", re.I)
_FIELD_RE: Final = re.compile(r"^c\.(?P<path>[\w.]+)(?: AS (?P<alias>\w+))?$", re.IGNORECASE)
_COMPARISON_RE: Final = re.compile(
    r"^c\.(?P<path>[\w.]+) (?P<op>=|!=|<>|>=|<=|>|<) (?P<param>@\w+)$"
)
_CONTAINS_RE: Final = re.compile(
    r"^ARRAY_CONTAINS\((?P<param>@\w+), c\.(?P<path>[\w.]+)\)$", re.IGNORECASE
)
_COMPARE: Final[Mapping[str, Callable[[Any, Any], bool]]] = {
    "=": operator.eq,
    "!=": operator.ne,
    "<>": operator.ne,
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
}


def _make[E: BaseException](cls: Callable[..., E], **kwargs: Any) -> E:
    # The SDK's exception constructors are untyped; the call through a typed callable isn't.
    return cls(**kwargs)


def not_found() -> CosmosResourceNotFoundError:
    return _make(
        CosmosResourceNotFoundError,
        status_code=int(HTTPStatus.NOT_FOUND),
        message="Entity with the specified id does not exist",
    )


def conflict() -> CosmosResourceExistsError:
    return _make(
        CosmosResourceExistsError,
        status_code=int(HTTPStatus.CONFLICT),
        message="Entity with the specified id already exists",
    )


def precondition_failed() -> CosmosAccessConditionFailedError:
    return _make(
        CosmosAccessConditionFailedError,
        status_code=int(HTTPStatus.PRECONDITION_FAILED),
        message="One of the specified pre-condition is not met",
    )


def http_error(status: int) -> CosmosHttpResponseError:
    """A service answer with `status`: a `429` left after the SDK's retries, a `503`, a `408`."""
    return _make(CosmosHttpResponseError, status_code=int(status), message=f"status {status}")


def batch_error(index: int, status: int, operations: int) -> CosmosBatchOperationError:
    """A transactional batch whose operation `index` failed with `status`."""
    responses = [{"statusCode": FAILED_DEPENDENCY} for _ in range(operations)]
    responses[index] = {"statusCode": int(status)}
    return _make(
        CosmosBatchOperationError,
        error_index=index,
        headers={},
        status_code=int(status),
        message=f"operation {index} failed with {status}",
        operation_responses=responses,
    )


def _error_for(status: int) -> CosmosHttpResponseError:
    match status:
        case HTTPStatus.NOT_FOUND:
            return not_found()
        case HTTPStatus.CONFLICT:
            return conflict()
        case HTTPStatus.PRECONDITION_FAILED:
            return precondition_failed()
        case _:
            return http_error(status)


class _OperationFailed(Exception):  # noqa: N818 - internal signal carrying a status
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True, slots=True)
class FakeCall:
    """One recorded call."""

    method: str
    item_id: str | None
    partition_key: Key | None
    kwargs: Mapping[str, Any]
    query: str | None = None


@dataclass(slots=True)
class _Fault:
    method: str
    item_id: str | None
    error: BaseException | Callable[[], BaseException] | None
    hang: bool
    after_commit: bool
    remaining: int

    def matches(self, method: str, item_id: str | None) -> bool:
        return (
            self.remaining > 0
            and self.method == method
            and (self.item_id is None or self.item_id == item_id)
        )

    def raised(self) -> BaseException:
        error = self.error
        if error is None:
            raise AssertionError("a fault without an error")
        return error() if callable(error) else error


@dataclass(slots=True)
class _Hook:
    method: str
    item_id: str | None
    callback: Callback
    remaining: int


def _get_path(item: Mapping[str, Any], path: str) -> Any:
    value: Any = item
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _pointer(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError(f"a patch path starts with /: {path}")
    return path[1:].split("/")


def _apply_patch(item: dict[str, Any], operations: Sequence[Mapping[str, Any]]) -> None:
    for operation in operations:
        op = operation["op"]
        parts = _pointer(operation["path"])
        parent: Any = item
        for part in parts[:-1]:
            parent = parent.setdefault(part, {})
        last = parts[-1]
        match op:
            case "set" | "add":
                parent[last] = copy.deepcopy(operation["value"])
            case "replace":
                if last not in parent:
                    raise _OperationFailed(HTTPStatus.BAD_REQUEST)
                parent[last] = copy.deepcopy(operation["value"])
            case "remove":
                if last not in parent:
                    raise _OperationFailed(HTTPStatus.BAD_REQUEST)
                del parent[last]
            case "incr":
                current = parent.get(last, 0)
                parent[last] = current + operation["value"]
            case _:
                raise ValueError(f"unsupported patch operation {op!r}")


class FakePages:
    """The pager `query_items` returns: `by_page()` fetches one page per step."""

    def __init__(
        self, container: FakeContainer, rows: list[dict[str, Any]], kwargs: Mapping[str, Any]
    ) -> None:
        self._container = container
        self._rows = rows
        self._kwargs = kwargs
        size = kwargs.get("max_item_count")
        self._size = size if isinstance(size, int) and size > 0 else max(len(rows), 1)

    def by_page(self, continuation_token: str | None = None) -> FakePageIterator:
        start = int(continuation_token) if continuation_token else 0
        return FakePageIterator(self, start)

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async for page in self.by_page():
            async for row in page:
                yield row


class FakePageIterator:
    """Pages in order; `continuation_token` is `None` after the last page."""

    def __init__(self, pages: FakePages, start: int) -> None:
        self._pages = pages
        self._next = start
        self._fetched_any = False

    @property
    def continuation_token(self) -> str | None:
        if not self._fetched_any or self._next >= len(self._pages._rows):
            return None
        return str(self._next)

    def __aiter__(self) -> FakePageIterator:
        return self

    async def __anext__(self) -> AsyncIterator[dict[str, Any]]:
        pages = self._pages
        if self._fetched_any and self._next >= len(pages._rows):
            raise StopAsyncIteration
        await pages._container._enter("query_items", None, pages._kwargs)
        page = pages._rows[self._next : self._next + pages._size]
        self._next += len(page)
        self._fetched_any = True
        pages._container.pages_served += 1
        pages._container._charge(pages._kwargs, {"Documents": page})
        return _AsyncRows(copy.deepcopy(page))


class _AsyncRows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = iter(rows)

    def __aiter__(self) -> _AsyncRows:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._rows)
        except StopIteration:
            raise StopAsyncIteration from None


class FakeContainer:
    """One container's items and the recorded calls on it."""

    def __init__(
        self,
        name: str,
        partition_key_paths: Sequence[str] = ("/userId",),
        *,
        clock: FakeClock | None = None,
        request_charge: float = 1.0,
    ) -> None:
        self.id = name
        self.partition_key_paths = tuple(path.lstrip("/") for path in partition_key_paths)
        self.clock = clock
        self.request_charge = request_charge
        self.items: dict[tuple[Key, str], dict[str, Any]] = {}
        self.calls: list[FakeCall] = []
        self.pages_served = 0
        self._etags = itertools.count(1)
        self._faults: list[_Fault] = []
        self._hooks: list[_Hook] = []

    @classmethod
    def of(
        cls, definition: ContainerDefinition, *, clock: FakeClock | None = None
    ) -> FakeContainer:
        """A container with a production container's name and partition key."""
        return cls(definition.name, definition.partition_key_paths, clock=clock)

    # Test controls --------------------------------------------------------------------------

    def fail(
        self,
        method: str,
        *,
        error: BaseException | Callable[[], BaseException] | None = None,
        status: int | None = None,
        hang: bool = False,
        item_id: str | None = None,
        times: int = 1,
        after_commit: bool = False,
    ) -> None:
        """Make the next `times` calls of `method` (on `item_id`, when given) raise `error` (or
        the SDK's exception for `status`), or never answer (`hang`). With `after_commit` the
        operation is applied first and the error raised after it, as when a response is lost."""
        raised = error
        if status is not None:
            if error is not None:
                raise ValueError("give error or status, not both")
            fixed_status = status

            def raised() -> BaseException:
                return _error_for(fixed_status)

        if (raised is None) == (not hang):
            raise ValueError("a fault raises an error or hangs")
        self._faults.append(_Fault(method, item_id, raised, hang, after_commit, times))

    def before(
        self, method: str, callback: Callback, *, item_id: str | None = None, times: int = 1
    ) -> None:
        """Run `callback` before the next `times` matching calls are served."""
        self._hooks.append(_Hook(method, item_id, callback, times))

    def calls_of(self, method: str) -> list[FakeCall]:
        return [call for call in self.calls if call.method == method]

    def key_of(self, body: Mapping[str, Any]) -> Key:
        values = [body.get(path) for path in self.partition_key_paths]
        if any(value is None for value in values):
            raise ValueError(f"the item lacks its partition key {self.partition_key_paths}")
        return tuple(str(value) for value in values)

    def get(self, partition_key: str | Sequence[str], item_id: str) -> dict[str, Any] | None:
        """A copy of a stored item, or `None`."""
        item = self.items.get((_key(partition_key), item_id))
        return copy.deepcopy(item) if item is not None else None

    def put(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Store `body` directly (seeding a test), with a fresh `_etag`; returns the copy."""
        stored = self._stamped(dict(body))
        self.items[(self.key_of(stored), str(stored["id"]))] = stored
        return copy.deepcopy(stored)

    def touch(self, partition_key: str | Sequence[str], item_id: str, **changes: Any) -> str:
        """Change a stored item as a concurrent writer would; returns its new `_etag`."""
        key = (_key(partition_key), item_id)
        stored = self._stamped({**self.items[key], **changes})
        self.items[key] = stored
        return str(stored["_etag"])

    # Internals ------------------------------------------------------------------------------

    def _stamped(self, body: dict[str, Any]) -> dict[str, Any]:
        body = copy.deepcopy(body)
        body["_etag"] = f'"{next(self._etags):08x}-0000-0000-0000-000000000000"'
        body["_ts"] = int(self.clock.now().timestamp()) if self.clock is not None else 0
        return body

    def _charge(self, kwargs: Mapping[str, Any], body: object) -> None:
        hook = kwargs.get("response_hook")
        if callable(hook):
            hook({REQUEST_CHARGE_HEADER: str(self.request_charge)}, body)

    async def _hang(self, kwargs: Mapping[str, Any]) -> None:
        timeout = kwargs.get("timeout")
        if timeout is None:
            await asyncio.Event().wait()
        if self.clock is not None:
            self.clock.advance(float(str(timeout)))
        await asyncio.sleep(0)
        raise CosmosClientTimeoutError

    async def _enter(
        self, method: str, item_id: str | None, kwargs: Mapping[str, Any]
    ) -> _Fault | None:
        """Run the hooks and the pre-commit faults of a call; return an after-commit fault."""
        for hook in self._hooks:
            if hook.remaining > 0 and hook.method == method and hook.item_id in (None, item_id):
                hook.remaining -= 1
                result = hook.callback()
                if inspect.isawaitable(result):
                    await result
        for fault in self._faults:
            if fault.matches(method, item_id):
                fault.remaining -= 1
                if fault.hang:
                    await self._hang(kwargs)
                if fault.after_commit:
                    return fault
                raise fault.raised()
        await asyncio.sleep(0)
        return None

    def _record(
        self,
        method: str,
        item_id: str | None,
        partition_key: Key | None,
        kwargs: Mapping[str, Any],
        query: str | None = None,
    ) -> None:
        self.calls.append(FakeCall(method, item_id, partition_key, dict(kwargs), query))

    @staticmethod
    def _condition_holds(stored: Mapping[str, Any] | None, kwargs: Mapping[str, Any]) -> bool:
        etag = kwargs.get("etag")
        condition = kwargs.get("match_condition")
        if etag is None or condition is None:
            return True
        if condition is MatchConditions.IfNotModified:
            return stored is not None and stored.get("_etag") == etag
        if condition is MatchConditions.IfModified:
            return stored is None or stored.get("_etag") != etag
        raise ValueError(f"unsupported match condition {condition!r}")

    async def _finish[T](self, fault: _Fault | None, result: T) -> T:
        if fault is not None:
            raise fault.raised()
        return result

    # The container's calls ------------------------------------------------------------------

    async def read_item(
        self, item: str, partition_key: str | Sequence[str], **kwargs: Any
    ) -> dict[str, Any]:
        key = _key(partition_key)
        self._record("read_item", item, key, kwargs)
        fault = await self._enter("read_item", item, kwargs)
        stored = self.items.get((key, item))
        if stored is None:
            raise not_found()
        result = copy.deepcopy(stored)
        self._charge(kwargs, result)
        return await self._finish(fault, result)

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        key, item_id = self.key_of(body), str(body["id"])
        self._record("create_item", item_id, key, kwargs)
        fault = await self._enter("create_item", item_id, kwargs)
        if (key, item_id) in self.items:
            raise conflict()
        stored = self._stamped(body)
        self.items[(key, item_id)] = stored
        result = copy.deepcopy(stored)
        self._charge(kwargs, result)
        return await self._finish(fault, result)

    async def upsert_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        key, item_id = self.key_of(body), str(body["id"])
        self._record("upsert_item", item_id, key, kwargs)
        fault = await self._enter("upsert_item", item_id, kwargs)
        if not self._condition_holds(self.items.get((key, item_id)), kwargs):
            raise precondition_failed()
        stored = self._stamped(body)
        self.items[(key, item_id)] = stored
        result = copy.deepcopy(stored)
        self._charge(kwargs, result)
        return await self._finish(fault, result)

    async def replace_item(self, item: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        key = self.key_of(body)
        self._record("replace_item", item, key, kwargs)
        fault = await self._enter("replace_item", item, kwargs)
        current = self.items.get((key, item))
        if current is None:
            raise not_found()
        if not self._condition_holds(current, kwargs):
            raise precondition_failed()
        stored = self._stamped(body)
        self.items[(key, item)] = stored
        result = copy.deepcopy(stored)
        self._charge(kwargs, result)
        return await self._finish(fault, result)

    async def patch_item(
        self,
        item: str,
        partition_key: str | Sequence[str],
        patch_operations: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        key = _key(partition_key)
        self._record("patch_item", item, key, kwargs)
        fault = await self._enter("patch_item", item, kwargs)
        current = self.items.get((key, item))
        if current is None:
            raise not_found()
        if not self._condition_holds(current, kwargs):
            raise precondition_failed()
        patched = copy.deepcopy(current)
        try:
            _apply_patch(patched, patch_operations)
        except _OperationFailed as failed:
            raise _error_for(failed.status) from None
        stored = self._stamped(patched)
        self.items[(key, item)] = stored
        result = copy.deepcopy(stored)
        self._charge(kwargs, result)
        return await self._finish(fault, result)

    async def delete_item(
        self, item: str, partition_key: str | Sequence[str], **kwargs: Any
    ) -> None:
        key = _key(partition_key)
        self._record("delete_item", item, key, kwargs)
        fault = await self._enter("delete_item", item, kwargs)
        current = self.items.get((key, item))
        if current is None:
            raise not_found()
        if not self._condition_holds(current, kwargs):
            raise precondition_failed()
        del self.items[(key, item)]
        self._charge(kwargs, None)
        await self._finish(fault, None)

    async def execute_item_batch(
        self,
        batch_operations: Sequence[Sequence[Any]],
        partition_key: str | Sequence[str],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        key = _key(partition_key)
        self._record("execute_item_batch", None, key, kwargs)
        fault = await self._enter("execute_item_batch", None, kwargs)
        scratch = dict(self.items)
        results: list[dict[str, Any]] = []
        for index, operation in enumerate(batch_operations):
            try:
                results.append(self._batch_operation(scratch, key, operation))
            except _OperationFailed as failed:
                raise batch_error(index, failed.status, len(batch_operations)) from None
        self.items = scratch
        self._charge(kwargs, None)
        return await self._finish(fault, results)

    def _batch_operation(
        self, scratch: dict[tuple[Key, str], dict[str, Any]], key: Key, operation: Sequence[Any]
    ) -> dict[str, Any]:
        kind = str(operation[0]).lower()
        args = operation[1]
        options: Mapping[str, Any] = operation[2] if len(operation) > 2 else {}
        if kind in {"create", "upsert"}:
            body = dict(args[0])
            item_id = str(body["id"])
        else:
            item_id = str(args[0])
            body = dict(args[1]) if len(args) > 1 and isinstance(args[1], Mapping) else {}
        if body and self.key_of(body) != key:
            raise _OperationFailed(HTTPStatus.BAD_REQUEST)
        current = scratch.get((key, item_id))
        etag = options.get("if_match_etag")
        if etag is not None and (current is None or current.get("_etag") != etag):
            raise _OperationFailed(
                HTTPStatus.NOT_FOUND if current is None else HTTPStatus.PRECONDITION_FAILED
            )
        if kind == "create" and current is not None:
            raise _OperationFailed(HTTPStatus.CONFLICT)
        if kind not in {"create", "upsert"} and current is None:
            raise _OperationFailed(HTTPStatus.NOT_FOUND)
        return self._batch_result(
            scratch, key, item_id, kind=kind, body=body, args=args, current=current
        )

    def _batch_result(
        self,
        scratch: dict[tuple[Key, str], dict[str, Any]],
        key: Key,
        item_id: str,
        *,
        kind: str,
        body: dict[str, Any],
        args: Sequence[Any],
        current: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if kind == "read" and current is not None:
            return {
                "statusCode": int(HTTPStatus.OK),
                "eTag": current["_etag"],
                "resourceBody": copy.deepcopy(current),
            }
        if kind == "delete":
            del scratch[(key, item_id)]
            return {"statusCode": int(HTTPStatus.NO_CONTENT)}
        if kind == "patch" and current is not None:
            body = copy.deepcopy(current)
            _apply_patch(body, args[1])
        elif kind not in {"create", "upsert", "replace"}:
            raise ValueError(f"unsupported batch operation {kind!r}")
        stored = self._stamped(body)
        scratch[(key, item_id)] = stored
        status = HTTPStatus.CREATED if kind == "create" else HTTPStatus.OK
        return {
            "statusCode": int(status),
            "eTag": stored["_etag"],
            "resourceBody": copy.deepcopy(stored),
        }

    def query_items(self, query: str, **kwargs: Any) -> FakePages:
        partition_key = kwargs.get("partition_key")
        key = _key(partition_key) if partition_key is not None else None
        self._record("query_items", None, key, kwargs, query)
        parameters = {
            str(parameter["name"]): parameter["value"] for parameter in kwargs.get("parameters", ())
        }
        rows = _run_query(query, parameters, self._scope(key))
        return FakePages(self, rows, kwargs)

    def _scope(self, key: Key | None) -> list[dict[str, Any]]:
        return [
            item
            for (item_key, _), item in sorted(self.items.items())
            if key is None or item_key[: len(key)] == key
        ]


def _key(partition_key: str | Sequence[str]) -> Key:
    if isinstance(partition_key, str):
        return (partition_key,)
    return tuple(str(value) for value in partition_key)


def _matches(item: Mapping[str, Any], clause: str, parameters: Mapping[str, Any]) -> bool:
    comparison = _COMPARISON_RE.match(clause)
    if comparison is not None:
        value = _get_path(item, comparison["path"])
        expected = parameters[comparison["param"]]
        if value is None:
            return False
        try:
            return _COMPARE[comparison["op"]](value, expected)
        except TypeError:
            return False
    contains = _CONTAINS_RE.match(clause)
    if contains is not None:
        values = parameters[contains["param"]]
        return _get_path(item, contains["path"]) in values
    raise NotImplementedError(f"the fake doesn't evaluate the clause {clause!r}")


def _project(item: Mapping[str, Any], fields: str) -> dict[str, Any]:
    if fields.strip() == "*":
        return copy.deepcopy(dict(item))
    projected: dict[str, Any] = {}
    for raw in fields.split(","):
        match = _FIELD_RE.match(raw.strip())
        if match is None:
            raise NotImplementedError(f"the fake doesn't project {raw.strip()!r}")
        path = match["path"]
        value = _get_path(item, path)
        name = match["alias"] or path.split(".")[-1]
        if value is not None or path.split(".")[0] in item:
            projected[name] = copy.deepcopy(value)
    return projected


def _split_top_level(expression: str, keyword: str) -> list[str]:
    """`expression` cut at each ` AND ` or ` OR ` outside parentheses."""
    separator = f" {keyword} "
    parts: list[str] = []
    depth = start = index = 0
    while index < len(expression):
        character = expression[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif depth == 0 and expression[index : index + len(separator)].upper() == separator:
            parts.append(expression[start:index].strip())
            index += len(separator)
            start = index
            continue
        index += 1
    parts.append(expression[start:].strip())
    return parts


def _enclosed(expression: str) -> bool:
    """Whether the whole expression is one parenthesised group."""
    if not (expression.startswith("(") and expression.endswith(")")):
        return False
    depth = 0
    for index, character in enumerate(expression):
        depth += {"(": 1, ")": -1}.get(character, 0)
        if depth == 0 and index < len(expression) - 1:
            return False
    return True


def _holds(item: Mapping[str, Any], expression: str, parameters: Mapping[str, Any]) -> bool:
    expression = expression.strip()
    while _enclosed(expression):
        expression = expression[1:-1].strip()
    alternatives = _split_top_level(expression, "OR")
    if len(alternatives) > 1:
        return any(_holds(item, part, parameters) for part in alternatives)
    conditions = _split_top_level(expression, "AND")
    if len(conditions) > 1:
        return all(_holds(item, part, parameters) for part in conditions)
    return _matches(item, expression, parameters)


def _sorted(rows: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    """`rows` in the order of the `ORDER BY` terms: stable sorts from the last term to the
    first."""
    for term in reversed([part.strip() for part in order.split(",")]):
        match = _ORDER_TERM_RE.match(term)
        if match is None:
            raise NotImplementedError(f"the fake doesn't order by {term!r}")
        path = match["path"]
        rows = sorted(
            rows,
            key=lambda item: (_get_path(item, path) is not None, _get_path(item, path)),
            reverse=(match["direction"] or "ASC").upper() == "DESC",
        )
    return rows


def _run_query(
    query: str, parameters: Mapping[str, Any], scope: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    text = " ".join(query.split())
    parsed = _SELECT_RE.match(text)
    if parsed is None:
        raise NotImplementedError(f"the fake doesn't run the query {text!r}")
    where = parsed["where"]
    rows = [item for item in scope if where is None or _holds(item, where, parameters)]
    if parsed["order"]:
        rows = _sorted(rows, parsed["order"])
    if parsed["top"]:
        top = parsed["top"]
        rows = rows[: int(parameters[top]) if top.startswith("@") else int(top)]
    return [_project(item, parsed["fields"]) for item in rows]
