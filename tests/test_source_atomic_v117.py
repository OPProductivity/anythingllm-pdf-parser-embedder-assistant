import hashlib
import json
import subprocess

import pytest

import anythingllm_source_atomic_v117 as adapter
import auto_anythingllm_pipeline as pipeline
from anythingllm_compatibility import OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS, V117_NATIVE_CONTRACT_ID

pytestmark = pytest.mark.offline_deterministic


def fixture_source():
    return (
        'const Tc={addDocuments:async function(s,e=[],t=null){legacyCalls++;return{embedded:e}},'
        'removeDocuments:async function(){}};'
        'async function api(e){let a={id:1,slug:"test"},n=["one","two","three"];'
        'return await Tc.addDocuments(a,n)};'
        'class Collector{async processDocument(e="",t=null,r={}){let n=JSON.stringify(r);return new Xs().xPayload}async processLink(){}};'
        'const browserRoute="UI_NATIVE_ROUTE_UNCHANGED";'
    )


def report(executable):
    return {"status": "pass", "characterization": {
        "desktop_executable": str(executable), "desktop_version_normalized": "1.17.0",
        "native_mutation_contract": V117_NATIVE_CONTRACT_ID,
        "desktop_package": {"app_asar_sha256": OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS["1.17.0"]},
    }}


def setup_install(tmp_path, monkeypatch):
    target = tmp_path / "resources/backend/server.js"
    target.parent.mkdir(parents=True)
    source = fixture_source().encode()
    target.write_bytes(source)
    monkeypatch.setattr(adapter, "V117_SERVER_SHA256", hashlib.sha256(source).hexdigest())
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")
    return target, executable, source


def test_installer_requires_restart_and_preserves_pristine_backup(tmp_path, monkeypatch):
    target, executable, source = setup_install(tmp_path, monkeypatch)
    installed = adapter.ensure_v117_embedding_server(report(executable))
    assert installed["installed"] and installed["restart_required"] and not installed["enabled"]
    assert target.with_name(target.name + ".pdf-assistant-v117.backup").read_bytes() == source
    monkeypatch.setattr(adapter, "_activation_state_for_installed_worker", lambda *_: (True, "", False))
    active = adapter.ensure_v117_embedding_server(report(executable))
    assert active["enabled"] and not active["restart_required"]
    assert active["status"] == "already_enabled"


@pytest.mark.parametrize("field,value", [
    ("desktop_version_normalized", "1.17.1"), ("native_mutation_contract", "unknown"),
    ("desktop_package", {"app_asar_sha256": "wrong"}),
])
def test_authority_mismatch_is_noop(tmp_path, monkeypatch, field, value):
    target, executable, source = setup_install(tmp_path, monkeypatch)
    authority = report(executable)
    authority["characterization"][field] = value
    outcome = adapter.ensure_v117_embedding_server(authority)
    assert not outcome["installed"] and not outcome["enabled"]
    assert target.read_bytes() == source
    assert len(list(target.parent.iterdir())) == 1


def test_unknown_backend_or_patched_bytes_are_never_overwritten(tmp_path, monkeypatch):
    target, executable, _ = setup_install(tmp_path, monkeypatch)
    target.write_bytes(b"unknown backend")
    assert adapter.ensure_v117_embedding_server(report(executable))["status"] == "disabled"
    assert target.read_bytes() == b"unknown backend"


def test_changed_installed_patch_is_refused(tmp_path, monkeypatch):
    target, executable, _ = setup_install(tmp_path, monkeypatch)
    adapter.ensure_v117_embedding_server(report(executable))
    changed = target.read_bytes() + b"/*unrecognized edit*/"
    target.write_bytes(changed)
    result = adapter.ensure_v117_embedding_server(report(executable))
    assert result["reason"] == "source_atomic_server_hash_mismatch"
    assert target.read_bytes() == changed


