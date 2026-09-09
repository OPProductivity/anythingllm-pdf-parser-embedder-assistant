"""Damaged-font rescue must not relax cleanup or ordinary prose protection."""
import random
from collections import Counter
import string
import unicodedata

import pytest

import text_export_hygiene as h

pytestmark = pytest.mark.offline_deterministic


def encoded(text):
    inverse = {value: chr(i) for i, value in enumerate(h._STANDARD_GLYPH_UNICODE) if value}
    # The live failing PDFs use glyph-valued spaces, unlike the older fixture.
    text = text.replace('fi', 'ﬁ').replace('fl', 'ﬂ')
    return ''.join(c if c in '\r\n' else inverse.get(c, c) for c in text)


def output(text):
    result, evidence = h.prepare_readable_pages('not-opened.pdf', [{'page': 1, 'text': text}])
    actual = result[0]['text']
    assert not any(unicodedata.category(c).startswith('C') and c not in '\n\t' for c in actual)
    assert h.prepare_readable_pages('not-opened.pdf', result)[0] == result
    return actual, evidence['counts']


def test_short_header_url_date_and_pagination_recover_independently():
    raw = encoded('09/08/2025, 11:07\nNew Mode of Cinema: How Digital Technologies are Changing Aesthetics and Style | Kinephanos\nhttps://www.kinephanos.ca/2009/new-mode-of-cinema-how-digital-technologies-are-changing-aesthetics-and-style/\n1/23\n')
    actual, counts = output(raw)
    assert 'New Mode of Cinema: How Digital Technologies' in actual
    assert '09/08/2025, 11:07' in actual and '1/23' in actual
    assert 'https://www.kinephanos.ca/2009/' in actual
    assert counts['signature_rescued_windows'] == 1


def test_bibliographic_digits_quotes_and_ampersand_not_deleted_or_reinterpreted():
    prose = ('Bazin, Andre. « The Evolution of the Language of Cinema. » What Is Cinema?\n'
             'University of California Press, 1971. 23-40.\n'
             'Belton, John. « Digital Cinema: A False Revolution. » October 100 (2002): 99-114.\n'
             'Peter Graham. Garden City: Doubleday & Company, 1948. 17-23.\n')
    actual, counts = output(encoded(prose))
    assert 'October 100 (2002): 99-114.' in actual
    assert 'Doubleday & Company, 1948. 17-23.' in actual
    assert '(c)' not in actual
    assert 'Bazin, Andre.' in actual
    assert counts['signature_rescued_windows'] == 1


def test_french_abstract_is_not_scored_as_if_it_were_english():
    prose = ('Résumé\nCet article examine comment les modes de production spécifiques au cinéma numérique – tant\n'
             'sur le plan technique qu’au niveau des procédures – diffèrent des modes de production qui ont\n'
             'recours à la pellicule. Ainsi, on remarque comment ce nouvel esthétisme lié aux technologies\n'
             'numériques vient changer la donne en ce qui a trait à la mise en scène et au langage.\n')
    actual, counts = output(encoded(prose))
    assert actual == h.readable_export_text(prose)[0]
    assert '^2' not in actual and 'spécifiques au cinéma numérique - tant' in actual
    assert counts['signature_rescued_windows'] == 1


def test_keyword_field_uses_its_own_encoded_label_not_a_prose_word_quota():
    prose = ('Key Words\ndigital cinema, cinema, aesthetics, mise-en-scène, auteur, montage, caméra-stylo, composit,\nlong-take\n')
    actual, counts = output(encoded(prose))
    assert actual == h.readable_export_text(prose)[0]
    assert counts['signature_rescued_windows'] == 1


@pytest.mark.parametrize('label', ['Café Müller', 'Yến Lê', 'ISBN 978-123', 'Copyright 2026', 'NOTES'])
def test_successful_fallback_does_not_borrow_its_mapping_for_plain_lines(label):
    body = encoded('Résumé\nCet article examine comment les modes de production spécifiques au cinéma numérique et les modes de production qui ont recours à la pellicule. Ainsi les techniques sont des outils pour les cinéastes dans la société.\n')
    actual, _ = output(body + label)
    assert actual.endswith(label)


