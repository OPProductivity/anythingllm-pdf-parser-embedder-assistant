import json
import shutil
import subprocess
import pytest
import anythingllm_source_atomic_v1161_policy as source_atomic
pytestmark = pytest.mark.offline_deterministic

def _run_provider_policy_probe(script: str) -> dict:
    """Run the generated Desktop helper without a provider or Desktop process."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise generated Desktop JavaScript")
    result = subprocess.run(
        [node, "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_generated_provider_policy_caps_retry_and_disables_sdk_retries():
    probe = _run_provider_policy_probe(
        """
let emitted=[],calls=[],intervals=[];
global.setInterval=(callback,delay)=>{intervals.push({callback,delay});return intervals.length};
global.clearInterval=()=>{};
global.setTimeout=(callback)=>{queueMicrotask(callback);return 1};
let l={openai:{embeddings:{create:async(request,options)=>{
  calls.push(options);
  if(calls.length===1){let error=new Error("rate limited");error.name="RateLimitError";error.status=429;error.headers={"retry-after-ms":"9000"};throw error}
  return {data:request.input.map((text,index)=>({embedding:[index,text.length]}))}
}}}};
let __sourceAtomicEmit=(event)=>emitted.push(event);
"""
        + source_atomic.SOURCE_ATOMIC_PROVIDER_POLICY_HELPER
        + """
(async()=>{let result=await __sourceAtomicEmbedBatch(["one","two"],{batchIndex:0,sourceKey:"probe"});console.log(JSON.stringify({calls,attemptCount:result.attemptCount,retryDelayMs:result.retryDelayMs,events:emitted.map(event=>event.type)}))})().catch(error=>{console.error(error);process.exit(1)});
"""
    )

    assert probe["calls"] == [
        {"maxRetries": 0, "timeout": 35_000},
        {"maxRetries": 0, "timeout": 45_000},
    ]
    assert probe["attemptCount"] == 2
    assert probe["retryDelayMs"] == 5_000
    assert "source_staging_provider_batch_retrying" in probe["events"]
    # Backoff is reported as retrying, never as an active provider wait.
    assert "source_staging_provider_batch_waiting" not in probe["events"]


def test_generated_provider_policy_retries_generic_error_with_timeout_message():
    """OpenAI-compatible clients can surface a timeout as plain ``Error``."""
    probe = _run_provider_policy_probe(
        """
let emitted=[],calls=[];
global.setInterval=()=>1;global.clearInterval=()=>{};
global.setTimeout=(callback)=>{queueMicrotask(callback);return 1};
let l={openai:{embeddings:{create:async(request,options)=>{
  calls.push(options);
  if(calls.length===1)throw new Error("Request timed out.");
  return {data:request.input.map((text,index)=>({embedding:[index,text.length]}))}
}}}};
let __sourceAtomicEmit=(event)=>emitted.push(event);
"""
        + source_atomic.SOURCE_ATOMIC_PROVIDER_POLICY_HELPER
        + """
(async()=>{let result=await __sourceAtomicEmbedBatch(["one"],{batchIndex:0,sourceKey:"probe"});console.log(JSON.stringify({calls,attemptCount:result.attemptCount,events:emitted.map(event=>event.type)}))})().catch(error=>{console.error(error);process.exit(1)});
"""
    )

    assert probe["attemptCount"] == 2
    assert probe["calls"] == [
        {"maxRetries": 0, "timeout": 35_000},
        {"maxRetries": 0, "timeout": 45_000},
    ]
    assert probe["events"].count("source_staging_provider_batch_retrying") == 1


def test_generated_provider_policy_never_retries_bad_request_and_heartbeats_active_request():
    probe = _run_provider_policy_probe(
        """
let emitted=[],calls=0,pulse=null;
global.setInterval=(callback)=>{pulse=callback;return 1};
global.clearInterval=()=>{};
let l={openai:{embeddings:{create:async(request,options)=>{
  calls++;
  if(calls===1){let error=new Error("invalid input");error.name="BadRequestError";error.status=400;throw error}
  pulse();
  return {data:request.input.map((text,index)=>({embedding:[index]}))}
}}}};
let __sourceAtomicEmit=(event)=>emitted.push(event);
"""
        + source_atomic.SOURCE_ATOMIC_PROVIDER_POLICY_HELPER
        + """
(async()=>{let firstError="";try{await __sourceAtomicEmbedBatch(["bad"],{batchIndex:0,sourceKey:"bad"})}catch(error){firstError=error.message};let result=await __sourceAtomicEmbedBatch(["good"],{batchIndex:1,sourceKey:"good"});console.log(JSON.stringify({calls,firstError,attemptCount:result.attemptCount,events:emitted.map(event=>event.type)}))})().catch(error=>{console.error(error);process.exit(1)});
"""
    )

    assert probe["calls"] == 2
    assert "HTTP 400" in probe["firstError"]
    assert probe["attemptCount"] == 1
    assert probe["events"].count("source_staging_provider_batch_retrying") == 0
    assert probe["events"].count("source_staging_provider_batch_waiting") == 1
