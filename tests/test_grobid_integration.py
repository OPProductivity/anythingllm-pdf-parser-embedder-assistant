"""Focused checks for the optional scholarly GROBID evidence path."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from io import BytesIO
import unittest

import fitz
import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.grobid import _post_pdf, analyze, author_evidence, parse_tei, reconcile
from author_metadata.identity import author_confidence
from author_metadata.pdf import infer_author_from_initial_pdf_pages
from author_metadata.profile import DocumentProfile, classify_document

pytestmark = pytest.mark.offline_deterministic


TEI = b'''<TEI xmlns="http://www.tei-c.org/ns/1.0">
<teiHeader><fileDesc><sourceDesc><biblStruct><analytic>
<title>The Politics of Rural Memory</title>
<author><persName><forename>Jane</forename><surname>Doe</surname></persName>
<affiliation><orgName>Example University</orgName></affiliation></author>
</analytic><monogr><title>Journal of Memory</title><imprint><date when="2024"/></imprint></monogr>
<idno type="DOI">10.1234/example</idno></biblStruct></sourceDesc></fileDesc>
<profileDesc><abstract>We study memory.</abstract><textClass><keywords><term>memory</term>
</keywords></textClass></profileDesc></teiHeader>
<text><body><p xml:id="p1"><s xml:id="s1" coords="1,1,1,10,1">Earlier work <ref type="bibr" target="#b1">(Roe 2020)</ref>.</s></p>
<figure type="table"><head>Table 1</head></figure></body><back><div type="references">
<listBibl><biblStruct xml:id="b1"><analytic><title>Another study</title>
<author><persName><forename>John</forename><surname>Roe</surname></persName></author>
</analytic><note type="raw_reference">Roe, Another study, 2020.</note>
</biblStruct></listBibl></div></back></text></TEI>'''


def context(kind="scholarly_article"):
    return AuthorEvidenceContext.from_samples(
        [{"page": 1, "text": "Journal of Memory 12(2)\nThe Politics of Rural Memory\n"
                             "Jane Doe\nExample University\nAbstract\nWe study memory."}],
        path=Path("doe-2024-memory.pdf"), title_hint="The Politics of Rural Memory",
        profile=DocumentProfile(kind, ("test",), (1,), publication_type=kind),
    )


class GrobidIntegrationTest(unittest.TestCase):
    def test_editor_and_reviewer_panels_do_not_become_article_authors(self):
        for opening in (
            "The Politics of Rural Memory\nJane Doe\nEDITED BY\nJohn Roe\n"
            "Example University\nREVIEWED BY\nMary Smith\n*CORRESPONDENCE\nJane Doe",
            "OPEN ACCESS\nEDITED BY\nJohn Roe\nREVIEWED BY\nMary Smith\n"
            "COPYRIGHT\nThe Politics of Rural Memory\nJane Doe\nAbstract",
        ):
            source = AuthorEvidenceContext.from_samples(
                [{"page": 1, "text": opening}],
                path=Path("doe-2024-memory.pdf"),
                title_hint="The Politics of Rural Memory",
                profile=DocumentProfile("scholarly_article", ("test",), (1,),
                                        publication_type="scholarly_article"),
            )
            analysis = {"status": "ok", "title": "The Politics of Rural Memory",
                        "authors": [{"name": name} for name in
                                    ("Jane Doe", "John Roe", "Mary Smith")]}
            candidate = author_evidence(source, analysis)
            self.assertEqual(candidate["author"], "Jane Doe")
            self.assertEqual(candidate["excluded_role_credits"], ["John Roe", "Mary Smith"])

    def test_compact_scholarly_opening_routes_only_a_visible_article_byline(self):
        body = "Scholarly argument about migration and culture. " * 45
        article = ("Cultural Memory and Social Mobility\n"
                   "Milena Feldmann and Markus Rieger-Ladich\n"
                   "1 Introduction\n(Habermas 1986) " + body)
        affiliated = ("What Has New Haven to Do\nwith Hungary? On Theology\n"
                      "and Political Postliberalism\n"
                      "David W. Congdon / University of Kansas\n" + body)
        for text, title in ((article, "Cultural Memory and Social Mobility"),
                            (affiliated, "What Has New Haven to Do with Hungary?")):
            profile = classify_document([{"page": 1, "text": text}], title_hint=title)
            self.assertEqual(profile.kind, "scholarly_article")
            self.assertIn("compact_title_byline_and_scholarly_body", profile.cues)
        self.assertEqual(classify_document(
            [{"page": 1, "text": article.replace("Milena Feldmann and Markus Rieger-Ladich\n", "")}],
            title_hint="Cultural Memory and Social Mobility",
        ).kind, "generic")

    def test_work_authors_are_separate_from_reference_authors(self):
        parsed = parse_tei(TEI, fulltext=True)
        self.assertEqual([a["name"] for a in parsed["authors"]], ["Jane Doe"])
        self.assertEqual(parsed["references"][0]["authors"], ["John Roe"])
        self.assertEqual(parsed["citation_contexts"][0]["target"], "#b1")
        self.assertEqual(parsed["citation_contexts"][0]["reference_id"], "b1")
        self.assertEqual(parsed["citation_contexts"][0]["reference_raw"],
                         "Roe, Another study, 2020.")
        self.assertEqual(parsed["citation_contexts"][0]["context_id"], "p1")
        self.assertIn("Earlier work", parsed["citation_contexts"][0]["context"])
        self.assertEqual(parsed["tables"][0]["caption"], "Table 1")
        self.assertEqual(parsed["paragraphs"][0]["text"], "Earlier work (Roe 2020) .")
        self.assertEqual(parsed["sentences"][0]["paragraph_id"], "p1")
        self.assertEqual(parsed["sentences"][0]["coords"], "1,1,1,10,1")
        self.assertGreater(parsed["body_word_count"], 0)
        self.assertEqual(parsed["doi"], "10.1234/example")

    def test_only_scholarly_genre_is_sent_to_service(self):
        with patch("author_metadata.grobid._post_pdf") as post:
            for genre in ("book", "book_chapter", "review", "report",
                          "thesis_dissertation", "periodical_issue", "web_article"):
                self.assertEqual(analyze("missing.pdf", context(genre))["status"],
                                 "skipped_genre")
            post.assert_not_called()

    def test_visible_header_fills_unknown_but_keeps_conflicting_credit(self):
        parsed = {"status": "ok", **parse_tei(TEI)}
        self.assertEqual(author_evidence(context(), parsed)["author"], "Jane Doe")
        chosen, decision = reconcile(context(), {"author": "", "source": "not_found"}, parsed)
        self.assertEqual((chosen["author"], decision), ("Jane Doe", "filled_unknown"))
        chosen, decision = reconcile(
            context(), {"author": "Mary Smith", "source": "text_byline"}, parsed,
        )
        self.assertEqual((chosen["author"], decision), ("Mary Smith", "conflict_kept_existing"))
        self.assertEqual(author_confidence({"author": "Jane Doe", "source": "text_affiliated_byline",
                                            "grobid_agrees": True})["score"], 92)
        parsed["title"] = "Completely Different Work"
        self.assertFalse(author_evidence(context(), parsed)["author"])

    def test_service_failure_keeps_existing_author(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.pdf"
            path.write_bytes(b"%PDF placeholder")
            with patch("author_metadata.grobid._post_pdf", side_effect=URLError("offline")):
                analysis = analyze(path, context())
            self.assertEqual(analysis["status"], "unavailable")
            existing = {"author": "Jane Doe", "source": "text_byline"}
            selected, _ = reconcile(context(), existing, analysis)
            self.assertEqual(selected, existing)

    def test_busy_service_retries_once_at_documented_interval(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.pdf"
            path.write_bytes(b"%PDF placeholder")
            busy = HTTPError("http://localhost:8070", 503, "busy", None, None)
            for fulltext, interval in ((False, 2), (True, 5)):
                with (patch("author_metadata.grobid.urlopen",
                            side_effect=[busy, BytesIO(TEI)]) as request,
                      patch("author_metadata.grobid.time.sleep") as wait):
                    self.assertEqual(_post_pdf(path, fulltext=fulltext, timeout=20), TEI)
                self.assertEqual(request.call_count, 2)
                wait.assert_called_once_with(interval)

    def test_no_content_is_distinct_from_service_unavailability(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.pdf"
            path.write_bytes(b"%PDF placeholder")
            response = BytesIO(b"")
            response.status = 204
            with patch("author_metadata.grobid.urlopen", return_value=response):
                self.assertEqual(analyze(path, context())["status"], "no_content")

    def test_preview_uses_grobid_for_unknown_scholarly_pdf(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "doe-2024-memory.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((60, 80), "Journal of Memory Vol. 12 No. 2\n"
                                       "The Politics of Rural Memory\n"
                                       "Jane Doe\nExample University\nAbstract\nWe study memory.")
            doc.save(path)
            doc.close()
            with patch("author_metadata.pdf.analyze_grobid",
                       return_value={"status": "ok", **parse_tei(TEI)}):
                report = infer_author_from_initial_pdf_pages(path, page_limit=1)
            self.assertEqual(report["author"], "Jane Doe")
            self.assertIn(report["grobid"]["decision"], {"filled_unknown", "agrees"})

    def test_workspace_native_metadata_path_consults_grobid(self):
        import rag_pdf_gradio_app as app

        with TemporaryDirectory() as directory:
            path = Path(directory) / "doe-memory.pdf"
            doc = fitz.open()
            doc.new_page()
            doc.save(path)
            doc.close()
            with patch("rag_pdf_gradio_app.analyze_grobid",
                       return_value={"status": "ok", **parse_tei(TEI)}) as service:
                report = app.workspace_person_identity_from_pdf(
                    path, native_metadata={"title": "The Politics of Rural Memory",
                                           "_author_text_samples": list(context().samples)},
                )
            service.assert_called_once()
            self.assertEqual(report["label"], "Doe")


if __name__ == "__main__":
    unittest.main()
