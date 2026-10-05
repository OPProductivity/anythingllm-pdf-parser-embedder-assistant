import json
from pathlib import Path
import tempfile
import unittest

from experiments.audit_full_automatic_runs_20261005 import audit_layout_deletions, deletion_signals


class LayoutDeletionAuditTests(unittest.TestCase):
    def review(self, pages, rows=(), export=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'layout-region-review.json').write_text(json.dumps({'pages': pages}), encoding='utf8')
            errors = []
            result = audit_layout_deletions(root, {}, rows, export, errors)
            self.assertEqual(errors, [])
            return result

    def test_lost_body_citation_is_flagged_despite_small_size(self):
        text = 'goano 1999; Equiano [1785] 2003; Wheatley [1773] 1999).'
        result = self.review([{'pdf_page': 2, 'removed_marginalia': [
            {'text': text, 'reason': 'italic_running_author', 'bbox': [290, 74, 520, 85]}]}],
            [{'pdf_page': 2, 'text': 'The remaining body text.'}])
        warning = result['warnings'][0]
        self.assertIn('citation_like_text', warning['signals'])
        self.assertFalse(warning['present_in_page_local_manifest'])
        self.assertEqual(warning['text'], text)
        self.assertEqual(warning['evidence_pointer'], '/pages/0/removed_marginalia/0')

    def test_observed_earlier_headers_and_letter_spaced_footers(self):
        for text, reason in [('Southwestern Historical Quarterly', 'italic_running_author'),
                             ('Book Reviews 35', 'repeated_running_header'),
                             ('36 Book Reviews', 'repeated_running_header'),
                             ('A b o l i t i o n', 'repeated_running_footer'),
                             ('R o b e R t F a n u z z i', 'repeated_running_footer'),
                             ('2018', 'positioned_page_number'), ('2', 'positioned_page_number')]:
            with self.subTest(text=text):
                self.assertEqual(deletion_signals(text, reason), [])

    def test_retained_duplicate_not_asserted_lost_and_ascii_export_matches(self):
        result = self.review([{'pdf_page': 1, 'removed_marginalia': [
            {'text': 'according to René this is retained', 'reason': 'italic_running_author'}]}],
            [{'pdf_page': 1, 'text': 'according to Re-\nne this is retained'}])
        self.assertTrue(result['records'][0]['present_in_page_local_manifest'])
        self.assertEqual(result['warnings'], [])

    def test_other_page_occurrence_does_not_mask_loss(self):
        result = self.review([{'pdf_page': 1, 'removed_marginalia': [
            {'text': 'Smith (1999); Jones (2000).', 'reason': 'italic_running_author'}]}],
            [{'pdf_page': 1, 'text': 'Other body'}, {'pdf_page': 2, 'text': 'Smith (1999); Jones (2000).'}],
            export='Smith (1999); Jones (2000).')
        self.assertEqual(result['warning_count'], 1)
        self.assertTrue(result['warnings'][0]['present_in_export_anywhere'])

    def test_page_spanning_rows_are_unknown_not_proven_absent(self):
        result = self.review([{'pdf_page': 1, 'removed_marginalia': [
            {'text': 'some body prose continues here', 'reason': 'italic_running_author'}]}],
            [{'pdf_page': 1, 'pdf_page_end': 2, 'text': 'some body prose continues here'}])
        self.assertIsNone(result['warnings'][0]['present_in_page_local_manifest'])

    def test_notes_unknown_reasons_and_body_reocr_are_reviewed(self):
        result = self.review([{'pdf_page': 1,
            'outer_margin_annotation': {'body_reocr': {'selected': True}},
            'excluded_footnotes': [{'text': '1. A relevant note.'}],
            'removed_marginalia': [{'text': 'a phrase', 'reason': 'new_filter'}]}])
        self.assertEqual(result['warning_count'], 3)
        self.assertEqual(result['selected_body_reocr_pages'], [1])

    def test_missing_evidence_not_reported_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            result = audit_layout_deletions(Path(directory), {}, [], '', [])
        self.assertEqual(result['status'], 'evidence_unavailable')


if __name__ == '__main__':
    unittest.main()
