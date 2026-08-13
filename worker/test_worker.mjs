// Worker 邏輯模擬測試（盤外無法驗證真實盤中行為，故以確定性輸入驗證判斷邏輯）
//
// 2026-08-13 修正紀律：休市日測試**一律餵真實檔案格式**。
// 舊版餵的是字串陣列 ['2026-08-13']，但 scripts/holidays.py 實際寫出的是
// 物件陣列 [{date,name,desc}] —— 測試因此在一個假世界裡全綠，而真實上線會失效。
// 現在直接讀 ../data/holidays.json 本尊進來測，並加一條迴歸鎖證明舊寫法必敗。
import { readFileSync } from 'node:fs';
import { taipei, inSession, priceOf, holidaySet, pingIntraday } from './src/index.js';

const P = [], F = [];
const ck = (n, c, d = '') => { (c ? P : F).push(n); console.log((c ? '  [PASS] ' : '  [FAIL] ') + n + (d ? '  ' + d : '')); };
const at = (iso) => new Date(iso);

// 真實檔案本尊（不是手寫的仿製品）
const REAL = JSON.parse(readFileSync(new URL('../data/holidays.json', import.meta.url), 'utf-8'));
const envWith = (hol = null) => ({ QUOTES: { get: async (k) => (k === 'holidays' ? hol : null) } });

console.log('=== 台北時間換算 ===');
ck('UTC 01:00 → 台北 09:00', taipei(at('2026-08-13T01:00:00Z')).minutes === 540);
ck('UTC 05:30 → 台北 13:30', taipei(at('2026-08-13T05:30:00Z')).minutes === 810);
ck('跨日：UTC 2026-08-12T16:30 → 台北 08-13', taipei(at('2026-08-12T16:30:00Z')).ymd === '2026-08-13');

console.log('\n=== 盤中視窗判斷 ===');
const e = envWith();
ck('台北 08:59 → 不抓', !(await inSession(e, at('2026-08-13T00:59:00Z'))).ok);
ck('台北 09:00 → 抓', (await inSession(e, at('2026-08-13T01:00:00Z'))).ok);
ck('台北 12:00 → 抓', (await inSession(e, at('2026-08-13T04:00:00Z'))).ok);
ck('台北 13:30 → 抓(含邊界)', (await inSession(e, at('2026-08-13T05:30:00Z'))).ok);
ck('台北 13:31 → 不抓(收盤凍結)', !(await inSession(e, at('2026-08-13T05:31:00Z'))).ok);
ck('台北 03:00 深夜 → 不抓', !(await inSession(e, at('2026-08-12T19:00:00Z'))).ok);
const sat = await inSession(e, at('2026-08-15T04:00:00Z'));   // 2026-08-15 是週六
ck('週六 → 不抓', !sat.ok && sat.why === '週末', 'why=' + sat.why);

console.log('\n=== 休市日曆（餵 data/holidays.json 真實檔案，非仿製格式）===');
ck(`真實檔是物件陣列 {date,name,desc}（共 ${REAL.dates.length} 筆）`,
  Array.isArray(REAL.dates) && typeof REAL.dates[0] === 'object' && !!REAL.dates[0].date,
  JSON.stringify(REAL.dates[0]).slice(0, 60));
// 迴歸鎖：舊寫法 hol.dates.includes(ymd) 對真實檔案必定失敗。此條若變綠，代表格式又改了，須重看 inSession。
ck('迴歸鎖：舊寫法 .includes() 對真實檔案必失敗', REAL.dates.includes('2026-05-01') === false);
ck(`holidaySet 從真實檔案取出 ${REAL.dates.length} 筆日期`, holidaySet(REAL).size === REAL.dates.length);
ck('holidaySet 命中 2026-05-01(勞動節)', holidaySet(REAL).has('2026-05-01'));

