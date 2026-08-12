// Worker 邏輯模擬測試（盤外無法驗證真實盤中行為，故以確定性輸入驗證判斷邏輯）
import { taipei, inSession, priceOf } from './src/index.js';
const P=[],F=[]; const ck=(n,c,d='')=>{(c?P:F).push(n);console.log((c?'  [PASS] ':'  [FAIL] ')+n+(d?'  '+d:''));};
const envWith=(hol=[])=>({QUOTES:{get:async(k,o)=>k==='holidays'?{dates:hol}:null}});
const at=(iso)=>new Date(iso);

console.log('=== 台北時間換算 ===');
ck('UTC 01:00 → 台北 09:00', taipei(at('2026-08-13T01:00:00Z')).minutes===540);
ck('UTC 05:30 → 台北 13:30', taipei(at('2026-08-13T05:30:00Z')).minutes===810);
ck('跨日：UTC 2026-08-12T16:30 → 台北 08-13', taipei(at('2026-08-12T16:30:00Z')).ymd==='2026-08-13');

console.log('\n=== 盤中視窗判斷 ===');
const e=envWith();
ck('台北 08:59 → 不抓', !(await inSession(e,at('2026-08-13T00:59:00Z'))).ok);
ck('台北 09:00 → 抓',    (await inSession(e,at('2026-08-13T01:00:00Z'))).ok);
ck('台北 12:00 → 抓',    (await inSession(e,at('2026-08-13T04:00:00Z'))).ok);
ck('台北 13:30 → 抓(含邊界)', (await inSession(e,at('2026-08-13T05:30:00Z'))).ok);
ck('台北 13:31 → 不抓(收盤凍結)', !(await inSession(e,at('2026-08-13T05:31:00Z'))).ok);
ck('台北 03:00 深夜 → 不抓', !(await inSession(e,at('2026-08-12T19:00:00Z'))).ok);
const sat=await inSession(e,at('2026-08-15T04:00:00Z'));   // 2026-08-15 是週六
ck('週六 → 不抓', !sat.ok && sat.why==='週末', 'why='+sat.why);
const hol=await inSession(envWith(['2026-08-13']),at('2026-08-13T04:00:00Z'));
ck('休市日曆命中 → 不抓', !hol.ok && hol.why==='休市日', 'why='+hol.why);

console.log('\n=== 暫定價取值規則 ===');
const stock={ch:'2330.tw',c:'2330',b:'2405.0000_2400.0000_',a:'2410.0000_2415.0000_',z:'-',y:'2395.0000'};
const r1=priceOf(stock);
ck('個股用買賣中價 (2405+2410)/2=2407.5', r1.p===2407.5 && r1.src==='mid', JSON.stringify(r1));
ck('個股【不】直接用 z（z 僅 12% 有值）', priceOf({...stock,z:'9999'}).p===2407.5);
const noQuote={ch:'1234.tw',c:'1234',b:'-',a:'-',z:'-',y:'50.0000'};
ck('無買賣價且無 z → 退回昨收並標記', priceOf(noQuote).src==='prev_close');
const idx={ch:'t00.tw',c:'t00',z:'45389.46',y:'45120.72'};
const r2=priceOf(idx);
ck('指數直接用 z', r2.p===45389.46 && r2.src==='index_z', JSON.stringify(r2));
ck('指數不受買賣價影響', priceOf({...idx,b:'1_',a:'2_'}).p===45389.46);

console.log(`\n=== ${P.length} passed, ${F.length} failed ===`);
if(F.length){console.log('FAILED:'+F.join(' | '));process.exit(1);}
