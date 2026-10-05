from pathlib import Path

import pytest

from author_metadata.dispatcher import infer_author_from_samples
from author_metadata.profile import classify_document


pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("text,subtype", [
    (
        "Investor Relations Contact\nAlice Smith\nFOR IMMEDIATE RELEASE\n"
        "Example Corporation Reports Fourth Quarter Results\n"
        "Example Corporation today reported financial results and revenue growth.",
        "corporate_release",
    ),
    (
        "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nFORM 10-K\n"
        "Example Corporation\n(Exact name of registrant as specified in its charter)\n"
        "For the fiscal year ended December 31, 2024",
        "corporate_filing",
    ),
    (
        "Fiscal Fourth Quarter 2024\nFinancial Results\n"
        "©2024 Example Corporation. All rights reserved.\nIncome Statement Summary",
        "corporate_presentation",
    ),
    (
        "Challenge\nOur customer needed a new solution.\n"
        "© Example Corporation. All rights reserved.\nSummary\n"
        "Our client saw improvements.\nINDUSTRY: Manufacturing\n"
        "Alice Smith\nHuman Resources Manager",
        "corporate_client_case",
    ),
])
def test_corporate_work_is_not_attributed_to_contact_or_subject(text, subtype):
    samples = [{"page": 1, "text": text}]
    profile = classify_document(samples)
    assert profile.kind == "corporate_publication"
    assert profile.publication_type == subtype
    report = infer_author_from_samples(samples, Path("corporate.pdf"))
    assert report["author"] == ""
    assert report["source"] == "corporate_work_author_not_resolved"


def test_corporate_cover_explicit_person_work_credit_is_allowed():
    samples = [{"page": 1, "text": (
        "Example Corporation Reports Fourth Quarter Results\n"
        "By Alice Smith\nFOR IMMEDIATE RELEASE\n"
        "The company reported revenue and net income growth."
    )}]
    report = infer_author_from_samples(samples, Path("corporate.pdf"))
    assert report["author"] == "Alice Smith"
    assert report["source"] == "text_corporate_cover_byline"


def test_release_contact_byline_before_headline_is_not_work_author():
    samples = [{"page": 1, "text": (
        "Investor Relations Contact\nBy Alice Smith\n"
        "FOR IMMEDIATE RELEASE\n"
        "Example Corporation Reports Fourth Quarter Results\n"
        "The company reported revenue and net income growth."
    )}]
    report = infer_author_from_samples(samples, Path("release.pdf"))
    assert report["author"] == ""


def test_generic_study_keyword_does_not_claim_corporate_work():
    samples = [{"page": 1, "text": (
        "Annual Report Analysis\nAbstract\nAlice Smith\n"
        "The study examines annual reports and financial results."
    )}]
    assert classify_document(samples).kind == "scholarly_article"
