"""
tests/test_outcome.py – Tests for ApplyResult, FailureType, and outcome helpers.

Run with:
    python -m pytest tests/test_outcome.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.outcome import (
    ApplyResult, FailureType, PageType, StepIntent,
    ReadinessReport, make_failure,
    RETRYABLE_FAILURES, MANUAL_INTERVENTION_FAILURES,
)


class TestApplyResult:
    def test_success_construction(self):
        r = ApplyResult(success=True)
        assert r.success is True
        assert r.failure_type is None
        assert r.failure_reason == ""
        assert r.retryable is False
        assert r.manual_intervention is False

    def test_failure_construction(self):
        r = ApplyResult(
            success=False,
            failure_type=FailureType.MISSING_PROFILE,
            failure_reason="No profile found",
            retryable=False,
            manual_intervention=True,
        )
        assert r.success is False
        assert r.failure_type == FailureType.MISSING_PROFILE
        assert r.failure_reason == "No profile found"
        assert r.manual_intervention is True

    def test_to_dict_keys(self):
        r = ApplyResult(
            success=False,
            failure_type=FailureType.TIMEOUT,
            failure_reason="timed out",
            debug_dir="/tmp/test",
        )
        d = r.to_dict()
        for key in ("success", "failure_type", "failure_reason", "retryable",
                    "manual_intervention", "debug_dir", "attempts", "unresolved_fields"):
            assert key in d, f"Missing key: {key!r}"

    def test_to_dict_failure_type_is_string(self):
        r = ApplyResult(success=False, failure_type=FailureType.TIMEOUT)
        d = r.to_dict()
        assert isinstance(d["failure_type"], str)
        assert d["failure_type"] == "timeout"

    def test_to_dict_no_failure_type(self):
        r = ApplyResult(success=True)
        d = r.to_dict()
        assert d["failure_type"] is None

    def test_unresolved_fields_default_empty_list(self):
        r = ApplyResult(success=False)
        assert r.unresolved_fields == []

    def test_unresolved_fields_populated(self):
        fields = [{"label": "Resume", "selector": "#resume"}]
        r = ApplyResult(success=False, unresolved_fields=fields)
        assert len(r.unresolved_fields) == 1


class TestFailureTypeRetryability:
    def test_timeout_is_retryable(self):
        assert FailureType.TIMEOUT in RETRYABLE_FAILURES

    def test_browser_launch_failed_is_retryable(self):
        assert FailureType.BROWSER_LAUNCH_FAILED in RETRYABLE_FAILURES

    def test_site_error_is_retryable(self):
        assert FailureType.SITE_ERROR in RETRYABLE_FAILURES

    def test_llm_analysis_failed_is_retryable(self):
        assert FailureType.LLM_ANALYSIS_FAILED in RETRYABLE_FAILURES

    def test_missing_profile_not_retryable(self):
        assert FailureType.MISSING_PROFILE not in RETRYABLE_FAILURES

    def test_login_required_not_retryable(self):
        assert FailureType.LOGIN_REQUIRED not in RETRYABLE_FAILURES

    def test_unresolved_required_fields_not_retryable(self):
        assert FailureType.UNRESOLVED_REQUIRED_FIELDS not in RETRYABLE_FAILURES

    def test_stuck_not_retryable(self):
        assert FailureType.STUCK_SAME_PAGE_NO_PROGRESS not in RETRYABLE_FAILURES


class TestManualInterventionClassification:
    def test_login_required_needs_manual(self):
        assert FailureType.LOGIN_REQUIRED in MANUAL_INTERVENTION_FAILURES

    def test_captcha_needs_manual(self):
        assert FailureType.CAPTCHA_OR_HUMAN_VERIFICATION in MANUAL_INTERVENTION_FAILURES

    def test_missing_profile_needs_manual(self):
        assert FailureType.MISSING_PROFILE in MANUAL_INTERVENTION_FAILURES

    def test_missing_resume_needs_manual(self):
        assert FailureType.MISSING_RESUME in MANUAL_INTERVENTION_FAILURES

    def test_missing_claude_needs_manual(self):
        assert FailureType.MISSING_CLAUDE in MANUAL_INTERVENTION_FAILURES

    def test_unresolved_required_fields_needs_manual(self):
        assert FailureType.UNRESOLVED_REQUIRED_FIELDS in MANUAL_INTERVENTION_FAILURES

    def test_timeout_not_manual(self):
        assert FailureType.TIMEOUT not in MANUAL_INTERVENTION_FAILURES

    def test_site_error_not_manual(self):
        assert FailureType.SITE_ERROR not in MANUAL_INTERVENTION_FAILURES


class TestMakeFailure:
    def test_make_failure_sets_retryable_true_for_retryable_type(self):
        r = make_failure(FailureType.TIMEOUT, reason="timed out")
        assert r.success is False
        assert r.retryable is True
        assert r.failure_type == FailureType.TIMEOUT

    def test_make_failure_sets_manual_intervention_true(self):
        r = make_failure(FailureType.LOGIN_REQUIRED, reason="login required")
        assert r.manual_intervention is True
        assert r.retryable is False

    def test_make_failure_default_reason_is_failure_type_value(self):
        r = make_failure(FailureType.TIMEOUT)
        assert r.failure_reason == "timeout"

    def test_make_failure_custom_reason(self):
        r = make_failure(FailureType.SITE_ERROR, reason="500 Internal Server Error")
        assert r.failure_reason == "500 Internal Server Error"

    def test_make_failure_debug_dir(self):
        r = make_failure(FailureType.TIMEOUT, debug_dir="/tmp/test")
        assert r.debug_dir == "/tmp/test"

    def test_make_failure_unresolved_fields(self):
        fields = [{"label": "Name"}]
        r = make_failure(FailureType.UNRESOLVED_REQUIRED_FIELDS, unresolved_fields=fields)
        assert r.unresolved_fields == fields


class TestEnumValues:
    def test_failure_type_string_values(self):
        assert FailureType.MISSING_PROFILE == "missing_profile"
        assert FailureType.LOGIN_REQUIRED == "login_required"
        assert FailureType.TIMEOUT == "timeout"

    def test_page_type_string_values(self):
        assert PageType.SUCCESS == "success"
        assert PageType.LOGIN_WALL == "login_wall"
        assert PageType.CAPTCHA == "captcha"
        assert PageType.FILL == "fill"

    def test_step_intent_string_values(self):
        assert StepIntent.CLICK_SUBMIT == "click_submit"
        assert StepIntent.CLICK_NEXT == "click_next"
        assert StepIntent.ABORT == "abort"


class TestReadinessReport:
    def test_ready_true(self):
        r = ReadinessReport(ready=True, checks={"profile": True, "resume": True, "claude": True})
        assert r.ready is True
        assert r.first_failure is None

    def test_ready_false_with_failure(self):
        r = ReadinessReport(
            ready=False,
            checks={"profile": False},
            first_failure=FailureType.MISSING_PROFILE,
            error="Profile missing",
        )
        assert r.ready is False
        assert r.first_failure == FailureType.MISSING_PROFILE

    def test_to_dict(self):
        r = ReadinessReport(
            ready=False,
            checks={"profile": False},
            first_failure=FailureType.MISSING_PROFILE,
            error="Profile missing",
        )
        d = r.to_dict()
        assert d["ready"] is False
        assert d["first_failure"] == "missing_profile"
        assert d["error"] == "Profile missing"
        assert d["checks"] == {"profile": False}
