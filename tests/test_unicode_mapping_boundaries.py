"""Semantic boundary fixtures, separate from Unicode output-policy coverage."""
import unicodedata
import pytest
import text_export_hygiene as h

pytestmark = pytest.mark.offline_deterministic

def test_complete_standard_table_matches_independent_glyph_names():
    order=pytest.importorskip('fontTools.ttLib.standardGlyphOrder').standardGlyphOrder
    agl=pytest.importorskip('fontTools.agl')
    assert len(order)==258
    assert h._STANDARD_GLYPH_UNICODE==[agl.toUnicode(name) for name in order]

@pytest.mark.parametrize('raw', [
    'Yến Lê reviewed Café and François.',
    'ASCII code labels U+EDD9 and \\uEDD9 are literal documentation.',
    'The name is À, not an instruction to decode glyph index 192.',
    'MacRoman, WinAnsiEncoding and Identity-H name different encodings.',
    'α β □ ■ \ue000 \U000f0000 \U0010fffd \ufffd',
])
def test_normal_text_does_not_trigger_unrelated_font_mapping(raw):
    decoded,counts=h.repair_font_encoded_text(raw)
    assert decoded==raw
    clean,_=h.readable_export_text(decoded)
    assert h.readable_export_text(clean)[0]==clean
    assert not any(unicodedata.category(c).startswith('C') and c not in '\n\t' for c in clean)

def test_all_private_use_ranges_not_just_observed_icons():
    for start,stop in [(0xe000,0xf900),(0xf0000,0xffffe),(0x100000,0x10fffe)]:
        text=''.join(chr(cp) for cp in range(start,stop))
        assert h.readable_export_text(text)[0]==''

@pytest.mark.parametrize('mark',['\u00ad','\u202d','\u200d','\u034f'])
def test_disposable_mark_cannot_corrupt_following_valid_accent(mark):
    raw='e'+mark+'\u0301n'
    out,_=h.readable_export_text(raw)
    assert unicodedata.normalize('NFC',out)=='én'
    assert h.readable_export_text(out)[0]==out
