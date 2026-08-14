// Worker 邏輯模擬測試（盤外無法驗證真實盤中行為，故以確定性輸入驗證判斷邏輯）
//
// 2026-08-13 修正紀律：休市日測試**一律餵真實檔案格式**。
// 舊版餵的是字串陣列 ['2026-08-13']，但 scripts/holidays.py 實際寫出的是
// 物件陣列 [{date,name,desc}] —— 測試因此在一個假世界裡全綠，而真實上線會失效。
// 現在直接讀 ../data/holidays.json 本尊進來測，並加一條迴歸鎖證明舊寫法必敗。
import { readFileSync } from 'node:fs';
import { taipei, inSession, priceOf, holidaySet, pingIntraday,
         isTradingDay, nextMode, nextGap, oneRound, throttle, alertDegrade, FAST } from './src/index.js';

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

// ══ 2026-08-14 快速模式（DO + alarm）新增護欄 ═══════════════════════════

console.log('\n=== 交易日判斷（前端「已收盤、未定稿」狀態靠它）===');
{
  const e2 = envWith(REAL);
  const sat = await isTradingDay(e2, at('2026-08-15T04:00:00Z'));   // 週六
  ck('週六 → 非交易日', sat.trading === false && sat.why === '週末', JSON.stringify(sat));
  const may1 = await isTradingDay(e2, at('2026-05-01T04:00:00Z'));  // 勞動節(週五)
  ck('勞動節(週五) → 非交易日', may1.trading === false && may1.why === '休市日', JSON.stringify(may1));
  const fri = await isTradingDay(e2, at('2026-08-14T04:00:00Z'));
  ck('一般週五 → 交易日', fri.trading === true, JSON.stringify(fri));
  // 關鍵：與時間無關。收盤後 16:43 問它，答案必須還是「今天是交易日」，
  // 否則前端就分不出「今天收盤了但還沒定稿」與「今天根本沒開市」。
  const late = await isTradingDay(e2, at('2026-08-14T08:43:00Z'));  // 台北 16:43
  ck('收盤後(台北 16:43) 問 → 仍答交易日（不看時間，只看日曆）', late.trading === true, JSON.stringify(late));
  ck('isTradingDay 與 inSession 不同：同一時刻 inSession 為 false',
    (await inSession(e2, at('2026-08-14T08:43:00Z'))).ok === false);
}

console.log('\n=== 模式狀態機（退回 60 秒模式的條件）===');
ck('fast + 有抓到 → 維持 fast、strikes 歸零',
  JSON.stringify(nextMode({ mode: 'fast', strikes: 2, tpe: '2026-08-14' }, 231, '2026-08-14')) ===
  JSON.stringify({ mode: 'fast', strikes: 0, event: null, reason: '' }));
{
  let m = { mode: 'fast', strikes: 0, tpe: '2026-08-14' };
  const seq = [];
  for (let i = 0; i < 3; i++) { const r = nextMode(m, 0, '2026-08-14'); seq.push(r.mode); m = { ...m, ...r }; }
  ck(`連續 ${FAST.STRIKES_TO_DEGRADE} 輪抓到 0 檔才退回（不是一抖就退）`,
    seq.join(',') === 'fast,fast,slow', seq.join(','));
  ck('退回那一次帶 degrade 事件（會觸發告警）',
    nextMode({ mode: 'fast', strikes: 2, tpe: '2026-08-14' }, 0, '2026-08-14').event === 'degrade');
}
ck('slow + 同一天 → 不自己跳回 fast（避免來回震盪）',
  nextMode({ mode: 'slow', strikes: 3, tpe: '2026-08-14' }, 231, '2026-08-14').mode === 'slow');
{
  const r = nextMode({ mode: 'slow', strikes: 3, tpe: '2026-08-13' }, 231, '2026-08-14');
  ck('slow + 換到新交易日 → 自動重試 fast（不會永久釘在慢速）', r.mode === 'fast' && r.event === 'retry', JSON.stringify(r));
}

