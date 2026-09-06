import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import auto_anythingllm_pipeline as pipeline
import rag_pdf_tools as tools
import anythingllm_source_atomic_worker as worker
from anythingllm_source_atomic_server import SOURCE_ATOMIC_SERVER_BODY
from anythingllm_persistence import AnythingLLMPersistenceAdapter
from anythingllm_state import read_env_values

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize(
    "text",
    [
        "2026",
        "$1,234.56",
        "95%",
        "(123.45)",
        "Ελληνική ιστορία",
        "Русская история",
        "Ordinary text",
    ],
)
def test_meaningful_elements_survive(text):
    element = SimpleNamespace(
        text=text, category="NarrativeText", metadata=SimpleNamespace(page_number=1)
    )
    pages, rows = tools._unstructured_elements_to_pages([element])
    assert text in pages[0]["text"]
    assert rows[0]["content_decision"] != "dropped_symbol_noise"


@pytest.mark.parametrize("text", ["", "{7", "“4 ‘cu: ™", "---", "@#"])
def test_noise_filter_remains(text):
    assert tools._unstructured_is_symbol_noise(text)


def test_spread_conserves_short_rows_in_both_halves():
    rows = []
    for i in range(12):
        for x0, x1 in [(100, 520), (480, 920)]:
            rows.append(
                dict(
                    text=f"Ordinary academic prose with enough letters {i}",
                    x0=x0,
                    x1=x1,
                    y0=120 + i * 25,
                    y1=132 + i * 25,
                )
            )
    rows += [
        dict(text=text, x0=x, x1=x + 80, y0=450, y1=462)
        for text, x in [("It matters.", 100), ("So does this.", 800)]
    ]
    rows += [dict(text="Central heading", x0=300, x1=700, y0=60, y1=72)]
    ordered, mode, regions = pipeline._layout_reading_order(rows, 1000, 700)
    assert mode == "photographed_spread_column_first"
    assert sorted(map(id, ordered)) == sorted(map(id, rows))
    assert "So does this." in regions[1]["text"]
    assert "It matters." in regions[0]["text"]


def ready():
    return dict(
        readiness_status="ready",
        api_upload_status="skipped_prepare_only",
        post_upload_verification_status="not_checked_no_upload",
        anythingllm_runtime_validation_status="not_checked_no_upload",
    )


@pytest.mark.parametrize("preexisting", [True, False])
def test_cleanup_honours_ownership(tmp_path, preexisting):
    folder = tmp_path / "inspection"
    folder.mkdir()
    item = folder / "notes.txt"
    item.write_text("notes")
    source = tmp_path / "new-pdf-parsed.txt"
    source.write_text("text")
    result = pipeline.retain_successful_run_leanly(
        tmp_path,
        ready(),
        {},
        source,
        preexisting_children={"inspection"} if preexisting else set(),
    )
    assert result["applied"]
    assert item.exists() == preexisting


def test_flat_export_does_not_rename_old_segments(tmp_path):
    old = tmp_path / "old-p001-s01.txt"
    old.write_text("old text")
    source = tmp_path / "new-pdf-parsed.txt"
    source.write_text("new text")
    result = pipeline.retain_successful_run_without_logs(
        tmp_path,
        ready(),
        {"filename": "new.pdf"},
        source,
        segments=[{"pdf_page": 1, "text": "new chunk"}],
    )
    assert result["retained_segment_files"] == 1
    assert old.read_text() == "old text"
    assert (tmp_path / "new-local-p001-s01.txt").read_text() == "new chunk"


@pytest.mark.parametrize("cached_pages", [{1, 2}, {2}, set()])
def test_checkpoint_neighbours_resolved_after_assembly(tmp_path, cached_pages):
    source = tmp_path / "mock.pdf"
    source.write_bytes(b"mock; opening intercepted")
    fragment = "A sufficiently long neighbouring fragment containing many distinctive academic words and concepts."

    def row(n):
        if n == 2:
            return {"page": 2, "text": fragment}
        return {
            "page": 1,
            "text": "body plus neighbour",
            "reading_regions": [{"text": "body plus neighbour"}],
            "_neighbour_page_runover_candidate": {
                "narrow_text": fragment,
                "dominant_text": "body",
                "dominant_crop_fraction": [0, 0, 1, 1],
            },
        }

    class Document:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __len__(self):
            return 2

    def load(*args, page_numbers=None, **kwargs):
        if page_numbers and len(page_numbers) == 1 and page_numbers[0] in cached_pages:
            return [row(page_numbers[0])], 2, []
        return None

    def fresh(*args, page_numbers=None, resolve_neighbours=True, **kwargs):
        assert not resolve_neighbours
        return [row(n) for n in page_numbers], 2, []

    with (
        patch.object(tools.fitz, "open", return_value=Document()),
        patch.object(tools, "load_unstructured_ocr_checkpoint", side_effect=load),
        patch.object(tools, "save_unstructured_ocr_checkpoint", return_value=None),
        patch.object(tools, "_parallel_unstructured_ocr_pages", side_effect=fresh),
    ):
        pages, _, _ = tools.get_pages_with_unstructured(
            source,
            "ocr_only",
            runtime_probe={},
            checkpoint_dir=tmp_path,
            page_numbers=[1, 2],
        )
    assert pages[0]["text"] == "body"
    assert "_neighbour_page_runover_candidate" not in pages[0]


def test_activation_requires_all_observed_roots_newer():
    result = SimpleNamespace(
        returncode=0, stdout='["2026-09-06T09:00:00+00:00","2026-09-06T11:00:00+00:00"]'
    )
    from datetime import datetime, timezone

    with (
        patch.object(worker, "os", SimpleNamespace(name="nt")),
        patch.object(worker.subprocess, "run", return_value=result) as run,
    ):
        active, _ = worker._desktop_root_started_after(
            Path("AnythingLLM.exe"),
            datetime(2026, 9, 6, 10, tzinfo=timezone.utc).timestamp(),
        )
    assert active is False
    assert "--type=" in run.call_args.args[0][-1]
    assert "CommandLine" in run.call_args.args[0][-1]