def test_harmless_span_is_not_repaired_just_because_it_is_uppercase_and_spanish():
    raw = '42\nMIGRACIONES INTERNACIONALES, VOL. 9, NÚM. 3, ENERO-JUNIO DE 2018\n\xad\nMIGRACIONES 34 Preliminar.indb 42\n2/2/18 12:42 PM\n'
    actual, counts = output(raw)
    assert actual == raw.replace('\xad', '')
    assert not counts.get('signature_rescued_windows')


def test_unknown_font_preserves_likely_spaces_without_inventing_symbol_meanings():
    raw = 'QZXW\x03JKQR\x03BZVX\x03ZVQW\x03KJXQ\x03JZVX\x03BQZX\x03JKQV\x03©² ' * 3
    actual, counts = output(raw)
    assert 'QZXW JKQR BZVX' in actual
    assert '(c)' not in actual and '^2' not in actual
    assert counts['encoded_gap_only_windows'] >= 1


@pytest.mark.parametrize('text', [
    'NOTES ON THE HISTORY OF CINEMA AND ITS CHANGING AESTHETICS. ' * 3,
    'LA HERENCIA DE COATLICUE, CONCIENCIA DE LA MESTIZA ' * 5,
    'LE CINÉMA FRANÇAIS ET LES MODES DE PRODUCTION DANS LA SOCIÉTÉ. ' * 4,
    'Bazin, Andre. The Evolution of the Language of Cinema. 1971. 23-40.\n' * 4,
    'Key Words\ndigital cinema, aesthetics, montage, auteur, cinéma, camera\n',
    'Copyright © 2026; 10²; Café Müller; Yến Lê; valid	layout. ' * 4,
])
def test_normal_inputs_keep_the_same_cleanup_policy(text):
    actual, _ = output(text)
    assert actual == h.readable_export_text(text)[0]


def test_random_font_like_values_do_not_become_decoded_prose():
    rng = random.Random(4401)
    for _ in range(100):
        tokens = [''.join(rng.choices(string.ascii_uppercase, k=rng.randrange(3, 12))) for _ in range(30)]
        raw = '\x03'.join(tokens)
        actual, counts = output(raw)
        assert not counts.get('signature_rescued_windows')
        assert actual.replace(' ', '') == ''.join(tokens)


@pytest.mark.parametrize('name', ['Café Müller', 'François', 'Yến Lê'])
@pytest.mark.parametrize('boundaries', [('\x03', '\x03'), (' ', '\x03'), ('\x03', ' ')])
@pytest.mark.parametrize('position', ['prefix', 'middle', 'suffix'])
def test_encoded_spaces_protect_only_the_accented_name(name, boundaries, position):
    prose = ('Cet article examine comment les modes de production spécifiques au cinéma numérique '
             'et les modes de production qui ont recours à la pellicule. Ainsi les techniques '
             'sont des outils pour les cinéastes dans la société.')
    body = encoded(prose)
    left, right = boundaries
    if position == 'prefix':
        raw = left + name + right + body
        expected = ' ' + name + ' ' + prose
    elif position == 'suffix':
        raw = body + left + name + right
        expected = prose + ' ' + name + ' '
    else:
        raw = body + left + name + right + body
        expected = prose + ' ' + name + ' ' + prose
    actual, counts = output(raw)
    assert actual == expected
    assert counts['readable_accented_words_preserved'] >= 1


def test_encoded_latin_click_is_not_mistaken_for_an_accented_word():
    raw = '\x03K\u01c3\x03'
    assert h._decode_selected_font_line(raw, 'standard_glyph_order', Counter()) == ' h\u01c3 '


@pytest.mark.parametrize('method', [-10, 10])
def test_encoded_space_guard_does_not_extend_to_unrelated_font_maps(method):
    counts = Counter()
    raw = '\x03Café\x03'
    h._decode_selected_font_line(raw, method, counts)
    assert not counts.get('readable_accented_words_preserved')
