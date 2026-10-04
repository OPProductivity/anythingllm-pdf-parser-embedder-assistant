"""Immutable provider retry policy retained only for qualified Desktop v1.16.1."""
from __future__ import annotations
from typing import Any
from anythingllm_compatibility import OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS, V1161_NATIVE_CONTRACT_ID

SOURCE_ATOMIC_PATCH_ID = "anythingllm_pdf_assistant_source_atomic_v11"


SOURCE_ATOMIC_PROVIDER_FIRST_ATTEMPT_TIMEOUT_MS = 35_000


SOURCE_ATOMIC_PROVIDER_RECOVERY_ATTEMPT_TIMEOUT_MS = 45_000


SOURCE_ATOMIC_PROVIDER_RETRY_DELAY_CAP_MS = 5_000


SOURCE_ATOMIC_PROVIDER_WAIT_HEARTBEAT_MS = 5_000


SOURCE_ATOMIC_PROVIDER_POLICY_HELPER = r'''
let __sourceAtomicFirstAttemptTimeoutMs=__SOURCE_ATOMIC_PROVIDER_FIRST_ATTEMPT_TIMEOUT_MS__,__sourceAtomicRecoveryAttemptTimeoutMs=__SOURCE_ATOMIC_PROVIDER_RECOVERY_ATTEMPT_TIMEOUT_MS__,__sourceAtomicRetryDelayCapMs=__SOURCE_ATOMIC_PROVIDER_RETRY_DELAY_CAP_MS__,__sourceAtomicWaitHeartbeatMs=__SOURCE_ATOMIC_PROVIDER_WAIT_HEARTBEAT_MS__,__sourceAtomicSleep=(ms)=>new Promise(resolve=>setTimeout(resolve,ms));
let __sourceAtomicHeaderValue=(error,name)=>{let headers=error?.headers||error?.response?.headers||{},lower=String(name||"").toLowerCase();try{if(typeof headers?.get==="function")return headers.get(name)||headers.get(lower)||""}catch(_){}return headers?.[name]||headers?.[lower]||""};
let __sourceAtomicErrorStatus=(error)=>{let status=Number(error?.status||error?.response?.status||0);return Number.isFinite(status)&&status>0?status:0};
let __sourceAtomicRetryable=(error)=>{if(error?.__sourceAtomicNoRetry)return false;let status=__sourceAtomicErrorStatus(error);if([408,409,429].includes(status)||status>=500)return true;if(status)return false;let name=String(error?.name||""),message=String(error?.message||"").toLowerCase();return name.includes("Connection")||name.includes("Timeout")||name==="AbortError"||name==="TypeError"||message.includes("timed out")||message.includes("timeout")||message.includes("connection reset")||message.includes("socket hang up")||message.includes("fetch failed")};
let __sourceAtomicRetryDelayMs=(error)=>{let retryAfterMs=Number.parseFloat(__sourceAtomicHeaderValue(error,"retry-after-ms")),retryAfter=String(__sourceAtomicHeaderValue(error,"retry-after")||"").trim(),delay=0;if(Number.isFinite(retryAfterMs)&&retryAfterMs>=0)delay=retryAfterMs;else if(retryAfter){let seconds=Number.parseFloat(retryAfter);if(Number.isFinite(seconds)&&seconds>=0)delay=seconds*1000;else{let dateMs=Date.parse(retryAfter);if(Number.isFinite(dateMs))delay=Math.max(0,dateMs-Date.now())}}if(!Number.isFinite(delay)||delay<=0)delay=500;return Math.min(__sourceAtomicRetryDelayCapMs,Math.max(0,Math.round(delay)))};
let __sourceAtomicErrorDetail=(error)=>({error_class:String(error?.name||error?.constructor?.name||"Error"),http_status:__sourceAtomicErrorStatus(error),message:String(error?.message||"provider request failed").slice(0,500)});
let __sourceAtomicAttemptId=(context,attempt)=>`__SOURCE_ATOMIC_PATCH_ID__:${String(context?.sourceKey||"source")}:${Number(context?.batchIndex||0)}:${attempt}`;
let __sourceAtomicEmbedBatch=async(texts,context)=>{if(!l?.openai?.embeddings||typeof l.openai.embeddings.create!=="function")throw new Error("source-atomic OpenRouter client is unavailable");let attempts=[],retryDelayMs=0;for(let attempt=1;attempt<=2;attempt++){let timeoutMs=attempt===1?__sourceAtomicFirstAttemptTimeoutMs:__sourceAtomicRecoveryAttemptTimeoutMs,started=Date.now(),pulse=null,attemptId=__sourceAtomicAttemptId(context,attempt);__sourceAtomicEmit({type:"source_staging_provider_batch_attempt",...context,attempt,attempt_id:attemptId,maximum_attempts:2,chunkCount:texts.length,request_timeout_ms:timeoutMs});try{pulse=setInterval(()=>__sourceAtomicEmit({type:"source_staging_provider_batch_waiting",...context,attempt,attempt_id:attemptId,maximum_attempts:2,chunkCount:texts.length,request_timeout_ms:timeoutMs,elapsed_ms:Date.now()-started}),__sourceAtomicWaitHeartbeatMs);let response=await l.openai.embeddings.create({model:l.model,input:texts},{maxRetries:0,timeout:timeoutMs}),vectors=Array.isArray(response?.data)?response.data.map(item=>item?.embedding):[];if(vectors.length!==texts.length||!vectors.every(vector=>Array.isArray(vector)&&vector.length>0&&vector.length===vectors[0].length&&vector.every(value=>typeof value==="number"&&Number.isFinite(value)))||!response.data.every((item,index)=>item.index===undefined||item.index===index)){let mismatch=new Error("embedding response did not match source-atomic batch");mismatch.__sourceAtomicNoRetry=true;throw mismatch}let elapsedMs=Date.now()-started;attempts.push({attempt,attempt_id:attemptId,elapsed_ms:elapsedMs,request_timeout_ms:timeoutMs,outcome:"success"});__sourceAtomicEmit({type:"source_staging_provider_batch_attempt_completed",...context,attempt,attempt_id:attemptId,maximum_attempts:2,chunkCount:texts.length,elapsed_ms:elapsedMs,request_timeout_ms:timeoutMs});return{vectors,attemptCount:attempt,retryDelayMs,attempts}}catch(error){if(pulse!==null){clearInterval(pulse);pulse=null}let elapsedMs=Date.now()-started,detail=__sourceAtomicErrorDetail(error),retryable=attempt<2&&__sourceAtomicRetryable(error),attemptEvidence={attempt,attempt_id:attemptId,elapsed_ms:elapsedMs,request_timeout_ms:timeoutMs,outcome:"failed",retryable,...detail};attempts.push(attemptEvidence);__sourceAtomicEmit({type:"source_staging_provider_batch_attempt_failed",...context,maximum_attempts:2,chunkCount:texts.length,...attemptEvidence});if(!retryable){let terminal=new Error(`source-atomic provider attempt ${attempt}/2 failed${detail.http_status?` (HTTP ${detail.http_status})`:""}: ${detail.message}`);terminal.__sourceAtomicNoRetry=true;throw terminal}let delayMs=__sourceAtomicRetryDelayMs(error);retryDelayMs+=delayMs;__sourceAtomicEmit({type:"source_staging_provider_batch_retrying",...context,attempt,attempt_id:attemptId,next_attempt:attempt+1,maximum_attempts:2,chunkCount:texts.length,retry_delay_ms:delayMs,retry_delay_cap_ms:__sourceAtomicRetryDelayCapMs,...detail});await __sourceAtomicSleep(delayMs)}finally{if(pulse!==null)clearInterval(pulse)}}throw new Error("source-atomic provider retry state exhausted")};
'''.replace(
    "__SOURCE_ATOMIC_PROVIDER_FIRST_ATTEMPT_TIMEOUT_MS__",
    str(SOURCE_ATOMIC_PROVIDER_FIRST_ATTEMPT_TIMEOUT_MS),
).replace(
    "__SOURCE_ATOMIC_PROVIDER_RECOVERY_ATTEMPT_TIMEOUT_MS__",
    str(SOURCE_ATOMIC_PROVIDER_RECOVERY_ATTEMPT_TIMEOUT_MS),
).replace(
    "__SOURCE_ATOMIC_PROVIDER_RETRY_DELAY_CAP_MS__",
    str(SOURCE_ATOMIC_PROVIDER_RETRY_DELAY_CAP_MS),
).replace(
    "__SOURCE_ATOMIC_PROVIDER_WAIT_HEARTBEAT_MS__",
    str(SOURCE_ATOMIC_PROVIDER_WAIT_HEARTBEAT_MS),
).replace(
    "__SOURCE_ATOMIC_PATCH_ID__",
    SOURCE_ATOMIC_PATCH_ID,
)


def source_atomic_provider_retry_policy() -> dict[str, int]:
    """Return the immutable pre-commit OpenRouter request policy."""
    return {
        "maximum_attempts": 2,
        "first_attempt_timeout_ms": SOURCE_ATOMIC_PROVIDER_FIRST_ATTEMPT_TIMEOUT_MS,
        "recovery_attempt_timeout_ms": SOURCE_ATOMIC_PROVIDER_RECOVERY_ATTEMPT_TIMEOUT_MS,
        "retry_delay_cap_ms": SOURCE_ATOMIC_PROVIDER_RETRY_DELAY_CAP_MS,
        "wait_heartbeat_ms": SOURCE_ATOMIC_PROVIDER_WAIT_HEARTBEAT_MS,
    }


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
