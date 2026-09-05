from concurrent.futures import Future
from pathlib import Path
import sys
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rag_pdf_tools as tools

pytestmark = pytest.mark.offline_deterministic


def words():
    return [dict(block=i, paragraph=1, line=1, word=1, left=x,
                 top=y, width=50, height=15, text=text,
                 crop_width=1000, crop_height=1400)
            for i,x,y,text in [(1,20,100,'Left'), (2,360,200,'Middle'),
                               (3,680,100,'Right'), (4,20,500,'End')]]


def reorder(text, rows):
    return tools.reorder_three_column_ocr_blocks(text, rows,
        {'detected':True,'gutters':[{'x_fraction':.33},{'x_fraction':.66}]},
        (0,0,1,1))


def test_actual_crop_dimensions_keep_blank_margins_out_of_column_scaling():
    text, evidence = reorder('Left Middle Right End', words())
    assert evidence['applied']
    assert evidence['coordinate_basis'] == 'actual_crop'
    assert text.index('End') < text.index('Middle') < text.index('Right')


@pytest.mark.parametrize('extra', ['2026', '$', '—', '123.45'])
def test_column_rebuild_cannot_drop_numbers_or_punctuation(extra):
    original='Left Middle Right End '+extra
    text,evidence=reorder(original,words())
    assert text == original
    assert evidence['reason'] == 'rebuilt_lexical_content_changed'


@pytest.mark.parametrize('callback_name',['completed_page_callback','progress_callback'])
def test_callback_failure_terminates_pool_without_waiting(callback_name):
    executor=mock.Mock()
    first=Future()
    first.set_result({'page_row':{'page':1,'text':'ok'},'element_rows':[]})
    hanging=Future()
    executor.submit.side_effect=[first,hanging]
    failure=OSError('checkpoint disk write failed')
    with mock.patch.object(tools,'ProcessPoolExecutor',return_value=executor), \
         mock.patch.object(tools,'wait',return_value=({first},{hanging})), \
         mock.patch.object(tools,'_terminate_unstructured_executor') as terminate:
        with pytest.raises(OSError,match='checkpoint disk write failed'):
            tools._parallel_unstructured_ocr_pages(Path('source.pdf'),2,'hi_res',2,{},
                **{callback_name:mock.Mock(side_effect=failure)})
        terminate.assert_called_once_with(executor)
        executor.shutdown.assert_not_called()


@pytest.mark.parametrize('recognized,expected', [('AS','AS\n\n'),('a?','')])
def test_sparse_region_recovery_only_inserts_verified_uppercase_line(recognized,expected):
    from PIL import Image, ImageDraw, ImageOps
    image=Image.new('L',(400,400),255)
    draw=ImageDraw.Draw(image)
    draw.rectangle((40,35,55,75),fill=0)
    draw.rectangle((65,35,80,75),fill=0)
    original='ALPHA BETA GAMMA DELTA EPSILON'
    rows=[dict(block=1,paragraph=1,line=1,word=i+1,text=word,left=20+i*60,
               top=140,width=50,height=20) for i,word in enumerate(original.split())]
    with mock.patch.object(tools,'_ocr_photographed_crop',return_value=recognized) as ocr:
        text,evidence=tools.recover_missing_display_regions(original,image,(0,0,1,1),rows,'ocr',ImageOps,page_number=1)
    assert text==expected+original
    assert evidence['recovered_count']==bool(expected)
    assert ocr.call_count==1


def test_display_recovery_never_reocrs_ordinary_prose_or_later_pages():
    with mock.patch.object(tools,'_ocr_photographed_crop') as ocr:
        for text,page in [('This is ordinary mixed case prose.',1),('ONE TWO THREE FOUR FIVE',3)]:
            result,evidence=tools.recover_missing_display_regions(text,None,None,[{}],'ocr',None,page_number=page)
            assert result==text
        ocr.assert_not_called()
