// Exercise the actual template reader; synthetic values only, no browser/server.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'../..');
const template=fs.readFileSync(path.join(root,'templates/surveys/day_survey.html'),'utf8');
const start=template.indexOf('function readValueFromControls(');
const end=template.indexOf('function readVisibleValue(',start);
assert.ok(start>=0 && end>start);
const read=vm.runInNewContext('('+template.slice(start,end).trim()+')',{
  inputName:question=>`q_${question.id}`,
});
const controls=[{name:'q_20',type:'radio',value:'yes',checked:false},
                {name:'q_20',type:'radio',value:'no',checked:true}];
assert.equal(read({id:20,question_type:'single'},controls),'yes',
  'Historical invalid fixture reproduces selected no / payload yes mismatch');
assert.equal(read({id:20,question_type:'single_choice'},controls),'no',
  'Valid model choice preserves the actual selected radio value');
console.log('PASS: invalid fixture reproduces payload mismatch; valid single_choice preserves selection');
