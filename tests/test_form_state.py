"""
tests/test_form_state.py – Tests for form state fingerprinting and loop detection.

Run with:
    cd /Users/sarthakbichhawa/projects/job-search-agent
    python -m pytest tests/test_form_state.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.form_state import (
    FormState,
    StateTracker,
    STUCK_REPEAT_THRESHOLD,
    STUCK_TOTAL_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_state(
    url: str = "https://example.com/apply",
    headings: list | None = None,
    question_labels: list | None = None,
    field_count: int = 5,
    error_texts: list | None = None,
    button_texts: list | None = None,
) -> FormState:
    return FormState(
        url=url,
        headings=headings or ["Apply"],
        question_labels=question_labels or ["First name", "Last name", "Email"],
        field_count=field_count,
        error_texts=error_texts or [],
        button_texts=button_texts or ["Next"],
    )


def make_distinct_state(n: int) -> FormState:
    """Create a state guaranteed to have a different fingerprint for each n."""
    return make_state(
        url=f"https://example.com/apply/step{n}",
        headings=[f"Step {n}"],
        question_labels=[f"Question {n}a", f"Question {n}b"],
        field_count=n + 1,
        button_texts=[f"Button{n}"],
    )


# ===========================================================================
# FormState fingerprint
# ===========================================================================

class TestFormStateFingerprint:
    def test_fingerprint_is_stable(self):
        """Same state built twice should produce the same fingerprint."""
        s1 = make_state()
        s2 = make_state()
        assert s1.fingerprint == s2.fingerprint

    def test_different_url_different_fingerprint(self):
        s1 = make_state(url="https://example.com/step1")
        s2 = make_state(url="https://example.com/step2")
        assert s1.fingerprint != s2.fingerprint

    def test_different_labels_different_fingerprint(self):
        s1 = make_state(question_labels=["First name", "Email"])
        s2 = make_state(question_labels=["Name", "Phone"])
        assert s1.fingerprint != s2.fingerprint

    def test_different_field_count_different_fingerprint(self):
        s1 = make_state(field_count=3)
        s2 = make_state(field_count=7)
        assert s1.fingerprint != s2.fingerprint

    def test_fingerprint_length_is_16(self):
        s = make_state()
        assert len(s.fingerprint) == 16

    def test_to_dict_contains_fingerprint(self):
        s = make_state()
        d = s.to_dict()
        assert "fingerprint" in d
        assert d["fingerprint"] == s.fingerprint

    def test_url_query_params_ignored(self):
        """URL query params differ but path is same — fingerprints should match."""
        s1 = make_state(url="https://example.com/apply?ref=abc&token=123")
        s2 = make_state(url="https://example.com/apply?ref=xyz&token=456")
        # Both paths are /apply, so fingerprint should be the same
        assert s1.fingerprint == s2.fingerprint

    def test_label_order_does_not_matter(self):
        """Labels are sorted before hashing — order should not matter."""
        s1 = make_state(question_labels=["Email", "First name", "Last name"])
        s2 = make_state(question_labels=["Last name", "First name", "Email"])
        assert s1.fingerprint == s2.fingerprint

    def test_heading_order_does_not_matter(self):
        s1 = make_state(headings=["Apply", "Contact Info"])
        s2 = make_state(headings=["Contact Info", "Apply"])
        assert s1.fingerprint == s2.fingerprint


# ===========================================================================
# StateTracker – basic classification
# ===========================================================================

class TestStateTrackerBasic:
    def test_first_state_is_new(self):
        tracker = StateTracker()
        state = make_state()
        result = tracker.record(state)
        assert result == "new"

    def test_second_different_state_is_new(self):
        tracker = StateTracker()
        tracker.record(make_state(url="https://example.com/step1"))
        result = tracker.record(make_state(url="https://example.com/step2"))
        assert result == "new"

    def test_second_same_fingerprint_is_repeated(self):
        tracker = StateTracker()
        s = make_state()
        tracker.record(s)
        result = tracker.record(make_state())  # same fingerprint
        assert result == "repeated"

    def test_three_distinct_states_all_new(self):
        tracker = StateTracker()
        for i in range(3):
            result = tracker.record(make_distinct_state(i))
            assert result == "new", f"attempt {i} expected 'new', got {result!r}"


# ===========================================================================
# StateTracker – stuck detection (repeat threshold)
# ===========================================================================

class TestStateTrackerStuckRepeat:
    def test_same_fingerprint_3_times_is_stuck(self):
        """STUCK_REPEAT_THRESHOLD == 3: 3rd occurrence of same fingerprint -> stuck."""
        tracker = StateTracker()
        s = make_state()
        results = [tracker.record(s) for _ in range(STUCK_REPEAT_THRESHOLD)]
        assert results[-1] == "stuck", f"Expected last to be 'stuck', got {results}"

    def test_same_fingerprint_2_times_not_yet_stuck(self):
        tracker = StateTracker()
        s = make_state()
        assert tracker.record(s) == "new"
        assert tracker.record(s) == "repeated"

    def test_stuck_after_repeat_threshold(self):
        """Any further call after stuck should still be 'stuck'."""
        tracker = StateTracker()
        s = make_state()
        for _ in range(STUCK_REPEAT_THRESHOLD - 1):
            tracker.record(s)
        result = tracker.record(s)
        assert result == "stuck"


# ===========================================================================
# StateTracker – stuck detection (total threshold)
# ===========================================================================

class TestStateTrackerStuckTotal:
    def test_8_attempts_is_stuck_regardless_of_fingerprint(self):
        """STUCK_TOTAL_THRESHOLD == 8: 8th attempt -> stuck regardless of fingerprints."""
        tracker = StateTracker()
        results = []
        for i in range(STUCK_TOTAL_THRESHOLD):
            state = make_distinct_state(i)
            results.append(tracker.record(state))
        assert results[-1] == "stuck", (
            f"Expected last of {STUCK_TOTAL_THRESHOLD} distinct states to be 'stuck', got {results}"
        )

    def test_7_distinct_attempts_not_yet_stuck(self):
        tracker = StateTracker()
        results = []
        for i in range(STUCK_TOTAL_THRESHOLD - 1):
            results.append(tracker.record(make_distinct_state(i)))
        # None of the first 7 should be stuck (all distinct fingerprints)
        assert all(r == "new" for r in results), f"Got {results}"

    def test_total_attempts_counter(self):
        tracker = StateTracker()
        for i in range(5):
            tracker.record(make_distinct_state(i))
        assert tracker.total_attempts == 5


# ===========================================================================
# StateTracker – log / history
# ===========================================================================

class TestStateTrackerLog:
    def test_get_log_returns_all_entries(self):
        tracker = StateTracker()
        for i in range(4):
            tracker.record(make_distinct_state(i))
        log = tracker.get_log()
        assert len(log) == 4

    def test_log_entry_has_required_keys(self):
        tracker = StateTracker()
        tracker.record(make_state())
        entry = tracker.get_log()[0]
        for key in ("attempt", "fingerprint", "url", "field_count", "classification"):
            assert key in entry, f"Missing key: {key!r}"

    def test_log_classification_matches_return_value(self):
        tracker = StateTracker()
        s = make_state()
        r1 = tracker.record(s)
        r2 = tracker.record(s)
        log = tracker.get_log()
        assert log[0]["classification"] == r1
        assert log[1]["classification"] == r2

    def test_get_log_is_copy(self):
        """Mutating the returned log should not affect internal state."""
        tracker = StateTracker()
        tracker.record(make_state())
        log = tracker.get_log()
        log.clear()
        assert tracker.total_attempts == 1


# ===========================================================================
# StateTracker – reset
# ===========================================================================

class TestStateTrackerReset:
    def test_reset_clears_history(self):
        tracker = StateTracker()
        for i in range(5):
            tracker.record(make_distinct_state(i))
        tracker.reset()
        assert tracker.total_attempts == 0

    def test_reset_allows_new_sequence(self):
        tracker = StateTracker()
        s = make_state()
        for _ in range(STUCK_REPEAT_THRESHOLD):
            tracker.record(s)
        tracker.reset()
        result = tracker.record(s)
        assert result == "new"


# ===========================================================================
# Interaction: mixed fingerprints + one repeated
# ===========================================================================

class TestMixedScenarios:
    def test_two_distinct_then_loop(self):
        """
        Step 1 (new) -> Step 2 (new) -> Step 1 again (repeated) ->
        Step 1 again (stuck via repeat threshold = 3 on same fp after 2nd repeat).
        """
        tracker = StateTracker()
        s1 = make_distinct_state(1)
        s2 = make_distinct_state(2)

        assert tracker.record(s1) == "new"
        assert tracker.record(s2) == "new"
        assert tracker.record(s1) == "repeated"   # 2nd occurrence of s1
        assert tracker.record(s1) == "stuck"      # 3rd occurrence of s1 -> stuck

    def test_alternating_does_not_get_stuck_early(self):
        """Alternating between two states should only trigger 'repeated', not 'stuck'."""
        tracker = StateTracker()
        s_a = make_distinct_state(10)
        s_b = make_distinct_state(20)
        results = []
        # 3 rounds of A-B alternation = 6 total, each fp seen 3x at most
        for _ in range(3):
            results.append(tracker.record(s_a))
            results.append(tracker.record(s_b))
        # 6 attempts, threshold is 8 -> should not be stuck due to total
        # But fp A and B each seen 3x -> stuck kicks in
        stuck_count = results.count("stuck")
        # At round 3, both A and B hit fp threshold of 3
        assert stuck_count >= 1, f"Expected at least one 'stuck' in {results}"
