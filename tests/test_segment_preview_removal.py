import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app


pytestmark = pytest.mark.offline_deterministic


def test_preview_controls_and_dedicated_readers_are_removed():
    labels = {component['props'].get('label') for component in app.demo.config['components']}
    assert not labels.intersection({'Segment preview', 'Segment number', 'Segment text', 'AnythingLLM storage match'})
    assert 'Open Output Folder' not in labels
    for name in ('selected_manifest_path', 'preview_manifest_segment', 'preview_workspace_segment',
                 'navigate_manifest_segment', 'navigate_manifest_segment_with_storage'):
        assert not hasattr(app, name)
    assert not hasattr(pipeline, 'workspace_segment_preview')


def test_existing_output_action_has_no_accordion_wrapper():
    components = app.demo.config['components']
    button = next(component for component in components if component['props'].get('elem_id') == 'open-generated-output-button')
    assert button['props']['value'] == 'Open Output Folder'

    def ancestors(node, identity, parents=()):
        if node.get('id') == identity:
            return parents
        for child in node.get('children', []):
            found = ancestors(child, identity, (*parents, node['id']))
            if found is not None:
                return found
        return None

    parents = ancestors(app.demo.config['layout'], button['id'])
    assert parents is not None
    assert not any(component['type'] == 'accordion' and component['id'] in parents for component in components)
    actions = [fn for fn in app.demo.fns.values() if fn.fn is app.open_generated_output_directory]
    assert len(actions) == 1
    assert len(actions[0].inputs) == 2


@pytest.mark.parametrize('status,selection', [({}, None), ({'state': 'running'}, None),
                                            ({'state': 'preparing', 'run_root': 'owned'}, None),
                                            ({'state': 'successful'}, {'state': 'ready'})])
def test_reset_output_arity_matches_all_registered_callbacks(monkeypatch, status, selection):
    monkeypatch.setattr(app, 'LIVE_AUTOMATIC_RUN_STATUS', status)
    updates = app.reset_automatic_run_presentation([], [], selection)
    assert len(updates) == 16
    callbacks = [fn for fn in app.demo.fns.values() if fn.fn is app.reset_automatic_run_presentation]
    assert callbacks and all(len(fn.outputs) == len(updates) for fn in callbacks)
