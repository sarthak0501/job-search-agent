"""
tests/test_question_map.py – Tests for the canonical question mapping layer.

Run with:
    cd /Users/sarthakbichhawa/projects/job-search-agent
    python -m pytest tests/test_question_map.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.question_map import match_field, CATALOG

# ---------------------------------------------------------------------------
# Fixture: full profile
# ---------------------------------------------------------------------------

PROFILE = {
    "first_name": "Sarthak",
    "last_name": "Bichhawa",
    "preferred_name": "Sarthak",
    "email": "sarthaksgsits@gmail.com",
    "phone": "3128383536",
    "phone_country_code": "+1",
    "city": "Redmond",
    "state": "WA",
    "country": "United States",
    "location": "Redmond, WA",
    "address": "17766 90th St NE",
    "zip": "98052",
    "current_company": "Microsoft",
    "current_title": "Senior Data & Applied Scientist",
    "years_experience": "8",
    "linkedin": "https://www.linkedin.com/in/sarthak-bichhawa/",
    "salary_min": "150000",
    "salary_max": "250000",
    "salary_range": "$150,000 - $250,000",
    "work_authorized": "Yes",
    "work_authorized_us": "Yes",
    "requires_sponsorship": "Yes",
    "visa_status": "H-1B",
    "resume_path": "/tmp/resume.pdf",
    "education_degree": "Master of Science",
    "education_field": "Data Science",
    "education_school": "University of Illinois at Chicago",
    "education_year": "2018",
    "has_advanced_stem_degree": "Yes",
    "has_masters_3yr_or_phd_2yr": "Yes",
    "pronouns": "He/Him",
    "gender": "Male",
    "gender_eeoc": "Male",
    "transgender": "No",
    "orientation": "Heterosexual / Straight",
    "hispanic_latino": "No",
    "disability": "No",
    "veteran": "I am not a protected veteran",
    "ethnicity": "Asian",
    "race": "Asian",
    "referral_source": "Job board",
    "willing_to_relocate": "Yes",
    "start_date": "Immediately",
    "github": "",
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def match(label="", placeholder="", name="", id_="", aria="", options=None):
    """Thin wrapper that feeds profile and returns (key, value, conf)."""
    return match_field(
        label=label,
        placeholder=placeholder,
        name=name,
        id=id_,
        aria_label=aria,
        options=options or [],
        profile=PROFILE,
    )


def assert_maps_to(canonical_key: str, *args, options=None, **kwargs):
    key, value, conf = match(*args, options=options, **kwargs)
    assert key == canonical_key, (
        f"Expected canonical_key={canonical_key!r}, got {key!r} (conf={conf:.3f})"
    )
    assert conf >= 0.30, f"Confidence too low: {conf:.3f} for key={key!r}"
    return key, value, conf


# ===========================================================================
# Work Authorization
# ===========================================================================

class TestWorkAuthorization:
    def test_are_you_legally_authorized(self):
        assert_maps_to("work_authorization",
                        label="Are you legally authorized to work in the United States?",
                        options=["Yes", "No"])

    def test_authorized_to_work_in_us(self):
        assert_maps_to("work_authorization",
                        label="Authorized to work in US",
                        options=["Yes", "No"])

    def test_can_you_work_in_united_states(self):
        assert_maps_to("work_authorization",
                        label="Can you work in the United States without restriction?",
                        options=["Yes", "No"])

    def test_right_to_work(self):
        assert_maps_to("work_authorization",
                        label="Do you have the right to work in the US?",
                        options=["Yes", "No"])

    def test_eligible_to_work(self):
        assert_maps_to("work_authorization",
                        label="Are you eligible to work in the US?",
                        options=["Yes", "No"])

    def test_profile_value_yes(self):
        _, value, _ = match(
            label="Are you legally authorized to work in the United States?",
            options=["Yes", "No"],
        )
        assert value == "Yes"

    def test_option_mapping_i_am_authorized(self):
        _, value, _ = match(
            label="Are you legally authorized to work in the United States?",
            options=["I am authorized", "I am not authorized"],
        )
        assert "authorized" in value.lower()


# ===========================================================================
# Sponsorship
# ===========================================================================

class TestSponsorship:
    def test_will_you_require_sponsorship(self):
        assert_maps_to("sponsorship",
                        label="Will you require sponsorship?",
                        options=["Yes", "No"])

    def test_do_you_need_visa_sponsorship(self):
        assert_maps_to("sponsorship",
                        label="Do you need visa sponsorship?",
                        options=["Yes", "No"])

    def test_immigration_sponsorship(self):
        assert_maps_to("sponsorship",
                        label="Will you need immigration sponsorship?",
                        options=["Yes", "No"])

    def test_require_sponsorship_name_attr(self):
        assert_maps_to("sponsorship",
                        name="requires_sponsorship",
                        options=["Yes", "No"])

    def test_profile_value_yes(self):
        _, value, _ = match(
            label="Will you require sponsorship?",
            options=["Yes", "No"],
        )
        assert value == "Yes"

    def test_not_confused_with_work_auth(self):
        # Sponsorship question should NOT map to work_authorization
        key, _, _ = match(label="Will you require visa sponsorship?")
        assert key == "sponsorship", f"Got {key!r} instead of 'sponsorship'"


# ===========================================================================
# Gender
# ===========================================================================

class TestGender:
    def test_gender_identity(self):
        assert_maps_to("gender", label="Gender identity")

    def test_sex_label(self):
        assert_maps_to("gender", label="Sex")

    def test_what_gender_do_you_identify_as(self):
        assert_maps_to("gender", label="What gender do you identify as?")

    def test_option_mapping_man(self):
        _, value, _ = match(
            label="Gender identity",
            options=["Man", "Woman", "Non-binary", "Prefer not to say"],
        )
        assert value == "Man"

    def test_option_mapping_he_him(self):
        _, value, _ = match(
            label="Gender",
            options=["He/Him", "She/Her", "They/Them", "Prefer not to say"],
        )
        assert value == "He/Him"

    def test_option_mapping_male(self):
        _, value, _ = match(
            label="Sex",
            options=["Male", "Female", "Other", "Prefer not to say"],
        )
        assert value == "Male"

    def test_not_transgender(self):
        key, _, _ = match(label="Do you identify as transgender?")
        assert key == "transgender", f"Got {key!r}"


# ===========================================================================
# Salary
# ===========================================================================

class TestSalary:
    def test_desired_compensation(self):
        assert_maps_to("salary_expected", label="Desired compensation")

    def test_expected_salary(self):
        assert_maps_to("salary_expected", label="Expected salary")

    def test_what_is_your_salary_expectation(self):
        assert_maps_to("salary_expected", label="What is your salary expectation?")

    def test_salary_expectation_name(self):
        assert_maps_to("salary_expected", name="salary_expectation")

    def test_profile_value_range(self):
        _, value, _ = match(label="Expected salary")
        assert "150" in value or "250" in value or "$" in value

    def test_minimum_salary(self):
        key, _, _ = match(label="Minimum salary expectation")
        assert key == "salary_min", f"Got {key!r}"

    def test_maximum_salary(self):
        key, _, _ = match(label="Maximum salary expectation")
        assert key == "salary_max", f"Got {key!r}"


# ===========================================================================
# EEOC Fields
# ===========================================================================

class TestEEOC:
    def test_race(self):
        assert_maps_to("race", label="Race")

    def test_race_what_is_your(self):
        assert_maps_to("race", label="What is your race?")

    def test_race_option_asian(self):
        _, value, _ = match(
            label="Race",
            options=["White", "Black or African American", "Asian", "Two or more races", "Decline to self-identify"],
        )
        assert value == "Asian"

    def test_ethnicity(self):
        assert_maps_to("ethnicity", label="Ethnicity")

    def test_hispanic_label(self):
        key, _, _ = match(label="Are you of Hispanic or Latino origin?")
        assert key in ("hispanic_latino", "ethnicity"), f"Got {key!r}"

    def test_disability(self):
        assert_maps_to("disability", label="Do you have a disability?")

    def test_disability_option_no(self):
        _, value, _ = match(
            label="Do you have a disability?",
            options=["Yes, I have a disability", "No, I don't have a disability", "Prefer not to say"],
        )
        assert "no" in value.lower() or "don't" in value.lower() or "not" in value.lower()

    def test_veteran(self):
        assert_maps_to("veteran", label="Veteran status")

    def test_protected_veteran(self):
        assert_maps_to("veteran", label="Are you a protected veteran?")

    def test_veteran_not_confused_with_disability(self):
        key, _, _ = match(label="Veteran or active duty military?")
        assert key == "veteran", f"Got {key!r}"

    def test_veteran_option_not_protected(self):
        _, value, _ = match(
            label="Veteran status",
            options=["I am a protected veteran", "I am not a protected veteran", "I choose not to self-identify"],
        )
        assert "not a protected veteran" in value.lower()


# ===========================================================================
# LinkedIn
# ===========================================================================

class TestLinkedIn:
    def test_linkedin_url(self):
        assert_maps_to("linkedin", label="LinkedIn URL")

    def test_linkedin_profile(self):
        assert_maps_to("linkedin", label="LinkedIn profile")

    def test_your_linkedin(self):
        assert_maps_to("linkedin", label="Your LinkedIn")

    def test_linkedin_name_attr(self):
        assert_maps_to("linkedin", name="linkedin_url")

    def test_linkedin_value(self):
        _, value, _ = match(label="LinkedIn URL")
        assert "linkedin.com" in value


# ===========================================================================
# Location
# ===========================================================================

class TestLocation:
    def test_city_label(self):
        assert_maps_to("city", label="City")

    def test_where_are_you_located(self):
        key, _, _ = match(label="Where are you located?")
        assert key in ("city", "location"), f"Got {key!r}"

    def test_current_location(self):
        assert_maps_to("location", label="Current location")

    def test_city_value(self):
        _, value, _ = match(label="City")
        assert value == "Redmond"

    def test_state_label(self):
        assert_maps_to("state", label="State")

    def test_zip_label(self):
        assert_maps_to("zip", label="Zip code")


# ===========================================================================
# Name Fields
# ===========================================================================

class TestNames:
    def test_first_name(self):
        assert_maps_to("first_name", label="First Name")

    def test_given_name(self):
        assert_maps_to("first_name", label="Given Name")

    def test_first_name_value(self):
        _, value, _ = match(label="First Name")
        assert value == "Sarthak"

    def test_last_name(self):
        assert_maps_to("last_name", label="Last Name")

    def test_surname(self):
        assert_maps_to("last_name", label="Surname")

    def test_last_name_value(self):
        _, value, _ = match(label="Last Name")
        assert value == "Bichhawa"

    def test_preferred_name(self):
        assert_maps_to("preferred_name", label="Preferred name")

    def test_preferred_name_goes_by(self):
        assert_maps_to("preferred_name", label="Name you go by")

    def test_first_not_last(self):
        key_first, _, _ = match(label="First Name")
        key_last, _, _ = match(label="Last Name")
        assert key_first == "first_name"
        assert key_last == "last_name"
        assert key_first != key_last


# ===========================================================================
# Email & Phone
# ===========================================================================

class TestContact:
    def test_email(self):
        assert_maps_to("email", label="Email address")

    def test_email_value(self):
        _, value, _ = match(label="Email")
        assert "@" in value

    def test_phone(self):
        assert_maps_to("phone", label="Phone number")

    def test_phone_value(self):
        _, value, _ = match(label="Phone")
        assert len(value) >= 10


# ===========================================================================
# Education
# ===========================================================================

class TestEducation:
    def test_highest_degree(self):
        assert_maps_to("education_degree", label="Highest level of education")

    def test_degree_label(self):
        key, _, _ = match(label="Degree")
        assert key == "education_degree", f"Got {key!r}"

    def test_school(self):
        key, _, _ = match(label="University or College")
        assert key == "education_school", f"Got {key!r}"

    def test_field_of_study(self):
        assert_maps_to("education_field", label="Field of study")

    def test_major(self):
        assert_maps_to("education_field", label="Major")

    def test_graduation_year(self):
        assert_maps_to("education_year", label="Graduation year")


# ===========================================================================
# Work History
# ===========================================================================

class TestWorkHistory:
    def test_current_company(self):
        assert_maps_to("current_company", label="Current company")

    def test_current_employer(self):
        assert_maps_to("current_company", label="Current employer")

    def test_current_company_value(self):
        _, value, _ = match(label="Current company")
        assert value == "Microsoft"

    def test_current_title(self):
        assert_maps_to("current_title", label="Current job title")

    def test_years_experience(self):
        assert_maps_to("years_experience", label="Years of experience")

    def test_years_experience_value(self):
        _, value, _ = match(label="Years of experience")
        assert value == "8"


# ===========================================================================
# Willing to Relocate
# ===========================================================================

class TestRelocation:
    def test_willing_to_relocate(self):
        assert_maps_to("willing_to_relocate", label="Are you willing to relocate?")

    def test_open_to_relocation(self):
        key, _, _ = match(label="Open to relocation?")
        assert key == "willing_to_relocate", f"Got {key!r}"

    def test_relocation_value(self):
        _, value, _ = match(label="Willing to relocate?", options=["Yes", "No"])
        assert value == "Yes"


# ===========================================================================
# Referral Source
# ===========================================================================

class TestReferral:
    def test_how_did_you_hear(self):
        assert_maps_to("referral_source", label="How did you hear about us?")

    def test_how_did_you_find_job(self):
        assert_maps_to("referral_source", label="How did you find this job?")


# ===========================================================================
# CATALOG integrity checks
# ===========================================================================

class TestCatalog:
    def test_all_entries_have_profile_key(self):
        for key, data in CATALOG.items():
            assert "profile_key" in data, f"CATALOG[{key!r}] missing 'profile_key'"

    def test_all_entries_have_type(self):
        for key, data in CATALOG.items():
            assert "type" in data, f"CATALOG[{key!r}] missing 'type'"

    def test_all_entries_have_keywords(self):
        for key, data in CATALOG.items():
            assert "keywords" in data, f"CATALOG[{key!r}] missing 'keywords'"

    def test_all_entries_have_aliases(self):
        for key, data in CATALOG.items():
            assert "aliases" in data, f"CATALOG[{key!r}] missing 'aliases'"

    def test_minimum_catalog_size(self):
        assert len(CATALOG) >= 20, f"CATALOG only has {len(CATALOG)} entries"


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:
    def test_empty_label_returns_empty(self):
        key, value, conf = match_field(profile=PROFILE)
        assert conf < 0.30 or key == ""

    def test_gibberish_returns_low_confidence(self):
        key, value, conf = match(label="xyzzy frobble wump")
        assert conf < 0.40

    def test_no_profile_returns_empty_value(self):
        key, value, conf = match_field(
            label="First name", profile={}
        )
        assert key == "first_name"
        assert value == ""

    def test_options_mapping_fallback_to_decline(self):
        # If profile value doesn't match any option, should try decline/prefer not to say
        _, value, _ = match(
            label="Gender identity",
            options=["Man", "Woman", "Non-binary", "Prefer not to say"],
        )
        # "Male" profile -> "Man" form option
        assert value in ("Man", "Male", "Prefer not to say")
