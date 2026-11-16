"""Evaluate compiled ARM templates offline, for the compiled-template tests (84-testing.md 8.1).

`bicep build` leaves every environment-dependent choice as an ARM expression (`condition`,
`copy`, `if(...)`), so the tests evaluate the compiled template with one environment's
parameters to see what that environment deploys. The evaluator covers the expression language the
DocChat templates use. It descends into the local modules, whose templates are DocChat's own, and
stops at every Azure Verified Module call: the tests read the parameters DocChat passes to a module
and never its internals, which belong to the module's publisher.

Values that exist only at deployment time (a resource's runtime properties, an AVM module's
outputs) evaluate to `Unknown`, labelled with what they stand for; string functions render an
`Unknown` as `<label>`. `uniqueString` and `guid` are deterministic stand-ins: the tests compare
names with their patterns and with each other, never with Azure's exact values.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, override

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
TENANT_ID = "66666666-7777-8888-9999-000000000000"
RESOURCE_GROUP_ID_PREFIX = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups"
LOCATION = "eastus2"

_GUID_NAMESPACE = uuid.UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830")
_DEPLOYMENTS = "Microsoft.Resources/deployments"
# Every Azure Verified Module carries a usage-telemetry deployment with this prefix.
_AVM_TELEMETRY_MARKER = "46d3xbcp."
_ENVIRONMENT = {
    "name": "AzureCloud",
    "authentication": {"loginEndpoint": "https://login.microsoftonline.com/"},
    "suffixes": {
        "storage": "core.windows.net",
        "sqlServerHostname": ".database.windows.net",
        "acrLoginServer": ".azurecr.io",
        "keyvaultDns": ".vault.azure.net",
    },
}


class EvaluationError(Exception):
    """An expression the evaluator can't resolve offline."""


class TemplateFailureError(EvaluationError):
    """The template's own `fail()`: Resource Manager would stop the deployment with this message."""


@dataclass(frozen=True)
class Unknown:
    """A value known only at deployment time."""

    label: str

    def child(self, name: str) -> Unknown:
        return Unknown(f"{self.label}.{name}")

    @override
    def __str__(self) -> str:
        return f"<{self.label}>"


# Expression parsing


@dataclass(frozen=True)
class _Literal:
    value: Any


@dataclass(frozen=True)
class _Call:
    name: str
    args: tuple[_Node, ...]


@dataclass(frozen=True)
class _Property:
    target: _Node
    name: str


@dataclass(frozen=True)
class _Index:
    target: _Node
    index: _Node


_Node = _Literal | _Call | _Property | _Index

_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<number>-?\d+(?:\.\d+)?)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<punct>[()\[\],.]))"
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _TOKEN.match(text, position)
        if match is None:
            raise EvaluationError(f"can't tokenize {text[position:]!r}")
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


class _Parser:
    def __init__(self, text: str) -> None:
        self._tokens = _tokenize(text)
        self._position = 0

    def parse(self) -> _Node:
        node = self._expression()
        if self._position != len(self._tokens):
            raise EvaluationError(
                f"trailing tokens in expression: {self._tokens[self._position :]}"
            )
        return node

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._position] if self._position < len(self._tokens) else None

    def _take(self, value: str | None = None) -> tuple[str, str]:
        token = self._peek()
        if token is None or (value is not None and token[1] != value):
            raise EvaluationError(f"expected {value!r}, found {token!r}")
        self._position += 1
        return token

    def _expression(self) -> _Node:
        node = self._primary()
        while (token := self._peek()) is not None and token[1] in {".", "["}:
            if self._take()[1] == ".":
                node = _Property(node, self._take()[1])
            else:
                node = _Index(node, self._expression())
                self._take("]")
        return node

    def _primary(self) -> _Node:
        kind, value = self._take()
        if kind == "string":
            return _Literal(value[1:-1].replace("''", "'"))
        if kind == "number":
            return _Literal(float(value) if "." in value else int(value))
        if kind != "name":
            raise EvaluationError(f"unexpected token {value!r}")
        # A user-defined function is called as namespace.member(...), Bicep's `__bicep.<name>`.
        following = self._tokens[self._position : self._position + 3]
        if (
            # The dot, the member's name and its parenthesis.
            len(following) == 3
            and following[0] == ("punct", ".")
            and following[1][0] == "name"
            and following[2] == ("punct", "(")
        ):
            self._take(".")
            value = f"{value}.{self._take()[1]}"
        args: list[_Node] = []
        self._take("(")
        if (token := self._peek()) is not None and token[1] != ")":
            args.append(self._expression())
            while (token := self._peek()) is not None and token[1] == ",":
                self._take(",")
                args.append(self._expression())
        self._take(")")
        return _Call(value.lower(), tuple(args))


