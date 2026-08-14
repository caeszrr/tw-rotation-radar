/**
 * 台股輪動雷達 — 盤中即時層 Worker（藍圖九A / Phase 4.5）
 *
 * 職責只有一件事：把 MIS 的「原始最新價」寫進熱儲存。
 * 不做任何 RRG 計算（重計算一律放前端）。
 * 鐵則：盤中暫定值永不寫入 history.json。本 Worker 只碰 Durable Object / KV。
 *
 * ══ 2026-08-14 快速模式（藍圖 stretch goal 正式實作）══════════════════════
 *
 * 舊版：cron 每分鐘喚醒一次，抓一輪就結束 → 前端最快 60 秒一動。
 * 新版：**Durable Object 用 alarm 自己排下一輪**，每輪 3 批 × 100 檔、批距 5 秒
 *      （守「每 5 秒 ≤ 3 請求」，實際只用掉配額 1/3），一輪約 10-11 秒，
 *       每分鐘約 5 輪 → 前端 10 秒級。
 *
 * ── 為什麼不是「一次 cron 喚醒內迴圈跑一分鐘」（工單原本的寫法）─────────
 * 兩個硬限制會讓那個寫法**看起來上線了但實際不работ**：
 *
 *   1. **CPU 上限**：Workers 免費層是「每次 invocation 10ms CPU」。目前這支
 *      每分鐘跑一輪（3 次 JSON.parse，各約 80KB）是能過的；同一次 invocation
 *      塞 5 輪就是 15 次 parse，很可能直接被砍。而 CPU 超標是**整個 invocation
 *      被終止**，程式沒有機會捕捉自己的死亡 —— 典型的靜默失敗。
 *      改成「一輪一次 alarm」之後，每一輪各自拿一份 10ms 預算，
 *      每輪的工作量與現在已經實測穩定運轉的版本**完全相同**。
 *
 *   2. **Cache API 是單一機房內的**：工單原本要把熱報價放 `caches.default`
 *      （理由正確：KV 免費層寫入 1,000 次/日，10 秒級是 271 分 × 5 輪 = 1,355 次/日，必爆）。
 *      但 Cloudflare 的 Cache API **不跨機房共享**，而 cron / alarm 在哪個機房執行
 *      不保證等於使用者請求落地的機房 —— 寫進去使用者多半讀不到，
 *      而且讀不到時「沒有任何錯誤」，又是一個靜默失敗。
 *      Durable Object 是全球單一實例，寫進去誰都讀得到，且**不佔 KV 寫入額度**。
 *
 * ── 額度（全部免費層）───────────────────────────────────────────────
 *   DO 請求：寫 1,355/日 + 讀（每觀看者 6/分 × 271 分 ≈ 1,626/日）  上限 100,000/日
 *   DO 時長：約 2,100 GB-s/日                                        上限 13,000 GB-s/日
 *   KV 寫入：保底副本每分鐘 1 次 = 271/日                             上限 1,000/日
 *   對外請求：271 分 × 5 輪 × 3 批 = 4,065/日                         上限 100,000/日
 *
 * ── 護欄（工單 B4：絕不靜默）────────────────────────────────────────
 *   每輪耗時、輪數、模式全部 console.log（`wrangler tail` 看得到，並寫進 payload）。
 *   連續 STRIKES_TO_DEGRADE 輪抓不到東西 → 自動退回 60 秒模式，
 *   同時 (a) Healthchecks /fail（Caesar 已接 Telegram）(b) Telegram 直發（若有設 secret）
 *   (c) payload.mode 傳到前端，畫面右上角直接寫「60 秒模式」。三條路互相獨立。
 */

const MIS = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp';
const BATCH = 100;              // Phase 0 實測安全值（真正限制是 URL 長度）
const WINDOW_MS = 5000;         // 節流視窗
const MAX_PER_WINDOW = 3;       // 每 5 秒 ≤3 請求（硬性後盾；批距 5 秒時根本碰不到）

