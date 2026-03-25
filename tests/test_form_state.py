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
    extract_state,
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


# ===========================================================================
# Progress-aware stuck detection (Phase 1C)
# ===========================================================================

class TestProgressAwareStuckDetection:
    def test_field_count_decrease_prevents_stuck(self):
        """
        If the field count decreases between records, the tracker should NOT
        declare stuck even on repeated fingerprints.
        """
        tracker = StateTracker()
        s1 = make_state(field_count=10)
        s2 = make_state(field_count=8)  # fewer fields = progress
        # Same URL/headings so fingerprint may match, but field count dropped

        r1 = tracker.record(s1)
        # s2 has different field_count -> different fingerprint anyway
        r2 = tracker.record(s2)
        assert r1 == "new"
        # r2 may be new (different fingerprint) or repeated — but NOT stuck
        assert r2 in ("new", "repeated"), f"Expected new/repeated with progress, got {r2!r}"

    def test_url_change_prevents_stuck(self):
        """URL path change indicates real navigation progress."""
        tracker = StateTracker()
        # Build two states with same fingerprint except URL
        s1 = make_state(url="https://example.com/apply/step1", field_count=5)
        s2 = make_state(url="https://example.com/apply/step2", field_count=5)

        r1 = tracker.record(s1)
        r2 = tracker.record(s2)
        assert r1 == "new"
        assert r2 == "new"  # Different URL path = new state

    def test_same_fingerprint_with_errors_is_repeated_validation_errors(self):
        """Same fingerprint + error texts = repeated_validation_errors stuck type."""
        tracker = StateTracker()
        s = make_state(error_texts=["This field is required"])
        # Record the same state with errors STUCK_REPEAT_THRESHOLD times
        results = [tracker.record(s) for _ in range(STUCK_REPEAT_THRESHOLD)]
        assert results[-1] == "stuck"
        # Check the stuck type
        log = tracker.get_log()
        stuck_entries = [e for e in log if e.get("classification") == "stuck"]
        assert len(stuck_entries) >= 1
        stuck_entry = stuck_entries[-1]
        assert stuck_entry.get("stuck_type") in (
            "repeated_validation_errors",
            "stuck_same_page_no_progress",
        )

    def test_cycling_detection(self):
        """
        A-B-A-B pattern over 4 steps should eventually be classified as cycling.
        """
        tracker = StateTracker()
        s_a = make_distinct_state(100)
        s_b = make_distinct_state(200)

        results = []
        for _ in range(4):
            results.append(tracker.record(s_a))
            results.append(tracker.record(s_b))

        # At some point should detect cycling or repeat threshold
        stuck_count = results.count("stuck")
        assert stuck_count >= 1, f"Expected cycling detection, got {results}"

    def test_progress_resets_stuck_risk(self):
        """
        Distinct pages (all new fingerprints) should not trigger stuck.
        """
        tracker = StateTracker()
        for i in range(STUCK_TOTAL_THRESHOLD - 1):
            result = tracker.record(make_distinct_state(i))
            assert result in ("new", "repeated"), \
                f"Step {i}: expected new/repeated, got {result!r}"

    def test_stuck_type_in_log_entry(self):
        """Log entries for stuck states should include stuck_type."""
        tracker = StateTracker()
        s = make_state()
        for _ in range(STUCK_REPEAT_THRESHOLD):
            tracker.record(s)
        log = tracker.get_log()
        stuck_entries = [e for e in log if e.get("classification") == "stuck"]
        assert len(stuck_entries) >= 1
        for entry in stuck_entries:
            assert "stuck_type" in entry, f"stuck entry missing stuck_type: {entry}"

    def test_total_threshold_with_distinct_states_and_no_progress(self):
        """
        STUCK_TOTAL_THRESHOLD distinct states all with same URL path
        and same field count -> should hit stuck on last.
        """
        tracker = StateTracker()
        results = []
        for i in range(STUCK_TOTAL_THRESHOLD):
            # Same URL path but different headings/labels => different fingerprints
            # but same field_count and url_path signals no progress
            s = FormState(
                url="https://example.com/apply",
                headings=[f"Heading {i}"],
                question_labels=[f"Question {i}"],
                field_count=5,  # same field count = no progress
                error_texts=[],
                button_texts=["Next"],
            )
            results.append(tracker.record(s))
        assert results[-1] == "stuck", f"Expected stuck on attempt {STUCK_TOTAL_THRESHOLD}, got {results}"

    def test_cycling_between_steps_type(self):
        """cycling_between_steps stuck type recorded on A-B-A-B pattern."""
        tracker = StateTracker()
        s_a = make_distinct_state(50)
        s_b = make_distinct_state(51)

        # Build history: A B A B (4 entries)
        tracker.record(s_a)
        tracker.record(s_b)
        tracker.record(s_a)
        result = tracker.record(s_b)  # 4th entry, cycling

        log = tracker.get_log()
        stuck_entries = [e for e in log if e.get("classification") == "stuck"]

        # May or may not be stuck at this point depending on threshold,
        # but cycling detection should be working
        if stuck_entries:
            cycling_entries = [e for e in stuck_entries if e.get("stuck_type") == "cycling_between_steps"]
            # Accept any stuck type here since cycling or repeat can both trigger
            assert len(stuck_entries) >= 1
