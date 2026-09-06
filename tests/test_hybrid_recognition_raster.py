"""Confirmed-spread enhancement must not alter auxiliary OCR or geometry."""
from pathlib import Path
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw, ImageOps
import rag_pdf_tools as t
import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('opt_in,requested,resolved,model,scale', [
    (True,4,6,True,1.5), (False,4,6,True,1),
    (True,4,4,False,1), (True,6,6,False,1), (True,3,3,False,1),
])
def test_text_raster_keeps_source_fraction_and_one_recognition(opt_in,requested,resolved,model,scale):
    image=Image.new('RGB',(503,601),'white')
    ImageDraw.Draw(image).rectangle((50,60,350,450),fill='black')
    before=image.tobytes()
    fraction=(.123,.057,.934,.951)
    args=['--tessdata-dir','qualified','--oem','1'] if model else []
    decision={'requested_psm':requested,'psm':resolved,'model':'qualified' if model else 'installed'}
    evidence={}
    captured=[]
    def run(command,**kwargs):
        with Image.open(command[1]) as submitted:
            captured.append(submitted.copy())
        return SimpleNamespace(returncode=0,stdout='Unmodified recognized source text')
    with patch.object(t,'_resolve_ocr_recognition',return_value=(resolved,args,decision)) as resolve, patch.object(t.subprocess,'run',side_effect=run) as call:
        result=t._ocr_photographed_crop(image,fraction,'tesseract',ImageOps,psm=requested,recognition_evidence=evidence,enhance_annotated_prose=opt_in)
    expected=image if scale==1 else image.resize((round(503*scale),round(601*scale)),Image.Resampling.LANCZOS)
    box=tuple(int(v*s) for v,s in zip(fraction,(expected.width,expected.height,expected.width,expected.height)))
    expected=ImageOps.autocontrast(expected.crop(box).convert('L'))
    assert captured[0].size==expected.size and captured[0].tobytes()==expected.tobytes()
    assert result=='Unmodified recognized source text'
    assert call.call_count==resolve.call_count==1
    assert image.tobytes()==before
    assert evidence['crop_fraction']==list(fraction)
    assert evidence['recognition_raster_scale']==scale


def test_optional_resampling_failure_keeps_original_recognition():
    evidence={}
    with patch.object(t,'_resolve_ocr_recognition',return_value=(6,['--oem','1'],{'requested_psm':4})),patch.object(Image.Image,'resize',side_effect=MemoryError),patch.object(t.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='Original route survives')) as call:
        text=t._ocr_photographed_crop(Image.new('RGB',(500,600)),(0,0,1,1),'tesseract',ImageOps,enhance_annotated_prose=True,recognition_evidence=evidence)
    assert text=='Original route survives' and call.call_count==1
    assert evidence['recognition_raster_scale']==1
    assert evidence['raster_enhancement_fallback']=='MemoryError'


def test_oversized_raster_preserves_original_without_allocating_enlargement():
    evidence={}
    with patch.object(t,'_resolve_ocr_recognition',return_value=(6,['--oem','1'],{'requested_psm':4})),patch.object(Image.Image,'resize',side_effect=AssertionError('must not enlarge')),patch.object(t.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='Original route survives')):
        t._ocr_photographed_crop(Image.new('L',(3000,3000)),(0,0,1,1),'tesseract',ImageOps,enhance_annotated_prose=True,recognition_evidence=evidence)
    assert evidence['raster_enhancement_fallback']=='raster_budget_preserve_original'


def test_tsv_route_keeps_original_pixel_coordinate_system():
    image=Image.new('RGB',(500,600),'white')
    captured=[]
    def run(command,**kwargs):
        with Image.open(command[1]) as submitted:
            captured.append(submitted.size)
        Path(command[2]).with_suffix('.txt').write_text('Actual text',encoding='utf8')
        Path(command[2]).with_suffix('.tsv').write_text(
            'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
            '5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t95\tActual\n',encoding='utf8')
        return SimpleNamespace(returncode=0)
    with patch.object(t,'_resolve_ocr_recognition',return_value=(6,['--oem','1'],{'requested_psm':4})),patch.object(t.subprocess,'run',side_effect=run) as call:
        _,rows=t._ocr_photographed_crop_with_layout(image,(.1,.1,.9,.9),'tesseract',ImageOps)
    assert captured==[(400,480)]
    assert rows[0]['left']==10 and rows[0]['top']==20
    assert rows[0]['crop_width']==400 and rows[0]['crop_height']==480
    assert call.call_count==1


def test_raster_decision_is_retained_in_existing_page_evidence():
    profile={'psm':6,'recognition_raster_scale':1.5,'subprocess_seconds':1.0}
    ledger=pipeline.ocr_page_evidence_ledger([{'page':1,'text':'Actual source text','reading_regions':[{'recognition_layout':profile}]}],{'status':'not_available'},[1])
    assert ledger['pages'][0]['recognition_layout']==[profile]


def test_revised_ocr_cannot_receive_cache_credit_for_old_page_text(tmp_path):
    documents=tmp_path/'documents'
    custom=documents/'custom-documents'
    custom.mkdir(parents=True)
    location='custom-documents/page.json'
    metadata={'title':'Same PDF page','docAuthor':'Same Author','description':'Same source',
              'docSource':'pdf','chunkSource':'same-source-sha-and-page'}
    original={'textContent':'Home building became 50 central','metadata':metadata}
    corrected={'textContent':'Home building became so central','metadata':metadata}
    (documents/location).write_text(json.dumps({'pageContent':original['textContent'],**metadata}),encoding='utf8')
    def snapshot():
        return {'status':'ready','documents_root':str(documents),'custom_documents':str(custom),
                'cache_entry_names':{str(uuid.uuid5(uuid.NAMESPACE_URL,location))},
                'locations_by_chunk_source':{metadata['chunkSource']:[location]}}
    assert pipeline.find_reusable_cached_document_locations_from_snapshot(snapshot(),[original])==[location]
    assert pipeline.find_reusable_cached_document_locations_from_snapshot(snapshot(),[corrected])==['']
