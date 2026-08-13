/**
 * 台股輪動雷達 — 盤中即時層 Worker（藍圖九A / Phase 4.5）
 *
 * 職責只有一件事：把 MIS 的「原始最新價」寫進 KV。
 * 不做任何 RRG 計算（避開免費層 10ms CPU 上限）——重計算一律放前端。
 *
 * 鐵則：盤中暫定值永不寫入 history.json。本 Worker 只碰 KV。
 */

const MIS = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp';
const BATCH = 100;          // Phase 0 實測安全值（真正限制是 URL 長度）
const WINDOW_MS = 5000;     // 節流視窗
const MAX_PER_WINDOW = 3;   // 每 5 秒 ≤3 請求

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
 * L4 盤中心跳：只有「真的成功寫進 KV」才 ping，否則這條心跳會變成謊報平安。
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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 節流器：每 WINDOW_MS 內最多 MAX_PER_WINDOW 次 */
function throttle() {
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

export async function tick(env, now = new Date()) {
  const sess = await inSession(env, now);
  if (!sess.ok) return { skipped: true, why: sess.why };

  const universe = await env.QUOTES.get('universe', { type: 'json' });
  if (!universe || !Array.isArray(universe.ch) || !universe.ch.length) {
    return { error: 'KV 缺少 universe（需先由 daily 管線寫入 ex_ch 清單）' };
  }
  const gate = throttle();
  const chs = universe.ch;
  const quotes = {};
  let ok = 0, fail = 0;
  for (let i = 0; i < chs.length; i += BATCH) {
    const slice = chs.slice(i, i + BATCH);
    try {
      const rows = await fetchBatch(gate, slice);
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
  const payload = {
    ts: now.toISOString(),
    tpe: taipei(now).ymd,
    session: 'intraday',
    n: Object.keys(quotes).length,
    batches_failed: fail,
    quotes,
  };
  await env.QUOTES.put('latest', JSON.stringify(payload));
  // 心跳在【put 之後】才發：抓到 0 檔或全批失敗不算成功，否則心跳綠燈但畫面沒資料。
  await pingIntraday(env, payload.n > 0 ? '' : '/fail');
  return { written: payload.n, ok, fail };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env, new Date(event.scheduledTime)));
  },
  // 供前端讀取（也方便手動驗證）；純讀 KV，不觸發抓取。
  async fetch(request, env) {
    const u = new URL(request.url);
    const cors = {
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'no-store',
      'Content-Type': 'application/json; charset=utf-8',
    };
    if (u.pathname === '/latest') {
      const v = await env.QUOTES.get('latest');
      return new Response(v || JSON.stringify({ error: 'no data yet' }), { headers: cors });
    }
    if (u.pathname === '/health') {
      const s = await inSession(env, new Date());
      return new Response(JSON.stringify({ ok: true, session: s }), { headers: cors });
    }
    return new Response(JSON.stringify({ endpoints: ['/latest', '/health'] }), { headers: cors });
  },
};