export const FAST = {
  // 批距 4 秒（決策 #28 / R2，Caesar 裁決 2026-08-14）。
  // 仍然守「每 5 秒 ≤3 請求」：3 批排在 t=0/4/8 秒，任何 5 秒視窗內最多 2 個請求，
  // 只用掉配額的 2/3。一輪 ≈ 8+ 秒 → 輪距約 10-11 秒（先前 5 秒批距是 12.6-14.7 秒）。
  BATCH_GAP_MS: 4000,
  BATCH_GAP_SAFE_MS: 5000,  // 退回值：連續 N 輪有批次失敗就回到 5 秒
  BATCH_FAIL_ROUNDS_TO_WIDEN: 2,
  ROUND_TARGET_MS: 10000,   // 目標輪距
  SLOW_TARGET_MS: 60000,    // 退回 60 秒模式的輪距
  MIN_GAP_MS: 1500,         // 下一次 alarm 至少隔這麼久，避免抓取異常快時打爆自己
  STRIKES_TO_DEGRADE: 3,    // 連續 N 輪抓到 0 檔 → 退回 60 秒模式
};

/** 台北時間的「當日分鐘數」與日期字串（UTC+8，無需 tz 資料庫） */
export function taipei(now) {
  const t = new Date(now.getTime() + 8 * 3600 * 1000);
  return {
    ymd: t.toISOString().slice(0, 10),
    minutes: t.getUTCHours() * 60 + t.getUTCMinutes(),
    dow: t.getUTCDay(),                       // 0=日 6=六
  };
}

/**
 * 休市日正規化 → Set<'YYYY-MM-DD'>。
 *
 * **2026-08-13 修正**：本函式原本不存在，`inSession()` 直接寫 `hol.dates.includes(ymd)`，
 * 亦即假設 dates 是字串陣列。但 `scripts/holidays.py` 實際寫出的
 * `data/holidays.json` 是**物件陣列** `{date,name,desc}` —— `.includes()` 永遠比對不中，
 * 休市日防護等於不存在。舊單元測試餵的是字串所以全綠，是測試自己造了一個假世界。
 * 現在一律走這裡正規化，並接受下列所有形狀：
 *   {dates:[{date:'YYYY-MM-DD',...}]}   ← 官方管線真實輸出
 *   {dates:['YYYY-MM-DD']}              ← 舊假設，保留相容
 *   [{date:...}] / ['YYYY-MM-DD']       ← 裸陣列
 */
export function holidaySet(hol) {
  const raw = Array.isArray(hol) ? hol : (hol && Array.isArray(hol.dates) ? hol.dates : []);
  const out = new Set();
  for (const r of raw) {
    if (typeof r === 'string') { if (r) out.add(r.slice(0, 10)); continue; }
    if (r && typeof r === 'object') { const d = r.date || r.Date || r.日期; if (d) out.add(String(d).slice(0, 10)); }
  }
  return out;
}

/** 盤中視窗：台北交易日 09:00–13:30。休市日曆由 KV 的 holidays 提供（缺就只擋週末）。 */
export async function inSession(env, now) {
  const { ymd, minutes, dow } = taipei(now);
  if (dow === 0 || dow === 6) return { ok: false, why: '週末' };
  if (minutes < 9 * 60 || minutes > 13 * 60 + 30) return { ok: false, why: `非盤中(台北 ${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')})` };
  const hol = await env.QUOTES.get('holidays', { type: 'json' });
  if (holidaySet(hol).has(ymd)) return { ok: false, why: '休市日' };
  return { ok: true, ymd };
}

/**
 * 今天是不是「台北交易日」——不看時間，只看週末與休市日曆。
 *
 * 前端要分辨「今天已收盤、尚未定稿」與「今天根本沒開市」需要這個答案，
 * 而前端手上沒有休市日曆。放在這裡是為了**不要再多一份日曆檔**：
 * 兩份日曆就是兩個真相，而這個專案已經被「兩份 index.html／兩份 history.json」
 * 咬過兩次了。
 */
export async function isTradingDay(env, now) {
  const { ymd, dow } = taipei(now);
  if (dow === 0 || dow === 6) return { trading: false, why: '週末', ymd };
  const hol = await env.QUOTES.get('holidays', { type: 'json' });
  if (holidaySet(hol).has(ymd)) return { trading: false, why: '休市日', ymd };
  return { trading: true, why: '交易日', ymd };
}

