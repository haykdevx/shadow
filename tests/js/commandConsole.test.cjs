const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function consoleState() {
  const listeners=[];
  const ctx = vm.createContext({window: {}, document: {addEventListener:(type,fn,capture)=>listeners.push({type,fn,capture})}, console, setTimeout, clearTimeout});
  ctx.listeners=listeners;
  vm.runInContext(fs.readFileSync(process.env.CONSOLE_SOURCE || 'static/js/commandConsole.js', 'utf8').replace(/^export default.*$/m, ''), ctx);
  return ctx;
}
test('device change discards an outstanding file response', async () => {
  const ctx = consoleState();
  vm.runInContext(`S.deviceId = 'a'; let resolveFiles; runAction = () => new Promise(r => resolveFiles = r); var pending = loadFiles('/a'); S.deviceId = 'b'; resetDeviceCaches(); resolveFiles({path:'/a',entries:[]});`, ctx);
  await ctx.pending;
  assert.equal(vm.runInContext('S._files', ctx), null);
});
test('device change clears working directory and terminal drafts', () => {
  const ctx = consoleState();
  vm.runInContext(`S.termCwd='/old-device'; S.termInput='old command'; resetDeviceCaches()`, ctx);
  assert.equal(vm.runInContext('S.termCwd', ctx), '');
  assert.equal(vm.runInContext('S.termInput', ctx), '');
});
test('explicit action target takes precedence over current selection', async () => {
  const ctx = consoleState();
  vm.runInContext(`S.deviceId='b'; var sent; api = async (url, opts) => {sent=JSON.parse(opts.body); return {result:{ok:true}}}; var pending=runAction('status', {}, {id:'a'});`, ctx);
  await ctx.pending;
  assert.equal(ctx.sent.device_id, 'a');
});
for (const [input, expected] of [
  ['take a screenshot', {action:'screenshot',args:{}}],
  ['find files report', {action:'file_search',args:{query:'report'}}],
  ['volume 30', {action:'volume',args:{percent:30}}],
  ['launch app firefox', {action:'app_launch',args:{app:'firefox'}}],
  ['read file C:\\Users\\me\\notes.txt', {action:'file_read',args:{path:'C:\\Users\\me\\notes.txt'}}],
  ['open missions', {tab:'missions'}], ['open tasks', {tab:'tasks'}],
  ['shell: echo hello', {action:'shell',args:{command:'echo hello'}}],
  ['mouse_move {"x":40,"y":80}', {action:'mouse_move',args:{x:40,y:80}}],
  ['shut down device', {action:'shutdown',args:{}}],
]) test('intent: '+input, () => {
  const ctx=consoleState(); ctx.input=input;
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(parseCommandIntent(input))',ctx)),expected);
});
for (const input of ['volume 101','delete everything','echo hello','unknown_action {}','shell {bad}']) test('reject unsupported or malformed intent: '+input, () => {
  const ctx=consoleState(); ctx.input=input;
  assert.throws(()=>vm.runInContext('parseCommandIntent(input)',ctx));
});
for (const [risk,expectedCalls] of [['critical',1],['high',2]]) test('existing approval policy preserved: '+risk,async()=>{
  const ctx=consoleState();ctx.risk=risk;
  vm.runInContext(`var calls=[]; api=async (path)=>{calls.push(path); return calls.length===1?{status:'pending_confirmation',pending:{id:'test',risk}}:{result:{ok:true}}}; var pending=runAction('shell',{}, {id:'a'})`,ctx);
  const result=await ctx.pending;
  assert.equal(ctx.calls.length,expectedCalls);
  if(risk==='critical') assert.equal(result.pending.id,'test');
});
for (const [fn,field] of [['loadProcesses()','_procs'],['loadWindows()','_windowsList'],['captureScreen()','_shot'],['readClipboard()','_clip'],["openFile('/a')",'_filePreview']]) test('stale response ignored: '+fn,async()=>{
  const ctx=consoleState();
  vm.runInContext(`let resolveRequest; runAction=()=>new Promise(r=>resolveRequest=r); var pending=${fn}; resetDeviceCaches(); resolveRequest({text:'old',windows:[],processes:[]})`,ctx);
  await ctx.pending;
  assert.equal(vm.runInContext('S.'+field,ctx),null);
});
test('slow refresh may complete while a newer poll is still pending',async()=>{
  const ctx=consoleState();
  vm.runInContext(`var overviewResolvers=[]; api=(path)=>path.startsWith('/overview')?new Promise(r=>overviewResolvers.push(r)):Promise.resolve({events:[]}); var first=refresh(); var second=refresh(); overviewResolvers[0]({devices:[{id:'a'}]});`,ctx);
  await ctx.first;
  assert.equal(vm.runInContext('S.overview.devices[0].id',ctx),'a');
  vm.runInContext("overviewResolvers[1]({devices:[{id:'b'}]})",ctx);
  await ctx.second;
  assert.equal(vm.runInContext('S.overview.devices[0].id',ctx),'b');
});

test('Ctrl+K captures the shortcut once; kill switch restores prior handlers',()=>{
  const ctx=consoleState();
  vm.runInContext('var opened=0; openPalette=()=>opened++',ctx);
  const listener=ctx.listeners.find(l=>l.type==='keydown');
  assert.equal(listener.capture,true);
  let prevented=0,stopped=0;
  const event={key:'k',ctrlKey:true,preventDefault:()=>prevented++,stopImmediatePropagation:()=>stopped++};
  listener.fn(event);
  assert.equal(ctx.opened,1); assert.equal(prevented,1); assert.equal(stopped,1);
  ctx.localStorage={getItem:()=> 'false'};
  listener.fn(event);
  assert.equal(ctx.opened,1); assert.equal(prevented,1);
});
