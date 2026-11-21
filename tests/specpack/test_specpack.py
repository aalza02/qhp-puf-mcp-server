"""Runs the official SpecPack conformance tests from tests.yaml against our implementation."""

import os
import yaml
import pytest

from qhp_mcp.specpack import validate_pack, attest_pack

TESTS_PATH = os.path.join(os.path.dirname(__file__), "tests.yaml")

with open(TESTS_PATH) as f:
    SUITE = yaml.safe_load(f)


@pytest.mark.parametrize(
    "case",
    SUITE.get("validate_pack", []),
    ids=[c["name"] for c in SUITE.get("validate_pack", [])],
)
def test_validate_pack(case):
    result = validate_pack(case["input"]["files"])
    expected = case["output"]
    assert result["valid"] == expected["valid"], f"valid mismatch: {result}"
    assert result["errors"] == expected["errors"], f"errors mismatch: {result['errors']}"
    assert result["warnings"] == expected["warnings"], f"warnings mismatch: {result['warnings']}"


@pytest.mark.parametrize(
    "case",
    SUITE.get("attest_pack", []),
    ids=[c["name"] for c in SUITE.get("attest_pack", [])],
)
def test_attest_pack(case):
    result = attest_pack(case["input"]["files"])
    expected = case["output"]
    assert result["files"] == expected["files"], f"files mismatch: {result['files']}"