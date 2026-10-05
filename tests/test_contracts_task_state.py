"""Unit tier (doc 11): contracts, predicates (3-valued), conditions, task validation."""
from __future__ import annotations

import pytest

from eva.core import (Condition, ContractError, IdempotencyClass, ParamSpec, Predicate,
                      RiskLevel, Step, Task, ToolContract, ToolRegistry, Truth,
                      VerificationSpec, parse_predicate)
from eva.core.state import Observation, request_key
from tests.fakes import FakeWorld, build_fake_registry


def _contract(**kw) -> ToolContract:
    base = dict(name="x.y", category="os", description="d",
                verification=VerificationSpec(method="L1_process"),
                idempotency=IdempotencyClass.NATURALLY_IDEMPOTENT, risk=RiskLevel.LOW)
    base.update(kw)
    return ToolContract(**base)


async def _handler(args, ctx):
    return None


# ---------------------------------------------------------------- contracts
def test_contract_without_effects_cannot_be_registered():
    reg = ToolRegistry()
    with pytest.raises(ContractError):
        reg.register(_contract(), _handler)


def test_verification_may_not_be_visual():
    with pytest.raises(ValueError):
        VerificationSpec(method="L6_screenshot")
    with pytest.raises(ValueError):
        VerificationSpec(method="L7_vision_llm")
    # structural names are fine
    for method in ("L1_process", "L2_a11y_state_diff", "L3_cdp_url", "fs_stat", "fs_event_downloads"):
        VerificationSpec(method=method)


def test_registry_rejects_unknown_and_missing_args():
    reg = build_fake_registry(FakeWorld())
    with pytest.raises(ContractError):
        reg.validate_args("chrome.open_profile", {"profile": "a", "url": "b", "boom": 1})
    with pytest.raises(ContractError):
        reg.validate_args("chrome.open_profile", {"profile": "a"})
    ok = reg.validate_args("chrome.open_profile", {"profile": "a", "url": "b"})
    assert ok == {"profile": "a", "url": "b"}


def test_registry_coerces_param_types():
    reg = ToolRegistry()
    reg.register(_contract(name="os.click", params={"x": ParamSpec(type="int"), "y": ParamSpec(type="int")},
                           effects=[Predicate(path="ui.last_click", op="exists")]), _handler)
    assert reg.validate_args("os.click", {"x": "10", "y": 20}) == {"x": 10, "y": 20}
    with pytest.raises(ContractError):
        reg.validate_args("os.click", {"x": "abc", "y": 1})


def test_unregistered_tool_is_not_executable():
    reg = build_fake_registry(FakeWorld())
    with pytest.raises(ContractError):
        reg.require("shell.exec")   # the v1 '/archive'-style fantasy route
    assert "shell.exec" not in reg.names()


# ---------------------------------------------------------------- predicates
def _obs(value, truth=Truth.TRUE) -> Observation:
    return Observation(path="p", truth=truth, value=value, level="L1")


def test_predicate_unknown_is_never_true_or_false():
    p = Predicate(path="chrome.running", op="==", value=True)
    assert p.matches_observation(None) is Truth.UNKNOWN
    unknown = Observation(path="chrome.running", truth=Truth.UNKNOWN, value=None)
    assert p.matches_observation(unknown) is Truth.UNKNOWN


def test_predicate_observed_false_is_a_real_answer():
    p = Predicate(path="wa.login", op="==", value=False)
    assert p.matches_observation(_obs(False)) is Truth.TRUE
    p2 = Predicate(path="wa.login", op="==", value=True)
    assert p2.matches_observation(_obs(False)) is Truth.FALSE


def test_predicate_strings_case_insensitive_and_globs():
    assert Predicate(path="wa.selected_chat", op="==", value="saad bhai").matches_observation(
        _obs("Saad Bhai")) is Truth.TRUE
    assert Predicate(path="chrome.page_url", op="matches", value="*web.whatsapp.com*").matches_observation(
        _obs("https://web.whatsapp.com/")) is Truth.TRUE
    assert Predicate(path="chrome.page_url", op="matches", value="*web.whatsapp.com*").matches_observation(
        _obs("https://example.com")) is Truth.FALSE


def test_predicate_exists_semantics_and_type_mismatch():
    assert Predicate(path="chrome.active_profile", op="exists").matches_observation(
        _obs(False)) is Truth.TRUE          # observed value present (False is a value)
    assert Predicate(path="chrome.active_profile", op="exists").matches_observation(
        _obs(None)) is Truth.FALSE
    assert Predicate(path="x.n", op=">", value=5).matches_observation(
        _obs("not-a-number")) is Truth.UNKNOWN


def test_condition_three_valued_folding():
    obs = {
        request_key("a.b"): _obs(True),
        request_key("c.d", {"k": 1}): _obs(False),
    }
    cond = Condition(all_of=[Predicate(path="a.b", op="==", value=True)])
    assert cond.evaluate(obs) is Truth.TRUE
    cond_bad = Condition(all_of=[Predicate(path="a.b", op="==", value=True),
                                 Predicate(path="c.d", op="==", value=True, args={"k": 1})])
    assert cond_bad.evaluate(obs) is Truth.FALSE
    cond_unknown = Condition(all_of=[Predicate(path="a.b", op="==", value=True),
                                     Predicate(path="zz.yy", op="==", value=1)])
    assert cond_unknown.evaluate(obs) is Truth.UNKNOWN
    any_cond = Condition(any_of=[Predicate(path="zz.yy", op="==", value=1),
                                 Predicate(path="a.b", op="==", value=True)])
    assert any_cond.evaluate(obs) is Truth.TRUE


def test_condition_must_be_nonempty():
    with pytest.raises(ValueError):
        Condition().validate_nonempty()
    with pytest.raises(ValueError):
        Task(goal="x", completion=Condition(), plan=[]).validate()


def test_parse_predicate_shorthand():
    p = parse_predicate("chrome.running == true")
    assert p.path == "chrome.running" and p.op == "==" and p.value is True
    p2 = parse_predicate("chrome.page_url ~= *whatsapp*")
    assert p2.op == "matches" and p2.value == "*whatsapp*"
    p3 = parse_predicate('wa.selected_chat == "Saad Bhai"')
    assert p3.value == "Saad Bhai"
    p4 = parse_predicate("system.cpu_percent >= 80")
    assert p4.value == 80 and p4.op == ">="


def test_task_rejects_duplicate_step_numbers():
    t = Task(goal="g", completion=Condition(all_of=[Predicate(path="a.b", op="==", value=1)]),
             plan=[Step(n=1, intent="a", tool="t1"), Step(n=1, intent="b", tool="t2")])
    with pytest.raises(ValueError):
        t.validate()
