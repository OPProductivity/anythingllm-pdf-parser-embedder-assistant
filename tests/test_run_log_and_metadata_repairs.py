import json
from pathlib import Path

import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('title', ['No Job Name', 'OP-MELU180025 86..102'])
def test_producer_placeholder_titles_are_not_metadata_titles(title):
    result = pipeline.resolve_title_from_metadata_or_filename(title, Path('Real Article Title.pdf'))
    assert result == {'title': 'Real Article Title', 'source': 'filename_fallback'}
    assert pipeline.resolve_title_from_metadata_or_filename(title, Path('x.pdf'), title_override=title)['title'] == title


def test_title_fragment_cannot_become_gans_author_after_placeholder_filter():
    path = Path('whitening-and-the-changing-american-racial-hierarchy.pdf')
    title = pipeline.resolve_title_from_metadata_or_filename('No Job Name', path)['title']
    samples = [{'page': 1, 'text': 'STATE OF THE DISCIPLINE\nWhitening and the Changing\n'
                'American Racial Hierarchy\nHerbert J. Gans\nDepartment of Sociology, Columbia University\nAbstract'}]
    report = pipeline.infer_author_from_samples_or_filename(samples, path, title_hint=title)
    assert report['author'] == 'Herbert J. Gans'


def test_catalog_title_does_not_include_author_or_publisher_and_author_requires_visible_credit():
    path = Path("The Racial Middle -- Eileen O'Brien -- University Press, 2008.pdf")
    title = pipeline.resolve_title_from_metadata_or_filename('', path)['title']
    assert title == 'The Racial Middle'
    samples = [{'page': 4, 'text': "The Racial Middle\nLatinos and Asian Americans\n"
                "Living beyond the Racial Divide\nEileen O'Brien\nNew York University Press"}]
    result = pipeline.infer_author_from_samples_or_filename(samples, path, title_hint=title)
    assert result['author'] == "Eileen O'Brien"
    assert pipeline.resolve_author_from_metadata_and_inference('', result)['author'] == "Eileen O'Brien"
    uncorroborated = pipeline.infer_author_from_samples_or_filename([], path, title_hint=title)
    assert not pipeline.resolve_author_from_metadata_and_inference('', uncorroborated)['author']


def test_preflight_preserves_more_than_96_events_once_with_source_names_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'TIMING_MODEL_EVENTS_PATH', tmp_path / 'global-events.jsonl')
    monkeypatch.setattr(app, 'TIMING_MODEL_EVENTS_CSV', tmp_path / 'global-events.csv', raising=False)
    rows = [{'state': 'coverage_complete', 'elapsed_seconds': index / 10,
             'source_index': index // 6 + 1, 'source_total': 20, 'source_name': f'PDF {index // 6 + 1}.pdf',
             'page_count': 100, 'risk': '', 'coverage_origin': 'inspection'} for index in range(120)]
    app.persist_confirmation_preflight(tmp_path, 15.5, rows)
    record = json.loads((tmp_path / 'confirmation-preflight.json').read_text())
    assert record['event_count'] == 120
    assert len(record['events']) == 120
    assert record['sources_checked'] == 20
    assert all('source_name' not in event for event in record['events'])
    assert record['sources']['20']['name'] == 'PDF 20.pdf'
    event = json.loads((tmp_path / 'timing-evidence-timeline.jsonl').read_text().splitlines()[-1])
    assert event['source_progress_events'] == 120
    assert event['sources_checked'] == 20