def probe(monkeypatch, *, flag=True, engine="openrouter", scenario="fresh", direct=False, native_timeout=None):
    source = fixture_source()
    monkeypatch.setattr(adapter, "V117_SERVER_SHA256", hashlib.sha256(source.encode()).hexdigest())
    patched = adapter.patch_v117_server_source(source)
    assert patched.endswith('const browserRoute="UI_NATIVE_ROUTE_UNCHANGED";')
    setup = r'''
const scenario=SCENARIO,events=[],calls=[],committed=[],stored=[],docs=new Map;let legacyCalls=0,id=0;
const cQ=()=>`id-${++id}`,Ao=e=>e.body;
for(const r of [{name:"one",source:"A",text:"alpha|beta"},{name:"two",source:"A",text:"gamma"},{name:"three",source:"B",text:"delta"}])docs.set(r.name,{pageContent:r.text,docSource:r.source,title:r.name});
if(scenario==="wide")docs.get("one").pageContent=Array.from({length:79},(_,i)=>`chunk-${i}`).join("|");
const cached=new Map;
if(scenario==="cached"||scenario==="mixed")cached.set("one",[[{values:[1,2],metadata:{text:"alpha"}},{values:[2,3],metadata:{text:"beta"}}]]);
if(scenario==="cached")for(const name of ["two","three"])cached.set(name,[[{values:[1,2],metadata:{text:docs.get(name).pageContent}}]]);
const V=()=>({fileData:async n=>docs.get(n),cachedVectorInformation:async n=>({exists:cached.has(n),chunks:cached.get(n)||[]}),storeVectorResult:async(c,n)=>{stored.push(n);cached.set(n,c)}});
const rx=()=>({addDocumentToNamespace:async(ns,r,n)=>{if(scenario==="commit_fail"&&n==="two")return{vectorized:false,error:"commit failure"};if(!cached.has(n))throw Error("provider cache missing");committed.push(n);return{vectorized:true}}});
const da=()=>({emitProgress:(s,e)=>events.push(e)}),N=()=>({SystemSettings:{getValueOrFallback:async()=>750}});
const O=()=>({getEmbeddingEngineSelection:()=>({model:"fixture",openai:{embeddings:{create:async request=>{calls.push(request.input);if(scenario==="provider_fail"&&request.input.includes("alpha")){const e=new Error("invalid input");e.status=400;throw e}if((scenario==="retry"&&calls.length===1)||scenario==="retry_fail"){const e=new Error("retryable");e.status=503;throw e}const rows=request.input.map((text,i)=>({embedding:[i+1,i+2],index:i}));if(scenario==="bad_index")rows[0].index=99;if(scenario==="missing_index")delete rows[0].index;if(scenario==="bad_dimension"&&rows.length>1)rows[rows.length-1].embedding=[1];if(scenario==="reordered")rows.reverse();return{data:rows}}}}})});
class Splitter{static determineMaxChunkSize(){return 750}static buildHeaderMeta(){return ""}async splitText(s){return s.split("|")}}
const tr=()=>({TextSplitter:Splitter}),mr={workspace_documents:{create:async()=>{}}},lQ={sendTelemetry:async()=>{}},nx={logEvent:async()=>{}},dQ=()=>"fixture";
'''.replace("SCENARIO", json.dumps(scenario))
    call = 'Tc.addDocuments({id:1,slug:"test"},["one","two","three"])' if direct else 'api({body:FLAG})'.replace("FLAG", json.dumps({adapter.REQUEST_FLAG: flag}))
    script = f"process.env.EMBEDDING_ENGINE={json.dumps(engine)};" + setup + patched
    script += f"process.env.ANYTHINGLLM_FETCH_TIMEOUT={json.dumps(native_timeout)};" if native_timeout is not None else "delete process.env.ANYTHINGLLM_FETCH_TIMEOUT;"
    script += call + '.then(result=>console.log(JSON.stringify({result,events,calls,committed,stored,legacyCalls,cached:Array.from(cached.entries())}))).catch(e=>{console.error(e);process.exitCode=1});'
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("flag,engine,direct", [(False, "openrouter", False), ("true", "openrouter", False), (True, "ollama", False), (True, "openrouter", True)])
def test_unmarked_non_openrouter_and_direct_clients_keep_legacy_route(monkeypatch, flag, engine, direct):
    result = probe(monkeypatch, flag=flag, engine=engine, direct=direct)
    assert result["legacyCalls"] == 1 and not result["calls"] and not result["stored"]


@pytest.mark.parametrize("scenario,inputs,cached_records", [("fresh", 4, 0), ("cached", 0, 3), ("mixed", 2, 1)])
def test_cache_first_staging(monkeypatch, scenario, inputs, cached_records):
    result = probe(monkeypatch, scenario=scenario)
    assert sum(len(call) for call in result["calls"]) == inputs
    assert result["committed"] == ["one", "two", "three"] and not result["legacyCalls"]
    assert len([e for e in result["events"] if e["type"] == "source_staging_cache_resolved"]) == cached_records
    assert result["result"]["embedded"] == ["one", "two", "three"]


