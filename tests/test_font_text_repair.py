import random
import string
import pytest
import text_export_hygiene as h

pytestmark = pytest.mark.offline_deterministic
PROSE = ('This is the history of the cinema and the ways in which it was changed. '
         'The film and its influence were important to the people who were there. '
         'We can see that this was not only about a new camera but also about the art of film. ')

def encode_standard(text):
    inverse={c:chr(i) for i,c in enumerate(h._STANDARD_GLYPH_UNICODE) if c}
    text=text.replace('fi','ﬁ').replace('fl','ﬂ')
    return ''.join(c if c.isspace() else inverse.get(c,c) for c in text)

def test_standard_order_repairs_ordinary_and_accented_and_ligature_forms():
    original=PROSE+'André saw the scène and the résumé; the film was influential.'
    encoded=encode_standard(original)
    repaired,evidence=h.repair_font_encoded_text(encoded)
    assert h.readable_export_text(repaired)[0]==h.readable_export_text(original)[0]
    assert evidence['standard_glyph_windows']==1
    assert h.repair_font_encoded_text(repaired)[0]==repaired

def test_generic_shift_not_source_name_or_pdf_dependent():
    encoded=''.join(chr(ord(c)-10) if not c.isspace() else c for c in PROSE)
    actual,counts=h.repair_font_encoded_text(encoded)
    assert actual==PROSE
    assert counts['uniform_shift_windows']==1

@pytest.mark.parametrize('text', [
    PROSE, PROSE.upper(),
    '“François” and “Yến Lê” are authors of this work, which is about the film. '*5,
    'The ~olor of the paper hut not the ()nes that I saw; <1on\'t replace OCR words. '*4,
    'ABC XYZ RGB HTTP GET POST SQL CPU GPU SHA256 ORGH 3LFWXUHV '*8,
    'E = MC^2; x[i] = y[i] + 1; return a[i] if b[i] else c[i]; '*8,
    'EUR 100.00 USD 42.30 ISIN NL0000000001 ISBN 978-0-123456-47-2 '*8,
    'Le cinéma français présente une étude du pouvoir dans la société. '*8,
    'LA HERENCIA DE COATLICUE, CONCIENCIA DE LA MESTIZA '*8,
    '中华人民共和国 東京大学 اللغة العربية Ελληνικά '*8,
    'institu\u00ad tions, https://example.org/A_B?q=C_D and [U+00AD] as literal notation',
])
def test_normal_prose_codes_languages_and_ocr_are_not_rewritten(text):
    assert h.repair_font_encoded_text(text)[0]==text

def test_windows_preserve_all_text_when_not_repaired():
    rng=random.Random(719)
    for size in (0,1,89,90,2499,2500,2501,5001,20000):
        raw=''.join(rng.choice(string.printable) for _ in range(size))
        assert h.repair_font_encoded_text(raw)[0]==raw

@pytest.mark.parametrize('separator',['\n\n','\r\n\r\n','\r\r'])
def test_mixed_paragraphs_do_not_share_a_decoding_decision(separator):
    bad=encode_standard(PROSE)
    raw=PROSE+separator+bad+separator+PROSE.upper()
    repaired,counts=h.repair_font_encoded_text(raw)
    assert repaired==PROSE+separator+PROSE.replace('fi','ﬁ').replace('fl','ﬂ')+separator+PROSE.upper()
    assert counts['font_decoded_windows']==1

def test_region_boundaries_not_licensed_by_neighbor():
    pages=[{'page':1,'text':'unused','reading_regions':[{'text':encode_standard(PROSE)},{'text':'ORGH'}]}]
    output,evidence=h.prepare_readable_pages('absent.pdf',pages)
    assert output[0]['reading_regions'][1]['text']=='ORGH'
    assert evidence['counts']['standard_glyph_windows']==1

def test_unknown_control_word_separators_in_shifted_text():
    encoded=''.join(chr(ord(c)-10) if not c.isspace() else '\x03' for c in PROSE)
    actual,counts=h.repair_font_encoded_text(encoded)
    assert actual==PROSE
    assert counts['encoded_word_separators_restored']==PROSE.count(' ')

def test_mixed_font_header_is_not_decoded_using_body_shift():
    header=encode_standard('Mohammad Reza Aslani with Forrest Cardamenis - The Brooklyn Rail. '*3)
    body=''.join(chr(ord(c)-10) if not c.isspace() else c for c in PROSE*5)
    raw=body+'\n'+header
    out,counts=h.repair_font_encoded_text(raw)
    assert header in out
    assert 'This is the history' in out
    assert counts['mixed_font_lines_left_unchanged']>=1

def test_shifted_unmapped_glyph_is_not_misrepresented_as_an_accent():
    body=''.join(chr(ord(c)-10) if not c.isspace() else c for c in PROSE)
    out,counts=h.repair_font_encoded_text(body+' CW]d_ÂY[dj')
    assert 'Magnicent' in out
    assert 'MagniAcent' not in h.readable_export_text(out)[0]
    assert counts['unmapped_font_glyphs_removed']==1

def test_short_repeated_page_header_does_not_inherit_the_body_encoding():
    header='0RKDPPDG\x035H]D\x03$VODQL\x03ZLWK\x03)RUUHVW\x03&DUGDPHQLV\x03\x10\x037KH\x03%URRNO\\Q\x035DLO'
    body=''.join(chr(ord(c)-10) if not c.isspace() else c for c in PROSE*4)
    out,_=h.repair_font_encoded_text(body+'\n'+header)
    # A validated separator restoration may replace the header's glyph gaps
    # with spaces, but its letters must not acquire the body's different shift.
    assert header.replace('\x03',' ') in out or header in out

@pytest.mark.parametrize('label', ['Café', 'Yến Lê', 'ISBN 978-123', 'Copyright 2026', 'NOTES', 'and'])
def test_short_valid_line_does_not_inherit_a_different_font(label):
    raw=encode_standard(PROSE)+'\n'+label
    out,counts=h.repair_font_encoded_text(raw)
    assert out.endswith('\n'+label)
    assert 'This is the history' in out
    assert counts['mixed_font_lines_left_unchanged']>=1

@pytest.mark.parametrize('url', ['https://example.org/A_B?q=C_D', 'http://test.example/a', 'www.example.org/file'])
def test_valid_url_is_preserved_inside_damaged_font_text(url):
    raw=encode_standard(PROSE)+' '+url+' '+encode_standard(PROSE)
    out,_=h.repair_font_encoded_text(raw)
    assert url in out
    assert 'This is the history' in out