console.log('\n=== 輪距計算 ===');
ck('fast：一輪 10.5 秒 → 1.5 秒後再來（≈12 秒輪距）', nextGap('fast', 10500) === 1500, String(nextGap('fast', 10500)));
ck('fast：一輪異常快(1 秒) → 仍等 11 秒，不打爆 MIS', nextGap('fast', 1000) === 11000, String(nextGap('fast', 1000)));
ck('fast：一輪拖到 30 秒 → 至少隔 MIN_GAP_MS 才再來', nextGap('fast', 30000) === FAST.MIN_GAP_MS);
ck('slow：退回模式輪距 ≈60 秒', nextGap('slow', 10000) === 50000, String(nextGap('slow', 10000)));

console.log('\n=== 一輪的分批與批距 ===');
{
  const realFetch2 = globalThis.fetch;
  const urls = [], t0 = Date.now();
  globalThis.fetch = async (u) => {
    urls.push(String(u));
    const q = decodeURIComponent(String(u).split('ex_ch=')[1].split('&')[0]).split('|');
    return { ok: true, json: async () => ({ msgArray: q.map((ch, i) => ({
      ch: ch.replace(/^(tse|otc)_/, ''), c: ch.replace(/^(tse|otc)_/, '').replace('.tw', ''),
      b: '10.0000_', a: '10.2000_', z: '-', y: '9.9000' })) }) };
  };
  try {
    const chs = Array.from({ length: 231 }, (_, i) => `tse_${1000 + i}.tw`);
    const r = await oneRound(chs, throttle(), { gapMs: 0 });          // gapMs=0：只測分批，不等
    ck('231 檔 → 3 批（每批 ≤100）', r.batches === 3 && urls.length === 3, `batches=${r.batches}`);
    ck('每批不超過 100 檔', urls.every(u => decodeURIComponent(u.split('ex_ch=')[1].split('&')[0]).split('|').length <= 100));
    ck('231 檔全部有報價', Object.keys(r.quotes).length === 231, String(Object.keys(r.quotes).length));
    ck('取的是買賣中價 (10.0+10.2)/2', r.quotes['1000'].p === 10.1, JSON.stringify(r.quotes['1000']));

    urls.length = 0;
    const t1 = Date.now();
    await oneRound(chs.slice(0, 300), throttle(), { gapMs: 300 });    // 3 批、批距 300ms
    const el = Date.now() - t1;
    // 最後一批之後【不】再等 —— 若多等一次，這裡會是 ~900ms 而不是 ~600ms
    ck('批距生效且最後一批後不空等（3 批 × 300ms 應約 600ms）', el >= 550 && el < 850, `elapsed=${el}ms`);

    urls.length = 0;
    globalThis.fetch = async () => { throw new Error('MIS down'); };
    const rf = await oneRound(chs, throttle(), { gapMs: 0 });
    ck('MIS 全掛 → 回 0 檔、3 批失敗，不拋例外', Object.keys(rf.quotes).length === 0 && rf.fail === 3, JSON.stringify({ n: 0, fail: rf.fail }));
  } finally { globalThis.fetch = realFetch2; }
}

console.log('\n=== 退回模式告警（絕不靜默）===');
{
  const realFetch3 = globalThis.fetch;
  let hits = [];
  globalThis.fetch = async (u, o) => { hits.push(String(u)); return { ok: true }; };
  try {
    hits = [];
    const r1 = await alertDegrade({ HC_PING_URL_INTRADAY: 'https://hc-ping.com/abc' }, '測試');
    ck('無 Telegram secret → 仍打 Healthchecks /fail，且回 false（不謊稱已通知）',
      r1 === false && hits.length === 1 && hits[0].endsWith('/fail'), JSON.stringify(hits));
    hits = [];
    const r2 = await alertDegrade({ HC_PING_URL_INTRADAY: 'https://hc-ping.com/abc', TELEGRAM_BOT_TOKEN: 'x', TELEGRAM_CHAT_ID: '1' }, '測試');
    ck('有 Telegram secret → Healthchecks 與 Telegram 各打一次', r2 === true && hits.length === 2 && /api\.telegram\.org/.test(hits[1]), JSON.stringify(hits));
    hits = [];
    const r3 = await alertDegrade({}, '測試');
    ck('兩者都沒設 → 不當機、回 false（畫面標示仍是第三條路）', r3 === false && hits.length === 0);
  } finally { globalThis.fetch = realFetch3; }
}

console.log(`\n=== ${P.length} passed, ${F.length} failed ===`);
if (F.length) { console.log('FAILED:' + F.join(' | ')); process.exit(1); }