def test_provider_rejection_is_source_local_and_precommit(monkeypatch):
    result = probe(monkeypatch, scenario="provider_fail")
    assert result["committed"] == ["three"] and result["stored"] == ["three"]
    assert result["result"]["failedToEmbed"] == ["one", "two"]


def test_commit_ambiguity_stops_later_sources(monkeypatch):
    result = probe(monkeypatch, scenario="commit_fail")
    assert result["committed"] == ["one"] and result["stored"] == ["one", "two"]
    assert result["result"]["failedToEmbed"] == ["two", "three"]
    assert len(result["calls"]) == 1


@pytest.mark.parametrize("scenario", ["bad_index", "bad_dimension", "missing_index"])
def test_invalid_vectors_cannot_write_cache_or_namespace(monkeypatch, scenario):
    result = probe(monkeypatch, scenario=scenario)
    expected_committed = ["three"] if scenario == "bad_dimension" else []
    assert result["committed"] == expected_committed and result["stored"] == expected_committed
    assert result["result"]["failedToEmbed"] == (["one", "two"] if expected_committed else ["one", "two", "three"])


def test_adapter_does_not_add_retries_on_top_of_desktop_sdk(monkeypatch):
    result = probe(monkeypatch, scenario="retry")
    assert len(result["calls"]) == 2  # one SDK call for A, one for B
    assert result["committed"] == ["three"]
    assert not any(e["type"] == "source_staging_provider_batch_retrying" for e in result["events"])
    attempts = [e for e in result["events"] if e["type"] == "source_staging_provider_batch_attempt"]
    assert all(e["maximum_attempts"] == 1 and e["request_timeout_ms"] == 600_000 for e in attempts)


def test_multi_chunk_record_crosses_provider_cap_without_crossing_source(monkeypatch):
    result = probe(monkeypatch, scenario="wide")
    assert [len(call) for call in result["calls"]] == [36, 36, 8, 1]
    assert result["committed"] == ["one", "two", "three"]
    assert result["stored"] == ["one", "two", "three"]
    assert [e["chunkCount"] for e in result["events"] if e["type"] == "source_staging_record"] == [79, 1, 1]


def test_exhausted_retry_cannot_commit_any_source(monkeypatch):
    result = probe(monkeypatch, scenario="retry_fail")
    assert len(result["calls"]) == 2 and not result["stored"] and not result["committed"]
    assert result["result"]["failedToEmbed"] == ["one", "two", "three"]


def test_valid_reordered_provider_rows_are_mapped_by_index(monkeypatch):
    result = probe(monkeypatch, scenario="reordered")
    assert result["committed"] == ["one", "two", "three"]
    rows = dict(result["cached"])["one"][0]
    assert [(row["metadata"]["text"], row["values"]) for row in rows] == [("alpha", [1, 2]), ("beta", [2, 3])]


@pytest.mark.parametrize("url,sources,marked", [
    ("http://127.0.0.1:3001", ["A.pdf", "A.pdf"], True),
    ("http://127.0.0.1:3001", ["A.pdf", "B.pdf"], False),
    ("http://127.0.0.1:3001", ["", ""], False),
    ("https://remote.example", ["A.pdf", "A.pdf"], False),
])
def test_production_post_marks_only_local_cross_record_sources(monkeypatch, url, sources, marked):
    bodies = []

    class Tracker:
        def wait(self, timeout=None):
            return True

        def outcome(self):
            return {"kind": "http_response", "status": 200, "response_text": "{}"}

    def tracker(_url, body, **_kwargs):
        bodies.append(body)
        return Tracker()

    monkeypatch.setattr(pipeline, "start_json_post_response_tracker", tracker)
    result = pipeline._update_workspace_embeddings_batched_serial(
        url, "key", "test", ["one", "two"], batch_size=2, warmup_batch_size=0,
        warmup_batch_count=0, receipt_observer=lambda *_: None,
        location_sources=[{"location": location, "source_path": source} for location, source in zip(["one", "two"], sources)],
    )
    assert result["accepted"] == 2 and len(bodies) == 1
    assert bodies[0].get(adapter.REQUEST_FLAG, False) is marked


@pytest.mark.parametrize("value,expected", [("180000", 180000), ("invalid", 600000), ("0", 600000), ("-1", 600000)])
def test_timeout_selection_matches_native_sdk(monkeypatch, value, expected):
    result = probe(monkeypatch, native_timeout=value)
    attempts = [e for e in result["events"] if e["type"] == "source_staging_provider_batch_attempt"]
    assert attempts and all(e["request_timeout_ms"] == expected for e in attempts)
    assert result["committed"] == ["one", "two", "three"]