/**
 * L4 盤中心跳：只有「真的成功寫進熱儲存」才 ping，否則這條心跳會變成謊報平安。
 * 未設定 HC_PING_URL_INTRADAY 時優雅跳過並印一行 L2-warn（與 scripts/notify.py 同紀律）。
 * 設定方式：`npx wrangler secret put HC_PING_URL_INTRADAY`
 */
export async function pingIntraday(env, suffix = '') {
  const url = (env && env.HC_PING_URL_INTRADAY ? String(env.HC_PING_URL_INTRADAY) : '').trim();
  if (!url) { console.log('L2-warn: HC_PING_URL_INTRADAY 未設定 → 跳過盤中心跳（沉默失敗防護尚未生效）'); return false; }
  try {
    await fetch(url.replace(/\/$/, '') + suffix, { method: 'GET' });
    console.log(`L4 盤中心跳已送出${suffix}`);
    return true;
  } catch (e) {
    console.log(`L4 盤中心跳送出失敗：${e.message}`);
    return false;
  }
}

/**
 * 退回 60 秒模式時的告警。**絕不靜默**：三條路各自獨立，任一條通就有人知道。
 *   1) console（`wrangler tail` 看得到）
 *   2) Healthchecks /fail（Caesar 已把它接到 Telegram）
 *   3) Telegram 直發（Worker 有設 secret 才有；沒設就印 L2-warn，不當作已通知）
 * 加上 payload.mode 會傳到前端，畫面直接寫「60 秒模式」——
 * 就算上面三條全斷，使用者眼睛也看得到，不會誤以為自己在看 10 秒級資料。
 */
export async function alertWorker(env, text) {
  await pingIntraday(env, '/fail');
  const tok = (env && env.TELEGRAM_BOT_TOKEN ? String(env.TELEGRAM_BOT_TOKEN) : '').trim();
  const chat = (env && env.TELEGRAM_CHAT_ID ? String(env.TELEGRAM_CHAT_ID) : '').trim();
  // Worker 的 Telegram secret 目前刻意不設（決策 #29 deferred）：
  // 靠 Healthchecks→Telegram、日線管線的 L6 事後檢查、以及畫面標示三條路。
  if (!tok || !chat) { console.log('L2-warn: Worker 未設 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID → 本則告警只走 Healthchecks 與畫面標示'); return false; }
  try {
    await fetch(`https://api.telegram.org/bot${tok}/sendMessage`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chat, text }),
    });
    return true;
  } catch (e) { console.log(`Telegram 告警送出失敗：${e.message}`); return false; }
}

export async function alertDegrade(env, reason) {
  console.log(`L5-degrade 退回 60 秒模式：${reason}`);
  return alertWorker(env, `🟠 台股輪動雷達 盤中即時層已退回 60 秒模式\n原因：${reason}\n畫面右上角會標示「60 秒模式」。`);
}

