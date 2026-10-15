"""Safe parsing of calculator expressions (used by ``custom_tools.calculator``).

Adapted from ``strands_tools/calculator.py`` in strands-agents-tools 0.8.9.
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. Licensed
under the Apache License, Version 2.0
(http://www.apache.org/licenses/LICENSE-2.0). Changed here: the safety checks
also run on the code SymPy's parser generates, powers and large-number
functions are bounded, names starting with ``_`` and unknown function names are
refused, and ``split_symbols`` is no longer applied (it broke keyword arguments
such as ``positive=True``).

Why these checks exist: SymPy's parser turns the text into Python code and
runs it with ``eval``. Code execution is blocked the same way the original
tool blocked it:

- the text must be a Python expression built only from the syntax in
  ``_ALLOWED_SYNTAX`` (no attribute access, subscripts, lambdas,
  comprehensions or imports), so ``solve.__globals__`` and similar escapes are
  rejected before anything runs;
- string literals are refused except as positional arguments of the
  constructors in ``_STRING_CONSTRUCTORS`` when the call has only true/false
  keywords. Other SymPy functions (``N``, ``simplify``, ``solve``) re-parse a
  string through ``sympify``, which runs it with full builtins; ``symbols('x',
  cls=N)`` does the same, so a non-boolean keyword makes the strings untrusted;
- ``eval`` sees only the names in ``_NAMESPACE`` and empty builtins.

Size checks keep one calculation from taking over the agent's process: a
calculation that runs too long is stopped between Python steps
(``custom_tools.calculator_time_limit``), but one giant exact-number step (for
example ``9**9**9``) cannot be stopped, so such inputs are refused here before
they run: the text length, exact powers with huge exponents (checked when the
power is computed, so a computed exponent counts too), factorial-like
functions of huge numbers, very high precision and ``symbols`` ranges.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication,
    standard_transformations,
    stringify_expr,
)

MAX_EXPRESSION_CHARS = 2000
# Largest exponent for an exact power such as 2**n or (x + 1)**n. A power with
# a decimal number in its base is computed approximately and is not limited.
MAX_EXACT_EXPONENT = 10_000
# Largest argument of factorial, binomial and gamma (factorial(10000) already
# has 35,660 digits).
MAX_FACTORIAL_ARGUMENT = 10_000
# Largest number of significant digits for N(), Float() and the tool's precision.
MAX_PRECISION_DIGITS = 1000
MAX_NAME_CHARS = 64

# `^` means power (convert_xor), and `(x + 1)(x - 1)` means multiplication.
# split_symbols is left out: it split keyword names (positive=True) and names
# such as `xy` into single letters.
_TRANSFORMATIONS = standard_transformations + (implicit_multiplication, convert_xor)
_POWER_FUNCTION = "_bounded_power"  # powers are rewritten to call this; input names cannot start with "_"
_STRING_CONSTRUCTORS = frozenset({"Symbol", "symbols", "Rational", "Integer", "Float"})

_ALLOWED_SYNTAX = frozenset(
    {
        ast.Expression, ast.Constant, ast.Name, ast.Load,
        ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.USub, ast.UAdd, ast.BitXor,
        ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
        ast.Tuple, ast.List, ast.And, ast.Or, ast.Not,
        ast.Starred, ast.keyword,
    }
)


class CalculatorInputError(ValueError):
    """The expression is refused or cannot be understood; the message says why."""


# -- bounded functions ---------------------------------------------------------


def _is_number_above(value: Any, limit: int) -> bool:
    number = sp.sympify(value) if isinstance(value, (int, float)) else value
    return bool(getattr(number, "is_Number", False) and abs(number) > limit)


def _bounded_power(base: Any, exponent: Any) -> Any:
    """``base ** exponent``, refusing an exact power with a huge exponent."""

    exact_exponent = getattr(exponent, "is_Rational", False) or isinstance(exponent, int)
    decimal_base = isinstance(base, sp.Basic) and base.has(sp.Float)
    if exact_exponent and not decimal_base and abs(exponent) > MAX_EXACT_EXPONENT:
        raise CalculatorInputError(
            f"Exponents above {MAX_EXACT_EXPONENT:,} are refused for exact powers: the exact result would be too "
            "large to compute. Use a decimal base (for example 2.0**n) for an approximate value."
        )
    return base**exponent


def _with_argument_limit(function: Callable[..., Any], name: str) -> Callable[..., Any]:
    def limited(*args: Any, **kwargs: Any) -> Any:
        if any(_is_number_above(value, MAX_FACTORIAL_ARGUMENT) for value in args):
            raise CalculatorInputError(f"{name}() is refused for numbers above {MAX_FACTORIAL_ARGUMENT:,}.")
        return function(*args, **kwargs)

    return limited


def _with_precision_limit(function: Callable[..., Any], name: str) -> Callable[..., Any]:
    """N(value, digits) and Float(value, digits): digits up to MAX_PRECISION_DIGITS."""

    def limited(*args: Any, **kwargs: Any) -> Any:
        digits = [*args[1:2], kwargs.get("n"), kwargs.get("dps")]
        bits = kwargs.get("precision")
        if any(_is_number_above(value, MAX_PRECISION_DIGITS) for value in digits if value is not None) or (
            bits is not None and _is_number_above(bits, MAX_PRECISION_DIGITS * 4)
        ):
            raise CalculatorInputError(f"{name}() is refused above {MAX_PRECISION_DIGITS} digits of precision.")
        return function(*args, **kwargs)

    return limited


def _symbols(names: Any, **assumptions: Any) -> Any:
    """``symbols('a b c')`` without ranges such as ``x:100000``, which create one symbol per step."""

    if isinstance(names, str) and ":" in names:
        raise CalculatorInputError("symbols() ranges (a:b) are refused; list the names instead, e.g. symbols('x0 x1 x2').")
    return sp.symbols(names, **assumptions)


# Only math functions and constants are reachable from an expression.
_NAMESPACE: dict[str, Any] = {
    # Constants
    "pi": sp.pi, "e": sp.E, "E": sp.E, "I": sp.I, "oo": sp.oo, "inf": sp.oo, "nan": sp.nan, "zoo": sp.zoo,
    "true": True, "false": False, "True": True, "False": False,
    # Trigonometric and hyperbolic
    "sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "cot": sp.cot, "sec": sp.sec, "csc": sp.csc,
    "asin": sp.asin, "acos": sp.acos, "atan": sp.atan, "atan2": sp.atan2,
    "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh, "asinh": sp.asinh, "acosh": sp.acosh, "atanh": sp.atanh,
    # Exponential, logarithm, powers and roots
    "exp": sp.exp, "log": sp.log, "ln": sp.ln, "sqrt": sp.sqrt, "cbrt": sp.cbrt,
    "Abs": sp.Abs, "abs": sp.Abs, "sign": sp.sign, "floor": sp.floor, "ceiling": sp.ceiling,
    # Combinatorics and special functions
    "factorial": _with_argument_limit(sp.factorial, "factorial"),
    "binomial": _with_argument_limit(sp.binomial, "binomial"),
    "gamma": _with_argument_limit(sp.gamma, "gamma"),
    "zeta": sp.zeta, "erf": sp.erf,
    "Max": sp.Max, "Min": sp.Min, "max": sp.Max, "min": sp.Min,
    # Constructors
    "Eq": sp.Eq, "Matrix": sp.Matrix, "Rational": sp.Rational, "Integer": sp.Integer,
    "Float": _with_precision_limit(sp.Float, "Float"), "Symbol": sp.Symbol, "symbols": _symbols,
    "N": _with_precision_limit(sp.N, "N"),
    # Matrices
    "det": sp.det, "transpose": sp.transpose, "trace": sp.trace,
    # Algebra and calculus
    "solve": sp.solve, "simplify": sp.simplify, "expand": sp.expand, "factor": sp.factor,
    "collect": sp.collect, "cancel": sp.cancel, "apart": sp.apart, "together": sp.together,
    "limit": sp.limit, "diff": sp.diff, "integrate": sp.integrate, "series": sp.series,
    "Sum": sp.Sum, "Product": sp.Product,
    _POWER_FUNCTION: _bounded_power,
}
_NO_BUILTINS: dict[str, Any] = {"__builtins__": {}}


# -- syntax checks -------------------------------------------------------------


def _only_boolean_keywords(call: ast.Call) -> bool:
    """True when every keyword is a true/false assumption such as ``positive=True``.

    Any other keyword (``cls=N``, or ``**options`` where ``arg`` is None) can
    change how the constructor treats its string, so the strings are not trusted.
    """

    return all(
        keyword.arg is not None
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, bool)
        for keyword in call.keywords
    )


def _trusted_strings(tree: ast.AST) -> set[int]:
    """The string literals that are positional arguments of a safe constructor call."""

    trusted: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id in _STRING_CONSTRUCTORS and _only_boolean_keywords(node):
            trusted.update(id(arg) for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
    return trusted


def _check_name(node: ast.Name) -> None:
    if node.id.startswith("_"):
        raise CalculatorInputError(f"Invalid mathematical expression: the name {node.id!r} is not allowed.")


def _check_call(node: ast.Call) -> None:
    """Calls go to a known function by name, or are implicit products such as ``(x + 1)(x - 1)``."""

    if not isinstance(node.func, ast.Name) or node.func.id in _NAMESPACE or node.func.id.startswith("_"):
        return  # names starting with "_" are refused by _check_name
    raise CalculatorInputError(
        f"Invalid mathematical expression: {node.func.id!r} is not a known function. "
        f"For multiplication write {node.func.id}*(...)."
    )


def _check_node(node: ast.AST, trusted_strings: set[int]) -> None:
    if type(node) not in _ALLOWED_SYNTAX:
        raise CalculatorInputError(f"Invalid mathematical expression: unsupported syntax '{type(node).__name__}'.")
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)) and id(node) not in trusted_strings:
        raise CalculatorInputError("Invalid mathematical expression: string literals are not supported here.")
    if isinstance(node, ast.BinOp) and any(isinstance(side, (ast.List, ast.Tuple)) for side in (node.left, node.right)):
        raise CalculatorInputError("Invalid mathematical expression: arithmetic on lists is not supported.")
    if isinstance(node, ast.Name):
        _check_name(node)
    elif isinstance(node, ast.Call):
        _check_call(node)


def check_syntax(tree: ast.Expression) -> None:
    """Refuse anything but plain math (module docstring); raises CalculatorInputError."""

    trusted = _trusted_strings(tree)
    for node in ast.walk(tree):
        _check_node(node, trusted)


def _parse_python(text: str) -> ast.Expression:
    try:
        return ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise CalculatorInputError(f"Invalid mathematical expression: {exc.msg}.") from exc


class _BoundPowers(ast.NodeTransformer):
    """Rewrites ``a ** b`` to ``_bounded_power(a, b)`` so the exponent is checked once it is known."""

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if not isinstance(node.op, ast.Pow):
            return node
        call = ast.Call(func=ast.Name(id=_POWER_FUNCTION, ctx=ast.Load()), args=[node.left, node.right], keywords=[])
        return ast.copy_location(call, node)


def parse_expression(text: str) -> Any:
    """Parse one calculator expression into SymPy objects, refusing unsafe or oversized input.

    The text is checked, SymPy's parser turns it into Python code (symbols,
    exact numbers, ``^`` as power), that generated code is checked again with
    the same rules, its powers are bounded, and only then is it run with the
    restricted names and no builtins.
    """

    if not isinstance(text, str) or not text.strip():
        raise CalculatorInputError("The expression must be non-empty text.")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise CalculatorInputError(f"The expression is longer than {MAX_EXPRESSION_CHARS} characters.")
    check_syntax(_parse_python(text))
    namespace = dict(_NAMESPACE)  # SymPy's parser may add entries; each call gets its own copy
    try:
        code = stringify_expr(text, namespace, _NO_BUILTINS, _TRANSFORMATIONS)
    except Exception as exc:  # noqa: BLE001 - SymPy's tokenizer raises assorted errors for malformed math
        raise CalculatorInputError(f"Invalid mathematical expression: {exc}") from exc
    generated = _parse_python(code)
    check_syntax(generated)
    bounded = ast.fix_missing_locations(_BoundPowers().visit(generated))
    try:
        return eval(compile(bounded, "<calculator>", "eval"), _NO_BUILTINS, namespace)  # noqa: S307 - checked, restricted names, no builtins
    except CalculatorInputError:
        raise
    except Exception as exc:  # noqa: BLE001 - SymPy reports bad math with many exception types
        raise CalculatorInputError(f"Invalid mathematical expression: {exc}") from exc


def check_name(name: str, *, what: str) -> str:
    """A variable name given outside the expression (wrt, variables): a plain identifier."""

    cleaned = str(name or "").strip()
    if not cleaned.isidentifier() or cleaned.startswith("_") or len(cleaned) > MAX_NAME_CHARS:
        raise CalculatorInputError(f"{what} must be a plain variable name such as x, got {name!r}.")
    return cleaned


__all__ = [
    "MAX_EXACT_EXPONENT",
    "MAX_EXPRESSION_CHARS",
    "MAX_FACTORIAL_ARGUMENT",
    "MAX_PRECISION_DIGITS",
    "CalculatorInputError",
    "check_name",
    "check_syntax",
    "parse_expression",
]
