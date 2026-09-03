"""Guarded OpenRouter source staging in AnythingLLM Desktop v1.16.1's live route."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anythingllm_compatibility import V1161_NATIVE_CONTRACT_ID
from anythingllm_source_atomic_worker import (
    SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,
    SOURCE_ATOMIC_MAX_PROVIDER_BATCH_SIZE,
    SOURCE_ATOMIC_PROVIDER_POLICY_HELPER,
    _activation_state_for_installed_worker,
    _atomic_write,
    _qualified_v1161_authority,
    _sha256_bytes,
    source_atomic_provider_retry_policy,
)


SOURCE_ATOMIC_SERVER_PATCH_ID = "anythingllm_pdf_assistant_source_atomic_server_v5"
# Every revision changes guarded execution semantics (v3 added native cache
# lookup; v4 hardens timeout classification). Older injected bodies are always
# rebuilt from the verified pristine backup instead of being edited in place.
SOURCE_ATOMIC_PREVIOUS_SERVER_PATCH_IDS = frozenset(
    {
        "anythingllm_pdf_assistant_source_atomic_server_v1",
        "anythingllm_pdf_assistant_source_atomic_server_v2",
        "anythingllm_pdf_assistant_source_atomic_server_v3",
        "anythingllm_pdf_assistant_source_atomic_server_v4",
    }
)
# Kept as a compatibility alias for callers/tests that only need the oldest
# migration marker.
SOURCE_ATOMIC_PREVIOUS_SERVER_PATCH_ID = "anythingllm_pdf_assistant_source_atomic_server_v1"
V1161_SERVER_SHA256 = (
    "b8e34b274d98f3748409511c61d071cb679834ed0fa34f3bef298d27a6136454"  # pragma: allowlist secret
)
SERVER_FUNCTION_PREFIX = "addDocuments:async function(s,e=[],t=null){"
SERVER_FUNCTION_FOLLOWER = "},removeDocuments:async function"
OPENROUTER_GATE = (
    'String(process.env.EMBEDDING_ENGINE||"")'
    ".replace(/^['\"]|['\"]$/g,\"\").trim().toLowerCase()===\"openrouter\""
)


SOURCE_ATOMIC_SERVER_BODY = r'''
let r=FM();if(e.length===0)return{failedToEmbed:[],errors:[],embedded:[]};
let{fileData:n,storeVectorResult:q,cachedVectorInformation:U}=Q(),{emitProgress:o}=ra(),a=[],i=[],c=new Set;
o(s.slug,{type:"batch_starting",workspaceSlug:s.slug,userId:t,filenames:e,totalDocs:e.length});
let d=new Map;
for(let [p,u]of e.entries()){
  let h=await n(u),f={workspaceSlug:s.slug,userId:t,filename:u,docIndex:p,totalDocs:e.length};
  if(!h){i.push(u),o(s.slug,{type:"doc_failed",...f,error:"Failed to load file data"});continue}
  let m=String(h.docSource||("file:"+u));d.has(m)||d.set(m,[]),d.get(m).push({filename:u,raw:h,progress:f});
}
let{SystemSettings:S}=x(),l=P().getEmbeddingEngineSelection(),T=Xt().TextSplitter,
  z=T.determineMaxChunkSize(await S.getValueOrFallback({label:"text_splitter_chunk_size"}),l?.embeddingMaxChunkLength),
  C=await S.getValueOrFallback({label:"text_splitter_chunk_overlap"},20),B=Math.min(64,Math.max(1,Number.parseInt(process.env.SOURCE_ATOMIC_EMBED_BATCH_SIZE||"__SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE__",10)||__SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE__));
let __sourceAtomicEmit=(event)=>o(s.slug,event);
__SOURCE_ATOMIC_PROVIDER_POLICY_HELPER__
for(let [N,R]of d){
  let A=Date.now(),H=R[0]?.filename||"";
  o(s.slug,{type:"source_staging_started",workspaceSlug:s.slug,sourceKey:N,filename:H,recordCount:R.length,provider_batch_size:B,concurrency:1,patchId:"__SOURCE_ATOMIC_SERVER_PATCH_ID__"});
  let V=[],E=null,Q=[],F={sourceRecordCount:R.length,cacheResolvedRecordCount:0,providerRequiredRecordCount:0,providerChunkCount:0};
  try{
    for(let G=0;G<R.length;G++){
      let W=R[G],X=await U(W.filename);
      if(X?.exists){
        F.cacheResolvedRecordCount++;
        V.push({record:W,cacheResolved:true});
        o(s.slug,{type:"source_staging_cache_resolved",workspaceSlug:s.slug,sourceKey:N,filename:W.filename,recordIndex:G,recordCount:R.length,cacheResolution:"native_cache_hit"});
        continue
      }
      let{pageContent:Y,...J}=W.raw,K=await new T({chunkSize:z,chunkOverlap:C,chunkHeaderMeta:T.buildHeaderMeta(J),chunkPrefix:l?.embeddingPrefix}).splitText(Y);
      F.providerRequiredRecordCount++,F.providerChunkCount+=K.length,V.push({record:W,metadata:J,texts:K,vectors:new Array(K.length),cacheResolved:false})
    }
    o(s.slug,{type:"source_staging_source_plan",workspaceSlug:s.slug,sourceKey:N,filename:H,recordCount:R.length,...F,provider_batch_size:B});
    let G=V.filter(W=>!W.cacheResolved).flatMap(W=>W.texts.map((X,Y)=>({item:W,chunkIndex:Y,text:X})));
    for(let W=0;W<G.length;W+=B){
      let X=G.slice(W,W+B),Y=Math.floor(W/B),J=Date.now(),Z=await __sourceAtomicEmbedBatch(X.map(L=>L.text),{workspaceSlug:s.slug,sourceKey:N,filename:H,recordCount:R.length,batchIndex:Y,provider_batch_size:B}),K=Z.vectors;
      if(!K||K.length!==X.length||!K.every(L=>Array.isArray(L)))throw new Error("embedding response did not match source-atomic batch");
      let ee={batchIndex:Y,chunkCount:X.length,elapsed_ms:Date.now()-J,provider_batch_size:B,attempt_count:Z.attemptCount,retry_delay_ms:Z.retryDelayMs,attempts:Z.attempts};Q.push(ee);o(s.slug,{type:"source_staging_provider_batch",workspaceSlug:s.slug,sourceKey:N,filename:H,recordCount:R.length,...ee});
      for(let L=0;L<X.length;L++)X[L].item.vectors[X[L].chunkIndex]=K[L]
    }
    for(let W of V){
      if(W.cacheResolved)continue;
      let X=W.vectors.map((Y,J)=>({id:o8(),values:Y,metadata:{...W.metadata,text:W.texts[J]}}));
      await q([X],W.record.filename),W.chunks=X;
      o(s.slug,{type:"source_staging_record",workspaceSlug:s.slug,sourceKey:N,filename:W.record.filename,chunkCount:W.texts.length,elapsed_ms:Date.now()-A,cacheResolution:"provider_staged"})
    }
  }catch(G){E=G?.message||String(G)}
  o(s.slug,{type:"source_staging_finished",workspaceSlug:s.slug,sourceKey:N,filename:H,recordCount:R.length,elapsed_ms:Date.now()-A,success:E===null,provider_batch_size:B,sourcePlan:F,providerBatches:Q,patchId:"__SOURCE_ATOMIC_SERVER_PATCH_ID__"});
  if(E!==null){for(let G of R){i.push(G.filename),c.add(E),o(s.slug,{type:"doc_failed",...G.progress,error:"Source rejected before namespace commit: "+E})}o(s.slug,{type:"source_rejected_before_commit",workspaceSlug:s.slug,sourceKey:N,filename:H,error:E});continue}
  for(let G of V){
    let W=G.record,X=o8(),{pageContent:Y,...J}=W.raw,K={docId:X,filename:W.filename.split(/[/\\]/).pop(),docpath:W.filename,workspaceId:s.id,metadata:JSON.stringify(J)};
    o(s.slug,{type:"doc_starting",...W.progress}),global.__embeddingProgress={workspaceSlug:s.slug,filename:W.filename,userId:t};
    let{vectorized:L,error:M}=await r.addDocumentToNamespace(s.slug,{...W.raw,docId:X},W.filename);
    if(!L){i.push(J?.title||K.filename),c.add(M),o(s.slug,{type:"doc_failed",...W.progress,error:M||"Unknown error"});continue}
    try{await ir.workspace_documents.create({data:K}),a.push(W.filename),o(s.slug,{type:"doc_complete",...W.progress,cacheResolution:G.cacheResolved?"native_cache_hit":"provider_staged"})}catch(O){i.push(J?.title||K.filename),c.add(O.message),o(s.slug,{type:"doc_failed",...W.progress,error:"Failed to save document record"}),o(s.slug,{type:"source_commit_ambiguous",workspaceSlug:s.slug,sourceKey:N,filename:W.filename,error:O.message});global.__embeddingProgress=null;break}
  }
  o(s.slug,{type:"source_committed",workspaceSlug:s.slug,sourceKey:N,filename:H,recordCount:R.length});
}
return global.__embeddingProgress=null,o(s.slug,{type:"all_complete",workspaceSlug:s.slug,userId:t,totalDocs:e.length,embedded:a.length,failed:i.length,embeddedFiles:a,failedFiles:i}),await a8.sendTelemetry("documents_embedded_in_workspace",{LLMSelection:process.env.LLM_PROVIDER||"openai",Embedder:process.env.EMBEDDING_ENGINE||"inherit",VectorDbSelection:process.env.VECTOR_DB||"lancedb",TTSSelection:process.env.TTS_PROVIDER||"native",LLMModel:c8()}),await jM.logEvent("workspace_documents_added",{workspaceName:s?.name||"Unknown Workspace",numberOfDocuments:e.length},t),{failedToEmbed:i,errors:Array.from(c),embedded:a};
'''.replace(
    "__SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE__",
    str(SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE),
).replace(
    "__SOURCE_ATOMIC_PROVIDER_POLICY_HELPER__",
    SOURCE_ATOMIC_PROVIDER_POLICY_HELPER,
).replace(
    "__SOURCE_ATOMIC_SERVER_PATCH_ID__",
    SOURCE_ATOMIC_SERVER_PATCH_ID,
)


def _server_path(report: dict[str, Any]) -> Path | None:
    executable = Path(str(dict(report.get("characterization") or {}).get("desktop_executable") or ""))
    return executable.parent / "resources" / "backend" / "server.js" if executable.is_file() else None


def patch_v1161_server_source(source: str) -> str:
    if SOURCE_ATOMIC_SERVER_PATCH_ID in source:
        return source
    if any(patch_id in source for patch_id in SOURCE_ATOMIC_PREVIOUS_SERVER_PATCH_IDS):
        raise ValueError("Previous source-atomic server patch must be rebuilt from its pristine backup.")
    start = source.find(SERVER_FUNCTION_PREFIX)
    end = source.find(SERVER_FUNCTION_FOLLOWER, start)
    if start < 0 or end < 0:
        raise ValueError("Expected v1.16.1 server addDocuments anchors were not found.")
    original = source[start:end]
    final_brace = original.rfind("}")
    if final_brace < len(SERVER_FUNCTION_PREFIX):
        raise ValueError("Expected v1.16.1 server addDocuments boundary was not found.")
    legacy_body = original[len(SERVER_FUNCTION_PREFIX):final_brace]
    patched = (
        f"{SERVER_FUNCTION_PREFIX}if({OPENROUTER_GATE}){{/*{SOURCE_ATOMIC_SERVER_PATCH_ID}*/"
        f"{SOURCE_ATOMIC_SERVER_BODY}}}{legacy_body}}}"
    )
    return f"{source[:start]}{patched}{source[end:]}"


def ensure_source_atomic_embedding_server(compatibility_report: dict[str, Any]) -> dict[str, Any]:
    qualified, reason = _qualified_v1161_authority(compatibility_report)
    target = _server_path(compatibility_report)
    result = {
        "patch_id": SOURCE_ATOMIC_SERVER_PATCH_ID,
        "provider": "openrouter",
        "provider_batch_size": SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,
        "max_provider_batch_size": SOURCE_ATOMIC_MAX_PROVIDER_BATCH_SIZE,
        "provider_retry_policy": source_atomic_provider_retry_policy(),
        "server_path": str(target or ""),
        "status": "disabled",
        "reason": reason,
        "enabled": False,
        "installed": False,
        "restart_required": False,
    }
    if not qualified or target is None or not target.is_file():
        if qualified:
            result["reason"] = "v1_16_1_server_missing"
        return result
    current = target.read_bytes()
    current_text = current.decode("utf-8")
    current_hash = _sha256_bytes(current)
    backup = target.with_name(f"{target.name}.pdf-assistant-v1161.backup")
    manifest = target.with_name(f"{target.name}.pdf-assistant-source-atomic.json")
    result.update(server_sha256=current_hash, backup_path=str(backup), manifest_path=str(manifest))
    if (
        SOURCE_ATOMIC_SERVER_PATCH_ID in current_text
        or any(patch_id in current_text for patch_id in SOURCE_ATOMIC_PREVIOUS_SERVER_PATCH_IDS)
    ):
        if not backup.is_file() or _sha256_bytes(backup.read_bytes()) != V1161_SERVER_SHA256:
            result["reason"] = "source_atomic_server_backup_hash_mismatch"
            return result
        expected = _sha256_bytes(patch_v1161_server_source(backup.read_bytes().decode("utf-8")).encode("utf-8"))
        if current_hash != expected:
            # A changed, explicitly recorded source-atomic configuration (for
            # example a revised provider-batch cap) may legitimately have the
            # same patch id but a different generated server hash.  Rebuild it
            # only from the verified pristine backup; never overwrite an
            # unrecognised patched server.
            try:
                previous_manifest = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                previous_manifest = {}
            known_previous_patch = (
                isinstance(previous_manifest, dict)
                and str(previous_manifest.get("patch_id") or "")
                in {SOURCE_ATOMIC_SERVER_PATCH_ID, *SOURCE_ATOMIC_PREVIOUS_SERVER_PATCH_IDS}
                and str(previous_manifest.get("original_server_sha256") or "")
                == V1161_SERVER_SHA256
                and str(previous_manifest.get("patched_server_sha256") or "") == current_hash
            )
            if not known_previous_patch:
                result["reason"] = "source_atomic_server_hash_mismatch"
                return result
            rebuilt = patch_v1161_server_source(backup.read_bytes().decode("utf-8")).encode("utf-8")
            _atomic_write(target, rebuilt)
            written_hash = _sha256_bytes(target.read_bytes())
            if written_hash != expected:
                _atomic_write(target, current)
                result["reason"] = "source_atomic_server_reconfigure_write_hash_mismatch_restored"
                return result
            _atomic_write(manifest, json.dumps({"patch_id": SOURCE_ATOMIC_SERVER_PATCH_ID,"desktop_version":"1.16.1","native_contract":V1161_NATIVE_CONTRACT_ID,"provider":"openrouter","provider_batch_size":SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,"provider_retry_policy":source_atomic_provider_retry_policy(),"original_server_sha256":V1161_SERVER_SHA256,"patched_server_sha256":written_hash,"restart_required_since_epoch":target.stat().st_mtime}, indent=2, sort_keys=True).encode("utf-8"))
            result.update(status="restart_required", reason="anythingllm_desktop_restart_required_after_source_atomic_reconfigure", installed=True, restart_required=True, server_sha256=written_hash)
            return result
        executable = Path(str(dict(compatibility_report.get("characterization") or {}).get("desktop_executable") or ""))
        active, activation_reason, restart_required = _activation_state_for_installed_worker(executable, target, manifest)
        result.update(status="already_enabled" if active else "restart_required", reason=activation_reason, enabled=active, installed=True, restart_required=restart_required)
        return result
    if current_hash != V1161_SERVER_SHA256:
        result["reason"] = "v1_16_1_server_hash_not_matched"
        return result
    patched = patch_v1161_server_source(current_text).encode("utf-8")
    if backup.exists() and _sha256_bytes(backup.read_bytes()) != V1161_SERVER_SHA256:
        result["reason"] = "source_atomic_server_existing_backup_hash_mismatch"
        return result
    if not backup.exists():
        _atomic_write(backup, current)
    _atomic_write(target, patched)
    written_hash = _sha256_bytes(target.read_bytes())
    if written_hash != _sha256_bytes(patched):
        _atomic_write(target, current)
        result["reason"] = "source_atomic_server_write_hash_mismatch_restored"
        return result
    _atomic_write(manifest, json.dumps({"patch_id": SOURCE_ATOMIC_SERVER_PATCH_ID,"desktop_version":"1.16.1","native_contract":V1161_NATIVE_CONTRACT_ID,"provider":"openrouter","provider_batch_size":SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,"provider_retry_policy":source_atomic_provider_retry_policy(),"original_server_sha256":V1161_SERVER_SHA256,"patched_server_sha256":written_hash,"restart_required_since_epoch":target.stat().st_mtime}, indent=2, sort_keys=True).encode("utf-8"))
    result.update(status="restart_required", reason="anythingllm_desktop_restart_required", installed=True, restart_required=True, server_sha256=written_hash)
    return result