/** 批距自動放寬（決策 #28 護欄）：連續 N 輪有批次失敗 → 退回 5 秒批距並告警。 */
export async function alertWiden(env, reason) {
  console.log(`L5-widen 批距退回 ${FAST.BATCH_GAP_SAFE_MS}ms：${reason}`);
  return alertWorker(env, `🟠 台股輪動雷達 盤中批距已自動退回 ${FAST.BATCH_GAP_SAFE_MS / 1000} 秒\n原因：${reason}\n更新頻率會從約 10 秒放慢到約 13 秒；下一個交易日自動重試 ${FAST.BATCH_GAP_MS / 1000} 秒。`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 節流器：每 WINDOW_MS 內最多 MAX_PER_WINDOW 次（批距已是 5 秒，這是硬性後盾） */
export function throttle() {
  const stamps = [];
  return async () => {
    for (;;) {
      const now = Date.now();
      while (stamps.length && now - stamps[0] > WINDOW_MS) stamps.shift();
      if (stamps.length < MAX_PER_WINDOW) { stamps.push(now); return; }
      await sleep(WINDOW_MS - (now - stamps[0]) + 30);
    }
  };
}

/** 個股：買賣中價；指數：直接用 z（Phase 0 實測 z 在個股僅 12% 有值） */
export function priceOf(m) {
  const isIndex = m.ch === 't00.tw' || m.ch === 'o00.tw';
  const num = (v) => { const n = Number(v); return Number.isFinite(n) && n > 0 ? n : null; };
  if (isIndex) return { p: num(m.z), src: 'index_z' };
  const bid = num((m.b || '').split('_')[0]);
  const ask = num((m.a || '').split('_')[0]);
  if (bid && ask) return { p: (bid + ask) / 2, src: 'mid' };
  const z = num(m.z);
  if (z) return { p: z, src: 'z' };                    // 退而求其次
  const y = num(m.y);
  return y ? { p: y, src: 'prev_close' } : { p: null, src: 'none' };
}

async function fetchBatch(gate, chs) {
  await gate();
  const url = `${MIS}?ex_ch=${encodeURIComponent(chs.join('|'))}&json=1&delay=0&_=${Date.now()}`;
  const res = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://mis.twse.com.tw/stock/index.jsp' },
    cf: { cacheTtl: 0, cacheEverything: false },
  });
  if (!res.ok) throw new Error(`MIS HTTP ${res.status}`);
  const j = await res.json();
  return Array.isArray(j.msgArray) ? j.msgArray : [];
}

/**
 * 一輪＝把整個 universe 掃一遍（3 批 × 100 檔，批距 gapMs）。
 * 最後一批之後不再等 —— 那 5 秒是純粹的浪費，會把輪距從 10 秒拖成 15 秒。
 */
export async function oneRound(chs, gate, opts = {}) {
  const gap = opts.gapMs == null ? FAST.BATCH_GAP_MS : opts.gapMs;
  const quotes = {};
  let ok = 0, fail = 0, batches = 0;
  for (let i = 0; i < chs.length; i += BATCH) {
    if (batches > 0 && gap > 0) await sleep(gap);
    batches++;
    try {
      const rows = await fetchBatch(gate, chs.slice(i, i + BATCH));
      for (const m of rows) {
        const { p, src } = priceOf(m);
        const yc = Number(m.y);
        if (p) {
          quotes[m.c] = { p: Math.round(p * 10000) / 10000, s: src,
                          prev: Number.isFinite(yc) && yc > 0 ? yc : null,   // 昨收：前端算當日報酬用
                          t: m.t || m['%'] || null };
          ok++;
        }
      }
    } catch (e) { fail++; }
  }
  return { quotes, ok, fail, batches };
}

/**
 * 模式狀態機（純函式，好測）。
 *   fast：連續 STRIKES_TO_DEGRADE 輪抓到 0 檔 → 退回 slow 並告警
 *   slow：換到新的交易日自動重試 fast（避免一次瞬時抖動把系統永久釘在慢速）
 */
export function nextMode(prev, n, tpeYmd) {
  const mode = (prev && prev.mode) || 'fast';
  const strikes = (prev && Number(prev.strikes)) || 0;
  const day = prev && prev.tpe;
  if (mode === 'slow') {
    if (day && day !== tpeYmd) return { mode: 'fast', strikes: 0, event: 'retry', reason: '新交易日自動重試快速模式' };
    return { mode: 'slow', strikes: 0, event: null, reason: '' };
  }
  if (n > 0) return { mode: 'fast', strikes: 0, event: null, reason: '' };
  const s = strikes + 1;
  if (s >= FAST.STRIKES_TO_DEGRADE) return { mode: 'slow', strikes: s, event: 'degrade', reason: `連續 ${s} 輪抓到 0 檔` };
  return { mode: 'fast', strikes: s, event: null, reason: '' };
}

/** 下一次 alarm 的間隔：目標輪距扣掉本輪已用掉的時間，但不小於 MIN_GAP_MS。 */
export function nextGap(mode, roundMs) {
  const target = mode === 'slow' ? FAST.SLOW_TARGET_MS : FAST.ROUND_TARGET_MS;
  return Math.max(FAST.MIN_GAP_MS, target - roundMs);
}

