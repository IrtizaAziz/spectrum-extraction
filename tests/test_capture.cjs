const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const js = () => fs.readFileSync(path.join(__dirname,'..','assets','capture.js'),'utf8');
const node = (text, href) => ({innerText:text, getAttribute:k=>k==='href'?href:null, getClientRects:()=>[1]});
function page(url,{main=true, authenticated=true, links=[], intro='', rows=[]}={}) {
  const root = {querySelector:s=>s.includes('#intro') && intro ? node(intro) : null,
    querySelectorAll:s=>s==='a[href]' ? links : s.includes('tr') ? rows.map(r=>({getClientRects:()=>[1],querySelectorAll:()=>r.map(t=>node(t))})) : []};
  return JSON.parse(JSON.stringify(vm.runInNewContext(js(), {URL, location:new URL(url), document:{
    querySelector:s=>s.includes('region-main') ? (main?root:null) : s.includes('h1') ? node('Example') : s.includes('login') ? null : authenticated?node('Account menu'):null
  }})));
}
const origin='https://spectrum.um.edu.my';
test('one course returns deduplicated same-origin activity references',()=>{
 const res=page(origin+'/course/view.php?id=12',{links:[node('Assignment','/mod/assign/view.php?id=34'),node('Duplicate','/mod/assign/view.php?id=34&secret=x'),node('Outside','https://elsewhere.invalid/mod/assign/view.php?id=35')]});
 assert.equal(res.status,'captured'); assert.equal(res.page.id,'12');
 assert.deepEqual(res.references,[{kind:'assign',id:'34',title:'Assignment',url:origin+'/mod/assign/view.php?id=34'}]);
 assert.equal(res.coverage,'visible_page_only');
});
test('fails closed outside the official origin and without an authenticated page',()=>{
 assert.equal(page('https://elsewhere.invalid/course/view.php?id=12').status,'out_of_scope');
 assert.equal(page(origin+'/login/index.php').status,'authentication_required');
 assert.equal(page(origin+'/course/view.php?id=12',{authenticated:false}).status,'authentication_required');
 assert.equal(page(origin+'/admin/index.php').status,'unsupported_page');
});
test('assignment preserves literal declared label and separates submission evidence',()=>{
 const res=page(origin+'/mod/assign/view.php?id=34',{intro:'Submit a report.',rows:[['Due date','Friday, 15 March 2030, 5:00 PM'],['Submission status','Submitted for grading'],['Time remaining','3 minutes']]});
 assert.equal(res.instructions,'Submit a report.');
 assert.deepEqual(res.deadline_claims,[{label:'Due date',text:'Friday, 15 March 2030, 5:00 PM'}]);
 assert.deepEqual(res.submission_evidence,[{label:'Submission status',text:'Submitted for grading'}]);
});
