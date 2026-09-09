from pathlib import Path
import pytest
import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic

@pytest.mark.parametrize('flat',[False,True])
@pytest.mark.parametrize('mode,expected_count',[('none',1),('passages',3)])
def test_whole_file_retains_one_text_but_snippets_remain(tmp_path,flat,mode,expected_count):
    selected=tmp_path/'selected'
    selected.mkdir()
    parsed=selected/'Example-pdf-parsed.txt'
    parsed.write_text('First part.\n\nSecond part.',encoding='utf8')
    summary={'readiness_status':'ready','api_upload_status':'skipped_prepare_only',
             'post_upload_verification_status':'not_checked_no_upload',
             'anythingllm_runtime_validation_status':'not_checked_no_upload',
             'segment_mode':mode,'upload_file':str(parsed)}
    profile={'filename':'Example.pdf','source_file':'Example.pdf','source_sha256':'a'*64}
    fn=pipeline.retain_successful_run_without_logs if flat else pipeline.retain_successful_run_leanly
    result=fn(tmp_path,summary,profile,parsed,segments=[{'pdf_page':1,'text':'First part.'},{'pdf_page':2,'text':'Second part.'}],preexisting_children=())
    assert result['applied']
    assert len(list(tmp_path.rglob('*.txt')))==expected_count
    assert Path(result['prepared_text']).read_text(encoding='utf8')=='First part.\n\nSecond part.'

def test_unresolved_whole_file_keeps_evidence(tmp_path):
    parsed=tmp_path/'source.txt'
    parsed.write_text('text',encoding='utf8')
    result=pipeline.retain_successful_run_leanly(tmp_path,{'readiness_status':'needs_review','segment_mode':'none'}, {},parsed)
    assert not result['applied'] and parsed.exists()
