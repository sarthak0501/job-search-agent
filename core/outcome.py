from __future__ import annotations
"""
outcome.py – Structured outcome types for the auto-apply pipeline.

Replaces loose bool + dict returns with typed, machine-readable results
that can drive retry logic, UI status, and debug artifact naming.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FailureType(str, Enum):
    MISSING_PROFILE = "missing_profile"
    MISSING_RESUME = "missing_resume"
    MISSING_CLAUDE = "missing_claude"
    BROWSER_LAUNCH_FAILED = "browser_launch_failed"
    LOGIN_REQUIRED = "login_required"
    CAPTCHA_OR_HUMAN_VERIFICATION = "captcha_or_human_verification"
    UNRESOLVED_REQUIRED_FIELDS = "unresolved_required_fields"
    UNSUPPORTED_WIDGET = "unsupported_widget"
    SELECTOR_RESOLUTION_FAILED = "selector_resolution_failed"
    LLM_ANALYSIS_FAILED = "llm_analysis_failed"
    LLM_ACTION_GENERATION_FAILED = "llm_action_generation_failed"
    REPEATED_VALIDATION_ERRORS = "repeated_validation_errors"
    STUCK_SAME_PAGE_NO_PROGRESS = "stuck_same_page_no_progress"
    CYCLING_BETWEEN_STEPS = "cycling_between_steps"
    UNKNOWN_POST_SUBMIT_STATE = "unknown_post_submit_state"
    SUBMISSION_NOT_CONFIRMED = "submission_not_confirmed"
    SITE_ERROR = "site_error"
    TIMEOUT = "timeout"
    HUMAN_VERIFICATION_BLOCKED = "human_verification_blocked"
    AUTH_SESSION_EXPIRED = "auth_session_expired"


class PageType(str, Enum):
    FILL = "fill"
    INTERMEDIATE = "intermediate"
    REVIEW = "review"
    FINAL_SUBMIT = "final_submit"
    SUCCESS = "success"
    LOGIN_WALL = "login_wall"
    CAPTCHA = "captcha"
    SITE_ERROR = "site_error"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class StepIntent(str, Enum):
    FILL_FIELDS = "fill_fields"
    CLICK_NEXT = "click_next"
    CLICK_CONTINUE = "click_continue"
    CLICK_REVIEW = "click_review"
    CLICK_SUBMIT = "click_submit"
    WAIT = "wait"
    ABORT = "abort"


@dataclass
class ApplyResult:
    success: bool
    failure_type: Optional[FailureType] = None
    failure_reason: str = ""
    retryable: bool = False
    manual_intervention: bool = False
    debug_dir: str = ""
    attempts: int = 0
    unresolved_fields: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "failure_type": self.failure_type.value if self.failure_type else None,
            "failure_reason": self.failure_reason,
            "retryable": self.retryable,
            "manual_intervention": self.manual_intervention,
            "debug_dir": self.debug_dir,
            "attempts": self.attempts,
            "unresolved_fields": self.unresolved_fields,
        }


# Failures that are worth retrying automatically (transient / infrastructure issues)
RETRYABLE_FAILURES: set[FailureType] = {
    FailureType.BROWSER_LAUNCH_FAILED,
    FailureType.TIMEOUT,
    FailureType.SITE_ERROR,
    FailureType.LLM_ANALYSIS_FAILED,
    FailureType.LLM_ACTION_GENERATION_FAILED,
    FailureType.SELECTOR_RESOLUTION_FAILED,
    FailureType.UNKNOWN_POST_SUBMIT_STATE,
}

# Failures that require a human to take action before retry
MANUAL_INTERVENTION_FAILURES: set[FailureType] = {
    FailureType.LOGIN_REQUIRED,
    FailureType.CAPTCHA_OR_HUMAN_VERIFICATION,
    FailureType.HUMAN_VERIFICATION_BLOCKED,
    FailureType.AUTH_SESSION_EXPIRED,
    FailureType.MISSING_PROFILE,
    FailureType.MISSING_RESUME,
    FailureType.MISSING_CLAUDE,
    FailureType.UNRESOLVED_REQUIRED_FIELDS,
}


def make_failure(
    failure_type: FailureType,
    reason: str = "",
    debug_dir: str = "",
    attempts: int = 0,
    unresolved_fields: list | None = None,
) -> ApplyResult:
    """Convenience constructor for failure results."""
    return ApplyResult(
        success=False,
        failure_type=failure_type,
        failure_reason=reason or failure_type.value,
        retryable=failure_type in RETRYABLE_FAILURES,
        manual_intervention=failure_type in MANUAL_INTERVENTION_FAILURES,
        debug_dir=debug_dir,
        attempts=attempts,
        unresolved_fields=unresolved_fields or [],
    )


@dataclass
class ReadinessReport:
    """Structured result from check_readiness()."""
    ready: bool
    checks: dict  # {item_name: bool | None}
    first_failure: Optional[FailureType] = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "checks": self.checks,
            "first_failure": self.first_failure.value if self.first_failure else None,
            "error": self.error,
        }
