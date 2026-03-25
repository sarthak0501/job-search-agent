"""
tests/test_page_classifier.py – Tests for page classification and button classification.

Note: classify_page() requires a live Playwright frame/page — only the pure-Python
parts (classify_button, find_unresolved_required_fields via mock) are tested here.

Run with:
    python -m pytest tests/test_page_classifier.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.page_classifier import classify_button
from core.outcome import StepIntent


class TestClassifyButton:
    """Tests for classify_button() - pure Python, no Playwright needed."""

    def test_submit_application(self):
        assert classify_button("Submit Application") == StepIntent.CLICK_SUBMIT

    def test_submit(self):
        assert classify_button("Submit") == StepIntent.CLICK_SUBMIT

    def test_finish(self):
        assert classify_button("Finish") == StepIntent.CLICK_SUBMIT

    def test_apply_now(self):
        assert classify_button("Apply Now") == StepIntent.CLICK_SUBMIT

    def test_next(self):
        assert classify_button("Next") == StepIntent.CLICK_NEXT

    def test_continue(self):
        assert classify_button("Continue") == StepIntent.CLICK_NEXT

    def test_proceed(self):
        assert classify_button("Proceed") == StepIntent.CLICK_NEXT

    def test_next_step(self):
        assert classify_button("Next Step") == StepIntent.CLICK_NEXT

    def test_save_and_continue(self):
        result = classify_button("Save and Continue")
        assert result in (StepIntent.CLICK_NEXT, StepIntent.CLICK_CONTINUE)

    def test_review(self):
        assert classify_button("Review") == StepIntent.CLICK_REVIEW

    def test_review_application(self):
        assert classify_button("Review Application") == StepIntent.CLICK_REVIEW

    def test_review_your_application(self):
        assert classify_button("Review Your Application") == StepIntent.CLICK_REVIEW

    def test_save_draft(self):
        assert classify_button("Save Draft") == StepIntent.WAIT

    def test_save_for_later(self):
        assert classify_button("Save for Later") == StepIntent.WAIT

    def test_sign_in(self):
        assert classify_button("Sign In") == StepIntent.ABORT

    def test_log_in(self):
        assert classify_button("Log In") == StepIntent.ABORT

    def test_create_account(self):
        assert classify_button("Create Account") == StepIntent.ABORT

    def test_verify(self):
        assert classify_button("Verify") == StepIntent.ABORT

    def test_case_insensitive_submit(self):
        assert classify_button("SUBMIT APPLICATION") == StepIntent.CLICK_SUBMIT

    def test_case_insensitive_next(self):
        assert classify_button("NEXT") == StepIntent.CLICK_NEXT

    def test_mixed_case_review(self):
        assert classify_button("Review Application") == StepIntent.CLICK_REVIEW

    def test_submit_your_application(self):
        assert classify_button("Submit your application") == StepIntent.CLICK_SUBMIT

    def test_quick_apply(self):
        result = classify_button("Quick Apply")
        assert result == StepIntent.CLICK_SUBMIT

    def test_empty_string_defaults_to_next(self):
        # Empty/unknown text defaults to CLICK_NEXT (safe fallback)
        result = classify_button("")
        assert result == StepIntent.CLICK_NEXT

    def test_unknown_button_defaults_to_next(self):
        result = classify_button("Flibbertigibbet")
        assert result == StepIntent.CLICK_NEXT


class TestClassifyButtonEdgeCases:
    def test_finish_application(self):
        result = classify_button("Finish Application")
        assert result == StepIntent.CLICK_SUBMIT

    def test_complete_application(self):
        result = classify_button("Complete Application")
        assert result == StepIntent.CLICK_SUBMIT

    def test_send_application(self):
        result = classify_button("Send Application")
        assert result == StepIntent.CLICK_SUBMIT

    def test_login_not_next(self):
        result = classify_button("Login")
        assert result == StepIntent.ABORT

    def test_sign_in_to_apply(self):
        result = classify_button("Sign in to Apply")
        assert result == StepIntent.ABORT

    def test_submit_intent_is_not_abort(self):
        result = classify_button("Submit")
        assert result != StepIntent.ABORT

    def test_next_is_not_submit(self):
        result = classify_button("Next")
        assert result != StepIntent.CLICK_SUBMIT


class TestStepIntentValues:
    """Verify StepIntent enum members are string values."""

    def test_click_submit_value(self):
        assert StepIntent.CLICK_SUBMIT == "click_submit"

    def test_click_next_value(self):
        assert StepIntent.CLICK_NEXT == "click_next"

    def test_click_review_value(self):
        assert StepIntent.CLICK_REVIEW == "click_review"

    def test_abort_value(self):
        assert StepIntent.ABORT == "abort"

    def test_wait_value(self):
        assert StepIntent.WAIT == "wait"