const realEnv = envWith(REAL);
const hol1 = await inSession(realEnv, at('2026-05-01T04:00:00Z'));      // 台北 05-01(五) 12:00 勞動節
ck('真實檔案：勞動節(週五) 盤中 → 不抓', !hol1.ok && hol1.why === '休市日', 'why=' + hol1.why);
const hol2 = await inSession(realEnv, at('2026-02-16T01:30:00Z'));      // 台北 02-16(一) 09:30 春節
ck('真實檔案：春節(週一) 盤中 → 不抓', !hol2.ok && hol2.why === '休市日', 'why=' + hol2.why);
const nonHol = await inSession(realEnv, at('2026-08-13T04:00:00Z'));    // 台北 08-13(四) 12:00 正常交易日
ck('真實檔案：正常交易日 → 抓', nonHol.ok);

console.log('\n--- 相容性（舊/裸格式仍須接受）---');
ck('字串陣列 {dates:[...]} 仍相容', holidaySet({ dates: ['2026-08-13'] }).has('2026-08-13'));
ck('裸物件陣列 [{date}] 仍相容', holidaySet([{ date: '2026-08-13' }]).has('2026-08-13'));
ck('null / 缺 KV → 空集合，不擋交易日', holidaySet(null).size === 0);
const noHol = await inSession(envWith(null), at('2026-05-01T04:00:00Z'));
ck('KV 缺 holidays → 只擋週末，勞動節照抓(已知降級，非靜默)', noHol.ok);

console.log('\n=== 暫定價取值規則 ===');
const stock = { ch: '2330.tw', c: '2330', b: '2405.0000_2400.0000_', a: '2410.0000_2415.0000_', z: '-', y: '2395.0000' };
const r1 = priceOf(stock);
ck('個股用買賣中價 (2405+2410)/2=2407.5', r1.p === 2407.5 && r1.src === 'mid', JSON.stringify(r1));
ck('個股【不】直接用 z（z 僅 12% 有值）', priceOf({ ...stock, z: '9999' }).p === 2407.5);
const noQuote = { ch: '1234.tw', c: '1234', b: '-', a: '-', z: '-', y: '50.0000' };
ck('無買賣價且無 z → 退回昨收並標記', priceOf(noQuote).src === 'prev_close');
const idx = { ch: 't00.tw', c: 't00', z: '45389.46', y: '45120.72' };
const r2 = priceOf(idx);
ck('指數直接用 z', r2.p === 45389.46 && r2.src === 'index_z', JSON.stringify(r2));
ck('指數不受買賣價影響', priceOf({ ...idx, b: '1_', a: '2_' }).p === 45389.46);

console.log('\n=== 盤中心跳 HC_PING_URL_INTRADAY ===');
const realFetch = globalThis.fetch;
let called = [];
globalThis.fetch = async (u) => { called.push(String(u)); return { ok: true }; };
try {
  called = [];
  const r3 = await pingIntraday({}, '');
  ck('未設定 → 不 ping、回 false、印 L2-warn', r3 === false && called.length === 0);

  called = [];
  const r4 = await pingIntraday({ HC_PING_URL_INTRADAY: 'https://hc-ping.com/abc/' }, '');
  ck('已設定 → ping 且去尾斜線', r4 === true && called[0] === 'https://hc-ping.com/abc', called[0]);

  called = [];
  await pingIntraday({ HC_PING_URL_INTRADAY: 'https://hc-ping.com/abc' }, '/fail');
  ck('抓到 0 檔 → ping /fail 而非謊報平安', called[0] === 'https://hc-ping.com/abc/fail', called[0]);

  called = [];
  globalThis.fetch = async () => { throw new Error('network down'); };
  const r5 = await pingIntraday({ HC_PING_URL_INTRADAY: 'https://hc-ping.com/abc' }, '');
  ck('ping 本身失敗 → 吞掉、回 false、不拖垮 tick', r5 === false);
} finally {
  globalThis.fetch = realFetch;
}

console.log(`\n=== ${P.length} passed, ${F.length} failed ===`);
if (F.length) { console.log('FAILED:' + F.join(' | ')); process.exit(1); }
