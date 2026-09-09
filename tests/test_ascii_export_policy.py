from pathlib import Path
import unicodedata
import pytest
import text_export_hygiene as h

pytestmark = pytest.mark.offline_deterministic

@pytest.mark.parametrize('source,expected', [
    ('Yê\uf7b4n Lê Espiritu', 'Yen Lê Espiritu'),
    ('Yến Lê Espiritu', 'Yến Lê Espiritu'),
    ('Łódź, François, Søren, Straße, encyclopædia', 'Łódź, François, Søren, Straße, encyclopædia'),
    ('Ye\u0302\uf7b4n Lê', 'Yen Lê'),
    ('Ye\u0302\u0301n Lê', 'Ye\u0302\u0301n Lê'),
    ('Ye\u00ad\u0302\u0301n Lê', 'Ye\u0302\u0301n Lê'),
    ('Ye\u202d\u0302\u0301n Lê', 'Ye\u0302\u0301n Lê'),
    ('Length 10 Å; Å and A\u030a; K', 'Length 10 Å; Å and A\u030a; K'),
    ('café \ue904 résumé', 'café  résumé'),
    ('\ue904école', 'école'),
    ('café\ue904', 'café'),
    ('“multi-stakeholder”—ﬁlm…', '"multi-stakeholder"-film...'),
    ('\ue000\uf8ff\U000f0000\U0010fffd', ''),
    ('\u0be0\u0be1\u0be2\u0be3\u0be4\u038b', ''),
    ('a\u2028b\u2029c\u00a0d', 'a\nb\nc d'),
    ('½ ≤ ¾ ≠ ٢', '1/2 <= 3/4 != 2'),
    ('10⁻³ H₂O 1½ 2²', '10^(-3) H_2O 1 1/2 2^2'),
    ('~olor hut ()nes <1on\'t', '~olor hut ()nes <1on\'t'),
    ('白 東京 مرحبا', '  '),
])
def test_readable_accent_policy(source,expected):
    actual,_=h.readable_export_text(source)
    assert actual==expected
    assert h.readable_export_text(actual)[0]==actual

def test_every_unicode_value_is_covered_not_just_corpus():
    # Includes surrogates, all three PUA ranges, unassigned and noncharacters.
    for first in range(0,0x110000,4096):
        source=''.join(chr(i) for i in range(first,min(first+4096,0x110000)))
        out,_=h.readable_export_text(source)
        out.encode('utf8',errors='strict')
        assert all(c in '\n\t' or not unicodedata.category(c).startswith('C') for c in out)
        assert h.readable_export_text(out)[0]==out

def test_metadata_labels_and_offsets_in_actual_segments():
    import auto_anythingllm_pipeline as p
    meta={'source_id':'s','source_sha256':'a'*64,'source_title':'Café — ﬁlm', 'source_author':'Yến Lê'}
    raw=('“Café” and ﬁlm. This is a sentence about an author. ')*8
    rows=p.make_segments(Path('absent.pdf'),'pymupdf',[{'page':1,'text':raw}],1,None,meta,140,segment_mode='passages')
    assert rows
    for row in rows:
        assert 'Café' in row['text']
        assert row['char_end_page']-row['char_start_page']==len(row['text'])
        assert 'Café' in p.inline_marker_text(row)
        author_label = dict(row, source_short_label='Yến Lê')
        assert 'Yến Lê' in p.inline_marker_text(author_label)
    for payload in p.generate_api_payloads(rows,'strict'):
        assert 'Café' in payload['textContent']
    assert 'Café' in p.generate_upload_text(rows)
