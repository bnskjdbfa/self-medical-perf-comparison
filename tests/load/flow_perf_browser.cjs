// Synthetic fixture only. No cookies, input values or response HTML in reports.
const fs = require('fs');
const {chromium} = require('playwright');
(async () => {
  const fixture = JSON.parse(fs.readFileSync(process.argv[2]));
  const browser = await chromium.launch({channel:'chrome',headless:true});
  const report = {};
  try {
    for (let run=0;run<3;run++) {
      const context=await browser.newContext();
      const page=await context.newPage();
      const prefix='/surveys/preview/v2/';
      async function navigate(phase,path) {
        const errors=[];
        const handler=response=>{if(response.status()>=400)errors.push(response.status());};
        page.on('response',handler);
        const response=await page.goto(fixture.base+path,{waitUntil:'load'});
        await page.waitForTimeout(10);
        const values=await page.evaluate(()=>{
          const nav=performance.getEntriesByType('navigation')[0];
          const resources=performance.getEntriesByType('resource');
          return {ttfb_ms:nav.responseStart-nav.startTime,load_ms:nav.loadEventEnd-nav.startTime,
            redirects:nav.redirectCount,static_requests:resources.length,
            transfer_bytes:resources.reduce((sum,r)=>sum+r.transferSize,0),
            static_max_ms:Math.max(0,...resources.map(r=>r.duration))};
        });
        values.status=response.status();values.resource_errors=errors;
        if(values.status!==200 || errors.length)throw Error('navigation failed: '+phase);
        (report[phase]??=[]).push(values);
        page.off('response',handler);
      }
      async function post(path,form) {
        const csrf=(await context.cookies()).find(c=>c.name==='csrftoken')?.value;
        const response=await context.request.post(fixture.base+path,{form:{...form,csrfmiddlewaretoken:csrf}});
        if(response.status()!==200)throw Error('fixture POST failed');
      }
      await navigate('code',prefix+'code/');
      await post(prefix+'code/',{team_code:fixture.team_code});
      await navigate('privacy',prefix+'privacy-consent/');
      await post(prefix+'privacy-consent/',{agreed:'on'});
      await navigate('identity',prefix+'identity/');
      await post(prefix+'identity/',{name:`Performance Fixture ${5000+run}`,birth_date:'900101',gender:'M'});
      const doc=prefix+'documents/third_party_consent/';
      await navigate('documents',doc);
      await post(doc,{action:'record_consent',document_type:'third_party_consent',agreed:'on'});
      await post(doc,{action:'finalize',document_type:'safety_pledge',agreed:'on'});
      await navigate('home',prefix+'home/');
      await navigate('home_repeat',prefix+'home/?lang=ko');
      await navigate('survey_start',prefix+'health/start/');
      await navigate('survey','/surveys/day/1/?source=v2');
      const hidden=await page.evaluate(()=>Object.fromEntries([...document.querySelectorAll('input')]
        .filter(input=>['survey_form_id','survey_form_version','survey_form_signature'].includes(input.name))
        .map(input=>[input.name,input.value])));
      const answers=Object.fromEntries(fixture.questions.map(id=>[id,'yes']));
      await post('/surveys/day/1/?source=v2',{...hidden,...Object.fromEntries(fixture.questions.map(id=>['q_'+id,'yes'])),
        submit:'1',answers_payload:JSON.stringify(answers)});
      await navigate('result','/surveys/day/1/result/?source=user&return_to='+prefix+'home/');
      await context.close();
    }
    fs.writeFileSync(process.argv[3],JSON.stringify(report,null,2));
  } finally {await browser.close();}
})().catch(error=>{console.error(error.message);process.exit(1);});
