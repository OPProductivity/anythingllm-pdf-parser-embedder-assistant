"""Exercise the active page-preparation path, not legacy lab helpers."""
import pytest
import text_export_hygiene as h

pytestmark = pytest.mark.offline_deterministic

PROSE = ('This is the history of the cinema and the ways in which it was changed. '
         'The film and its influence were important to the people who were there. '
         'We can see that this was not only about a new camera but also about the art of film. ')

def encoded(text):
    inverse = {c: chr(i) for i, c in enumerate(h._STANDARD_GLYPH_UNICODE) if c}
    return ''.join(c if c.isspace() else inverse.get(c, c) for c in text)

@pytest.mark.parametrize('identifier', [
    'alice@example.org', 'first.last+papers@dept.example.ac.uk',
    'doi:10.1234/ABC-123', 'DOI: 10.12345/example.2026',
    '10.1000/ABC_123(4)', 'https://example.org/A_B?q=C_D',
])
@pytest.mark.parametrize('surround', [(' ', ' '), (' (', ') '), ('\n', '\n')])
def test_identifier_survives_actual_page_preparation(identifier, surround):
    raw = encoded(PROSE) + surround[0] + identifier + surround[1] + encoded(PROSE)
    pages = [{'page': 1, 'text': raw}]
    output, evidence = h.prepare_readable_pages('not-opened.pdf', pages)
    assert identifier in output[0]['text']
    assert 'This is the history' in output[0]['text']
    assert evidence['counts']['font_decoded_windows'] >= 1
    assert pages[0]['text'] == raw
    assert h.prepare_readable_pages('not-opened.pdf', output)[0] == output

@pytest.mark.parametrize('identifier', ['alice@example.org', '10.1234/ABC-123'])
def test_already_readable_identifiers_do_not_activate_font_repair(identifier):
    raw = PROSE + identifier
    output, evidence = h.prepare_readable_pages('not-opened.pdf', [{'page': 1, 'text': raw}])
    assert output[0]['text'] == raw
    assert evidence['counts'].get('font_decoded_windows', 0) == 0


def test_active_preparation_does_not_call_legacy_diagnostic_or_source_helpers():
    raw = 'Native in\x80uence and normal Yến Lê; do not spellcheck ~olor.'
    assert not hasattr(h, 'infer_source_glyph')
    assert not hasattr(h, 'filter_export_text')
    output, evidence = h.prepare_readable_pages('not-opened.pdf', [{'page': 1, 'text': raw}])
    assert output[0]['text'] == 'Native inuence and normal Yến Lê; do not spellcheck ~olor.'
    assert evidence['counts']['unresolved_character_removed'] == 1