def test_duplicate_settings_agree_with_reader(tmp_path):
    env = tmp_path / ".env"
    env.write_text('EMBEDDING_ENGINE="first"\nEMBEDDING_ENGINE="last"\n')
    report = {
        "matched_profile": "fixture",
        "capabilities": {"can_write_env_settings": {"status": "supported"}},
    }
    with patch("anythingllm_persistence.characterize", return_value=report):
        result = AnythingLLMPersistenceAdapter(
            tmp_path, "fixture", tmp_path / "snapshots"
        ).write_env_setting("EMBEDDING_ENGINE", "requested")
    assert result["status"] == "verified"
    assert read_env_values(env)["EMBEDDING_ENGINE"] == "requested"
    assert "last" not in env.read_text()


def node(script, payload):
    if not shutil.which("node"):
        pytest.skip("Node required for exact injected JavaScript tests")
    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "vectors,ok",
    [
        ([[1, 2], [3, 4]], True),
        ([[], []], False),
        ([[1], [2, 3]], False),
        ([["1"], [2]], False),
    ],
)
def test_provider_shape_validation(vectors, ok):
    script = r"""const p=JSON.parse(require('fs').readFileSync(0,'utf8')),F=Object.getPrototypeOf(async()=>{}).constructor;
    const f=new F('l','__sourceAtomicEmit',p.helper+';return __sourceAtomicEmbedBatch(["a","b"],{});');
    let calls=0;f({openai:{embeddings:{create:async()=>{calls++;return {data:p.vectors.map(embedding=>({embedding}))}}}}},()=>{}).then(()=>console.log(JSON.stringify({ok:true,calls}))).catch(()=>console.log(JSON.stringify({ok:false,calls})));"""
    result = node(
        script,
        {"helper": worker.SOURCE_ATOMIC_PROVIDER_POLICY_HELPER, "vectors": vectors},
    )
    assert result == {"ok": ok, "calls": 1}


@pytest.mark.parametrize(
    "failure", ["none", "namespace", "sqlite", "throw", "precommit"]
)
def test_server_commit_stops_and_accounts_for_every_record(failure):
    script = r"""const p=JSON.parse(require('fs').readFileSync(0,'utf8')),F=Object.getPrototypeOf(async()=>{}).constructor;let events=[],attempted=[];
    const FM=()=>({addDocumentToNamespace:async(s,d,f)=>{attempted.push(f);if(p.failure==='throw')throw Error('failed');return {vectorized:p.failure!=='namespace',error:'failed'}}});
    const Q=()=>({fileData:async f=>({docSource:f==='c'?'two':'one',pageContent:'text'}),storeVectorResult:async()=>{},cachedVectorInformation:async f=>{if(p.failure==='precommit'&&f==='a')throw Error('cannot stage');return {exists:true}}});
    const ir={workspace_documents:{create:async()=>{if(p.failure==='sqlite')throw Error('db')}}};
    const f=new F('s','e','t','FM','Q','ra','x','P','Xt','o8','ir','a8','jM','c8',p.body);
    f({slug:'test',id:1},['a','b','c'],null,FM,Q,()=>({emitProgress:(s,e)=>events.push(e)}),()=>({SystemSettings:{getValueOrFallback:async()=>100}}),()=>({getEmbeddingEngineSelection:()=>({})}),()=>({TextSplitter:class{static determineMaxChunkSize(){return 100}}}),()=> 'id',ir,{sendTelemetry:async()=>{}},{logEvent:async()=>{}},()=> '').then(receipt=>console.log(JSON.stringify({receipt,attempted,committed:events.filter(e=>e.type==='source_committed').length}))).catch(e=>{console.error(e);process.exitCode=1});"""
    result = node(script, {"body": SOURCE_ATOMIC_SERVER_BODY, "failure": failure})
    assert sorted(
        result["receipt"]["embedded"] + result["receipt"]["failedToEmbed"]
    ) == ["a", "b", "c"]
    assert result["attempted"] == (
        ["a", "b", "c"]
        if failure == "none"
        else ["c"]
        if failure == "precommit"
        else ["a"]
    )
    assert result["committed"] == (
        2 if failure == "none" else 1 if failure == "precommit" else 0
    )


@pytest.mark.parametrize(
    "case,ok",
    [
        ("ordered", True),
        ("reversed", False),
        ("duplicate", False),
        ("nonfinite", False),
    ],
)
def test_provider_identity_and_finiteness_validation(case, ok):
    script = r"""const p=JSON.parse(require('fs').readFileSync(0,'utf8')),F=Object.getPrototypeOf(async()=>{}).constructor;
    const f=new F('l','__sourceAtomicEmit',p.helper+';return __sourceAtomicEmbedBatch(["a","b"],{});');
    let calls=0;const data=[{index:0,embedding:[1,2]},{index:1,embedding:[3,4]}];
    if(p.case==='reversed')data.reverse();if(p.case==='duplicate')data[1].index=0;if(p.case==='nonfinite')data[1].embedding[0]=NaN;
    f({openai:{embeddings:{create:async()=>{calls++;return {data}}}}},()=>{}).then(()=>console.log(JSON.stringify({ok:true,calls}))).catch(()=>console.log(JSON.stringify({ok:false,calls})));"""
    assert node(
        script, {"helper": worker.SOURCE_ATOMIC_PROVIDER_POLICY_HELPER, "case": case}
    ) == {"ok": ok, "calls": 1}
