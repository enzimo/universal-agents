"""The calculator tool: its math, the checks that keep expressions from running code, its limits, and its catalog entry.

The tool runs inside agent processes, so the security tests matter most: the
expression reaches Python ``eval`` through SymPy's parser, and every escape
the original strands_tools calculator blocked must stay blocked here.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from access_catalog.catalog import load_catalog
from access_catalog.tool_functions import functions_of_tool_sets
from access_control.standard_grants import AGENT_RUNTIME_SET, INTERNET_SET, STANDARD_TOOL_SETS_BY_ROLE
from custom_messages.contracts.access_v2 import AccessLevel
from custom_tools import calculator as calculator_module
from custom_tools import calculator_parsing, calculator_time_limit
from custom_tools.calculator import calculator
from custom_tools.calculator_parsing import CalculatorInputError, parse_expression


def calc(expression: str, **arguments):
    result = calculator(expression=expression, **arguments)
    return result["status"], json.loads(result["content"][0]["text"])


def refused(expression: str, **arguments) -> str:
    status, payload = calc(expression, **arguments)
    assert status == "error", payload
    return payload["error"]


# -- math --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "arguments", "expected"),
    [
        ("2 * sin(pi/4) + log(e**2)", {}, {"result": "sqrt(2) + 2", "numeric": "3.414213562"}),
        ("sin(pi/6)", {}, {"result": "1/2", "numeric": "0.5000000000"}),
        ("2^10", {}, {"result": "1024"}),
        ("(x + 1)(x - 1)", {}, {"result": "x**2 - 1"}),
        ("N(pi, 30)", {}, {"result": "3.14159265358979323846264338328"}),
        ("sqrt(2)", {"precision": 20}, {"numeric": "1.4142135623730950488"}),
        ("x**2 + 1", {"force_numeric": True, "variables": {"x": "1/3"}}, {"result": "1.111111111"}),
        ("a*x + b", {"variables": {"a": 2, "b": "pi/4", "x": 1}}, {"result": "pi/4 + 2"}),
        ("integrate(x**2, (x, 0, 1))", {}, {"result": "1/3"}),
        ("1.0001**100000", {}, {"result": "22015.456"}),  # a decimal base is not limited
    ],
)
def test_evaluate(expression, arguments, expected):
    status, payload = calc(expression, **arguments)
    assert status == "success", payload
    for key, value in expected.items():
        assert payload[key].startswith(value) if key == "result" and "." in value else payload[key] == value


def test_solve_returns_exact_solutions_with_numeric_values():
    status, payload = calc("x**2 - 2", mode="solve")
    assert status == "success"
    assert payload["solutions"] == [{"x": "-sqrt(2)"}, {"x": "sqrt(2)"}]
    assert payload["numeric"] == [{"x": "-1.414213562"}, {"x": "1.414213562"}]


def test_solve_systems_and_named_unknowns():
    assert calc("x + y - 10, x - y - 2", mode="solve")[1]["solutions"] == [{"x": "6", "y": "4"}]
    assert calc("Eq(x**2 + y, 4)", mode="solve", wrt="y")[1]["solutions"] == [{"y": "4 - x**2"}]
    assert "Name the unknown with wrt" in refused("x**2 + y", mode="solve")


@pytest.mark.parametrize(
    ("expression", "arguments", "result"),
    [
        ("sin(x)", {"mode": "derive", "order": 2}, "-sin(x)"),
        ("e**x", {"mode": "derive"}, "exp(x)"),  # e stays exact (the original replaced it with a decimal)
        ("Symbol('x', positive=True)**2", {"mode": "derive"}, "2*x"),
        ("x*y", {"mode": "derive", "wrt": "y"}, "x"),
        ("x**2 + 2*x", {"mode": "integrate"}, "x**3/3 + x**2"),
        ("sin(x)/x", {"mode": "limit"}, "1"),
        ("1/x", {"mode": "limit", "point": "oo"}, "0"),
        ("exp(x)", {"mode": "series", "order": 4}, "1 + x + x**2/2 + x**3/6 + O(x**4)"),
        ("det(Matrix([[1, 2], [3, 4]]))", {"mode": "matrix"}, "-2"),
        ("Matrix([[1, 2], [3, 4]])**2", {"mode": "matrix"}, "Matrix([[7, 10], [15, 22]])"),
    ],
)
def test_calculus_and_matrix_modes(expression, arguments, result):
    status, payload = calc(expression, **arguments)
    assert status == "success", payload
    assert payload["result"] == result


def test_calculus_modes_need_wrt_when_the_expression_has_several_variables():
    assert "Name one with wrt" in refused("x*y", mode="derive")
    assert "undefined or infinite" in refused("1/0", mode="integrate", wrt="x")


# -- code execution is refused ----------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "solve.__globals__['__builtins__']['__import__']('os').system('id')",  # attribute access
        "(1).__class__",
        "Matrix([[1]])[0]",  # subscript
        "lambda: 1",
        "[x for x in (1, 2)]",
        "__import__('os')",  # underscore names never reach the namespace
        "_bounded_power(2, 3)",
        "import os",
        "N('__import__(\"os\").system(\"id\")')",  # strings re-parsed by sympify with full builtins
        "simplify('1+1')",
        "symbols('x', cls=N)",  # cls reroutes symbols through sympify
        "Symbol('x', **{'cls': N})",
        "f(x)",  # unknown names cannot be called
        "{1: 2}",
    ],
)
def test_expressions_that_could_run_code_are_refused(expression):
    with pytest.raises(CalculatorInputError):
        parse_expression(expression)


def test_string_constructors_still_accept_names_and_numbers():
    assert str(parse_expression("Symbol('x') + Rational('1/3') + Integer('5') + Float('0.5')")) == "x + 5.83333333333333"
    assert str(parse_expression("symbols('a b')")) == "(a, b)"


def test_the_code_sympy_generates_is_checked_too(monkeypatch):
    """Everything SymPy's parser outputs passes the same checks before it runs."""

    checked: list[str] = []
    original = calculator_parsing.check_syntax
    monkeypatch.setattr(calculator_parsing, "check_syntax", lambda tree: (checked.append(type(tree).__name__), original(tree))[1])
    parse_expression("x + 1")
    assert checked == ["Expression", "Expression"]


