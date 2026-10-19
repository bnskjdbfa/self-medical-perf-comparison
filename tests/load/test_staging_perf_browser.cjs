// Deterministic collector regression: a 503 whose body never finishes must be saved.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {EventEmitter}=require('node:events');

(async()=>{
  const host='self-medical-perf-comparison.onrender.com';
  const build='a'.repeat(40), digest='b'.repeat(64);
  let saved, closed=false;
  const listeners={};
  const page={
    on:(event,callback)=>{listeners[event]=callback;},
    goto:async()=>{
      setImmediate(()=>listeners.response({
        request:()=>({method:()=> 'POST',resourceType:()=> 'fetch'}),
        url:()=>`https://${host}/surveys/preview/v2/identity/`,
        status:()=>503,
        allHeaders:async()=>({'x-perf-build':build,'x-perf-code-digest':digest}),
        finished:()=>new Promise(()=>{}),
      }));
      return {status:()=>200,headerValue:async name=>name==='x-perf-build'?build:digest};
    },
  };
  const fakeProcess=new EventEmitter();
  fakeProcess.argv=['node','collector','--url',`https://${host}`,'--expect-host',host,
    '--build',build,'--code-digest',digest,'--output','unused-fixture-output'];
  fakeProcess.env={STAGING_PERF_TOKEN:'secret-fixture-only-'.repeat(3)};
  fakeProcess.stdin={};fakeProcess.stdout={};
  const mocks={
    fs:{existsSync:()=>false,writeFileSync:(_file,data)=>{saved=JSON.parse(data);}},
    readline:{createInterface:()=>({
      once:(_event,callback)=>setTimeout(callback,5),close:()=>{},
    })},
    playwright:{chromium:{launch:async()=>({
      newContext:async()=>({addCookies:async()=>{},newPage:async()=>page}),
      close:async()=>{closed=true;},
    })}},
  };
  const source=fs.readFileSync(path.join(__dirname,'staging_perf_browser.cjs'),'utf8');
  await vm.runInNewContext(source,{
    require:name=>{assert.ok(name in mocks);return mocks[name];},
    URL,process:fakeProcess,console:{log:()=>{},error:()=>{}},
    // Replace 10s collection deadlines with 20ms for this test only.
    setTimeout:(fn,ms)=>setTimeout(fn,ms===10000?20:ms),clearTimeout,
  });
  assert.equal(saved.requests[0].status,503);
  assert.equal(saved.requests[0].collection_complete,false);
  assert.equal(saved.collection_errors,1);
  assert.equal(saved.valid,false);
  assert.equal(closed,true);
  assert.ok(!JSON.stringify(saved).includes('secret-fixture-only'));
  console.log('PASS: stuck 503 response saved as incomplete; collector exits; no token in report');
})().catch(error=>{console.error(error);process.exitCode=1;});
