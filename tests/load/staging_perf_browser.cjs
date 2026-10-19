// One manually operated browser. No cookies, HAR, HTML, form values or screenshots saved.
const fs = require('fs');
const readline = require('readline');
const {chromium} = require('playwright');
async function bounded(promise, ms=10000) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject)=>{
      timer=setTimeout(()=>reject(new Error('collection_timeout')),ms);
    })]);
  } finally {clearTimeout(timer);}
}
function arg(name) {return process.argv[process.argv.indexOf(name)+1];}
function route(path) {
  const prefix='/surveys/preview/v2/';
  const known={'code/':'code','privacy-consent/':'privacy','identity/':'identity',
    'documents/next/':'documents_next','home/':'home','health/start/':'survey_start'};
  if(path.startsWith(prefix)) {
    const rest=path.slice(prefix.length);
    if(known[rest])return known[rest];
    if(['documents/third_party_consent/','documents/safety_pledge/'].includes(rest))return 'documents';
    if(rest.startsWith('completed/'))return 'completed';
  }
  if(/^\/surveys\/day\/\d+\/$/.test(path))return 'survey';
  if(/^\/surveys\/day\/\d+\/result\/(detail\/)?$/.test(path))return 'result';
  return null;
}
const span=(start,end)=>start>=0&&end>=start?end-start:null;
function summary(records,key) {
  const groups={};
  for(const row of records) {
    const name=row.method?row.method+' '+row.route:row.route;
    (groups[name]??=[]).push(row);
  }
  return Object.fromEntries(Object.entries(groups).map(([name,rows])=>{
    const values=rows.map(row=>row[key]).filter(value=>Number.isFinite(value)).sort((a,b)=>a-b);
    return [name,{count:rows.length,p50:values.length?values[Math.ceil(values.length*.5)-1]:null,
      p95:values.length?values[Math.ceil(values.length*.95)-1]:null,
      http_errors:rows.filter(row=>row.status>=400).length}];
  }));
}
(async()=>{
  for(const name of ['--url','--expect-host','--build','--code-digest','--output'])
    if(!process.argv.includes(name))throw Error('Required: --url --expect-host --build --code-digest --output');
  const base=new URL(arg('--url'));
  if(base.protocol!=='https:'||base.hostname!==arg('--expect-host')||
     !(base.hostname.includes('staging')||base.hostname==='self-medical-perf-comparison.onrender.com'))
    throw Error('Use only an explicitly confirmed HTTPS staging hostname.');
  if(base.username||base.password||base.search||base.hash)throw Error('Do not put credentials in the URL.');
  const token=process.env.STAGING_PERF_TOKEN;
  if(!token||token.length<32)throw Error('Set STAGING_PERF_TOKEN privately in the environment.');
  const build=arg('--build');
  const digest=arg('--code-digest');
  if(!/^[a-f0-9]{64}$/.test(digest))throw Error('Provide the expected source manifest digest.');
  if(fs.existsSync(arg('--output')))throw Error('Output exists; preserve it and use a new filename.');
  const report={build,code_digest:digest,scope:'one manual Chrome browser, actual HTTPS staging',requests:[],navigations:[],
    metric_relationship:'session/db-open/driver/query/commit timings overlap; never add them'};
  const browser=await chromium.launch({channel:'chrome',headless:false});
  const tasks=[];
  let mismatches=0;
  let collectionErrors=0;
  let collecting=true;
  const track=promise=>tasks.push(bounded(promise).catch(()=>{collectionErrors++;}));
  try {
    const context=await browser.newContext();
    // A host-only secure cookie avoids leaking a header to third-party assets
    // and avoids request routing, which disables Playwright's HTTP cache.
    await context.addCookies([{name:'__Host-staging_perf',value:token,url:base.origin+'/',
      secure:true,httpOnly:true,sameSite:'Lax'}]);
    const page=await context.newPage();
    page.on('response',response=>{
      if(!collecting)return;
      const req=response.request();const url=new URL(response.url());const phase=route(url.pathname);
      if(url.origin!==base.origin||!phase)return;
      // Preserve HTTP status even if a document replacement/aborted response
      // prevents Playwright from retrieving its body or headers afterwards.
      const record={route:phase,method:req.method(),resource_type:req.resourceType(),
        status:response.status(),collection_complete:false};
      report.requests.push(record);
      track((async()=>{
        const headers=await response.allHeaders();
        await response.finished();
        const timing=req.timing();
        const server={};
        for(const item of (headers['server-timing']||'').split(',')) {
          const match=item.trim().match(/^(app_ms|db_open_ms|driver_connect_ms|query_ms|healthcheck_ms|commit_ms|rollback_ms|session_ms);dur=([0-9.]+)/);
          if(match)server[match[1]]=Number(match[2]);
        }
        if(headers['x-perf-build']!==build||headers['x-perf-code-digest']!==digest)mismatches++;
        let redirects=0;for(let parent=req.redirectedFrom();parent;parent=parent.redirectedFrom())redirects++;
        Object.assign(record,{collection_complete:true,
          start_epoch_ms:timing.startTime,ttfb_ms:timing.responseStart>=0?timing.responseStart:null,
          response_complete_ms:timing.responseEnd>=0?timing.responseEnd:null,
          dns_ms:span(timing.domainLookupStart,timing.domainLookupEnd),
          tcp_including_tls_ms:span(timing.connectStart,timing.connectEnd),
          tls_ms:span(timing.secureConnectionStart,timing.connectEnd),redirect_chain_count:redirects,
          request_id:/^[a-f0-9]{32}$/.test(headers['x-perf-request-id']||'')?headers['x-perf-request-id']:null,
          sql_count:Number(headers['x-perf-sql-count']||0),db_opens:Number(headers['x-perf-db-opens']||0),
          worker:Number(headers['x-perf-worker']||0),
          session_saves:Number(headers['x-perf-session-saves']||0),server});
      })());
    });
    page.on('load',()=>{
      if(!collecting)return;
      track((async()=>{
        await page.waitForTimeout(20);
        if(new URL(page.url()).origin!==base.origin)return;
        const phase=route(new URL(page.url()).pathname);if(!phase)return;
        const values=await page.evaluate(()=>{
          const n=performance.getEntriesByType('navigation')[0];
          const resources=performance.getEntriesByType('resource');
          return {start_epoch_ms:performance.timeOrigin+n.startTime,ttfb_ms:n.responseStart,
            load_ms:n.loadEventEnd>0?n.loadEventEnd:null,redirect_ms:n.redirectEnd-n.redirectStart,
            redirect_count:n.redirectCount,dom_content_loaded_ms:n.domContentLoadedEventEnd,
            resource_count:resources.length,resource_transfer_bytes:resources.reduce((a,r)=>a+r.transferSize,0)};
        });
        report.navigations.push({route:phase,...values});
      })());
    });
    const first=await page.goto(base.origin+'/surveys/preview/v2/code/',{waitUntil:'load'});
    if(first.status()!==200||(await first.headerValue('x-perf-build'))!==build||
       (await first.headerValue('x-perf-code-digest'))!==digest)
      throw Error('Build/profile verification failed. Stop; do not measure an unverified service.');
    console.log('Use synthetic staging participants only. Complete code → identity/consents → home → survey → result.');
    console.log('Press Enter here after the final page finishes loading. Do not export a HAR.');
    const input=readline.createInterface({input:process.stdin,output:process.stdout});
    let finish;
    await new Promise(resolve=>{
      finish=resolve;
      input.once('line',resolve);
      input.once('SIGINT',resolve);
      process.once('SIGINT',resolve);
    });
    process.removeListener('SIGINT',finish);
    input.close();
    collecting=false;
    console.log('Saving partial timings; waiting at most 10 seconds for pending collection.');
    await Promise.all(tasks);
    report.build_mismatches=mismatches;
    report.collection_errors=collectionErrors;
    report.request_ttfb_summary=summary(report.requests,'ttfb_ms');
    report.navigation_load_summary=summary(report.navigations,'load_ms');
    report.missing_pages=['code','identity','documents','home','survey','result']
      .filter(name=>!report.navigations.some(row=>row.route===name));
    report.valid=mismatches===0&&collectionErrors===0&&report.requests.length>0&&report.missing_pages.length===0;
    fs.writeFileSync(arg('--output'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
    console.log('Report saved: '+arg('--output'));
    if(!report.valid)throw Error('Saved run is invalid: inspect build, page coverage and collection errors.');
    console.log('Saved sanitized one-browser timings. This is not a concurrent-user load test.');
  } finally {await bounded(browser.close(),5000).catch(()=>{});}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
