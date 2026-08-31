"""Guarded source-atomic OpenRouter worker integration for Desktop v1.16.1."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from anythingllm_compatibility import (
    OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS,
    V1161_NATIVE_CONTRACT_ID,
)


SOURCE_ATOMIC_PATCH_ID = "anythingllm_pdf_assistant_source_atomic_v3"
SOURCE_ATOMIC_LEGACY_PATCH_ID = "anythingllm_pdf_assistant_source_atomic_v1"
SOURCE_ATOMIC_PREVIOUS_PATCH_ID = "anythingllm_pdf_assistant_source_atomic_v2"
SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE = 36
SOURCE_ATOMIC_MAX_PROVIDER_BATCH_SIZE = 64
V1161_EMBEDDING_WORKER_SHA256 = (
    "fec9f180920d42429e482452931513c7d635cca29371cb2a96d2cf65509afbd4"  # pragma: allowlist secret
)
WORKER_FUNCTION_PREFIX = "async function ah(){"
WORKER_FUNCTION_FOLLOWER = 'process.on("message",async s=>{'
SOURCE_ATOMIC_OPENROUTER_GATE = (
    'String(process.env.EMBEDDING_ENGINE||"")'
    ".replace(/^['\"]|['\"]$/g,\"\").trim().toLowerCase()===\"openrouter\""
)
SOURCE_ATOMIC_LEGACY_OPENROUTER_GATE = 'process.env.EMBEDDING_ENGINE==="openrouter"'
SOURCE_ATOMIC_GATE_PREAMBLE = r'''
let __sourceAtomicConfiguredEngine=String(process.env.EMBEDDING_ENGINE||"").replace(/^['"]|['"]$/g,"").trim().toLowerCase();
xr({type:"source_atomic_gate_observed",workspaceSlug:on,filename:Nr[0]||"",configuredEngine:__sourceAtomicConfiguredEngine,enabled:__sourceAtomicConfiguredEngine==="openrouter"});
'''


# Filled below with the OpenRouter-only branch that is inserted inside the
# known worker's ``ah`` function. Its non-OpenRouter branch stays legacy.
SOURCE_ATOMIC_OPENROUTER_BODY = r'''
if(Jo||Nr.length===0)return;
Jo=!0;
let s=hW(),e=[...Nr];Nr.length=0;
xr({type:"batch_starting",workspaceSlug:on,userId:Ts,filenames:e,totalDocs:e.length});
mW.sendTelemetry("documents_embedded_in_workspace").catch(()=>{});
let t=[],r=[],n=new Map;
for(let o of e){
  if(ih.has(o)){ih.delete(o);continue}
  let i=await pW(o);
  if(!i){xr({type:"doc_failed",workspaceSlug:on,userId:Ts,filename:o,error:"Failed to load file data"});r.push(o);continue}
  let a=String(i.docSource||("file:"+o));n.has(a)||n.set(a,[]),n.get(a).push({filename:o,raw:i});
}
let {SystemSettings:c}=j(),l=P().getEmbeddingEngineSelection(),u=it().TextSplitter,
  d=u.determineMaxChunkSize(await c.getValueOrFallback({label:"text_splitter_chunk_size"}),l?.embeddingMaxChunkLength),
  p=await c.getValueOrFallback({label:"text_splitter_chunk_overlap"},20),m=Z().storeVectorResult;
for(let [f,y] of n){
  let __sourceAtomicStarted=Date.now(),__sourceAtomicBatchSize=Math.min(64,Math.max(1,Number.parseInt(process.env.SOURCE_ATOMIC_EMBED_BATCH_SIZE||"36",10)||36)),__sourceAtomicFilename=y[0]?.filename||"";
  xr({type:"source_staging_started",workspaceSlug:on,sourceKey:f,filename:__sourceAtomicFilename,recordCount:y.length,provider_batch_size:__sourceAtomicBatchSize,concurrency:1});
  let _=new Array(y.length),v=null;
  try{
    let A=[];
    for(let S=0;S<y.length;S++){
      let T=y[S],{pageContent:L,...I}=T.raw,O=await new u({chunkSize:d,chunkOverlap:p,chunkHeaderMeta:u.buildHeaderMeta(I),chunkPrefix:l?.embeddingPrefix}).splitText(L);
      A.push({recordIndex:S,entry:T,metadata:I,texts:O,vectors:new Array(O.length)})
    }
    let S=A.flatMap(T=>T.texts.map((L,I)=>({item:T,chunkIndex:I,text:L})));
    for(let T=0;T<S.length;T+=__sourceAtomicBatchSize){
      let L=S.slice(T,T+__sourceAtomicBatchSize),I=Math.floor(T/__sourceAtomicBatchSize),O=Date.now(),V=await l.embedChunks(L.map(H=>H.text));
      if(!V||V.length!==L.length||!V.every(H=>Array.isArray(H)))throw new Error("embedding response did not match source-atomic batch");
      xr({type:"source_staging_provider_batch",workspaceSlug:on,sourceKey:f,filename:__sourceAtomicFilename,batchIndex:I,chunkCount:L.length,recordCount:y.length,elapsed_ms:Date.now()-O,provider_batch_size:__sourceAtomicBatchSize});
      for(let H=0;H<L.length;H++)L[H].item.vectors[L[H].chunkIndex]=V[H]
    }
    for(let T of A){
      _[T.recordIndex]={entry:T.entry,chunks:T.vectors.map((L,I)=>({id:uW(),values:L,metadata:{...T.metadata,text:T.texts[I]}}))};
      xr({type:"source_staging_record",workspaceSlug:on,sourceKey:f,filename:T.entry.filename,recordIndex:T.recordIndex,chunkCount:T.texts.length,elapsed_ms:Date.now()-__sourceAtomicStarted})
    }
  }catch(A){v={error:A?.message||String(A)}}
  xr({type:"source_staging_finished",workspaceSlug:on,sourceKey:f,filename:__sourceAtomicFilename,recordCount:y.length,elapsed_ms:Date.now()-__sourceAtomicStarted,success:v===null,provider_batch_size:__sourceAtomicBatchSize});
  if(v!==null){
    for(let A of y){xr({type:"doc_failed",workspaceSlug:on,userId:Ts,filename:A.filename,error:"Source rejected before namespace commit: "+v.error});r.push(A.raw?.title||A.filename)}
    xr({type:"source_rejected_before_commit",workspaceSlug:on,sourceKey:f,filename:__sourceAtomicFilename,error:v.error});continue;
  }
  try{for(let A of _)await m([A.chunks],A.entry.filename)}catch(A){
    let S=A?.message||String(A);for(let T of y){xr({type:"doc_failed",workspaceSlug:on,userId:Ts,filename:T.filename,error:"Source cache staging failed before namespace commit: "+S});r.push(T.raw?.title||T.filename)}
    xr({type:"source_rejected_before_commit",workspaceSlug:on,sourceKey:f,filename:__sourceAtomicFilename,error:S});continue;
  }
  for(let A of _){
    let S=A.entry,T=S.raw,L=uW(),{pageContent:I,...O}=T,V={docId:L,filename:S.filename.split(/[/\\]/).pop(),docpath:S.filename,workspaceId:gL,metadata:JSON.stringify(O)},H={workspaceSlug:on,userId:Ts,filename:S.filename,docIndex:t.length+r.length,totalDocs:e.length};
    xr({type:"doc_starting",...H}),global.__embeddingProgress={workspaceSlug:on,filename:S.filename,userId:Ts};
    let {vectorized:te,error:z}=await s.addDocumentToNamespace(on,{...T,docId:L},S.filename);
    if(!te){
      let Q=z||"Unknown error";console.error("Source-atomic namespace commit became ambiguous",O?.title||V.filename,Q),r.push(O?.title||V.filename),xr({type:"doc_failed",...H,error:Q}),xr({type:"source_commit_ambiguous",workspaceSlug:on,sourceKey:f,filename:S.filename,error:Q});
      Jo=!1,global.__embeddingProgress=null,xr({type:"all_complete",workspaceSlug:on,userId:Ts,totalDocs:e.length,embedded:t.length,failed:r.length,embeddedFiles:t,failedFiles:r,error:"Source-atomic commit ambiguity; later sources were not started."}),process.exit(0);return;
    }
    try{await dW.workspace_documents.create({data:V}),t.push(S.filename),xr({type:"doc_complete",...H})}catch(Q){
      console.error(Q.message),r.push(O?.title||V.filename),xr({type:"doc_failed",...H,error:"Failed to save document record"}),xr({type:"source_commit_ambiguous",workspaceSlug:on,sourceKey:f,filename:S.filename,error:Q.message});
      Jo=!1,global.__embeddingProgress=null,xr({type:"all_complete",workspaceSlug:on,userId:Ts,totalDocs:e.length,embedded:t.length,failed:r.length,embeddedFiles:t,failedFiles:r,error:"Source-atomic commit ambiguity; later sources were not started."}),process.exit(0);return;
    }
  }
  xr({type:"source_committed",workspaceSlug:on,sourceKey:f,filename:__sourceAtomicFilename,recordCount:y.length});
}
global.__embeddingProgress=null,Jo=!1;
if(Nr.length>0){await ah();return}
xr({type:"all_complete",workspaceSlug:on,userId:Ts,totalDocs:e.length,embedded:t.length,failed:r.length,embeddedFiles:t,failedFiles:r}),process.exit(0);return;
'''


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _render_v1161_embedding_worker_source(
    source: str,
    *,
    patch_id: str,
    openrouter_gate: str,
    gate_preamble: str = "",
) -> str:
    """Render a precisely anchored hybrid worker from the pristine v1.16.1 file."""
    if any(
        patch_id in source
        for patch_id in (
            SOURCE_ATOMIC_PATCH_ID,
            SOURCE_ATOMIC_LEGACY_PATCH_ID,
            SOURCE_ATOMIC_PREVIOUS_PATCH_ID,
        )
    ):
        raise ValueError("Embedding worker is already patched and cannot be rendered again.")
    start = source.find(WORKER_FUNCTION_PREFIX)
    end = source.find(WORKER_FUNCTION_FOLLOWER, start)
    if start < 0 or end < 0:
        raise ValueError("Expected AnythingLLM v1.16.1 embedding-worker anchors were not found.")
    original_function = source[start:end]
    final_brace = original_function.rfind("}")
    if final_brace < len(WORKER_FUNCTION_PREFIX):
        raise ValueError("Expected AnythingLLM v1.16.1 worker function boundary was not found.")
    legacy_body = original_function[len(WORKER_FUNCTION_PREFIX):final_brace]
    patched_function = (
        f"{WORKER_FUNCTION_PREFIX}"
        f"{gate_preamble}if({openrouter_gate}){{/*{patch_id}*/"
        f"{SOURCE_ATOMIC_OPENROUTER_BODY}}}"
        f"{legacy_body}}}"
    )
    return f"{source[:start]}{patched_function}{source[end:]}"


def patch_v1161_embedding_worker_source(source: str) -> str:
    """Return a hybrid worker whose non-OpenRouter branch remains legacy."""
    if SOURCE_ATOMIC_PATCH_ID in source:
        return source
    return _render_v1161_embedding_worker_source(
        source,
        patch_id=SOURCE_ATOMIC_PATCH_ID,
        openrouter_gate=SOURCE_ATOMIC_OPENROUTER_GATE,
        gate_preamble=SOURCE_ATOMIC_GATE_PREAMBLE,
    )


def _legacy_v1_patched_worker_source(source: str) -> str:
    """Recreate the exact known v1 patch solely to migrate it safely."""
    return _render_v1161_embedding_worker_source(
        source,
        patch_id=SOURCE_ATOMIC_LEGACY_PATCH_ID,
        openrouter_gate=SOURCE_ATOMIC_LEGACY_OPENROUTER_GATE,
    )


def _previous_v2_patched_worker_source(source: str) -> str:
    """Recreate the exact known v2 patch solely to migrate it safely."""
    return _render_v1161_embedding_worker_source(
        source,
        patch_id=SOURCE_ATOMIC_PREVIOUS_PATCH_ID,
        openrouter_gate=SOURCE_ATOMIC_OPENROUTER_GATE,
    )


def _worker_path_from_report(compatibility_report: dict[str, Any]) -> Path | None:
    characterization = dict((compatibility_report or {}).get("characterization") or {})
    executable = Path(str(characterization.get("desktop_executable") or ""))
    if not executable.is_file():
        return None
    return executable.parent / "resources" / "backend" / "jobs" / "embedding-worker.js"


def _desktop_root_started_after(
    executable: Path,
    not_before_epoch: float,
) -> tuple[bool | None, str]:
    """Return whether the exact Desktop root process is newer than a patch.

    The worker module is loaded by Desktop's Node service.  Updating its file
    while Desktop is already running does not establish that a running backend
    has reloaded it.  We therefore use the root process creation time only as
    an activation boundary: a fresh Desktop launch after the patch is required
    before the source-atomic path can be advertised as available.

    ``None`` is deliberately distinct from ``False``.  If Windows process
    inspection itself is unavailable, the caller fails closed instead of
    claiming activation from the file hash alone.
    """
    if os.name != "nt":
        return None, "desktop_restart_detection_requires_windows"
    quoted_executable = str(executable).replace("'", "''")
    command = (
        "$target='" + quoted_executable + "';"
        "$roots=Get-CimInstance Win32_Process -Filter \"Name='AnythingLLM.exe'\" | "
        "Where-Object { $_.ExecutablePath -eq $target } | "
        # Get-CimInstance already materializes CreationDate as DateTime.  The
        # older ManagementDateTimeConverter expects a DMTF string and would
        # otherwise discard every live Desktop process.
        "ForEach-Object { ([datetime]$_.CreationDate).ToUniversalTime().ToString('o') };"
        "$roots | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"desktop_restart_detection_error:{type(exc).__name__}"
    if completed.returncode != 0:
        return None, "desktop_restart_detection_command_failed"
    raw = str(completed.stdout or "").strip()
    if not raw:
        return False, "anythingllm_desktop_not_running"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, "desktop_restart_detection_invalid_output"
    starts = parsed if isinstance(parsed, list) else [parsed]
    try:
        started_epochs = [
            datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            for value in starts
            if str(value).strip()
        ]
    except (TypeError, ValueError):
        return None, "desktop_restart_detection_invalid_timestamp"
    if not started_epochs:
        return False, "anythingllm_desktop_not_running"
    return any(started >= float(not_before_epoch) for started in started_epochs), ""


def _activation_state_for_installed_worker(
    executable: Path | None,
    worker: Path,
    manifest: Path,
) -> tuple[bool, str, bool]:
    """Return ``(active, reason, restart_required)`` for a verified patch."""
    if executable is None or not executable.is_file():
        return False, "desktop_executable_missing_for_restart_check", False
    threshold = worker.stat().st_mtime
    try:
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        threshold = float(manifest_payload.get("restart_required_since_epoch") or threshold)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        # The worker mtime is a conservative fallback for manifests written by
        # an earlier assistant version that lacked the activation marker.
        pass
    restarted, reason = _desktop_root_started_after(executable, threshold)
    if restarted is True:
        return True, "", False
    if restarted is False:
        return False, reason or "anythingllm_desktop_restart_required", True
    return False, reason or "desktop_restart_state_unknown", False


def _qualified_v1161_authority(compatibility_report: dict[str, Any]) -> tuple[bool, str]:
    report = dict(compatibility_report or {})
    characterization = dict(report.get("characterization") or {})
    if str(report.get("status") or "") != "pass":
        return False, "native_mutation_contract_not_qualified"
    if str(characterization.get("desktop_version_normalized") or "") != "1.16.1":
        return False, "desktop_version_is_not_exact_v1_16_1"
    if str(characterization.get("native_mutation_contract") or "") != V1161_NATIVE_CONTRACT_ID:
        return False, "v1_16_1_native_mutation_contract_not_matched"
    package = dict(characterization.get("desktop_package") or {})
    if str(package.get("app_asar_sha256") or "").casefold() != OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS["1.16.1"]:
        return False, "v1_16_1_package_fingerprint_not_matched"
    return True, ""


def ensure_source_atomic_embedding_worker(
    compatibility_report: dict[str, Any],
    *,
    worker_path: Path | None = None,
) -> dict[str, Any]:
    """Install the exact qualified worker patch or leave Desktop unchanged."""
    qualified, reason = _qualified_v1161_authority(compatibility_report)
    target = Path(worker_path) if worker_path else _worker_path_from_report(compatibility_report)
    result: dict[str, Any] = {
        "patch_id": SOURCE_ATOMIC_PATCH_ID,
        "provider": "openrouter",
        "provider_batch_size": SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,
        "max_provider_batch_size": SOURCE_ATOMIC_MAX_PROVIDER_BATCH_SIZE,
        "worker_path": str(target or ""),
        "status": "disabled",
        "reason": reason,
        "enabled": False,
        "installed": False,
        "restart_required": False,
    }
    if not qualified:
        return result
    if target is None or not target.is_file():
        result["reason"] = "v1_16_1_embedding_worker_missing"
        return result
    try:
        current = target.read_bytes()
        current_text = current.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        result["reason"] = f"embedding_worker_read_error:{type(exc).__name__}"
        return result
    current_hash = _sha256_bytes(current)
    backup = target.with_name(f"{target.name}.pdf-assistant-v1161.backup")
    manifest = target.with_name(f"{target.name}.pdf-assistant-source-atomic.json")
    result.update({
        "worker_sha256": current_hash,
        "backup_path": str(backup),
        "manifest_path": str(manifest),
    })
    if (
        SOURCE_ATOMIC_PATCH_ID in current_text
        or SOURCE_ATOMIC_LEGACY_PATCH_ID in current_text
        or SOURCE_ATOMIC_PREVIOUS_PATCH_ID in current_text
    ):
        if not backup.is_file():
            result["reason"] = "source_atomic_worker_backup_missing"
            return result
        try:
            original = backup.read_bytes()
        except OSError as exc:
            result["reason"] = f"source_atomic_worker_backup_read_error:{type(exc).__name__}"
            return result
        if _sha256_bytes(original) != V1161_EMBEDDING_WORKER_SHA256:
            result["reason"] = "source_atomic_worker_backup_hash_mismatch"
            return result
        original_text = original.decode("utf-8")
        desired_patch = patch_v1161_embedding_worker_source(original_text).encode("utf-8")
        desired_hash = _sha256_bytes(desired_patch)
        known_predecessors = {
            SOURCE_ATOMIC_LEGACY_PATCH_ID: _sha256_bytes(
                _legacy_v1_patched_worker_source(original_text).encode("utf-8")
            ),
            SOURCE_ATOMIC_PREVIOUS_PATCH_ID: _sha256_bytes(
                _previous_v2_patched_worker_source(original_text).encode("utf-8")
            ),
        }
        upgraded_from_patch_id = next(
            (
                patch_id
                for patch_id, expected_hash in known_predecessors.items()
                if current_hash == expected_hash
            ),
            "",
        )
        if upgraded_from_patch_id:
            # Every migration is byte-for-byte exact. It changes only a
            # known assistant-owned revision, retains the pristine Desktop
            # backup, and requires a new Desktop process before activation.
            try:
                _atomic_write(target, desired_patch)
                written_hash = _sha256_bytes(target.read_bytes())
                if written_hash != desired_hash:
                    _atomic_write(target, current)
                    result["reason"] = "source_atomic_worker_upgrade_hash_mismatch_restored"
                    return result
                manifest_payload = {
                    "patch_id": SOURCE_ATOMIC_PATCH_ID,
                    "desktop_version": "1.16.1",
                    "native_contract": V1161_NATIVE_CONTRACT_ID,
                    "provider": "openrouter",
                    "provider_batch_size": SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,
                    "original_worker_sha256": _sha256_bytes(original),
                    "patched_worker_sha256": written_hash,
                    "restart_required_since_epoch": target.stat().st_mtime,
                    "upgraded_from_patch_id": upgraded_from_patch_id,
                }
                _atomic_write(
                    manifest,
                    json.dumps(manifest_payload, indent=2, sort_keys=True).encode("utf-8"),
                )
            except (OSError, UnicodeError, ValueError) as exc:
                try:
                    if target.exists() and _sha256_bytes(target.read_bytes()) != current_hash:
                        _atomic_write(target, current)
                except OSError:
                    result["reason"] = (
                        f"source_atomic_worker_upgrade_error_restore_failed:{type(exc).__name__}"
                    )
                    return result
                result["reason"] = f"source_atomic_worker_upgrade_error:{type(exc).__name__}"
                return result
            result.update(
                {
                    "status": "restart_required",
                    "reason": "anythingllm_desktop_restart_required",
                    "enabled": False,
                    "installed": True,
                    "restart_required": True,
                    "worker_sha256": written_hash,
                    "upgraded_from_patch_id": upgraded_from_patch_id,
                }
            )
            return result
        if current_hash != desired_hash:
            result["reason"] = "source_atomic_worker_hash_mismatch"
            return result
        executable = Path(
            str(dict(compatibility_report.get("characterization") or {}).get("desktop_executable") or "")
        )
        active, activation_reason, restart_required = _activation_state_for_installed_worker(
            executable,
            target,
            manifest,
        )
        result.update(
            {
                "status": "already_enabled" if active else "restart_required",
                "reason": activation_reason,
                "enabled": active,
                "installed": True,
                "restart_required": restart_required,
            }
        )
        if not active and not restart_required:
            result["status"] = "restart_state_unknown"
        return result
    if current_hash != V1161_EMBEDDING_WORKER_SHA256:
        result["reason"] = "v1_16_1_embedding_worker_hash_not_matched"
        return result
    try:
        patched = patch_v1161_embedding_worker_source(current_text).encode("utf-8")
        if backup.is_file():
            # A stale or user-supplied backup must not be silently replaced
            # just because the current worker happens to match the exact
            # baseline. Preserve the prior recovery boundary unless it is the
            # same known original byte-for-byte.
            if _sha256_bytes(backup.read_bytes()) != V1161_EMBEDDING_WORKER_SHA256:
                result["reason"] = "source_atomic_worker_existing_backup_hash_mismatch"
                return result
        else:
            _atomic_write(backup, current)
        _atomic_write(target, patched)
        written_hash = _sha256_bytes(target.read_bytes())
        expected_hash = _sha256_bytes(patched)
        if written_hash != expected_hash:
            _atomic_write(target, current)
            result["reason"] = "source_atomic_worker_write_hash_mismatch_restored"
            return result
        manifest_payload = {
            "patch_id": SOURCE_ATOMIC_PATCH_ID,
            "desktop_version": "1.16.1",
            "native_contract": V1161_NATIVE_CONTRACT_ID,
            "provider": "openrouter",
            "provider_batch_size": SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,
            "original_worker_sha256": current_hash,
            "patched_worker_sha256": written_hash,
            "restart_required_since_epoch": target.stat().st_mtime,
        }
        _atomic_write(manifest, json.dumps(manifest_payload, indent=2, sort_keys=True).encode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        try:
            if target.exists() and _sha256_bytes(target.read_bytes()) != current_hash:
                _atomic_write(target, current)
        except OSError:
            result["reason"] = f"source_atomic_install_error_restore_failed:{type(exc).__name__}"
            return result
        result["reason"] = f"source_atomic_install_error:{type(exc).__name__}"
        return result
    # The on-disk patch is real, but the already-running Desktop backend has
    # not demonstrated that it reloaded this module.  Never call it enabled
    # until a later Desktop root process is observed after this write.
    result.update(
        {
            "status": "restart_required",
            "reason": "anythingllm_desktop_restart_required",
            "enabled": False,
            "installed": True,
            "restart_required": True,
            "worker_sha256": written_hash,
        }
    )
    return result
