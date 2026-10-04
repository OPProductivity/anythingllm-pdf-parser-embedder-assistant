"""Request-scoped provider staging for the qualified Desktop v1.17.0 API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anythingllm_compatibility import OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS, V117_NATIVE_CONTRACT_ID
from anythingllm_source_atomic_server import OPENROUTER_GATE, SOURCE_ATOMIC_SERVER_BODY_TEMPLATE
from anythingllm_source_atomic_common import (
    SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,
    SOURCE_ATOMIC_MAX_PROVIDER_BATCH_SIZE,
    _activation_state_for_installed_worker,
    _atomic_write,
    _sha256_bytes,
)

V117_SERVER_SHA256 = "a27009d6c87a476b68e619f1e65a83aa81ca257efc904b7c87c1b7deaf0b1a51"  # pragma: allowlist secret
PATCH_ID = "anythingllm_pdf_assistant_source_atomic_server_v117_1"
REQUEST_FLAG = "pdfAssistantSourceAtomic"
FUNCTION_PREFIX = "addDocuments:async function(s,e=[],t=null){"
FUNCTION_FOLLOWER = "},removeDocuments:async function"
API_CALL = "await Tc.addDocuments(a,n)"


NATIVE_PROVIDER_HELPER = r'''
let __nativeTimeout=Number.parseInt(process.env.ANYTHINGLLM_FETCH_TIMEOUT||"",10),
  __sourceAtomicTimeoutMs=Number.isFinite(__nativeTimeout)&&__nativeTimeout>0?__nativeTimeout:600000;
let __sourceAtomicEmbedBatch=async(texts,context)=>{
  if(!l?.openai?.embeddings||typeof l.openai.embeddings.create!=="function")throw new Error("source-atomic OpenRouter client is unavailable");
  let started=Date.now(),attemptId=`__PATCH_ID__:${String(context?.sourceKey||"source")}:${Number(context?.batchIndex||0)}:1`,
    evidence={...context,attempt:1,attempt_id:attemptId,maximum_attempts:1,attempts_scope:"adapter",retry_owner:"desktop_sdk",chunkCount:texts.length,request_timeout_ms:__sourceAtomicTimeoutMs};
  __sourceAtomicEmit({type:"source_staging_provider_batch_attempt",...evidence});
  let pulse=setInterval(()=>__sourceAtomicEmit({type:"source_staging_provider_batch_waiting",...evidence,elapsed_ms:Date.now()-started}),5000);
  try{
    let response=await l.openai.embeddings.create({model:l.model,input:texts},{timeout:__sourceAtomicTimeoutMs}),
      data=Array.isArray(response?.data)?[...response.data].sort((a,b)=>Number(a?.index)-Number(b?.index)):[],
      vectors=data.map(item=>item?.embedding);
    if(vectors.length!==texts.length||!data.every((item,index)=>item?.index===index)||
      !vectors.every(vector=>Array.isArray(vector)&&vector.length>0&&vector.length===vectors[0].length&&vector.every(value=>typeof value==="number"&&Number.isFinite(value))))
      throw new Error("embedding response did not match source-atomic batch");
    let elapsedMs=Date.now()-started;
    __sourceAtomicEmit({type:"source_staging_provider_batch_attempt_completed",...evidence,elapsed_ms:elapsedMs});
    return{vectors,attemptCount:1,retryDelayMs:0,attempts:[{attempt:1,attempt_id:attemptId,elapsed_ms:elapsedMs,request_timeout_ms:__sourceAtomicTimeoutMs,outcome:"success",attempts_scope:"adapter",retry_owner:"desktop_sdk"}]};
  }catch(error){
    let status=Number(error?.status||error?.response?.status||0),detail={error_class:String(error?.name||error?.constructor?.name||"Error"),
      http_status:Number.isFinite(status)&&status>0?status:0,message:String(error?.message||"provider request failed").slice(0,500)};
    __sourceAtomicEmit({type:"source_staging_provider_batch_attempt_failed",...evidence,elapsed_ms:Date.now()-started,outcome:"failed",retryable:false,...detail});
    throw new Error(`source-atomic provider SDK request failed${detail.http_status?` (HTTP ${detail.http_status})`:""}: ${detail.message}`);
  }finally{clearInterval(pulse)}
};
'''.replace("__PATCH_ID__", PATCH_ID)


def provider_staging_body() -> str:
    # Translate our own guarded body, never guessed identifiers in vendor code.
    substitutions = {
        "FM()": "rx()", "=Q()": "=V()", "=ra()": "=da()", "=x()": "=N()",
        "P().getEmbeddingEngineSelection()": "O().getEmbeddingEngineSelection()",
        "Xt().TextSplitter": "tr().TextSplitter", "o8()": "cQ()",
        "ir.workspace_documents": "mr.workspace_documents", "a8.sendTelemetry": "lQ.sendTelemetry",
        "c8()": "dQ()", "jM.logEvent": "nx.logEvent",
        "anythingllm_pdf_assistant_source_atomic_server_v5": PATCH_ID,
        "numberOfDocuments:e.length": "numberOfDocumentsAdded:e.length",
    }
    if SOURCE_ATOMIC_SERVER_BODY_TEMPLATE.count("__SOURCE_ATOMIC_PROVIDER_POLICY_HELPER__") != 1:
        raise ValueError("Expected one assistant-owned provider helper.")
    value = SOURCE_ATOMIC_SERVER_BODY_TEMPLATE.replace("__SOURCE_ATOMIC_PROVIDER_POLICY_HELPER__", NATIVE_PROVIDER_HELPER)
    value = value.replace("__SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE__", str(SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE))
    value = value.replace("__SOURCE_ATOMIC_SERVER_PATCH_ID__", "anythingllm_pdf_assistant_source_atomic_server_v5")
    for old, new in substitutions.items():
        if old not in value:
            raise ValueError(f"Source staging contract changed: {old}")
        value = value.replace(old, new)
    return value


def native_provider_policy() -> dict[str, Any]:
    return {
        "maximum_attempts": 1, "attempts_scope": "adapter",
        "timeout_owner": "desktop_sdk", "default_timeout_ms": 600_000,
        "timeout_env": "ANYTHINGLLM_FETCH_TIMEOUT", "retry_owner": "desktop_sdk",
        "retry_env": "ANYTHINGLLM_MAX_RETRIES", "wait_heartbeat_ms": 5_000,
    }


def patch_v117_server_source(source: str) -> str:
    if _sha256_bytes(source.encode("utf-8")) != V117_SERVER_SHA256:
        raise ValueError("Expected pristine qualified v1.17.0 backend hash.")
    if source.count(FUNCTION_PREFIX) != 1 or source.count(API_CALL) != 1:
        raise ValueError("Expected exact v1.17.0 backend/API anchors.")
    start = source.index(FUNCTION_PREFIX)
    end = source.index(FUNCTION_FOLLOWER, start)
    original = source[start:end]
    legacy = original[len(FUNCTION_PREFIX):original.rfind("}")]
    replacement = (
        "addDocuments:async function(s,e=[],t=null,pdfAssistantSourceAtomic=false){"
        f"if(pdfAssistantSourceAtomic===true&&({OPENROUTER_GATE})){{/*{PATCH_ID}*/"
        + provider_staging_body() + "}" + legacy + "}"
    )
    patched = source[:start] + replacement + source[end:]
    return patched.replace(API_CALL, API_CALL[:-1] + f",null,Ao(e).{REQUEST_FLAG}===true)")


def ensure_v117_embedding_server(report: dict[str, Any]) -> dict[str, Any]:
    characterization = dict(report.get("characterization") or {})
    executable = Path(str(characterization.get("desktop_executable") or ""))
    target = executable.parent / "resources/backend/server.js"
    result: dict[str, Any] = {
        "patch_id": PATCH_ID, "desktop_version": "1.17.0", "provider": "openrouter",
        "provider_batch_size": SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,
        "max_provider_batch_size": SOURCE_ATOMIC_MAX_PROVIDER_BATCH_SIZE,
        "provider_retry_policy": native_provider_policy(), "server_path": str(target),
        "status": "disabled", "reason": "v1_17_native_mutation_contract_not_qualified",
        "enabled": False, "installed": False, "restart_required": False,
    }
    package = dict(characterization.get("desktop_package") or {})
    if (
        report.get("status") != "pass"
        or characterization.get("desktop_version_normalized") != "1.17.0"
        or characterization.get("native_mutation_contract") != V117_NATIVE_CONTRACT_ID
        or str(package.get("app_asar_sha256") or "").casefold()
        != OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS["1.17.0"]
        or not executable.is_file() or not target.is_file()
    ):
        return result
    backup = target.with_name(target.name + ".pdf-assistant-v117.backup")
    manifest = target.with_name(target.name + ".pdf-assistant-source-atomic.json")
    current = target.read_bytes()
    current_hash = _sha256_bytes(current)
    result.update(server_sha256=current_hash, backup_path=str(backup), manifest_path=str(manifest))
    if backup.exists() and _sha256_bytes(backup.read_bytes()) != V117_SERVER_SHA256:
        result["reason"] = "source_atomic_server_existing_backup_hash_mismatch"
        return result
    if current_hash == V117_SERVER_SHA256:
        pristine = current
    elif backup.is_file():
        pristine = backup.read_bytes()
    else:
        result["reason"] = "v1_17_server_hash_not_matched"
        return result
    patched = patch_v117_server_source(pristine.decode("utf-8")).encode("utf-8")
    expected_hash = _sha256_bytes(patched)
    if current_hash == expected_hash:
        active, reason, restart = _activation_state_for_installed_worker(executable, target, manifest)
        result.update(status="already_enabled" if active else "restart_required", enabled=active,
                      installed=True, reason=reason, restart_required=restart)
        return result
    if current_hash != V117_SERVER_SHA256:
        result["reason"] = "source_atomic_server_hash_mismatch"
        return result
    if not backup.exists():
        _atomic_write(backup, pristine)
    _atomic_write(target, patched)
    if _sha256_bytes(target.read_bytes()) != expected_hash:
        _atomic_write(target, current)
        result["reason"] = "source_atomic_server_write_hash_mismatch_restored"
        return result
    _atomic_write(manifest, json.dumps({
        "patch_id": PATCH_ID, "desktop_version": "1.17.0", "native_contract": V117_NATIVE_CONTRACT_ID,
        "provider": "openrouter", "provider_batch_size": SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE,
        "provider_retry_policy": native_provider_policy(),
        "original_server_sha256": V117_SERVER_SHA256, "patched_server_sha256": expected_hash,
        "restart_required_since_epoch": target.stat().st_mtime,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    result.update(status="restart_required", reason="anythingllm_desktop_restart_required",
                  installed=True, restart_required=True, server_sha256=expected_hash)
    return result
