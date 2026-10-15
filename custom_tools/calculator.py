"""The ``calculator`` tool: exact and numeric math with SymPy, for every agent.

Adapted from ``strands_tools/calculator.py`` in strands-agents-tools 0.8.9.
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. Licensed
under the Apache License, Version 2.0
(http://www.apache.org/licenses/LICENSE-2.0). Changed here: no console output,
environment-variable defaults or strands_tools helpers; results are plain JSON;
exact values stay exact (pi and e are no longer replaced by 50-digit decimals
before solving or differentiating); solutions are exact with numeric
approximations; a missing ``wrt`` is an error when the expression has several
variables instead of an arbitrary pick; ``variables`` apply in every mode; the
``scientific`` flag is dropped (SymPy already writes very large and very small
numbers with an exponent); and every calculation has size and time limits.

The tool computes only: it opens no files, reaches no network and runs no
code beyond the checked expression (``custom_tools.calculator_parsing``
explains the checks). It is in the ``agent_runtime`` tool set
(``ua_definitions/access/tools/functions.yaml``, tool ``fn:calculator``), so
every agent has it. Calculations run with ``CALCULATION_TIME_LIMIT_SECONDS``
(``custom_tools.calculator_time_limit``).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional, Union

import sympy as sp
from strands import tool
from sympy.logic.boolalg import BooleanAtom

from custom_tools.calculator_parsing import (
    MAX_PRECISION_DIGITS,
    CalculatorInputError,
    check_name,
    parse_expression,
)
from custom_tools.calculator_time_limit import CalculationTimedOut, CalculatorBusy, run_with_time_limit

logger = logging.getLogger(__name__)

CALCULATION_TIME_LIMIT_SECONDS = 10.0
DEFAULT_PRECISION = 10
MAX_ORDER = 100
DEFAULT_SERIES_ORDER = 5
MAX_VARIABLES = 50
MAX_RESULT_CHARS = 20_000
MAX_ECHOED_CHARS = 200  # the expression is repeated in the result up to this length
# Python refuses to print integers with more digits than this (sys.int_max_str_digits).
MAX_EXACT_DIGITS = 4000

CalculatorMode = Literal["evaluate", "solve", "derive", "integrate", "limit", "series", "matrix"]
_UNDEFINED = (sp.zoo, sp.nan, sp.oo, -sp.oo)


@dataclass(frozen=True)
class CalculationRequest:
    """One tool call's arguments, before they are checked."""

    expression: str
    mode: str
    precision: int
    force_numeric: bool
    variables: dict[str, Any] = field(default_factory=dict)
    wrt: Optional[str] = None
    point: Optional[str] = None
    order: Optional[int] = None


# -- presenting results ----------------------------------------------------------


def _text(value: Any) -> str:
    """A result as text, with an approximation for integers too long to print exactly."""

    if isinstance(value, sp.Integer) and int(value).bit_length() * math.log10(2) > MAX_EXACT_DIGITS:
        digits = int(int(value).bit_length() * math.log10(2)) + 1
        return f"{sp.N(value, DEFAULT_PRECISION)} (about {digits:,} digits; too long to show exactly)"
    text = str(value)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + f"... (cut at {MAX_RESULT_CHARS:,} characters)"
    return text


def _numeric(value: Any, precision: int) -> Optional[str]:
    """``value`` to ``precision`` significant digits, or None when it is not a plain number."""

    if not isinstance(value, sp.Basic) or value.free_symbols or not value.is_number:
        return None
    return _text(sp.N(value, precision))


# -- modes -----------------------------------------------------------------------


def _symbol_named(expr: Any, name: str) -> sp.Symbol:
    """The expression's own symbol with this name (keeping assumptions such as positive=True)."""

    for symbol in getattr(expr, "free_symbols", ()):
        if isinstance(symbol, sp.Symbol) and symbol.name == name:
            return symbol
    return sp.Symbol(name)


def _variable(expr: Any, wrt: Optional[str]) -> sp.Symbol:
    if wrt:
        return _symbol_named(expr, check_name(wrt, what="wrt"))
    symbols = sorted(getattr(expr, "free_symbols", ()), key=str)
    if len(symbols) == 1:
        return symbols[0]
    if not symbols:
        raise CalculatorInputError("The expression has no variable. Name one with wrt, for example wrt='x'.")
    raise CalculatorInputError(
        f"The expression has several variables ({', '.join(map(str, symbols))}). Name one with wrt."
    )


def _require_defined(expr: Any) -> None:
    if isinstance(expr, sp.Basic) and expr.has(*_UNDEFINED):
        raise CalculatorInputError(f"The expression is undefined or infinite ({expr}), for example a division by zero.")


def _evaluate(expr: Any, request: CalculationRequest) -> dict[str, Any]:
    if request.force_numeric:
        return {"result": _text(sp.N(expr, request.precision))}
    exact = sp.simplify(expr) if isinstance(expr, sp.Basic) else expr
    result: dict[str, Any] = {"result": _text(exact)}
    if isinstance(exact, (sp.Integer, sp.Float)) or (isinstance(exact, sp.Basic) and exact.has(sp.Float)):
        return result  # already a decimal (the input had decimals or used N()), at the precision it asked for
    numeric = _numeric(exact, request.precision)
    if numeric is not None:
        result["numeric"] = numeric
    return result


