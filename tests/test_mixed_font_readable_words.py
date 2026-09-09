"""Constrained preservation in the actual text-only export preparation path."""
import pytest
import text_export_hygiene as h

pytestmark = pytest.mark.offline_deterministic
PROSE = ('This is the history of the cinema and the ways in which it was changed. '
         'The film and its influence were important to the people who were there. '
         'We can see that this was not only about a new camera but also about the art of film. ')

def encoded(text, method):
    if method == 'standard':
        inverse = {c: chr(i) for i, c in enumerate(h._STANDARD_GLYPH_UNICODE) if c}
        return ''.join(c if c.isspace() else inverse.get(c, c) for c in text)
    return ''.join(chr(ord(c) - 10) if c.isascii() and not c.isspace() else c for c in text)

@pytest.mark.parametrize('method', ['standard', 'shift'])
@pytest.mark.parametrize('name', ['Café Müller', 'Yến Lê', 'François'])
@pytest.mark.parametrize('position', ['prefix', 'middle', 'suffix'])
def test_exact_body_and_name_preserved_together(method, name, position):
    body = encoded(PROSE, method)
    raw = name+' '+body if position == 'prefix' else body+' '+name+' '+body if position == 'middle' else body+' '+name
    expected = name+' '+PROSE if position == 'prefix' else PROSE+' '+name+' '+PROSE if position == 'middle' else PROSE+' '+name
    original = [{'page': 1, 'text': raw}]
    out, evidence = h.prepare_readable_pages('not-opened.pdf', original)
    assert out[0]['text'] == expected
    assert original[0]['text'] == raw
    assert evidence['policy'] == 'text_only_readable_v5'
    assert evidence['counts']['readable_accented_words_preserved'] >= 1
    assert h.prepare_readable_pages('not-opened.pdf', out)[0] == out

def test_protected_name_does_not_exempt_its_whole_line():
    body = encoded(PROSE, 'standard')
    out, _ = h.prepare_readable_pages('not-opened.pdf', [{'page': 1, 'text': body+' Café Müller '+body}])
    assert out[0]['text'].count('This is the history') == 2
    assert 'Café Müller' in out[0]['text']

def test_damaged_token_fragments_are_not_mistaken_for_normal_accented_words():
    from collections import Counter
    source = 'CW]d_ÂY[dj'
    counts = Counter()
    result = h._decode_selected_font_line(source, 10, counts)
    assert result == 'Magnicent'
    assert counts.get('readable_accented_words_preserved', 0) == 0

@pytest.mark.parametrize('source,expected', [('Âbc','lm'),('Âbci','lms')])
def test_leading_unknown_ligature_is_not_preserved_as_an_accent(source,expected):
    from collections import Counter
    counts = Counter()
    assert h._decode_selected_font_line(source, 10, counts) == expected
    assert counts.get('readable_accented_words_preserved', 0) == 0
