"""
tests/test_interaction.py – Tests for interaction primitives (pure Python parts).

Tests the option normalization, matching, synonym logic, and checkbox detection
without requiring Playwright.

Run with:
    python -m pytest tests/test_interaction.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.interaction import (
    find_best_option_match,
    _norm,
    _sim,
    _is_consent_checkbox,
    SYNONYM_THRESHOLD,
    _SYNONYMS,
)


class TestNorm:
    def test_lowercase(self):
        assert _norm("Hello World") == "hello world"

    def test_strips_accents(self):
        result = _norm("café")
        assert "cafe" in result.lower()

    def test_collapses_whitespace(self):
        assert _norm("  hello   world  ") == "hello world"

    def test_removes_punctuation(self):
        result = _norm("Hello, World!")
        assert "," not in result
        assert "!" not in result

    def test_empty_string(self):
        assert _norm("") == ""


class TestSim:
    def test_identical_strings(self):
        assert _sim("hello", "hello") == 1.0

    def test_completely_different(self):
        score = _sim("abc", "xyz")
        assert score < 0.5

    def test_empty_string_returns_zero(self):
        assert _sim("", "hello") == 0.0
        assert _sim("hello", "") == 0.0

    def test_similar_strings_high_score(self):
        score = _sim("united states", "united states of america")
        assert score > 0.6


class TestFindBestOptionMatch:
    """Tests for find_best_option_match() — pure Python, no Playwright."""

    def test_exact_match_case_insensitive(self):
        options = ["Yes", "No", "Maybe"]
        result = find_best_option_match("yes", options)
        assert result == "Yes"

    def test_exact_match_case_preserved(self):
        options = ["United States", "Canada", "Mexico"]
        result = find_best_option_match("United States", options)
        assert result == "United States"

    def test_substring_containment_target_in_option(self):
        options = ["I am authorized to work", "I am not authorized"]
        result = find_best_option_match("authorized", options)
        assert result is not None

    def test_substring_containment_option_in_target(self):
        options = ["Yes", "No"]
        result = find_best_option_match("Yes I agree", options)
        assert result == "Yes"

    def test_no_match_returns_none(self):
        options = ["Cat", "Dog", "Bird"]
        result = find_best_option_match("Automobile", options, threshold=0.9)
        assert result is None

    def test_empty_target_returns_none(self):
        assert find_best_option_match("", ["Yes", "No"]) is None

    def test_empty_options_returns_none(self):
        assert find_best_option_match("Yes", []) is None

    def test_synonym_match_united_states(self):
        options = ["United States", "Canada", "Mexico"]
        result = find_best_option_match("USA", options)
        assert result == "United States"

    def test_synonym_match_us(self):
        options = ["United States", "United Kingdom", "France"]
        result = find_best_option_match("us", options)
        assert result == "United States"

    def test_synonym_match_yes_y(self):
        options = ["Yes", "No"]
        result = find_best_option_match("Y", options)
        assert result == "Yes"

    def test_synonym_match_no_n(self):
        options = ["Yes", "No"]
        result = find_best_option_match("N", options)
        assert result == "No"

    def test_synonym_match_male_man(self):
        options = ["Man", "Woman", "Non-binary", "Prefer not to say"]
        result = find_best_option_match("Male", options)
        assert result == "Man"

    def test_synonym_match_male_he_him(self):
        options = ["He/Him", "She/Her", "They/Them"]
        result = find_best_option_match("Male", options)
        assert result == "He/Him"

    def test_synonym_master_degree(self):
        options = ["Bachelor's Degree", "Master's Degree", "PhD", "Other"]
        result = find_best_option_match("Master of Science", options)
        assert result == "Master's Degree"

    def test_synonym_immediately(self):
        options = ["Immediately", "2 weeks", "1 month"]
        result = find_best_option_match("ASAP", options)
        assert result == "Immediately"

    def test_similarity_fallback(self):
        options = ["Authorized to work", "Not authorized"]
        result = find_best_option_match("authorized work", options, threshold=0.5)
        assert result is not None

    def test_prefer_not_to_say_synonym(self):
        options = ["Prefer not to say", "Yes", "No"]
        result = find_best_option_match("Decline", options)
        assert result == "Prefer not to say"

    def test_threshold_respected(self):
        options = ["Completely unrelated option A", "Completely unrelated option B"]
        result = find_best_option_match("specific value", options, threshold=0.95)
        assert result is None


class TestComboboxValueMatching:
    """Test combobox-specific value matching scenarios."""

    def test_country_united_states(self):
        options = ["United States", "United Kingdom", "Canada", "Australia"]
        assert find_best_option_match("United States", options) == "United States"

    def test_country_us_abbreviation(self):
        options = ["United States", "United Kingdom", "Canada"]
        result = find_best_option_match("US", options)
        assert result == "United States"

    def test_gender_male_to_man(self):
        options = ["Man", "Woman", "Non-binary", "Decline to self-identify"]
        assert find_best_option_match("Male", options) == "Man"

    def test_gender_female_to_woman(self):
        options = ["Man", "Woman", "Non-binary", "Decline to self-identify"]
        assert find_best_option_match("Female", options) == "Woman"

    def test_yes_to_i_am_authorized(self):
        options = ["I am authorized", "I am not authorized"]
        result = find_best_option_match("Yes", options)
        # "Yes" should match "I am authorized" via synonym lookup
        assert result is not None

    def test_no_to_not_authorized(self):
        options = ["I am authorized", "I am not authorized"]
        result = find_best_option_match("No", options)
        assert result is not None


class TestRadioLabelMatching:
    """Test radio group label-to-element matching logic."""

    def test_exact_label_match(self):
        options = ["Yes", "No", "Maybe"]
        assert find_best_option_match("Yes", options) == "Yes"

    def test_partial_label_match(self):
        options = ["Yes, I am willing to relocate", "No, I am not willing"]
        result = find_best_option_match("Yes", options)
        assert result is not None
        assert "yes" in result.lower() or "willing" in result.lower()

    def test_not_a_protected_veteran(self):
        options = [
            "I am a protected veteran",
            "I am not a protected veteran",
            "I choose not to self-identify",
        ]
        result = find_best_option_match("I am not a protected veteran", options)
        assert result == "I am not a protected veteran"


class TestIsConsentCheckbox:
    """Tests for consent checkbox detection."""

    def test_i_agree_is_consent(self):
        assert _is_consent_checkbox("I agree to the terms and conditions") is True

    def test_i_consent_is_consent(self):
        assert _is_consent_checkbox("I consent to background check") is True

    def test_privacy_policy_is_consent(self):
        assert _is_consent_checkbox("I have read the privacy policy") is True

    def test_newsletter_is_not_consent(self):
        assert _is_consent_checkbox("Subscribe to newsletter") is False

    def test_marketing_is_not_consent(self):
        assert _is_consent_checkbox("Receive marketing communications") is False

    def test_product_updates_is_not_consent(self):
        assert _is_consent_checkbox("I want product updates") is False

    def test_eeoc_is_consent(self):
        assert _is_consent_checkbox("EEOC voluntary self-identification") is True

    def test_empty_label_is_not_consent(self):
        assert _is_consent_checkbox("") is False

    def test_acknowledge_is_consent(self):
        assert _is_consent_checkbox("I acknowledge the above") is True

    def test_job_alerts_is_not_consent(self):
        assert _is_consent_checkbox("Receive job alerts") is False


class TestSynonymTable:
    """Verify the synonym table contains expected entries."""

    def test_united_states_synonyms_exist(self):
        assert "united states" in _SYNONYMS
        syns = _SYNONYMS["united states"]
        assert "us" in syns
        assert "usa" in syns

    def test_yes_synonyms_exist(self):
        assert "yes" in _SYNONYMS
        syns = _SYNONYMS["yes"]
        assert "y" in syns
        assert "true" in syns

    def test_no_synonyms_exist(self):
        assert "no" in _SYNONYMS
        syns = _SYNONYMS["no"]
        assert "n" in syns
        assert "false" in syns

    def test_male_synonyms_exist(self):
        assert "male" in _SYNONYMS
        syns = _SYNONYMS["male"]
        assert "man" in syns

    def test_immediately_synonyms_exist(self):
        assert "immediately" in _SYNONYMS
        syns = _SYNONYMS["immediately"]
        assert "asap" in syns
