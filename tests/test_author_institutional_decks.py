import pytest
from author_metadata.contextual_roles import claims
from author_metadata.publishing_relations import select

pytestmark = pytest.mark.offline_deterministic


def test_company_profile_needs_profile_act_and_self_responsibility():
    samples = [
        {"page": 1, "text": "Company Overview\nNovember 2025"},
        {
            "page": 3,
            "text": "EXAMPLE\nEXAMPLE’s products serve researchers. Our platform supports public work.\nHeadquarters: Westport | Headcount: 200",
        },
    ]
    found = claims(samples, kind="corporate_publication", work_scope="single_work")
    assert (
        select({}, found, kind="corporate_publication", work_scope="single_work")[0][
            "author"
        ]
        == "EXAMPLE"
    )
    assert not claims(
        samples[1:], kind="corporate_publication", work_scope="single_work"
    )


def test_annual_report_running_issuer_outweighs_nested_ceo_letter():
    samples = [
        {"page": 1, "text": "2024 Annual Report"},
        {"page": 3, "text": "EXAMPLE 2024 Annual Report\nDear EXAMPLE Investor:\nOur CEO writes this letter."},
        {"page": 5, "text": "EXAMPLE 2024 Annual Report\nFinancial statements and notes"},
    ]
    found = claims(samples, kind="corporate_publication", work_scope="single_work")
    assert select({}, found, kind="corporate_publication", work_scope="single_work")[0]["author"] == "EXAMPLE"
    assert not claims(samples[:2], kind="corporate_publication", work_scope="single_work")


def test_product_brochure_needs_owned_product_and_separate_publisher_voice():
    samples = [
        {"page": 1, "text": "Less time evaluating software. More time delighting customers.\nLearn more"},
        {"page": 7, "text": "With Marketing Hub Enterprise, our dashboards show results — all within ExampleCo."},
        {"page": 8, "text": "Results with ExampleCo:\nThe platform helps teams connect with customers."},
    ]
    found = claims(samples, kind="generic", work_scope="single_work")
    assert select({}, found, kind="generic", work_scope="single_work")[0]["author"] == "ExampleCo"
    assert not claims(samples[:2], kind="generic", work_scope="single_work")


def test_financial_deck_requires_reporting_subject_beyond_ownership():
    samples = [
        {"page": 1, "text": "Financial Results\n©2025 ExampleCo. All rights reserved."},
        {
            "page": 2,
            "text": "ExampleCo’s quarterly cash dividend increased.\n©2025 ExampleCo. All rights reserved.",
        },
    ]
    found = claims(samples, kind="corporate_publication", work_scope="single_work")
    assert (
        select({}, found, kind="corporate_publication", work_scope="single_work")[0][
            "author"
        ]
        == "ExampleCo"
    )
    samples[1]["text"] = (
        "A partner reported its earnings. ©2025 ExampleCo. All rights reserved."
    )
    assert not claims(samples, kind="corporate_publication", work_scope="single_work")


def test_corporate_claim_keeps_an_explicit_personal_report_writer():
    assertion = {
        "author": "Example Research Institute",
        "role": "author",
        "work": "selected work",
        "family": "organization_research_act_and_apparatus",
        "source": "text_role_scoped_creator",
        "creator_type": "organization",
        "priority": 109,
        "page": 1,
        "evidence": "conducted this study",
    }
    current = {"author": "Amelia Hart", "source": "text_byline"}
    assert (
        select(current, [assertion], kind="report", work_scope="single_work")[0]
        == current
    )


def test_report_research_act_survives_a_chapter_genre_guess():
    samples = [
        {"page": 1, "text": "State of Coastal Research"},
        {
            "page": 2,
            "text": "In this report, Example Research surveyed more than 500 professionals. Example Research provides reports.",
        },
    ]
    found = claims(samples, kind="book_chapter", work_scope="chapter")
    assert any(c["author"] == "Example Research" for c in found)


def test_dated_personal_weblog_voice_records_owner_without_inventing_post_credit():
    samples = [
        {
            "page": 1,
            "text": "Amelia Hart’s Weblog\nRecords and Public Memory\n29th May 2025\nI’m beginning to examine this question. I have studied the public records.",
        }
    ]
    found = claims(samples, kind="web_article", work_scope="single_work")
    assert (
        select({}, found, kind="web_article", work_scope="single_work")[0].get(
            "author", ""
        )
        == ""
    )
    assert found[0]["role"] == "site_owner_hypothesis"
    assert found[0]["work"] == "hosting site"
    current = {"author": "Benedict Vale", "source": "text_byline"}
    assert (
        select(current, found, kind="web_article", work_scope="single_work")[0]
        == current
    )
    samples[0]["text"] += "\nGuest post by Benedict Vale"
    assert not claims(samples, kind="web_article", work_scope="single_work")
