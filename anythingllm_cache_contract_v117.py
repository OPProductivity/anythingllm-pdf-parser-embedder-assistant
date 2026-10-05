"""Exact native cache contract extension for the qualified v1.17 backend."""

CACHE_CONTRACT_SOURCE = r'''
/* Assistant cache contract: retain native nested arrays and ignore other engines. */
const __pdfCacheFs=f("fs"),__pdfCachePath=f("path"),__pdfCacheCrypto=f("crypto"),__pdfCacheUuid=f("uuid");
const __pdfCacheContract="openrouter-v117-cache-identity-1";
const __pdfCacheActive=()=>String(process.env.EMBEDDING_ENGINE||"").replace(/^['"]|['"]$/g,"").trim().toLowerCase()==="openrouter";
if(__pdfCacheActive()){
  let marker=__pdfCachePath.resolve(process.env.STORAGE_DIR,"pdf-assistant-embedding-cache-contract.json");
  try{__pdfCacheFs.writeFileSync(marker,JSON.stringify({contract:__pdfCacheContract}),"utf8")}catch{console.warn("PDF assistant cache contract marker could not be published")}
}
async function __pdfCacheConfiguration(){
 let settings=N().SystemSettings;
 return{engine:"openrouter",model:String(process.env.EMBEDDING_MODEL_PREF||"baai/bge-m3"),
  chunk_size:String(await settings.getValueOrFallback({label:"text_splitter_chunk_size"})),
  chunk_overlap:String(await settings.getValueOrFallback({label:"text_splitter_chunk_overlap"},20)),
  chunk_limit:String(process.env.EMBEDDING_MODEL_MAX_CHUNK_LENGTH||"")};
}
function __pdfCacheProof(chunk,configuration){
 let metadata=chunk?.metadata||{},values=chunk?.values;
 if(!Array.isArray(values)||!values.length||!values.every(value=>typeof value==="number"&&Number.isFinite(value))||typeof metadata!=="object")throw Error("Invalid cache vector data");
 let binary=Buffer.alloc(values.length*8);values.forEach((value,index)=>binary.writeDoubleLE(value,index*8));
 let text=[metadata.text||"",metadata.docSource||"",metadata.chunkSource||""].map(String).join("\0");
 return{contract:__pdfCacheContract,configuration,text_sha256:__pdfCacheCrypto.createHash("sha256").update(text).digest("hex"),
  vector_sha256:__pdfCacheCrypto.createHash("sha256").update(binary).digest("hex")};
}
async function __pdfCacheRead(location,existenceOnly=false){
 if(!location)return existenceOnly?false:{exists:false,chunks:[]};
 let filename=__pdfCachePath.resolve(process.env.STORAGE_DIR,"vector-cache",`${__pdfCacheUuid.v5(location,__pdfCacheUuid.v5.URL)}.json`);
 try{
  let stat=__pdfCacheFs.lstatSync(filename);if(!stat.isFile()||stat.isSymbolicLink()||stat.size<=0||stat.size>67108864)return existenceOnly?false:{exists:false,chunks:[]};
  let bytes=__pdfCacheFs.readFileSync(filename);if(bytes.length>67108864)return existenceOnly?false:{exists:false,chunks:[]};
  let groups=JSON.parse(bytes.toString("utf8")),configuration=await __pdfCacheConfiguration(),dimension=null;
  if(!Array.isArray(groups)||!groups.length||!groups.every(group=>Array.isArray(group)&&group.length))return existenceOnly?false:{exists:false,chunks:[]};
  for(let chunk of groups.flat()){
   let expected=__pdfCacheProof(chunk,configuration),proof=chunk.pdfAssistantCacheIdentity;
   dimension=dimension??chunk.values.length;
   if(chunk.values.length!==dimension||!proof||JSON.stringify(proof.configuration)!==JSON.stringify(configuration)||
      proof.contract!==expected.contract||proof.text_sha256!==expected.text_sha256||proof.vector_sha256!==expected.vector_sha256)
      return existenceOnly?false:{exists:false,chunks:[]};
  }
  if(!existenceOnly)console.log(`Cached vectorized results of ${location} found! Using cached data to save on embed costs.`);
  return existenceOnly?true:{exists:true,chunks:groups};
 }catch{return existenceOnly?false:{exists:false,chunks:[]}}
}
async function __pdfCacheWrite(groups,location){
 if(!location)return;
 if(!Array.isArray(groups)||!groups.length||!groups.every(group=>Array.isArray(group)&&group.length))throw Error("Invalid cache groups");
 let configuration=await __pdfCacheConfiguration(),dimension=null;
 for(let chunk of groups.flat()){
  chunk.pdfAssistantCacheIdentity=__pdfCacheProof(chunk,configuration);dimension=dimension??chunk.values.length;
  if(chunk.values.length!==dimension)throw Error("Inconsistent cache vector dimensions");
 }
 let directory=__pdfCachePath.resolve(process.env.STORAGE_DIR,"vector-cache");__pdfCacheFs.mkdirSync(directory,{recursive:true});
 let filename=__pdfCachePath.resolve(directory,`${__pdfCacheUuid.v5(location,__pdfCacheUuid.v5.URL)}.json`),
  temporary=filename+"."+__pdfCacheCrypto.randomBytes(12).toString("hex")+".tmp",descriptor=null;
 try{
  descriptor=__pdfCacheFs.openSync(temporary,"wx",0o600);__pdfCacheFs.writeFileSync(descriptor,JSON.stringify(groups),"utf8");
  __pdfCacheFs.fsyncSync(descriptor);__pdfCacheFs.closeSync(descriptor);descriptor=null;
  __pdfCacheFs.renameSync(temporary,filename);
  console.log(`Caching vectorized results of ${location} to prevent duplicated embedding.`);
 }finally{if(descriptor!==null)__pdfCacheFs.closeSync(descriptor);try{__pdfCacheFs.unlinkSync(temporary)}catch{}}
}

'''


def patch_v117_cache_contract(source):
    reader = "async function of(s=null,e=!1){"
    writer = "async function SQ(s=[],e=null){"
    if source.count(reader) != 1 or source.count(writer) != 1:
        raise ValueError("Qualified v1.17 native cache anchors changed.")
    source = source.replace(
        reader,
        CACHE_CONTRACT_SOURCE + "\n" + reader
        + "if(__pdfCacheActive())return await __pdfCacheRead(s,e);",
    )
    return source.replace(
        writer,
        writer + "if(__pdfCacheActive())return await __pdfCacheWrite(s,e);",
    )
