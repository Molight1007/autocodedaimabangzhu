"""validator 注册表门禁测试（设计 §2「加单测 for a in ALLOWED_ACTIONS: assert a in _VALIDATORS」）。

守两件事：
1. 词表里的每个动作都必须有校验 schema（漏登记 -> 红）。
2. 未知动作、已登记但缺 schema 的动作都必须 raise，禁止旧实现的 fall-through 静默通过。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from autocode.agent.contracts import validator as validator_module
from autocode.agent.contracts.errors import (
    ERR_INVALID_ACTION,
    ERR_INVALID_FIELD_TYPE,
    ERR_INVALID_PAYLOAD,
    ERR_MISSING_FIELD,
    ERR_VALIDATOR_MISSING,
    ProtocolViolation,
)
from autocode.agent.contracts.protocol import ALLOWED_ACTIONS
from autocode.agent.contracts.validator import _VALIDATORS, validate_action_request


class TestValidatorRegistry(unittest.TestCase):
    def test_every_allowed_action_has_callable_validator(self) -> None:
        for action in ALLOWED_ACTIONS:
            self.assertIn(action, _VALIDATORS, f"动作 {action!r} 未登记校验 schema")
            self.assertTrue(callable(_VALIDATORS[action]), f"动作 {action!r} 的校验器不可调用")

    def test_registry_has_no_action_outside_vocabulary(self) -> None:
        self.assertEqual(set(_VALIDATORS), set(ALLOWED_ACTIONS))

    def test_unknown_action_raises_invalid_action(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request({"action": "run_case"})
        self.assertEqual(ctx.exception.error_code, ERR_INVALID_ACTION)
        self.assertIn("allowed", dict(ctx.exception.details or {}))

    def test_registered_action_without_schema_raises(self) -> None:
        """已进词表但注册表缺项：必须 raise，不能 fall-through 零校验通过。"""
        with patch.dict(validator_module._VALIDATORS, {"self_test": None}):
            with self.assertRaises(ProtocolViolation) as ctx:
                validate_action_request({"action": "self_test", "cases": []})
        self.assertEqual(ctx.exception.error_code, ERR_VALIDATOR_MISSING)
        self.assertEqual(str((ctx.exception.details or {}).get("action")), "self_test")

    def test_non_mapping_payload_raises(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request([])  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.error_code, ERR_INVALID_PAYLOAD)

    def test_action_name_case_and_whitespace_normalized(self) -> None:
        self.assertEqual(validate_action_request({"action": "  READ  "}), {"action": "read", "target": "code"})


class TestExistingValidatorsKeepBehavior(unittest.TestCase):
    """重构只改结构，不改既有 5 个动作的校验语义。"""

    def test_read_default_target(self) -> None:
        self.assertEqual(validate_action_request({"action": "read"})["target"], "code")

    def test_read_accepts_version_target(self) -> None:
        self.assertEqual(validate_action_request({"action": "read", "target": "v3"})["target"], "v3")

    def test_read_rejects_unknown_target(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request({"action": "read", "target": "secrets"})
        self.assertEqual(ctx.exception.error_code, ERR_INVALID_FIELD_TYPE)

    def test_read_rejects_non_string_target(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request({"action": "read", "target": None})
        self.assertEqual(ctx.exception.error_code, ERR_INVALID_FIELD_TYPE)

    def test_modify_requires_code(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request({"action": "modify"})
        self.assertEqual(ctx.exception.error_code, ERR_MISSING_FIELD)

    def test_modify_keeps_code_verbatim(self) -> None:
        payload = validate_action_request({"action": "modify", "code": "print(1)\n"})
        self.assertEqual(payload, {"action": "modify", "code": "print(1)\n"})

    def test_run_requires_code_or_use_current(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request({"action": "run"})
        self.assertEqual(ctx.exception.error_code, ERR_MISSING_FIELD)

    def test_run_use_current_mode(self) -> None:
        self.assertEqual(
            validate_action_request({"action": "run", "use_current": True}),
            {"action": "run", "mode": "current"},
        )

    def test_run_inline_mode(self) -> None:
        self.assertEqual(
            validate_action_request({"action": "run", "code": "x=1\n"}),
            {"action": "run", "mode": "inline", "code": "x=1\n"},
        )

    def test_finalize_code_hash_optional_and_stripped(self) -> None:
        self.assertEqual(validate_action_request({"action": "finalize"}), {"action": "finalize"})
        self.assertEqual(
            validate_action_request({"action": "finalize", "code_hash": "  abc  "}),
            {"action": "finalize", "code_hash": "abc"},
        )

    def test_finalize_rejects_non_string_code_hash(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request({"action": "finalize", "code_hash": 1})
        self.assertEqual(ctx.exception.error_code, ERR_INVALID_FIELD_TYPE)

    def test_self_test_requires_list(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request({"action": "self_test", "cases": "x"})
        self.assertEqual(ctx.exception.error_code, ERR_INVALID_FIELD_TYPE)

    def test_self_test_case_count_bounds(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request({"action": "self_test", "cases": []})
        self.assertEqual(ctx.exception.error_code, ERR_INVALID_FIELD_TYPE)
        too_many = [{"stdin": ""} for _ in range(6)]
        with self.assertRaises(ProtocolViolation):
            validate_action_request({"action": "self_test", "cases": too_many})

    def test_self_test_normalizes_case(self) -> None:
        payload = validate_action_request(
            {"action": "self_test", "cases": [{"stdin": "1 2\n", "label": " t "}]}
        )
        self.assertEqual(payload["cases"][0]["stdin"], "1 2\n")
        self.assertEqual(payload["cases"][0]["label"], "t")
        self.assertAlmostEqual(payload["cases"][0]["timeout_sec"], 2.0)

    def test_self_test_case_requires_stdin(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request({"action": "self_test", "cases": [{"label": "x"}]})
        self.assertEqual(ctx.exception.error_code, ERR_MISSING_FIELD)

    def test_self_test_timeout_out_of_range(self) -> None:
        with self.assertRaises(ProtocolViolation) as ctx:
            validate_action_request(
                {"action": "self_test", "cases": [{"stdin": "", "timeout_sec": 99}]}
            )
        self.assertEqual(ctx.exception.error_code, ERR_INVALID_FIELD_TYPE)


if __name__ == "__main__":
    unittest.main()