def _equations(expr: Any) -> list[Any]:
    items = list(expr) if isinstance(expr, (list, tuple)) else [expr]
    return [item if isinstance(item, (sp.Equality, BooleanAtom)) else sp.Eq(item, 0) for item in items]


def _unknowns(equations: list[Any], wrt: Optional[str]) -> list[sp.Symbol]:
    free = set().union(*(getattr(item, "free_symbols", set()) for item in equations))
    if wrt:
        names = [check_name(name, what="wrt") for name in wrt.replace(",", " ").split()]
        return [_symbol_named(sp.Tuple(*free), name) for name in names]
    unknowns = sorted(free, key=str)
    if len(equations) == 1 and len(unknowns) > 1:
        raise CalculatorInputError(
            f"The equation has several variables ({', '.join(map(str, unknowns))}). Name the unknown with wrt."
        )
    return unknowns


def _solve(expr: Any, request: CalculationRequest) -> dict[str, Any]:
    equations = _equations(expr)
    unknowns = _unknowns(equations, request.wrt)
    solutions = sp.solve(equations, unknowns, dict=True)
    exact = [{str(name): _text(value) for name, value in solution.items()} for solution in solutions]
    result: dict[str, Any] = {"solutions": exact, "unknowns": [str(name) for name in unknowns]}
    numeric = [{str(name): _numeric(value, request.precision) for name, value in solution.items()} for solution in solutions]
    if numeric and all(value is not None for solution in numeric for value in solution.values()):
        result["numeric"] = numeric
    return result


def _order(request: CalculationRequest, default: int) -> int:
    order = default if request.order is None else request.order
    if not 1 <= order <= MAX_ORDER:
        raise CalculatorInputError(f"order must be between 1 and {MAX_ORDER}.")
    return order


def _derive(expr: Any, request: CalculationRequest) -> dict[str, Any]:
    _require_defined(expr)
    variable = _variable(expr, request.wrt)
    order = _order(request, 1)
    return {"result": _text(sp.diff(expr, variable, order)), "wrt": str(variable), "order": order}


def _integrate(expr: Any, request: CalculationRequest) -> dict[str, Any]:
    _require_defined(expr)
    variable = _variable(expr, request.wrt)
    return {"result": _text(sp.integrate(expr, variable)), "wrt": str(variable), "note": "indefinite; no constant added"}


def _point(request: CalculationRequest) -> Any:
    return parse_expression(request.point) if request.point else sp.Integer(0)


def _limit(expr: Any, request: CalculationRequest) -> dict[str, Any]:
    _require_defined(expr)
    variable = _variable(expr, request.wrt)
    point = _point(request)
    return {"result": _text(sp.limit(expr, variable, point)), "wrt": str(variable), "point": str(point)}


def _series(expr: Any, request: CalculationRequest) -> dict[str, Any]:
    _require_defined(expr)
    variable = _variable(expr, request.wrt)
    point = _point(request)
    order = _order(request, DEFAULT_SERIES_ORDER)
    expansion = sp.series(expr, variable, point, order)
    return {"result": _text(expansion), "wrt": str(variable), "point": str(point), "order": order}


def _matrix(expr: Any, request: CalculationRequest) -> dict[str, Any]:
    value = sp.N(expr, request.precision) if request.force_numeric else expr
    result: dict[str, Any] = {"result": _text(value)}
    if isinstance(value, sp.MatrixBase):
        result["shape"] = list(value.shape)
    return result


_MODES = {
    "evaluate": _evaluate,
    "solve": _solve,
    "derive": _derive,
    "integrate": _integrate,
    "limit": _limit,
    "series": _series,
    "matrix": _matrix,
}


# -- the tool --------------------------------------------------------------------


def _substitute(expr: Any, variables: dict[str, Any]) -> Any:
    if not variables:
        return expr
    if not isinstance(expr, sp.Basic):
        raise CalculatorInputError("variables can only be substituted into a single expression.")
    values = {
        _symbol_named(expr, check_name(name, what="A variables name")): parse_expression(str(value))
        for name, value in variables.items()
    }
    return expr.subs(values)


def _calculate(request: CalculationRequest) -> dict[str, Any]:
    expr = _substitute(parse_expression(request.expression), request.variables)
    return _MODES[request.mode](expr, request)


def _checked_request(request: CalculationRequest) -> None:
    if request.mode not in _MODES:
        raise CalculatorInputError(f"mode must be one of: {', '.join(_MODES)}.")
    if not 1 <= request.precision <= MAX_PRECISION_DIGITS:
        raise CalculatorInputError(f"precision must be between 1 and {MAX_PRECISION_DIGITS} digits.")
    if len(request.variables) > MAX_VARIABLES:
        raise CalculatorInputError(f"At most {MAX_VARIABLES} variables can be substituted.")