/**
 * 批距狀態機（決策 #28 護欄，純函式好測）。
 *
 * 4 秒批距是「對證交所端點的禮貌參數」被主動收緊的結果，所以要有一條路自己走回去：
 *   連續 BATCH_FAIL_ROUNDS_TO_WIDEN 輪【任一批失敗】→ 退回 5 秒並告警
 *   中間只要有一輪乾淨 → 連敗歸零（是「連續」，不是「累計」）
 *   換到新交易日 → 自動重試 4 秒（避免一次瞬時抖動把系統永久釘在保守值）
 *
 * 注意：這與 nextMode() 的「抓到 0 檔 → 60 秒模式」是兩層不同的護欄。
 * 批次失敗但仍抓到資料 → 只放寬批距；整輪抓到 0 檔 → 才降到 60 秒模式。
 */
export function nextBatchGap(prev, roundHadFail, tpeYmd) {
  const cur = (prev && Number(prev.gapMs)) || FAST.BATCH_GAP_MS;
  const streak = (prev && Number(prev.failStreak)) || 0;
  const day = prev && prev.tpe;
  if (cur !== FAST.BATCH_GAP_MS && day && day !== tpeYmd)
    return { gapMs: FAST.BATCH_GAP_MS, failStreak: 0, event: 'retry', reason: `新交易日自動重試 ${FAST.BATCH_GAP_MS / 1000} 秒批距` };
  if (!roundHadFail) return { gapMs: cur, failStreak: 0, event: null, reason: '' };
  const s = streak + 1;
  if (cur === FAST.BATCH_GAP_MS && s >= FAST.BATCH_FAIL_ROUNDS_TO_WIDEN)
    return { gapMs: FAST.BATCH_GAP_SAFE_MS, failStreak: s, event: 'widen', reason: `連續 ${s} 輪有批次失敗` };
  return { gapMs: cur, failStreak: s, event: null, reason: '' };
}

const J = (o, status = 200) => new Response(JSON.stringify(o), {
  status,
  headers: { 'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store' },
});

/**
 * ── Durable Object：熱報價的唯一真相 ──────────────────────────────────
 * 全球單一實例（`idFromName('latest')`），所以任何機房的請求讀到的都是同一份。
 * 用 alarm 自己排下一輪（10-12 秒），每一輪是獨立的 invocation、各自一份 CPU 預算。
 * 收盤（inSession 為 false）就不再續排，鬧鐘自然停下；隔天 cron 的 /ensure 再點火。
 *
 * 注意：用的是「經典」DO 寫法（class + fetch + alarm），不 import 'cloudflare:workers'，
 * 這樣 worker/test_worker.mjs 才能用純 Node 直接 import 本檔做單元測試。
 */
export class Latest {
  constructor(state, env) { this.state = state; this.env = env; }

  async load() {
    if (this.meta === undefined) this.meta = (await this.state.storage.get('meta')) || { mode: 'fast', strikes: 0, tpe: null, kvMin: null };
    return this.meta;
  }

  async fetch(request) {
    const u = new URL(request.url);
    if (u.pathname === '/get') {
      const v = this.body || (await this.state.storage.get('body')) || '';
      return new Response(v, { headers: { 'Content-Type': 'application/json; charset=utf-8' } });
    }
    if (u.pathname === '/ensure') {
      // cron 每分鐘點一次火：鬧鐘沒排就排。已排就什麼都不做（不會疊加）。
      const cur = await this.state.storage.getAlarm();
      if (cur == null) { await this.state.storage.setAlarm(Date.now() + 100); return J({ armed: true }); }
      return J({ armed: false, at: cur });
    }
    if (u.pathname === '/meta') return J({ meta: await this.load(), alarm: await this.state.storage.getAlarm(), sim: (await this.state.storage.get('sim')) || null });
    /**
     * /sim：演練用的模擬時鐘（對應日線管線的 RADAR_SIMULATE_DATE，同一套紀律）。
     * 把時鐘平移 offset 毫秒、跑 rounds 輪完整鬧鐘鏈，然後自動關掉。
     *
     * 為什麼要有它：盤中鬧鐘鏈一天只有 09:00–13:30 會動，不演練就只能「寫好了，週一見」——
     * 而這個專案已經被「看起來上線了、其實沒動」咬過兩次。
     * 演練期間**所有寫入都導到 _sim 影子鍵**：不碰 body、不碰 KV、不發心跳、不發告警，
     * 所以正式資料完全不受污染。每一輪都會印 L0-SIMULATE 標記。
     */
    if (u.pathname === '/sim') {
      const offset = Number(u.searchParams.get('offset')) || 0;
      const rounds = Math.min(8, Math.max(1, Number(u.searchParams.get('rounds')) || 1));
      await this.state.storage.put('sim', { offset, left: rounds, started: new Date().toISOString() });
      await this.state.storage.setAlarm(Date.now() + 100);
      return J({ sim: true, offset, rounds, simulated_now: new Date(Date.now() + offset).toISOString() });
    }
    if (u.pathname === '/simresult') return J({ body: (await this.state.storage.get('body_sim')) || null, meta: (await this.state.storage.get('meta_sim')) || null, sim: (await this.state.storage.get('sim')) || null });
    return J({ endpoints: ['/get', '/ensure', '/meta', '/sim', '/simresult'] });
  }

