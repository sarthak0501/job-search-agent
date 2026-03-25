from __future__ import annotations
"""
question_map.py – Canonical question mapping layer.

Maps arbitrary form field labels / placeholders / names to a canonical key
and a profile value without any external dependencies beyond stdlib.

Usage:
    from core.question_map import match_field

    canonical_key, profile_value, confidence = match_field(
        label="Are you legally authorized to work in the United States?",
        placeholder="",
        name="authorized",
        id="authorized",
        aria_label="",
        options=["Yes", "No"],
        profile=profile_dict,
    )
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Lower-case, strip accents, collapse whitespace, remove punctuation."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sim(a: str, b: str) -> float:
    """Sequence similarity 0-1."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _keyword_score(text: str, keywords: list[str]) -> float:
    """Fraction of keywords present in text (normalised)."""
    if not keywords:
        return 0.0
    hits = sum(1 for kw in keywords if kw in text)
    return hits / len(keywords)


def _best_option_match(profile_value: str, options: list[str],
                       options_map: dict[str, list[str]]) -> Optional[str]:
    """
    Given a profile_value and a list of form options, return the best
    matching option text, or None if no match found.

    options_map maps profile_value variants -> list of possible form option texts.
    """
    if not options:
        return None

    pv_norm = _norm(profile_value)

    # 1. Check options_map first (highest priority)
    # options_map is keyed by canonical profile value aliases
    for pv_alias, form_candidates in options_map.items():
        if _norm(pv_alias) == pv_norm or pv_norm in _norm(pv_alias) or _norm(pv_alias) in pv_norm:
            for candidate in form_candidates:
                cand_norm = _norm(candidate)
                for opt in options:
                    opt_norm = _norm(opt)
                    if opt_norm == cand_norm or cand_norm in opt_norm or opt_norm in cand_norm:
                        return opt

    # 2. Direct / partial match against option texts
    opts_norm = [(_norm(o), o) for o in options]

    # exact
    for on, o in opts_norm:
        if on == pv_norm:
            return o

    # profile value contained in option
    for on, o in opts_norm:
        if pv_norm and pv_norm in on:
            return o

    # option contained in profile value
    for on, o in opts_norm:
        if on and on in pv_norm:
            return o

    # similarity fallback (>0.6)
    best_score = 0.0
    best_opt = None
    for on, o in opts_norm:
        s = _sim(pv_norm, on)
        if s > best_score:
            best_score = s
            best_opt = o
    if best_score >= 0.6:
        return best_opt

    return None


# ---------------------------------------------------------------------------
# The CATALOG
# ---------------------------------------------------------------------------
# Each entry:
#   canonical_key -> {
#       "profile_key": str,          # key in profile dict
#       "type": str,                 # fill | select | combobox | check | radio | upload
#       "keywords": list[str],       # normalised tokens that strongly suggest this field
#       "aliases": list[str],        # full normalised phrase aliases
#       "negative_keywords": list,   # if present, reduces score
#       "options_map": dict,         # profile_value -> [possible form option texts]
#   }