# -- size and time limits -------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("9**9**9", "Exponents above"),
        ("2**factorial(20)", "Exponents above"),  # a computed exponent counts too
        ("Rational(1, 2)**(10**6)", "Exponents above"),
        ("(x + 1)**20000", "Exponents above"),
        ("factorial(10**6)", "factorial() is refused"),
        ("binomial(10**9, 5*10**8)", "binomial() is refused"),
        ("gamma(10**7)", "gamma() is refused"),
        ("N(pi, 5000)", "N() is refused"),
        ("Float(1, 5000)", "Float() is refused"),
        ("symbols('x:100000000')", "ranges"),
        ("[0]*10**8", "arithmetic on lists"),
        ("x" * 2001, "longer than 2000"),
    ],
)
def test_oversized_calculations_are_refused_before_they_run(expression, message):
    assert message in refused(expression)


def test_argument_limits():
    assert "precision must be between" in refused("1", precision=5000)
    assert "order must be between" in refused("exp(x)", mode="series", order=1000)
    assert "plain variable name" in refused("x", mode="derive", wrt="x.real")
    assert "at most" in refused("1", variables={f"v{index}": 1 for index in range(51)}).lower()


def test_huge_integers_are_shown_as_approximations():
    status, payload = calc("factorial(5000)")
    assert status == "success"
    assert "digits; too long to show exactly" in payload["result"]


def _calculator_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "calculator"]


def test_a_long_calculation_is_stopped_and_the_next_one_runs(monkeypatch):
    monkeypatch.setattr(calculator_module, "CALCULATION_TIME_LIMIT_SECONDS", 0.3)
    started = time.monotonic()
    error = refused("expand((x + y + z)**300)")
    assert "took longer than 0.3 seconds" in error
    assert time.monotonic() - started < 2
    deadline = time.monotonic() + 5
    while _calculator_threads() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _calculator_threads(), "the stopped calculation thread is still running"
    assert calc("1 + 1") == ("success", {"mode": "evaluate", "expression": "1 + 1", "result": "2"})


def test_the_stop_request_is_repeated_when_code_swallows_it():
    """mpmath has bare ``except:`` blocks; one swallowed stop request must not leave the work running."""

    swallowed = []

    def stubborn() -> None:
        while True:
            try:
                while True:
                    pass
            except BaseException:  # noqa: BLE001 - imitates a bare except in library code
                swallowed.append(True)
                if len(swallowed) > 1:
                    raise

    with pytest.raises(calculator_time_limit.CalculationTimedOut):
        calculator_time_limit.run_with_time_limit(stubborn, seconds=0.2)
    deadline = time.monotonic() + 5
    while _calculator_threads() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(swallowed) == 2
    assert not _calculator_threads()


def test_the_time_limit_returns_values_and_errors():
    assert calculator_time_limit.run_with_time_limit(lambda: 42, seconds=1) == 42
    with pytest.raises(ZeroDivisionError):
        calculator_time_limit.run_with_time_limit(lambda: 1 / 0, seconds=1)


def test_the_tool_description_states_the_real_limits():
    words = " ".join(calculator.tool_spec["description"].split())
    assert f"expressions up to {calculator_parsing.MAX_EXPRESSION_CHARS:,} characters" in words
    assert f"exact powers up to exponent {calculator_parsing.MAX_EXACT_EXPONENT:,}" in words
    assert f"gamma up to {calculator_parsing.MAX_FACTORIAL_ARGUMENT:,}" in words
    assert f"precision up to {calculator_parsing.MAX_PRECISION_DIGITS:,} digits" in words
    assert f"order up to {calculator_module.MAX_ORDER}" in words
    assert f"{calculator_module.CALCULATION_TIME_LIMIT_SECONDS:g} seconds per calculation" in words


# -- catalog and tool lists -------------------------------------------------------------


def test_the_calculator_is_a_read_level_agent_runtime_tool():
    catalog = load_catalog()
    resolution = catalog.resolve_function("calculator", {})
    assert (resolution.tool_id, resolution.level) == ("fn:calculator", AccessLevel.READ)
    assert AGENT_RUNTIME_SET in catalog.tools["fn:calculator"].tool_sets


@pytest.mark.parametrize("role", sorted(STANDARD_TOOL_SETS_BY_ROLE))
def test_every_role_starts_with_the_calculator(role):
    """What tool_sync.startup_tools and the agent catalog give each role without grants."""

    functions = functions_of_tool_sets(load_catalog(), (*STANDARD_TOOL_SETS_BY_ROLE[role], INTERNET_SET))
    assert "calculator" in functions


def test_shipped_agent_types_have_the_calculator_with_authorization_off():
    from custom_agents.agent_catalog import load_agent_catalog

    agent_types = load_agent_catalog(include_configured=False).agent_types
    missing = [name for name, definition in agent_types.items() if "calculator" not in {tool.tool_name for tool in definition.tools}]
    assert not missing