  async alarm() {
    const sim = await this.state.storage.get('sim');
    const now = new Date(Date.now() + (sim ? sim.offset : 0));
    if (sim) console.log(`L0-SIMULATE 演練模式：模擬時鐘 ${now.toISOString()}（尚餘 ${sim.left} 輪，所有寫入導向影子鍵）`);

    const sess = await inSession(this.env, now);
    if (!sess.ok) {
      console.log(`L3 ${sess.why} → 停止續排鬧鐘`);
      if (sim) await this.state.storage.delete('sim');
      return;                                                    // 收盤：鏈條自然結束
    }

    const universe = await this.env.QUOTES.get('universe', { type: 'json' });
    if (!universe || !Array.isArray(universe.ch) || !universe.ch.length) {
      console.log('L3-error KV 缺少 universe（需先跑 scripts/feed_kv.py）→ 60 秒後重試');
      await this.state.storage.setAlarm(Date.now() + FAST.SLOW_TARGET_MS);
      return;
    }
    const meta = sim ? ((await this.state.storage.get('meta_sim')) || { mode: 'fast', strikes: 0, tpe: null, kvMin: null }) : await this.load();
    const tpe = taipei(now);

    const gapMs = Number(meta.gapMs) || FAST.BATCH_GAP_MS;
    const t0 = Date.now();
    const r = await oneRound(universe.ch, throttle(), { gapMs });
    const roundMs = Date.now() - t0;
    const n = Object.keys(r.quotes).length;

    const nm = nextMode(meta, n, tpe.ymd);
    const ng = nextBatchGap(meta, r.fail > 0, tpe.ymd);
    const payload = {
      ts: new Date().toISOString(),
      tpe: tpe.ymd,
      session: 'intraday',
      n, batches_failed: r.fail,
      mode: nm.mode,
      batch_gap_ms: ng.gapMs,          // 前端據此判斷「是不是已經退回保守批距」
      round_ms: roundMs,
      next_gap_ms: nextGap(nm.mode, roundMs),
      strikes: nm.strikes,
      quotes: r.quotes,
    };
    if (sim) payload.sim = true;
    const body = JSON.stringify(payload);
    meta.mode = nm.mode; meta.strikes = nm.strikes; meta.tpe = tpe.ymd;
    meta.gapMs = ng.gapMs; meta.failStreak = ng.failStreak;

    if (sim) {
      // 演練：一律寫影子鍵，不碰正式資料、不發心跳、不發告警
      await this.state.storage.put('body_sim', body);
      await this.state.storage.put('meta_sim', meta);
    } else {
      this.body = body;
      await this.state.storage.put('body', body);
      // KV 保底副本：每分鐘至多一次（271/日，遠低於免費層 1,000/日）。
      // 目的只有一個 —— DO 若因故讀不到，/latest 還有東西可端，而且會誠實標示 src:'kv'。
      if (meta.kvMin !== tpe.minutes) {
        try { await this.env.QUOTES.put('latest', body); meta.kvMin = tpe.minutes; }
        catch (e) { console.log(`KV 保底寫入失敗：${e.message}`); }
      }
      await this.state.storage.put('meta', meta);
      if (nm.event === 'degrade') await alertDegrade(this.env, nm.reason);
      if (nm.event === 'retry') console.log(`L5 ${nm.reason}`);
      if (ng.event === 'widen') await alertWiden(this.env, ng.reason);
      if (ng.event === 'retry') console.log(`L5 ${ng.reason}`);
      // 心跳在【寫入之後】才發：抓到 0 檔不算成功，否則心跳綠燈但畫面沒資料。
      // 每分鐘至多一次，不然 Healthchecks 會被打爆。
      if (meta.hcMin !== tpe.minutes) { meta.hcMin = tpe.minutes; await pingIntraday(this.env, n > 0 ? '' : '/fail'); }
    }

    const gap = nextGap(nm.mode, roundMs);
    console.log(`L3${sim ? '-SIM' : ''} 一輪完成：${n} 檔、失敗批 ${r.fail}、耗時 ${roundMs}ms、批距 ${ng.gapMs}ms、模式 ${nm.mode}、${gap}ms 後再來`);

    if (sim) {
      sim.left -= 1;
      if (sim.left <= 0) { await this.state.storage.delete('sim'); console.log('L0-SIMULATE 演練結束，鬧鐘鏈停止（正式資料未受影響）'); return; }
      await this.state.storage.put('sim', sim);
    }
    await this.state.storage.setAlarm(Date.now() + gap);
  }
}