CATALOG: dict[str, dict] = {

    # ── Identity ───────────────────────────────────────────────────────────────
    "first_name": {
        "profile_key": "first_name",
        "type": "fill",
        "keywords": ["first", "given", "fname"],
        "aliases": [
            "first name", "given name", "firstname", "fname",
            "legal first name", "applicant first name",
        ],
        "negative_keywords": ["last", "middle", "family", "preferred"],
        "options_map": {},
    },

    "last_name": {
        "profile_key": "last_name",
        "type": "fill",
        "keywords": ["last", "surname", "family", "lname"],
        "aliases": [
            "last name", "surname", "family name", "lastname", "lname",
            "applicant last name", "legal last name",
        ],
        "negative_keywords": ["first", "given", "preferred"],
        "options_map": {},
    },

    "preferred_name": {
        "profile_key": "preferred_name",
        "type": "fill",
        "keywords": ["preferred", "goes by", "nickname"],
        "aliases": [
            "preferred name", "preferred first name", "goes by", "nickname",
            "name you go by", "what should we call you",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    "email": {
        "profile_key": "email",
        "type": "fill",
        "keywords": ["email", "e-mail", "email address"],
        "aliases": [
            "email", "email address", "e-mail", "e-mail address",
            "your email", "contact email", "work email",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    "phone": {
        "profile_key": "phone",
        "type": "fill",
        "keywords": ["phone", "mobile", "cell", "telephone", "contact number"],
        "aliases": [
            "phone", "phone number", "mobile number", "cell phone",
            "telephone", "contact number", "mobile phone",
        ],
        "negative_keywords": ["country", "code", "extension"],
        "options_map": {},
    },

    # ── Location ───────────────────────────────────────────────────────────────
    "city": {
        "profile_key": "city",
        "type": "fill",
        "keywords": ["city"],
        "aliases": [
            "city", "city of residence", "current city", "town",
            "where are you located", "location city",
        ],
        "negative_keywords": ["state", "country", "zip", "postal", "address"],
        "options_map": {},
    },

    "state": {
        "profile_key": "state",
        "type": "fill",
        "keywords": ["state", "province", "region"],
        "aliases": [
            "state", "state of residence", "province", "region",
            "state province", "us state",
        ],
        "negative_keywords": ["country", "zip", "postal", "united states", "authorized", "work", "eligible"],
        "options_map": {},
    },

    "zip": {
        "profile_key": "zip",
        "type": "fill",
        "keywords": ["zip", "postal", "postcode"],
        "aliases": [
            "zip", "zip code", "postal code", "postcode", "zip postal code",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    "country": {
        "profile_key": "country",
        "type": "fill",
        "keywords": ["country"],
        "aliases": [
            "country", "country of residence", "country of citizenship",
            "nation", "current country",
        ],
        "negative_keywords": ["code", "state", "city"],
        "options_map": {},
    },

    "location": {
        "profile_key": "location",
        "type": "fill",
        "keywords": ["location", "current location", "where are you based"],
        "aliases": [
            "location", "current location", "your location",
            "where are you located", "where are you based",
            "city state", "city and state",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    "address": {
        "profile_key": "address",
        "type": "fill",
        "keywords": ["address", "street", "mailing"],
        "aliases": [
            "address", "street address", "mailing address", "home address",
            "residential address",
        ],
        "negative_keywords": ["city", "state", "zip", "country"],
        "options_map": {},
    },

    "willing_to_relocate": {
        "profile_key": "willing_to_relocate",
        "type": "radio",
        "keywords": ["relocate", "relocation", "willing to move"],
        "aliases": [
            "willing to relocate", "open to relocation", "able to relocate",
            "would you relocate", "are you willing to relocate",
            "relocation", "open to moving",
        ],
        "negative_keywords": [],
        "options_map": {
            "Yes": ["Yes", "I am willing", "Willing", "Open to relocation", "Yes, I am willing"],
            "No": ["No", "Not willing", "No, I am not willing"],
        },
    },

    # ── Work ───────────────────────────────────────────────────────────────────
    "current_company": {
        "profile_key": "current_company",
        "type": "fill",
        "keywords": ["current company", "current employer", "employer"],
        "aliases": [
            "current company", "current employer", "employer", "company name",
            "where do you currently work", "current organization",
            "most recent employer", "current workplace",
        ],
        "negative_keywords": ["title", "role", "position"],
        "options_map": {},
    },

    "current_title": {
        "profile_key": "current_title",
        "type": "fill",
        "keywords": ["current title", "current role", "job title", "position"],
        "aliases": [
            "current title", "job title", "current job title", "current role",
            "current position", "most recent title", "your title",
        ],
        "negative_keywords": ["company", "employer"],
        "options_map": {},
    },

    "years_experience": {
        "profile_key": "years_experience",
        "type": "fill",
        "keywords": ["years", "experience", "years of experience"],
        "aliases": [
            "years of experience", "years experience", "how many years",
            "total years", "total experience", "years of relevant experience",
            "years of professional experience",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    "start_date": {
        "profile_key": "start_date",
        "type": "fill",
        "keywords": ["start date", "availability", "available", "when can you start"],
        "aliases": [
            "start date", "available start date", "earliest start date",
            "when can you start", "availability date", "when are you available",
            "desired start date",
        ],
        "negative_keywords": [],
        "options_map": {
            "Immediately": ["Immediately", "ASAP", "As soon as possible", "Right away", "Now"],
        },
    },

    # ── Social / Links ─────────────────────────────────────────────────────────
    "linkedin": {
        "profile_key": "linkedin",
        "type": "fill",
        "keywords": ["linkedin", "linkedin url", "linkedin profile"],
        "aliases": [
            "linkedin", "linkedin url", "linkedin profile", "your linkedin",
            "linkedin profile url", "linkedin link",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    "github": {
        "profile_key": "github",
        "type": "fill",
        "keywords": ["github", "github url", "github profile"],
        "aliases": [
            "github", "github url", "github profile", "your github",
            "github profile url", "github link", "portfolio url",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    "website": {
        "profile_key": "github",
        "type": "fill",
        "keywords": ["website", "portfolio", "personal website"],
        "aliases": [
            "website", "portfolio", "personal website", "personal site",
            "portfolio url", "personal url",
        ],
        "negative_keywords": ["linkedin", "github"],
        "options_map": {},
    },

    # ── Salary ─────────────────────────────────────────────────────────────────
    "salary_expected": {
        "profile_key": "salary_range",
        "type": "fill",
        "keywords": ["salary", "compensation", "pay", "expected", "desired"],
        "aliases": [
            "expected salary", "desired salary", "salary expectation",
            "salary expectations", "desired compensation", "expected compensation",
            "what is your salary expectation", "compensation expectation",
            "target salary", "base salary expectation",
        ],
        "negative_keywords": ["min", "max", "minimum", "maximum", "range"],
        "options_map": {},
    },

    "salary_min": {
        "profile_key": "salary_min",
        "type": "fill",
        "keywords": ["minimum", "min salary", "floor"],
        "aliases": [
            "minimum salary", "salary minimum", "minimum compensation",
            "minimum pay", "salary floor",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    "salary_max": {
        "profile_key": "salary_max",
        "type": "fill",
        "keywords": ["maximum", "max salary", "ceiling"],
        "aliases": [
            "maximum salary", "salary maximum", "maximum compensation",
            "salary ceiling",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    # ── Work Authorization ─────────────────────────────────────────────────────
    "work_authorization": {
        "profile_key": "work_authorized",
        "type": "radio",
        "keywords": ["authorized", "authorization", "legally", "eligible to work"],
        "aliases": [
            "are you legally authorized to work",
            "authorized to work in the us",
            "authorized to work in the united states",
            "work authorization",
            "can you work in the united states",
            "can you work in the us",
            "can you legally work in the united states",
            "are you eligible to work",
            "eligible to work in us",
            "do you have the right to work",
            "right to work in the united states",
            "legally authorized",
            "are you authorized",
            "work without restriction",
            "work in the united states without restriction",
        ],
        "negative_keywords": ["sponsorship", "visa", "sponsor"],
        "options_map": {
            "Yes": [
                "Yes", "I am authorized", "I am legally authorized",
                "Yes, I am authorized to work", "Yes I am authorized",
                "Authorized", "Yes, legally authorized",
            ],
            "No": ["No", "I am not authorized", "No, I am not authorized"],
        },
    },

    "sponsorship": {
        "profile_key": "requires_sponsorship",
        "type": "radio",
        "keywords": ["sponsorship", "sponsor", "visa sponsorship", "immigration"],
        "aliases": [
            "will you require sponsorship",
            "do you need visa sponsorship",
            "sponsorship required",
            "do you require sponsorship",
            "visa sponsorship",
            "immigration sponsorship",
            "require sponsorship",
            "will you need sponsorship",
            "will you need work authorization sponsorship",
            "require work authorization sponsorship",
            "do you need employment authorization sponsorship",
        ],
        "negative_keywords": ["authorized", "authorization", "legally"],
        "options_map": {
            "Yes": [
                "Yes", "Yes I will", "I will require sponsorship",
                "Yes, I require sponsorship", "Required", "Yes, will need sponsorship",
            ],
            "No": [
                "No", "No I will not", "I will not require sponsorship",
                "Not required", "No, I do not require",
            ],
        },
    },

    "visa_status": {
        "profile_key": "visa_status",
        "type": "fill",
        "keywords": ["visa", "visa status", "immigration status", "work visa"],
        "aliases": [
            "visa status", "visa type", "current visa status", "work visa",
            "immigration status", "visa category",
        ],
        "negative_keywords": [],
        "options_map": {
            "H-1B": ["H-1B", "H1B", "H-1B Visa", "H1B Visa"],
        },
    },

    # ── Education ──────────────────────────────────────────────────────────────
    "education_degree": {
        "profile_key": "education_degree",
        "type": "fill",
        "keywords": ["degree", "highest degree", "education level", "highest education"],
        "aliases": [
            "highest degree", "education level", "highest level of education",
            "degree", "degree level", "highest education level",
            "educational attainment", "highest qualification",
        ],
        "negative_keywords": ["school", "institution", "university", "year", "major", "field"],
        "options_map": {
            "Master of Science": [
                "Master of Science", "Master's", "Masters", "MS", "M.S.", "M.S",
                "Master's Degree", "Masters Degree", "Graduate Degree",
            ],
        },
    },

    "education_school": {
        "profile_key": "education_school",
        "type": "fill",
        "keywords": ["school", "university", "college", "institution"],
        "aliases": [
            "school", "university", "college", "institution", "alma mater",
            "educational institution", "where did you go to school",
            "school name", "university name",
        ],
        "negative_keywords": ["degree", "major", "year", "gpa"],
        "options_map": {},
    },

    "education_field": {
        "profile_key": "education_field",
        "type": "fill",
        "keywords": ["major", "field of study", "concentration", "program"],
        "aliases": [
            "major", "field of study", "area of study", "concentration",
            "program", "degree field", "subject",
        ],
        "negative_keywords": ["degree", "school", "university", "year"],
        "options_map": {},
    },

    "education_year": {
        "profile_key": "education_year",
        "type": "fill",
        "keywords": ["graduation year", "year graduated", "year of graduation"],
        "aliases": [
            "graduation year", "year graduated", "year of graduation",
            "when did you graduate", "graduation date", "degree year",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    # ── EEOC / Diversity ───────────────────────────────────────────────────────
    "gender": {
        "profile_key": "gender",
        "type": "radio",
        "keywords": ["gender", "sex"],
        "aliases": [
            "gender", "gender identity", "sex", "what gender do you identify as",
            "gender identification", "what is your gender",
        ],
        "negative_keywords": ["transgender", "orientation", "pronoun"],
        "options_map": {
            "Male": [
                "Male", "Man", "He/Him", "He / Him", "M", "Men", "Boy",
                "He", "Him",
            ],
            "Female": [
                "Female", "Woman", "She/Her", "She / Her", "F", "Women",
            ],
            "Prefer not to say": [
                "Prefer not to say", "Decline to self-identify",
                "Prefer not to disclose", "I prefer not to say",
                "Decline", "I choose not to disclose",
            ],
        },
    },

    "race": {
        "profile_key": "race",
        "type": "radio",
        "keywords": ["race"],
        "aliases": [
            "race", "racial identity", "race or ethnicity",
            "what is your race",
        ],
        "negative_keywords": ["veteran", "disability"],
        "options_map": {
            "Asian": [
                "Asian", "Asian or Pacific Islander",
                "Asian / Pacific Islander", "Asian American",
                "Asian (not Hispanic or Latino)",
            ],
            "Prefer not to say": [
                "Prefer not to say", "Decline to self-identify",
                "I prefer not to identify", "Decline",
            ],
        },
    },

    "ethnicity": {
        "profile_key": "ethnicity",
        "type": "radio",
        "keywords": ["ethnicity", "ethnic"],
        "aliases": [
            "ethnicity", "ethnic background", "ethnic identity",
            "hispanic or latino", "ethnic group",
        ],
        "negative_keywords": ["race", "veteran", "disability"],
        "options_map": {
            "Asian": [
                "Asian", "Asian or Pacific Islander",
                "Not Hispanic or Latino",
            ],
            "No": [
                "Not Hispanic or Latino", "No", "Non-Hispanic",
                "Not Hispanic", "No, not Hispanic or Latino",
            ],
        },
    },

    "hispanic_latino": {
        "profile_key": "hispanic_latino",
        "type": "radio",
        "keywords": ["hispanic", "latino", "latina", "latinx"],
        "aliases": [
            "hispanic or latino", "are you hispanic or latino",
            "do you identify as hispanic or latino",
            "hispanic latino", "are you of hispanic or latino origin",
        ],
        "negative_keywords": [],
        "options_map": {
            "No": [
                "No", "Not Hispanic or Latino", "Non-Hispanic",
                "No, I am not Hispanic or Latino",
            ],
            "Yes": [
                "Yes", "Hispanic or Latino", "Yes, I am Hispanic or Latino",
            ],
        },
    },

    "disability": {
        "profile_key": "disability",
        "type": "radio",
        "keywords": ["disability", "disabled", "impairment", "accommodation"],
        "aliases": [
            "disability", "do you have a disability", "disability status",
            "are you a person with a disability",
            "do you have any disabilities",
            "disability or medical condition",
        ],
        "negative_keywords": ["veteran", "gender", "race"],
        "options_map": {
            "No": [
                "No", "I do not have a disability",
                "I don't have a disability",
                "No, I don't have a disability",
                "No disability",
                "I am not a person with a disability",
            ],
            "Yes": [
                "Yes", "I have a disability",
                "Yes, I have a disability",
            ],
            "Prefer not to say": [
                "Prefer not to say", "Decline to self-identify",
                "I don't wish to answer", "Decline",
            ],
        },
    },

    "veteran": {
        "profile_key": "veteran",
        "type": "radio",
        "keywords": ["veteran", "military", "armed forces", "protected veteran"],
        "aliases": [
            "veteran status", "are you a veteran", "military veteran",
            "protected veteran", "are you a protected veteran",
            "veteran or active duty",
            "are you a disabled veteran",
        ],
        "negative_keywords": ["disability", "gender", "race"],
        "options_map": {
            "I am not a protected veteran": [
                "I am not a protected veteran",
                "Not a protected veteran",
                "No, not a protected veteran",
                "I am not a veteran",
                "Not a Veteran",
                "No",
                "None",
                "I am not a veteran nor active duty",
            ],
            "Yes": [
                "I am a protected veteran",
                "Yes, I am a protected veteran",
                "Protected Veteran",
                "Veteran",
            ],
            "Prefer not to say": [
                "I choose not to self-identify",
                "Decline to self-identify",
                "Prefer not to say",
                "Decline",
            ],
        },
    },

    "transgender": {
        "profile_key": "transgender",
        "type": "radio",
        "keywords": ["transgender", "trans"],
        "aliases": [
            "transgender", "do you identify as transgender",
            "transgender identity", "are you transgender",
        ],
        "negative_keywords": ["gender", "orientation"],
        "options_map": {
            "No": ["No", "I am not transgender", "No, I am not transgender"],
            "Yes": ["Yes", "I am transgender", "Yes, I am transgender"],
            "Prefer not to say": ["Prefer not to say", "Decline", "Decline to self-identify"],
        },
    },

    "orientation": {
        "profile_key": "orientation",
        "type": "radio",
        "keywords": ["orientation", "sexual orientation", "sexuality"],
        "aliases": [
            "sexual orientation", "sexual identity",
            "what is your sexual orientation",
            "orientation", "sexuality",
        ],
        "negative_keywords": ["gender", "transgender"],
        "options_map": {
            "Heterosexual / Straight": [
                "Heterosexual", "Straight", "Heterosexual / Straight",
                "Heterosexual or Straight",
            ],
            "Prefer not to say": [
                "Prefer not to say", "Decline to self-identify",
                "I prefer not to answer", "Decline",
            ],
        },
    },

    # ── Application Meta ───────────────────────────────────────────────────────
    "referral_source": {
        "profile_key": "referral_source",
        "type": "fill",
        "keywords": ["referral", "how did you hear", "source", "how did you find"],
        "aliases": [
            "how did you hear about us", "how did you hear about this job",
            "how did you find out about this position",
            "referral source", "source", "how did you find this job",
            "where did you hear about this role",
        ],
        "negative_keywords": [],
        "options_map": {
            "Job board": [
                "Job board", "Job Board", "Online job board",
                "LinkedIn", "Indeed", "Glassdoor", "Other",
            ],
        },
    },

    "cover_letter_text": {
        "profile_key": "_cover_letter_text",
        "type": "fill",
        "keywords": ["cover letter", "letter of interest", "why are you interested"],
        "aliases": [
            "cover letter", "cover letter text", "letter of interest",
            "why are you interested in this role",
            "why do you want to work here",
            "why are you applying",
        ],
        "negative_keywords": [],
        "options_map": {},
    },

    "resume_upload": {
        "profile_key": "resume_path",
        "type": "upload",
        "keywords": ["resume", "cv", "curriculum vitae", "upload resume"],
        "aliases": [
            "resume", "cv", "curriculum vitae", "upload resume",
            "attach resume", "resume upload", "upload your resume",
        ],
        "negative_keywords": ["cover letter"],
        "options_map": {},
    },

    "cover_letter_upload": {
        "profile_key": "_cover_letter_path",
        "type": "upload",
        "keywords": ["cover letter", "upload cover letter", "attach cover letter"],
        "aliases": [
            "cover letter upload", "upload cover letter",
            "attach cover letter", "cover letter file",
        ],
        "negative_keywords": ["resume"],
        "options_map": {},
    },
}


# ---------------------------------------------------------------------------
# Main matching function
# ---------------------------------------------------------------------------

def match_field(
    label: str = "",
    placeholder: str = "",
    name: str = "",
    id: str = "",  # noqa: A002 (shadowing builtin is fine here)
    aria_label: str = "",
    options: list[str] | None = None,
    profile: dict | None = None,
) -> tuple[str, str, float]:
    """
    Match a form field to a canonical key and profile value.

    Returns:
        (canonical_key, profile_value, confidence)
        confidence: 0.0 – 1.0  (>0.85 = deterministic, skip LLM)
    """
    if profile is None:
        profile = {}
    if options is None:
        options = []

    # Build a combined text signal from all field signals
    raw_label = label or placeholder or aria_label or ""
    raw_name = re.sub(r"[-_]", " ", name or id or "")

    signals = [raw_label, placeholder, aria_label, raw_name]
    combined = _norm(" ".join(s for s in signals if s))

    best_key: str = ""
    best_score: float = 0.0

    for ckey, cdata in CATALOG.items():
        score = 0.0
        aliases: list[str] = cdata.get("aliases", [])
        keywords: list[str] = cdata.get("keywords", [])
        neg_kws: list[str] = cdata.get("negative_keywords", [])

        # 1. Alias exact / similarity match (weight 0.6)
        alias_best = 0.0
        for alias in aliases:
            s = _sim(combined, alias)
            # boost for substring — but only when combined is non-empty
            if combined and (alias in combined or combined in alias):
                s = max(s, 0.80)
            alias_best = max(alias_best, s)
        score += alias_best * 0.60

        # 2. Keyword match (weight 0.4)
        kw_score = _keyword_score(combined, keywords)
        score += kw_score * 0.40

        # 3. Negative keyword penalty
        neg_hit = _keyword_score(combined, neg_kws)
        score -= neg_hit * 0.25

        score = max(0.0, min(1.0, score))

        if score > best_score:
            best_score = score
            best_key = ckey

    if not best_key or best_score < 0.30:
        return ("", "", 0.0)

    # Resolve profile value
    cdata = CATALOG[best_key]
    pkey = cdata["profile_key"]
    pval = profile.get(pkey, "")

    # Special cases
    if pkey == "_cover_letter_text":
        pval = profile.get("_cover_letter_text", "") or profile.get("_cover_letter_path", "")
    elif pkey == "_cover_letter_path":
        pval = profile.get("_cover_letter_path", "")

    # Resolve options mapping if field has choices
    if options and pval:
        resolved = _best_option_match(str(pval), options, cdata.get("options_map", {}))
        if resolved:
            pval = resolved

    return (best_key, str(pval), round(best_score, 4))