def _result(status: str, payload: dict[str, Any]) -> Dict[str, Any]:
    return {"status": status, "content": [{"text": json.dumps(payload, ensure_ascii=False)}]}


def run_calculation(request: CalculationRequest) -> Dict[str, Any]:
    """Check, calculate within the time limit, and shape the tool result."""

    shown = request.expression
    if len(shown) > MAX_ECHOED_CHARS:
        shown = shown[:MAX_ECHOED_CHARS] + "..."
    base = {"mode": request.mode, "expression": shown}
    try:
        _checked_request(request)
        answer = run_with_time_limit(lambda: _calculate(request), seconds=CALCULATION_TIME_LIMIT_SECONDS)
    except CalculatorInputError as exc:
        return _result("error", {**base, "error": str(exc)})
    except (CalculationTimedOut, CalculatorBusy) as exc:
        logger.warning(
            "A calculation was stopped or refused for time. event=calculator_time_limit mode=%s chars=%s reason=%s",
            request.mode,
            len(request.expression),
            type(exc).__name__,
        )
        return _result("error", {**base, "error": f"{exc} Simplify the problem or split it into smaller steps."})
    except Exception as exc:  # noqa: BLE001 - SymPy reports unsolvable or malformed math with many exception types
        return _result("error", {**base, "error": f"Could not calculate this: {exc}"})
    return _result("success", {**base, **answer})


@tool
def calculator(
    expression: str,
    mode: CalculatorMode = "evaluate",
    precision: int = DEFAULT_PRECISION,
    force_numeric: bool = False,
    variables: Optional[Dict[str, Union[str, int, float]]] = None,
    wrt: Optional[str] = None,
    point: Optional[str] = None,
    order: Optional[int] = None,
) -> Dict[str, Any]:
    """Calculate with SymPy: arithmetic, algebra, equations, calculus and matrices, exactly or numerically.

    Write the expression in Python syntax with explicit operators and SymPy
    names: "2*x + 1", "sin(pi/6)", "factorial(5)", "Abs(x)", "Eq(x**2, 4)",
    "det(Matrix([[1, 2], [3, 4]]))". `^` also means power. Use pi, e (or E), I
    and oo (infinity). Quoted text is accepted only as Symbol('x'),
    symbols('x y'), Rational('1/3'), Integer('5') or Float('3.14'), optionally
    with true/false assumptions such as Symbol('x', positive=True).

    Modes:
    - "evaluate" (default): simplified exact result, plus "numeric" to
      `precision` significant digits when it is a number. With symbols the
      result stays symbolic unless force_numeric is true.
    - "solve": solve an equation (an expression means "= 0", or use Eq(a, b))
      or a system written as "x + y - 10, x - y - 2". Returns every solution
      as {variable: value}, exact, plus "numeric" when all values are numbers.
      With several variables in one equation, name the unknown in wrt.
    - "derive": derivative of order `order` (default 1) with respect to wrt.
    - "integrate": indefinite integral with respect to wrt (no constant
      added). For a definite integral use evaluate with
      "integrate(x**2, (x, 0, 1))".
    - "limit": limit as wrt approaches `point` (default 0; "oo" is infinity).
    - "series": series of `order` terms (default 5) around `point` (default 0).
    - "matrix": the matrix expression's exact value and shape.

    wrt may be left out when the expression has exactly one variable.

    Limits: expressions up to 2,000 characters; exact powers up to exponent
    10,000 (use a decimal base such as 2.0**n beyond that); factorial,
    binomial and gamma up to 10,000; precision up to 1,000 digits; order up to
    100; 10 seconds per calculation.

    Args:
        expression: The math to calculate, in Python syntax.
        mode: evaluate, solve, derive, integrate, limit, series or matrix.
        precision: Significant digits for numeric results (default 10).
        force_numeric: Return a decimal approximation even when an exact
            value exists (evaluate and matrix modes).
        variables: Values to substitute before calculating, for example
            {"a": 2, "b": "pi/4"}.
        wrt: The variable to differentiate, integrate, take the limit or
            expand in; for solve, the unknowns ("x" or "x, y").
        point: Where the limit is taken or the series is centred, as an
            expression such as "0", "pi/2" or "oo".
        order: Derivative order (derive) or number of series terms (series).

    Returns:
        A result whose text is JSON: "mode", "expression" and "result" (or
        "solutions"), plus "numeric", "wrt", "point", "order" or "shape" where
        they apply. On failure the status is error and "error" says why.
    """

    request = CalculationRequest(
        expression=str(expression or ""),
        mode=str(mode or "evaluate"),
        precision=int(precision if precision is not None else DEFAULT_PRECISION),
        force_numeric=bool(force_numeric),
        variables=dict(variables or {}),
        wrt=wrt,
        point=point,
        order=order,
    )
    return run_calculation(request)


__all__ = ["CALCULATION_TIME_LIMIT_SECONDS", "CalculationRequest", "calculator", "run_calculation"]