/** 沒有 DO 綁定時的降級路徑：維持舊行為（每分鐘一輪，寫 KV）。 */
export async function tickFallback(env, now = new Date()) {
  const sess = await inSession(env, now);
  if (!sess.ok) return { skipped: true, why: sess.why };
  const universe = await env.QUOTES.get('universe', { type: 'json' });
  if (!universe || !Array.isArray(universe.ch) || !universe.ch.length) return { error: 'KV 缺少 universe' };
  const t0 = Date.now();
  const r = await oneRound(universe.ch, throttle(), { gapMs: FAST.BATCH_GAP_MS });
  const n = Object.keys(r.quotes).length;
  const body = JSON.stringify({ ts: new Date().toISOString(), tpe: taipei(now).ymd, session: 'intraday',
    n, batches_failed: r.fail, mode: 'slow', round_ms: Date.now() - t0, degraded_reason: 'DO 綁定不存在，走每分鐘一輪的降級路徑', quotes: r.quotes });
  await env.QUOTES.put('latest', body);
  await pingIntraday(env, n > 0 ? '' : '/fail');
  console.log(`L3-fallback 一輪：${n} 檔（無 DO 綁定，60 秒模式）`);
  return { written: n, mode: 'slow' };
}

/** 讀熱資料：DO 優先，失敗才回頭讀 KV，並誠實標示來源。 */
async function readLatest(env) {
  if (env.LATEST) {
    try {
      const stub = env.LATEST.get(env.LATEST.idFromName('latest'));
      const txt = await (await stub.fetch('https://do/get')).text();
      if (txt) { const j = JSON.parse(txt); j.src = 'do'; return j; }
    } catch (e) { console.log(`DO 讀取失敗，改讀 KV：${e.message}`); }
  }
  const kv = await env.QUOTES.get('latest');
  if (!kv) return null;
  try { const j = JSON.parse(kv); j.src = 'kv'; return j; } catch (e) { return null; }
}