def _is_expression(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("[")
        and value.endswith("]")
        and not value.startswith("[[")
    )


# Helpers shared by the functions


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Unknown):
        return str(value)
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, dict | list):
        return json.dumps(value, separators=(",", ":"), default=str)
    if value is None:
        return ""
    return str(value)


def unique_string(*parts: Any) -> str:
    """A deterministic stand-in for ARM's uniqueString: 13 lower-case letters and digits."""
    digest = hashlib.sha256("-".join(_render(part) for part in parts).encode()).digest()
    return base64.b32encode(digest).decode().lower()[:13]


def guid(*parts: Any) -> str:
    """A deterministic stand-in for ARM's guid over the rendered arguments."""
    return str(uuid.uuid5(_GUID_NAMESPACE, "-".join(_render(part) for part in parts)))


def _format(template: str, *args: Any) -> str:
    def substitute(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in {"{{", "}}"}:
            return token[0]
        return _render(args[int(match.group(1))])

    return re.sub(r"\{\{|\}\}|\{(\d+)(?::[^}]*)?\}", substitute, template)


def resource_id_path(resource_type: str, names: list[Any]) -> str:
    provider, *segments = resource_type.split("/")
    path = provider
    for segment, name in zip(segments, names, strict=True):
        path += f"/{segment}/{_render(name)}"
    return path


def _union(*values: Any) -> Any:
    if all(isinstance(value, dict) for value in values):
        merged: dict[str, Any] = {}
        for value in values:
            merged.update(value)
        return merged
    result: list[Any] = []
    for value in values:
        result.extend(item for item in value if item not in result)
    return result


def _fail(message: Any) -> Any:
    raise TemplateFailureError(_render(message))


def _cidr_subnet(network: str, new_prefix: int, index: int) -> str:
    return str(list(ipaddress.ip_network(network).subnets(new_prefix=new_prefix))[index])


def _to_int(value: Any) -> int:
    return int(value)


def _tryget(target: Any, *keys: Any) -> Any:
    for key in keys:
        in_dict = isinstance(target, dict) and key in target
        in_list = isinstance(target, list) and isinstance(key, int) and 0 <= key < len(target)
        if in_dict or in_list:
            target = target[key]
        elif isinstance(target, Unknown):
            target = target.child(str(key))
        else:
            return None
    return target


_EAGER: dict[str, Callable[..., Any]] = {
    "concat": lambda *values: (
        "".join(_render(value) for value in values)
        if all(not isinstance(value, list) for value in values)
        else [item for value in values for item in value]
    ),
    "format": _format,
    "createobject": lambda *pairs: {pairs[i]: pairs[i + 1] for i in range(0, len(pairs), 2)},
    "createarray": lambda *items: list(items),
    "union": _union,
    # Bicep compiles an object spread ({...a, b: 1}) to shallowMerge over an array of objects.
    "shallowmerge": lambda values: _union(*values) if values else {},
    "equals": lambda left, right: left == right,
    "not": lambda value: not value,
    "and": lambda *values: all(values),
    "or": lambda *values: any(values),
    "empty": lambda value: value is None or len(value) == 0,
    "length": len,
    "take": lambda value, count: value[:count],
    "flatten": lambda values: [item for value in values for item in value],
    "skip": lambda value, count: value[count:],
    "first": lambda value: value[0] if value else None,
    "last": lambda value: value[-1] if value else None,
    "split": lambda value, separator: value.split(separator),
    "join": lambda values, separator: separator.join(_render(value) for value in values),
    "replace": lambda value, old, new: _render(value).replace(old, _render(new)),
    "tolower": lambda value: _render(value).lower(),
    "toupper": lambda value: _render(value).upper(),
    "startswith": lambda value, prefix: _render(value).lower().startswith(prefix.lower()),
    "endswith": lambda value, suffix: _render(value).lower().endswith(suffix.lower()),
    "contains": lambda container, item: item in container,
    "string": _render,
    "int": _to_int,
    "bool": lambda value: value if isinstance(value, bool) else str(value).lower() == "true",
    "json": json.loads,
    "range": lambda start, count: list(range(start, start + count)),
    "greater": lambda left, right: left > right,
    "less": lambda left, right: left < right,
    "greaterorequals": lambda left, right: left >= right,
    "lessorequals": lambda left, right: left <= right,
    "add": lambda left, right: left + right,
    "sub": lambda left, right: left - right,
    "mul": lambda left, right: left * right,
    "div": lambda left, right: left // right,
    "mod": lambda left, right: left % right,
    "min": lambda *values: min(values),
    "max": lambda *values: max(values),
    "coalesce": lambda *values: next((value for value in values if value is not None), None),
    "tryget": _tryget,
    "items": lambda value: [{"key": key, "value": item} for key, item in value.items()],
    "objectkeys": list,
    "uniquestring": unique_string,
    "guid": guid,
    "cidrsubnet": _cidr_subnet,
    "fail": _fail,
    "true": lambda: True,
    "false": lambda: False,
    "null": lambda: None,
    "environment": lambda: _ENVIRONMENT,
}


# Templates and deployments


@dataclass
class Resource:
    """One resource instance a deployment declares, with its expressions evaluated."""

    deployment: Deployment
    symbol: str | None
    raw: Mapping[str, Any]
    copy_index: int | None
    type: str
    api_version: str
    name: Any
    existing: bool
    resource_group: str
    _properties: Any = field(default=None, repr=False)
    _child: Deployment | None = field(default=None, repr=False)

    @property
    def properties(self) -> Any:
        if self._properties is None:
            self._properties = self.deployment.evaluate(
                self.raw.get("properties", {}), self.copy_index
            )
        return self._properties

    def field(self, key: str) -> Any:
        """A top-level field other than properties (sku, kind, location, tags, scope)."""
        return self.deployment.evaluate(self.raw.get(key), self.copy_index)

    @property
    def is_module(self) -> bool:
        return self.type == _DEPLOYMENTS

    @property
    def child(self) -> Deployment:
        """The nested deployment of a module call."""
        if not self.is_module:
            raise EvaluationError(f"{self.name} is not a module call")
        if self._child is None:
            self._child = Deployment(
                template=self.raw["properties"]["template"],
                parameters=self.module_parameters,
                name=_render(self.name),
                resource_group=self.resource_group,
                parent=self.deployment,
            )
        return self._child

    @property
    def module_parameters(self) -> dict[str, Any]:
        """The parameter values the calling template passes to a module."""
        raw = self.raw["properties"].get("parameters", {})
        values: dict[str, Any] = {}
        for key, entry in raw.items():
            evaluated = self.deployment.evaluate(entry, self.copy_index)
            values[key] = (
                evaluated["value"]
                if isinstance(evaluated, dict) and "value" in evaluated
                else evaluated
            )
        return values

    @property
    def id(self) -> str:
        names = _render(self.name).split("/")
        return self.deployment.resource_id(self.type, names, self.resource_group)


class Deployment:
    """One template deployed with concrete parameters into one resource group."""

    def __init__(
        self,
        template: Mapping[str, Any],
        parameters: Mapping[str, Any],
        *,
        name: str,
        resource_group: str,
        parent: Deployment | None = None,
    ) -> None:
        self.template = template
        self.name = name
        self.resource_group = resource_group
        self.parent = parent
        self._variables: dict[str, Any] = {}
        self._resolving: set[str] = set()
        self._resources: list[Resource] | None = None
        self._outputs: dict[str, Any] | None = None
        self._copy_contexts: list[dict[str, int]] = []
        self._lambda_scopes: list[dict[str, Any]] = []
        # Inside a user-defined function, parameters() names the function's own parameters.
        self._function_scopes: list[dict[str, Any]] = []
        self.parameters = self._bind_parameters(parameters)

    # The template's own declarations

    @property
    def is_external(self) -> bool:
        """True for an Azure Verified Module, whose internals the tests never evaluate."""
        return any(
            isinstance(declaration.get("name"), str)
            and _AVM_TELEMETRY_MARKER in declaration["name"]
            for declaration in self._declarations().values()
        )

    def _declarations(self) -> dict[str, Mapping[str, Any]]:
        resources = self.template.get("resources", [])
        if isinstance(resources, dict):
            return dict(resources)
        return {f"#{index}": declaration for index, declaration in enumerate(resources)}

    def declared_types(self) -> set[str]:
        return {declaration["type"] for declaration in self._declarations().values()}

    def _bind_parameters(self, supplied: Mapping[str, Any]) -> dict[str, Any]:
        bound: dict[str, Any] = {}
        for key, definition in self.template.get("parameters", {}).items():
            if key in supplied:
                bound[key] = supplied[key]
            elif "defaultValue" in definition:
                bound[key] = None
                bound[key] = self.evaluate(definition["defaultValue"])
            elif definition.get("nullable"):
                bound[key] = None
            else:
                raise EvaluationError(f"deployment {self.name} has no value for parameter {key}")
        return bound

    def variable(self, key: str) -> Any:
        if key not in self._variables:
            if key in self._resolving:
                raise EvaluationError(f"variable cycle at {key}")
            self._resolving.add(key)
            variables = self.template.get("variables", {})
            if key in variables:
                self._variables[key] = self.evaluate(variables[key])
            else:
                loop = next(
                    (entry for entry in variables.get("copy", []) if entry["name"] == key), None
                )
                if loop is None:
                    raise EvaluationError(f"no variable {key} in {self.name}")
                self._variables[key] = self._expand_copy(loop)
            self._resolving.discard(key)
        return self._variables[key]

    def resources(self, *, include_existing: bool = False) -> list[Resource]:
        """The resources this template deploys in this environment, loops expanded."""
        if self._resources is None:
            self._resources = []
            for symbol, declaration in self._declarations().items():
                copy = declaration.get("copy")
                indexes: list[int | None] = (
                    list(range(self.evaluate(copy["count"]))) if copy is not None else [None]
                )
                for index in indexes:
                    if "condition" in declaration and not self.evaluate(
                        declaration["condition"], index
                    ):
                        continue
                    self._resources.append(self._instantiate(symbol, declaration, index))
        return [
            resource for resource in self._resources if include_existing or not resource.existing
        ]

    def _instantiate(
        self, symbol: str, declaration: Mapping[str, Any], index: int | None
    ) -> Resource:
        resource_group = self.resource_group
        if "resourceGroup" in declaration:
            resource_group = _render(self.evaluate(declaration["resourceGroup"], index))
        return Resource(
            deployment=self,
            symbol=None if symbol.startswith("#") else symbol,
            raw=declaration,
            copy_index=index,
            type=declaration["type"],
            api_version=declaration.get("apiVersion", ""),
            # Extension resources (Microsoft Graph) carry no ARM name; their symbol stands in.
            name=self.evaluate(declaration.get("name", symbol), index),
            existing=bool(declaration.get("existing", False)),
            resource_group=resource_group,
        )

    def outputs(self) -> dict[str, Any]:
        if self._outputs is None:
            self._outputs = {}
            for key, definition in self.template.get("outputs", {}).items():
                if "copy" in definition:
                    self._outputs[key] = self._expand_copy({"name": "", **definition["copy"]})
                else:
                    self._outputs[key] = self.evaluate(definition["value"])
        return self._outputs

    # Navigation

    def modules(self, name: str | None = None) -> list[Resource]:
        return [
            resource
            for resource in self.resources()
            if resource.is_module and (name is None or resource.name == name)
        ]

    def module(self, name: str) -> Deployment:
        matches = self.modules(name)
        if len(matches) != 1:
            raise EvaluationError(f"{len(matches)} module calls named {name} in {self.name}")
        return matches[0].child

    def walk(self) -> Iterator[Resource]:
        """Every resource of this deployment and of the local modules under it."""
        for resource in self.resources():
            yield resource
            if resource.is_module and not resource.child.is_external:
                yield from resource.child.walk()

    def external_calls(self) -> Iterator[Resource]:
        """Every Azure Verified Module call under this deployment."""
        for resource in self.walk():
            if resource.is_module and resource.child.is_external:
                yield resource

    def resource_id(
        self, resource_type: str, names: list[Any], resource_group: str | None = None
    ) -> str:
        group = resource_group or self.resource_group
        return (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{group}/providers/"
            + resource_id_path(resource_type, names)
        )

    # Expression evaluation

    def evaluate(self, value: Any, copy_index: int | None = None) -> Any:
        self._copy_contexts.append({"": copy_index} if copy_index is not None else {})
        try:
            return self._value(value)
        finally:
            self._copy_contexts.pop()

    def _value(self, value: Any) -> Any:
        if isinstance(value, str):
            if _is_expression(value):
                return self._node(_Parser(value[1:-1]).parse())
            return value[1:] if value.startswith("[[") else value
        if isinstance(value, list):
            return [self._value(item) for item in value]
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if key == "copy" and isinstance(item, list):
                    for loop in item:
                        result[loop["name"]] = self._expand_copy(loop)
                else:
                    result[key] = self._value(item)
            return result
        return value

    def _expand_copy(self, loop: Mapping[str, Any]) -> list[Any]:
        count = self._value(loop["count"])
        outer = self._copy_contexts[-1] if self._copy_contexts else {}
        items: list[Any] = []
        for index in range(count):
            self._copy_contexts.append({**outer, loop["name"]: index})
            try:
                items.append(self._value(loop["input"]))
            finally:
                self._copy_contexts.pop()
        return items

    def _node(self, node: _Node) -> Any:
        if isinstance(node, _Literal):
            return node.value
        if isinstance(node, _Property):
            target = self._node(node.target)
            if isinstance(target, Unknown):
                return target.child(node.name)
            if not isinstance(target, dict) or node.name not in target:
                raise EvaluationError(f"no property {node.name} in {_render(target)[:200]}")
            return target[node.name]
        if isinstance(node, _Index):
            target = self._node(node.target)
            index = self._node(node.index)
            if isinstance(target, Unknown):
                return target.child(_render(index))
            if isinstance(index, Unknown):
                raise EvaluationError(f"index {index} isn't known offline")
            return target[index]
        return self._call(node)

    def _call(self, node: _Call) -> Any:
        special = self._special(node)
        if special is not _NOT_SPECIAL:
            return special
        args = [self._node(arg) for arg in node.args]
        if "." in node.name:
            return self._user_function(node.name, args)
        function = _EAGER.get(node.name)
        if function is None:
            raise EvaluationError(f"unsupported function {node.name}")
        if node.name not in _ACCEPT_UNKNOWN and any(isinstance(arg, Unknown) for arg in args):
            return Unknown(f"{node.name}({', '.join(_render(arg) for arg in args)})")
        return function(*args)

    def _user_function(self, name: str, args: list[Any]) -> Any:
        """A user-defined function of the template's `functions` (Bicep's `func`)."""
        namespace, member = name.split(".", 1)
        definition = next(
            (
                definition
                for group in self.template.get("functions", [])
                if str(group.get("namespace", "")).lower() == namespace
                for key, definition in group.get("members", {}).items()
                if key.lower() == member
            ),
            None,
        )
        if definition is None:
            raise EvaluationError(f"no user-defined function {name}")
        names = [parameter["name"] for parameter in definition.get("parameters", [])]
        self._function_scopes.append(dict(zip(names, args, strict=True)))
        try:
            return self._value(definition["output"]["value"])
        finally:
            self._function_scopes.pop()

    def _special(self, node: _Call) -> Any:  # noqa: PLR0911 - one return per special form
        name = node.name
        if name == "parameters":
            key = self._node(node.args[0])
            return self._function_scopes[-1][key] if self._function_scopes else self.parameters[key]
        if name == "variables":
            return self.variable(self._node(node.args[0]))
        if name == "if":
            condition = self._node(node.args[0])
            if isinstance(condition, Unknown):
                raise EvaluationError(f"condition {condition} isn't known offline")
            return self._node(node.args[1] if condition else node.args[2])
        if name == "copyindex":
            return self._copy_index([self._node(arg) for arg in node.args])
        if name in _LAMBDA_FUNCTIONS:
            return _LAMBDA_FUNCTIONS[name](self, node)
        if name == "lambdavariables":
            key = self._node(node.args[0])
            return next(scope[key] for scope in reversed(self._lambda_scopes) if key in scope)
        if name in _CONTEXT_FUNCTIONS:
            return _CONTEXT_FUNCTIONS[name](self, [self._node(arg) for arg in node.args])
        return _NOT_SPECIAL

    def _copy_index(self, args: list[Any]) -> int:
        loop_name = next((arg for arg in args if isinstance(arg, str)), "")
        offset = next((arg for arg in args if isinstance(arg, int)), 0)
        for context in reversed(self._copy_contexts):
            if loop_name in context:
                return int(context[loop_name]) + offset
        raise EvaluationError(f"copyIndex({loop_name!r}) outside its loop")

    def call_lambda(self, node: _Node, *values: Any) -> Any:
        if not isinstance(node, _Call) or node.name != "lambda":
            raise EvaluationError("expected a lambda")
        names = [self._node(arg) for arg in node.args[:-1]]
        self._lambda_scopes.append(dict(zip(names, values, strict=True)))
        try:
            return self._node(node.args[-1])
        finally:
            self._lambda_scopes.pop()

    def reference(self, target: Any) -> Any:
        """reference(symbol) of a module returns its outputs; anything else is runtime-only."""
        marker = f"/providers/{_DEPLOYMENTS}/"
        if isinstance(target, str) and marker in target:
            # Templates without symbolic names refer to a module by its deployment's resource ID.
            name = target.rsplit("/", 1)[-1]
            module = next((resource for resource in self.modules() if resource.name == name), None)
            if module is not None:
                return self._module_reference(module)
        if isinstance(target, str) and "/" not in target:
            declaration = self._declarations().get(target)
            if declaration is not None and declaration["type"] == _DEPLOYMENTS:
                module = next(
                    resource
                    for resource in self.resources(include_existing=True)
                    if resource.symbol == target and resource.copy_index is None
                )
                return self._module_reference(module)
            if declaration is not None:
                return Unknown(_render(self.evaluate(declaration.get("name", target))))
        return Unknown(_render(target))

    @staticmethod
    def _module_reference(module: Resource) -> Any:
        if module.child.is_external:
            return Unknown(_render(module.name))
        return {"outputs": {key: {"value": value} for key, value in module.child.outputs().items()}}


_NOT_SPECIAL = object()

# Constructors keep an Unknown as an element, and string builders render it as <label>, so a
# structure or a name that embeds a principal ID stays comparable. Every other function with an
# Unknown argument yields Unknown.
_ACCEPT_UNKNOWN = {
    "createobject",
    "createarray",
    "coalesce",
    "format",
    "concat",
    "guid",
    "uniquestring",
    "string",
    "replace",
    "tolower",
    "join",
}


def _map(deployment: Deployment, node: _Call) -> Any:
    items = deployment._node(node.args[0])
    return [
        deployment.call_lambda(
            node.args[1], *((item, index) if _arity(node.args[1]) == 2 else (item,))
        )
        for index, item in enumerate(items)
    ]


def _filter(deployment: Deployment, node: _Call) -> Any:
    items = deployment._node(node.args[0])
    return [item for item in items if deployment.call_lambda(node.args[1], item)]


def _reduce(deployment: Deployment, node: _Call) -> Any:
    items = deployment._node(node.args[0])
    accumulator = deployment._node(node.args[1])
    for item in items:
        accumulator = deployment.call_lambda(node.args[2], accumulator, item)
    return accumulator


def _to_object(deployment: Deployment, node: _Call) -> Any:
    items = deployment._node(node.args[0])
    result: dict[str, Any] = {}
    for item in items:
        key = deployment.call_lambda(node.args[1], item)
        result[key] = deployment.call_lambda(node.args[2], item) if len(node.args) > 2 else item
    return result


def _arity(node: _Node) -> int:
    return len(node.args) - 1 if isinstance(node, _Call) else 0


_LAMBDA_FUNCTIONS: dict[str, Callable[[Deployment, _Call], Any]] = {
    "map": _map,
    "filter": _filter,
    "reduce": _reduce,
    "toobject": _to_object,
}


def _resource_id(deployment: Deployment, args: list[Any]) -> str:
    type_index = next(
        index for index, arg in enumerate(args) if isinstance(arg, str) and "/" in arg
    )
    resource_group = args[type_index - 1] if type_index >= 1 else None
    return deployment.resource_id(args[type_index], args[type_index + 1 :], resource_group)


_CONTEXT_FUNCTIONS: dict[str, Callable[[Deployment, list[Any]], Any]] = {
    "reference": lambda deployment, args: deployment.reference(args[0]),
    "resourceid": _resource_id,
    "subscriptionresourceid": lambda _deployment, args: (
        f"/subscriptions/{SUBSCRIPTION_ID}/providers/" + resource_id_path(args[0], args[1:])
    ),
    "tenantresourceid": lambda _deployment, args: (
        "/providers/" + resource_id_path(args[0], args[1:])
    ),
    "extensionresourceid": lambda _deployment, args: (
        f"{_render(args[0])}/providers/" + resource_id_path(args[1], args[2:])
    ),
    "subscription": lambda _deployment, _args: {
        "id": f"/subscriptions/{SUBSCRIPTION_ID}",
        "subscriptionId": SUBSCRIPTION_ID,
        "tenantId": TENANT_ID,
    },
    "resourcegroup": lambda deployment, _args: {
        "id": f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{deployment.resource_group}",
        "name": deployment.resource_group,
        "location": LOCATION,
    },
    "tenant": lambda _deployment, _args: {"tenantId": TENANT_ID},
    "deployment": lambda deployment, _args: {"name": deployment.name},
}


def load_deployment(
    template: Mapping[str, Any], parameters: Mapping[str, Any], *, resource_group: str
) -> Deployment:
    """The top-level deployment of a compiled template into one resource group."""
    return Deployment(template, parameters, name="main", resource_group=resource_group)
