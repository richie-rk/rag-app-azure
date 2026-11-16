"""Run the KQL of the log search alerts over constant tables, for the alert query tests (T-501).

Azure Monitor's engine isn't available offline, so this evaluates the small part of the Kusto
Query Language the rate rules and A58 use, with Kusto's semantics for it:

- statements `let <name> = <scalar expression>;` followed by one tabular expression;
- a table name piped through `where <predicate>`, `summarize <aggregations>` without a `by`
  clause, and `project <columns>`;
- the aggregations `sum(x)`, `sumif(x, predicate)` and `count()`, which over no rows give 0, the
  default Kusto gives them; an unnamed `sum(x)` is the column `sum_x`;
- `toscalar(<tabular expression>)`, the first column of the first row, or null without a row;
- `tostring(x)` (null gives the empty string), `toint(x)` (null when it doesn't parse), a dynamic
  column's `Properties["key"]`, the operators `* / + -`, `== != < <= > >=`, `in (...)` and
  `!in (...)`, `and`, `or`, string and number literals.

Null follows Kusto: arithmetic and comparisons with null give null, and `where` keeps only rows
whose predicate is true. Anything else raises `KqlError`, so a query outside the subset never
passes by accident.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Row = dict[str, Any]
Tables = Mapping[str, Sequence[Row]]


class KqlError(Exception):
    """A query outside the subset this evaluator covers, or a malformed one."""


_TOKEN = re.compile(
    r"\s*(?:(?P<string>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
    r"|(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>==|!=|>=|<=|!in\b|[|;()\[\],=<>*/+-]))"
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while text[position:].strip():
        match = _TOKEN.match(text, position)
        if match is None:
            raise KqlError(f"can't read {text[position : position + 20]!r}")
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


# Syntax tree


@dataclass(frozen=True)
class _Literal:
    value: Any


@dataclass(frozen=True)
class _Name:
    name: str


@dataclass(frozen=True)
class _Index:
    target: _Expression
    key: _Expression


@dataclass(frozen=True)
class _Call:
    name: str
    args: tuple[_Expression, ...]


@dataclass(frozen=True)
class _Scalar:
    """`toscalar(<tabular expression>)`."""

    query: _Pipeline


@dataclass(frozen=True)
class _Binary:
    operator: str
    left: _Expression
    right: _Expression


@dataclass(frozen=True)
class _In:
    value: _Expression
    options: tuple[_Expression, ...]
    negated: bool


_Expression = _Literal | _Name | _Index | _Call | _Scalar | _Binary | _In


@dataclass(frozen=True)
class _Where:
    predicate: _Expression


@dataclass(frozen=True)
class _Column:
    name: str
    value: _Expression


@dataclass(frozen=True)
class _Summarize:
    aggregations: tuple[_Column, ...]


@dataclass(frozen=True)
class _Project:
    columns: tuple[_Column, ...]


@dataclass(frozen=True)
class _Pipeline:
    table: str
    operators: tuple[_Where | _Summarize | _Project, ...]


@dataclass(frozen=True)
class _Query:
    bindings: tuple[tuple[str, _Expression], ...]
    body: _Pipeline


# Binding strength of each binary operator; `in` and `!in` bind as comparisons.
_PRECEDENCE = {
    "or": 1,
    "and": 2,
    "==": 3,
    "!=": 3,
    "<": 3,
    "<=": 3,
    ">": 3,
    ">=": 3,
    "in": 3,
    "!in": 3,
    "+": 4,
    "-": 4,
    "*": 5,
    "/": 5,
}
_AGGREGATIONS = {"sum", "sumif", "count"}


class _Parser:
    def __init__(self, text: str) -> None:
        self._tokens = _tokenize(text)
        self._position = 0

    def _peek(self) -> str | None:
        if self._position < len(self._tokens):
            return self._tokens[self._position][1]
        return None

    def _next(self) -> tuple[str, str]:
        if self._position >= len(self._tokens):
            raise KqlError("unexpected end of query")
        token = self._tokens[self._position]
        self._position += 1
        return token

    def _expect(self, text: str) -> None:
        _, value = self._next()
        if value != text:
            raise KqlError(f"expected {text!r}, found {value!r}")

    def _name(self) -> str:
        kind, value = self._next()
        if kind != "name":
            raise KqlError(f"expected a name, found {value!r}")
        return value

    def query(self) -> _Query:
        bindings: list[tuple[str, _Expression]] = []
        while self._peek() == "let":
            self._next()
            name = self._name()
            self._expect("=")
            bindings.append((name, self._expression(0)))
            self._expect(";")
        body = self._pipeline()
        if self._peek() is not None:
            raise KqlError(f"unexpected {self._peek()!r} after the query")
        return _Query(tuple(bindings), body)

    def _pipeline(self) -> _Pipeline:
        table = self._name()
        operators: list[_Where | _Summarize | _Project] = []
        while self._peek() == "|":
            self._next()
            operator = self._name()
            if operator == "where":
                operators.append(_Where(self._expression(0)))
            elif operator == "summarize":
                aggregations = self._columns()
                if self._peek() == "by":
                    raise KqlError("summarize ... by is outside the subset")
                for aggregation in aggregations:
                    value = aggregation.value
                    if not (isinstance(value, _Call) and value.name in _AGGREGATIONS):
                        raise KqlError(f"{aggregation.name} isn't an aggregation of the subset")
                operators.append(_Summarize(aggregations))
            elif operator == "project":
                operators.append(_Project(self._columns()))
            else:
                raise KqlError(f"the operator {operator!r} is outside the subset")
        return _Pipeline(table, tuple(operators))

    def _columns(self) -> tuple[_Column, ...]:
        columns = [self._column()]
        while self._peek() == ",":
            self._next()
            columns.append(self._column())
        return tuple(columns)

    def _column(self) -> _Column:
        ahead = self._tokens[self._position : self._position + 2]
        if len(ahead) == 2 and ahead[0][0] == "name" and ahead[1][1] == "=":
            name = self._name()
            self._next()
            return _Column(name, self._expression(0))
        value = self._expression(0)
        if isinstance(value, _Name):
            return _Column(value.name, value)
        if isinstance(value, _Call) and len(value.args) == 1 and isinstance(value.args[0], _Name):
            # Kusto names an unnamed aggregation of a column `<function>_<column>`.
            return _Column(f"{value.name}_{value.args[0].name}", value)
        raise KqlError("an unnamed column must be a column or an aggregation of one")

    def _expression(self, minimum: int) -> _Expression:
        left = self._operand()
        while True:
            operator = self._peek()
            strength = _PRECEDENCE.get(operator or "")
            if operator is None or strength is None or strength <= minimum:
                return left
            self._next()
            if operator in {"in", "!in"}:
                self._expect("(")
                options = [self._expression(0)]
                while self._peek() == ",":
                    self._next()
                    options.append(self._expression(0))
                self._expect(")")
                left = _In(left, tuple(options), operator == "!in")
            else:
                left = _Binary(operator, left, self._expression(strength))

    def _operand(self) -> _Expression:
        kind, value = self._next()
        node: _Expression
        if kind == "string" and value.startswith('"'):
            node = _Literal(json.loads(value))
        elif kind == "string":
            node = _Literal(value[1:-1].replace("\\'", "'"))
        elif kind == "number":
            node = _Literal(float(value) if "." in value else int(value))
        elif value == "(":
            node = self._expression(0)
            self._expect(")")
        elif kind == "name" and self._peek() == "(":
            self._next()
            if value == "toscalar":
                node = _Scalar(self._pipeline())
                self._expect(")")
            else:
                args: list[_Expression] = []
                if self._peek() != ")":
                    args.append(self._expression(0))
                    while self._peek() == ",":
                        self._next()
                        args.append(self._expression(0))
                self._expect(")")
                node = _Call(value, tuple(args))
        elif kind == "name":
            node = _Name(value)
        else:
            raise KqlError(f"unexpected {value!r}")
        while self._peek() == "[":
            self._next()
            key = self._expression(0)
            self._expect("]")
            node = _Index(node, key)
        return node


# Evaluation


def _tostring(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def _toint(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        return None


_ARITHMETIC: dict[str, Callable[[Any, Any], Any]] = {
    "+": lambda left, right: left + right,
    "-": lambda left, right: left - right,
    "*": lambda left, right: left * right,
    # Kusto divides integers as integers, truncating towards zero.
    "/": lambda left, right: (
        int(left / right) if isinstance(left, int) and isinstance(right, int) else left / right
    ),
}
_COMPARISON: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda left, right: bool(left == right),
    "!=": lambda left, right: bool(left != right),
    "<": lambda left, right: bool(left < right),
    "<=": lambda left, right: bool(left <= right),
    ">": lambda left, right: bool(left > right),
    ">=": lambda left, right: bool(left >= right),
}


class _Evaluator:
    def __init__(self, tables: Tables) -> None:
        self._tables = tables
        self._scalars: dict[str, Any] = {}

    def run(self, query: _Query) -> list[Row]:
        for name, expression in query.bindings:
            self._scalars[name] = self._value(expression, {})
        return self._pipeline(query.body)

    def _pipeline(self, pipeline: _Pipeline) -> list[Row]:
        if pipeline.table not in self._tables:
            raise KqlError(f"no table {pipeline.table!r}")
        rows = [dict(row) for row in self._tables[pipeline.table]]
        for operator in pipeline.operators:
            if isinstance(operator, _Where):
                rows = [row for row in rows if self._value(operator.predicate, row) is True]
            elif isinstance(operator, _Summarize):
                # Without group keys, summarize gives one row even over no input.
                rows = [
                    {
                        column.name: self._aggregate(column.value, rows)
                        for column in operator.aggregations
                    }
                ]
            else:
                rows = [
                    {column.name: self._value(column.value, row) for column in operator.columns}
                    for row in rows
                ]
        return rows

    def _aggregate(self, expression: _Expression, rows: list[Row]) -> Any:
        assert isinstance(expression, _Call)
        if expression.name == "count":
            if expression.args:
                raise KqlError("count() takes no argument in the subset")
            return len(rows)
        if expression.name == "sumif":
            value, predicate = expression.args
            rows = [row for row in rows if self._value(predicate, row) is True]
        else:
            (value,) = expression.args
        values = [self._value(value, row) for row in rows]
        return sum(item for item in values if item is not None)

    def _value(self, expression: _Expression, row: Row) -> Any:  # noqa: PLR0911 - one per node
        if isinstance(expression, _Literal):
            return expression.value
        if isinstance(expression, _Name):
            if expression.name in row:
                return row[expression.name]
            if expression.name in self._scalars:
                return self._scalars[expression.name]
            raise KqlError(f"no column or binding {expression.name!r}")
        if isinstance(expression, _Index):
            target = self._value(expression.target, row)
            key = self._value(expression.key, row)
            return target.get(key) if isinstance(target, dict) else None
        if isinstance(expression, _Scalar):
            result = self._pipeline(expression.query)
            return next(iter(result[0].values())) if result else None
        if isinstance(expression, _Call):
            return self._call(expression, row)
        if isinstance(expression, _In):
            value = self._value(expression.value, row)
            options = [self._value(option, row) for option in expression.options]
            if value is None:
                return None
            return (value in options) != expression.negated
        return self._binary(expression, row)

    def _call(self, expression: _Call, row: Row) -> Any:
        if expression.name in _AGGREGATIONS:
            raise KqlError(f"{expression.name}() belongs in summarize")
        if len(expression.args) != 1:
            raise KqlError(f"{expression.name}() takes one argument in the subset")
        argument = self._value(expression.args[0], row)
        if expression.name == "tostring":
            return _tostring(argument)
        if expression.name == "toint":
            return _toint(argument)
        raise KqlError(f"the function {expression.name}() is outside the subset")

    def _binary(self, expression: _Binary, row: Row) -> Any:  # noqa: PLR0911 - null rules
        left = self._value(expression.left, row)
        right = self._value(expression.right, row)
        if expression.operator == "and":
            if left is False or right is False:
                return False
            return None if left is None or right is None else True
        if expression.operator == "or":
            if left is True or right is True:
                return True
            return None if left is None or right is None else False
        if left is None or right is None:
            return None
        if expression.operator in _ARITHMETIC:
            return _ARITHMETIC[expression.operator](left, right)
        return _COMPARISON[expression.operator](left, right)


def run(query: str, tables: Tables) -> list[Row]:
    """The rows `query` returns over `tables`, each table a list of rows of column values."""
    return _Evaluator(tables).run(_Parser(query).query())