export default {
  async scheduled(event, env, ctx) {
    // cron 只負責「點火」：確認鬧鐘鏈還活著。真正的抓取在 DO 的 alarm 裡。
    ctx.waitUntil((async () => {
      const now = new Date(event.scheduledTime);
      const sess = await inSession(env, now);
      if (!sess.ok) return;                                  // 盤外：不發任何對外請求
      if (!env.LATEST) { await tickFallback(env, now); return; }
      const stub = env.LATEST.get(env.LATEST.idFromName('latest'));
      const r = await (await stub.fetch('https://do/ensure')).json();
      if (r.armed) console.log('L3 鬧鐘鏈已點火（本日第一次，或上一輪意外中斷）');
    })());
  },

  async fetch(request, env) {
    const u = new URL(request.url);
    if (u.pathname === '/latest') {
      const j = await readLatest(env);
      return J(j || { error: 'no data yet' });
    }
    if (u.pathname === '/health') {
      const now = new Date();
      const [s, td, j] = [await inSession(env, now), await isTradingDay(env, now), await readLatest(env)];
      return J({ ok: true, session: s, today: td,
        latest: j ? { ts: j.ts, tpe: j.tpe, n: j.n, mode: j.mode || 'fast', round_ms: j.round_ms, src: j.src } : null });
    }
    /**
     * /selftest：強制跑【一輪】並回報耗時，不管在不在盤中。
     * 存在的唯一理由是「量得到才算數」—— Workers 不讓程式讀自己的 CPU 時間，
     * 要知道一輪吃掉多少 CPU 只能真的跑一次，再用 `npx wrangler tail` 看 cpuTime。
     * 必須帶 token（`wrangler secret put SELFTEST_TOKEN`）；沒設 secret 就整個關閉，
     * 不會留下「忘了關的後門」。
     */
    if (u.pathname === '/selftest') {
      const want = (env.SELFTEST_TOKEN || '').trim();
      if (!want || u.searchParams.get('token') !== want) return J({ error: 'forbidden' }, 403);
      // 演練入口（全部 token 保護）：
      //   mode=ensure   → 點火，驗證鬧鐘真的會被叫起來
      //   mode=sim      → 用模擬時鐘跑 N 輪完整鬧鐘鏈（寫影子鍵，不動正式資料）
      //   mode=result   → 讀演練結果
      //   mode=meta     → 讀目前模式／鬧鐘時間
      const mode = u.searchParams.get('mode');
      /**
       * mode=ping：從 Worker 內部真的打一次盤中心跳。
       * 這是唯一能在盤外證明「secret 設對了 + Worker 連得到 hc-ping + URL 沒打錯」的方法
       * —— 否則要等到週一 09:00 才知道，而那時若沒設對，沉默失敗防護本身就是沉默的。
       * ⚠️ 有副作用：會讓該 check 開始計時。若 check 用的是 Period 而非 cron schedule，
       * 盤外沒有後續心跳，Period+Grace 之後就會發出「逾時」通知（見 README/交接文件）。
       */
      if (mode === 'ping') {
        const sent = await pingIntraday(env, u.searchParams.get('fail') === '1' ? '/fail' : '');
        return J({ ping_sent: sent, configured: !!(env.HC_PING_URL_INTRADAY || '').trim(),
          note: sent ? '心跳已由 Worker 送出（值不列印）' : 'HC_PING_URL_INTRADAY 未設定或送出失敗' });
      }
      if (mode && env.LATEST) {
        const stub = env.LATEST.get(env.LATEST.idFromName('latest'));
        const path = mode === 'ensure' ? '/ensure'
          : mode === 'sim' ? `/sim?offset=${Number(u.searchParams.get('offset')) || 0}&rounds=${Number(u.searchParams.get('rounds')) || 1}`
          : mode === 'result' ? '/simresult' : '/meta';
        return new Response(await (await stub.fetch('https://do' + path)).text(),
          { headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' } });
      }
      const universe = await env.QUOTES.get('universe', { type: 'json' });
      if (!universe || !Array.isArray(universe.ch)) return J({ error: 'KV 缺少 universe' });
      const gap = Number(u.searchParams.get('gap'));
      const t0 = Date.now();
      const r = await oneRound(universe.ch, throttle(), { gapMs: Number.isFinite(gap) ? gap : FAST.BATCH_GAP_MS });
      const ms = Date.now() - t0;
      console.log(`L9-selftest 一輪：${Object.keys(r.quotes).length} 檔、批 ${r.batches}、失敗 ${r.fail}、牆鐘 ${ms}ms`);
      return J({ n: Object.keys(r.quotes).length, batches: r.batches, fail: r.fail, wall_ms: ms,
        note: 'wall_ms 含批距等待；真正的 CPU 時間看 wrangler tail 的 cpuTime' });
    }
    return J({ endpoints: ['/latest', '/health'] });
  },
};
