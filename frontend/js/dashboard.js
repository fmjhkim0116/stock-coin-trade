/* 대시보드 탭: 서버 응답은 모두 esc()를 거쳐 출력한다. 수신 실패·미설정은 숨기지 않고 사유를 보여 준다. */
(() => {
  const $ = selector => document.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const num = (v, d = 0) => v === null || v === undefined || Number.isNaN(Number(v)) ? '–' : Number(v).toLocaleString('ko-KR', { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (v, d = 2) => v === null || v === undefined ? '–' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(d)}%`;
  const rate = v => v === null || v === undefined ? '–' : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`;
  const dir = v => v > 0 ? 'up' : v < 0 ? 'down' : '';
  const TABS = ['overview', 'flows', 'fundamentals', 'portfolio', 'conditions', 'ai'];
  const loaded = new Set();
  let currentUser = null;
  const shared = {};  // helpers for dashboard-ai.js (filled below)

  async function getJson(path, options) {
    const res = await apiFetch(path, options);
    let body = null;
    try { body = await res.json(); } catch { /* non-JSON error page */ }
    return { ok: res.ok, status: res.status, body: body || {} };
  }

  function table(columns, rows, empty = '표시할 데이터가 없습니다.') {
    if (!rows.length) return `<div class="dash-empty">${esc(empty)}</div>`;
    const headCls = c => { try { return typeof c.cls === 'function' ? c.cls({}) : (c.cls || ''); } catch { return ''; } };
    const head = columns.map(c => `<th class="${headCls(c)}">${esc(c.h)}</th>`).join('');
    const body = rows.map(r => `<tr>${columns.map(c => `<td class="${(typeof c.cls === 'function' ? c.cls(r) : c.cls) || ''}">${c.f(r)}</td>`).join('')}</tr>`).join('');
    return `<div class="dash-table"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  const stockLink = r => `<a href="/trade/stock.html?symbol=${encodeURIComponent(r.symbol)}">${esc(r.name || r.symbol)}</a> <small>${esc(r.symbol)}</small>`;
  const meta = (el, parts) => { $(el).textContent = parts.filter(Boolean).join(' · '); };
  const unavailable = (el, body, fallback) => {
    $(el).hidden = false; $(el).className = 'dash-empty';
    $(el).innerHTML = `<b>${esc(body.reason || fallback)}</b>${body.configured === false ? '<br><small>서버 환경변수 <code>DART_API_KEY</code>(OpenDART 인증키)를 설정하면 표시됩니다.</small>' : ''}`;
  };

  Object.assign(shared, { esc, num, pct, rate, dir, table, getJson, meta, stockLink });
  window.dashUtil = shared;

  /* ── 1. 지수 · 브리핑 ───────────────────────────────────────── */
  async function loadOverview() {
    const { ok, body } = await getJson('/api/dashboard/overview');
    if (!ok) { $('#indexGrid').innerHTML = `<div class="dash-empty">지수를 불러오지 못했습니다.</div>`; return; }
    $('#indexGrid').innerHTML = body.indices.map(i => i.available
      ? `<div class="index-card"><small>${esc(i.name)}</small><b>${num(i.price, 2)}</b><span class="${dir(i.changeRate)}">${num(i.change, 2)} (${rate(i.changeRate)})</span><em>기준 ${esc(i.asOf)}</em></div>`
      : `<div class="index-card off"><small>${esc(i.name)}</small><b>수신 실패</b><em>${esc(i.reason || '')}</em></div>`).join('');
    meta('#overviewMeta', [`조회 ${body.asOf}`, (body.sources || [])[0]]);
    $('#briefingList').innerHTML = body.briefing.length
      ? body.briefing.map(b => `<li class="k-${esc(b.kind)}">${esc(b.text)}</li>`).join('')
      : '<li class="dash-empty">브리핑을 만들 수 있는 시세를 받지 못했습니다.</li>';
    meta('#briefingMeta', body.limits);
  }

  /* ── 2. 수급 Top15 · 시간외 ─────────────────────────────────── */
  const flowCols = (key, label) => [
    { h: '#', f: r => r.rank, cls: 'c' }, { h: '종목', f: stockLink },
    { h: label, f: r => num(r[key]), cls: r => 'r ' + dir(r[key]) },
  ];
  async function loadFlows() {
    const days = $('#flowDays').value;
    const { body } = await getJson(`/api/dashboard/flows?days=${encodeURIComponent(days)}`);
    if (!body.available) { $('#flowsTables').hidden = true; unavailable('#flowsStatus', body, '수급 데이터를 불러오지 못했습니다.'); $('#flowsMeta').textContent = ''; return; }
    $('#flowsStatus').hidden = true; $('#flowsTables').hidden = false;
    $('#flowForeign').innerHTML = table(flowCols('foreign_net', '순매수'), body.foreign, '순매수 종목이 없습니다.');
    $('#flowInstitution').innerHTML = table(flowCols('institution_net', '순매수'), body.institution, '순매수 종목이 없습니다.');
    $('#flowBoth').innerHTML = table(flowCols('combined', '합계'), body.both, '동반 순매수 종목이 없습니다.');
    meta('#flowsMeta', [`기준일 ${body.asOf}${body.from !== body.asOf ? ` (${body.from} ~)` : ''}`, `${body.symbols}종목`, body.source, body.note]);
    $('#flowsStatus').hidden = true;
  }
  async function loadOvertime() {
    const { body } = await getJson('/api/dashboard/overtime');
    if (!body.available) { unavailable('#overtimeBody', body, '시간외 시세를 불러오지 못했습니다.'); $('#overtimeMeta').textContent = ''; return; }
    $('#overtimeBody').className = '';
    $('#overtimeBody').innerHTML = table([
      { h: '#', f: r => r.rank, cls: 'c' }, { h: '종목', f: stockLink }, { h: '시장', f: r => esc(r.market) },
      { h: '시간외 현재가', f: r => num(r.price), cls: 'r' }, { h: '등락', f: r => `${num(r.change)} (${rate(r.changeRate)})`, cls: r => 'r ' + dir(r.changeRate) },
      { h: '체결 시각', f: r => esc(r.tradedAt || '–') },
    ], body.rows, '시간외에 상승 중인 종목이 없습니다.');
    meta('#overtimeMeta', [`조회 ${body.asOf}`, `확인 ${body.checked}종목 중 시간외 시세 ${body.withOvertime}종목${body.failed ? `, 수신 실패 ${body.failed}` : ''}`, body.source, body.note]);
  }
  async function uploadFlows() {
    const file = $('#flowFile').files[0], msg = $('#flowUploadMsg');
    if (!file) { msg.textContent = 'CSV 파일을 선택하세요.'; return; }
    const form = new FormData(); form.append('file', file);
    msg.textContent = '업로드 중…';
    const { ok, body } = await getJson('/api/dashboard/flows/upload', { method: 'POST', body: form });
    msg.textContent = ok ? `${body.symbols}종목 ${body.rows}행 저장 (${body.start} ~ ${body.end})` : (body.message || '업로드 실패');
    if (ok) loadFlows();
  }

  /* ── 3. 실적 · 공시 ─────────────────────────────────────────── */
  async function loadFundamentals() {
    const { body } = await getJson('/api/dashboard/fundamentals');
    if (!body.available) { unavailable('#fundBody', body, '실적 데이터를 불러오지 못했습니다.'); $('#fundMeta').textContent = ''; return; }
    $('#fundBody').className = '';
    $('#fundBody').innerHTML = table([
      { h: '종목', f: stockLink }, { h: '판정', f: r => `<span class="chip ${r.turnaround ? 'hot' : ''}">${esc(r.status)}</span>` },
      { h: '매출 증가율', f: r => pct(r.revenueGrowth, 1), cls: r => 'r ' + dir(r.revenueGrowth) },
      { h: '영업이익(당기/전기)', f: r => r.operatingIncome == null ? '–' : `${num(r.operatingIncome / 1e8)} / ${num(r.operatingIncomePrev / 1e8)}억`, cls: 'r' },
      { h: '영업이익률', f: r => pct(r.operatingMargin, 1), cls: 'r' }, { h: 'ROE', f: r => pct(r.roe, 1), cls: 'r' },
      { h: '부채비율', f: r => pct(r.debtRatio, 0), cls: 'r' }, { h: '비고', f: r => esc(r.reason || '') },
    ], body.rows);
    meta('#fundMeta', [`조회 ${body.asOf}`, body.source, body.note]);
  }
  async function loadContracts() {
    const { body } = await getJson(`/api/dashboard/disclosures/contracts?days=${encodeURIComponent($('#contractDays').value)}`);
    if (!body.available) { unavailable('#contractBody', body, '수주 공시를 불러오지 못했습니다.'); $('#contractMeta').textContent = ''; return; }
    $('#contractBody').className = '';
    $('#contractBody').innerHTML = table([
      { h: '접수일', f: r => esc(r.date) }, { h: '회사', f: r => `${esc(r.company)} <small>${esc(r.symbol)}</small>` }, { h: '시장', f: r => esc(r.market) },
      { h: '공시', f: r => `${r.correction ? '<span class="chip">정정</span> ' : ''}${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a>` : esc(r.title)}` },
    ], body.rows, '해당 기간에 수주 공시가 없습니다.');
    meta('#contractMeta', [`조회 ${body.asOf}`, body.source, body.note]);
  }

  /* ── 4. 보유 · 관심 ─────────────────────────────────────────── */
  const loginPrompt = '<a href="/member/login.html">로그인</a> 후 이용할 수 있습니다.';
  const priceCell = r => r.price == null ? '<span class="dim">수신 실패</span>' : `${num(r.price)}${r.simulated ? ' <span class="chip">시뮬레이션 가격</span>' : ''}`;
  async function loadHoldings() {
    const { ok, status, body } = await getJson('/api/dashboard/holdings');
    if (status === 401) { $('#holdingsBody').innerHTML = loginPrompt; return; }
    if (!ok) { $('#holdingsBody').textContent = '보유종목을 불러오지 못했습니다.'; return; }
    $('#holdingsBody').className = '';
    $('#holdingsBody').innerHTML = table([
      { h: '종목', f: stockLink }, { h: '수량', f: r => num(r.quantity), cls: 'r' }, { h: '평균단가', f: r => num(r.avgPrice), cls: 'r' },
      { h: '현재가', f: priceCell, cls: 'r' }, { h: '등락', f: r => rate(r.changeRate), cls: r => 'r ' + dir(r.changeRate) },
      { h: '평가금액', f: r => num(r.evalAmount), cls: 'r' }, { h: '손익', f: r => `${num(r.pnl)} (${rate(r.pnlRate)})`, cls: r => 'r ' + dir(r.pnl) },
    ], body.rows, '보유 중인 주식이 없습니다. 주식 실습에서 매수하면 여기에 표시됩니다.');
    meta('#holdingsMeta', [`조회 ${body.asOf}`, body.rows.length ? `합계 평가 ${num(body.totalEval)}원 · 손익 ${num(body.totalPnl)}원 (${rate(body.totalPnlRate)})` : '', body.note]);
  }
  async function loadWatchlist() {
    const { ok, status, body } = await getJson('/api/dashboard/watchlist');
    if (status === 401) { $('#watchBody').innerHTML = loginPrompt; return; }
    if (!ok) { $('#watchBody').textContent = '관심종목을 불러오지 못했습니다.'; return; }
    $('#watchBody').className = '';
    $('#watchBody').innerHTML = table([
      { h: '종목', f: stockLink }, { h: '시장', f: r => esc(r.market) }, { h: '현재가', f: priceCell, cls: 'r' },
      { h: '등락', f: r => rate(r.changeRate), cls: r => 'r ' + dir(r.changeRate) },
      { h: '', f: r => `<button type="button" class="dash-btn small" data-unwatch="${esc(r.symbol)}">삭제</button>`, cls: 'c' },
    ], body.rows, '관심종목이 없습니다. 위에서 종목코드를 추가하세요.');
    meta('#watchMeta', [`조회 ${body.asOf}`, `최대 ${body.limit}개`]);
  }
  async function addWatch(event) {
    event.preventDefault();
    const symbol = $('#watchSymbol').value.trim();
    if (!/^\d{6}$/.test(symbol)) { $('#watchMeta').textContent = '종목코드는 숫자 6자리입니다.'; return; }
    const { ok, status, body } = await getJson(`/api/dashboard/watchlist/${symbol}`, { method: 'PUT' });
    if (status === 401) { $('#watchBody').innerHTML = loginPrompt; return; }
    if (!ok) { $('#watchMeta').textContent = body.message || '추가하지 못했습니다.'; return; }
    $('#watchSymbol').value = ''; loadWatchlist();
  }

  /* ── 5. 조건식 · 백테스트 ───────────────────────────────────── */
  const SAVE_KEY = 'dashConditions';
  const readSaved = () => { try { return JSON.parse(localStorage.getItem(SAVE_KEY) || '[]'); } catch { return []; } };
  const writeSaved = list => { try { localStorage.setItem(SAVE_KEY, JSON.stringify(list.slice(0, 20))); } catch { /* storage blocked */ } };
  function renderSaved() {
    $('#condSaved').innerHTML = readSaved().map((c, i) => `<span class="chip pick"><button type="button" data-load-cond="${i}">${esc(c.name)}</button><button type="button" data-del-cond="${i}" aria-label="삭제">×</button></span>`).join('');
  }
  let presets = [];
  async function loadPresets() {
    const { ok, body } = await getJson('/api/dashboard/conditions/presets');
    if (!ok) return;
    presets = body.presets;
    $('#condPreset').innerHTML = '<option value="">직접 입력</option>' + presets.map((p, i) => `<option value="${i}">${esc(p.name)}${p.needs_flows ? ' (수급 필요)' : ''}</option>`).join('');
  }
  async function runCondition() {
    const out = $('#condResult');
    const val = id => $(id).value.trim();
    const payload = {
      symbol: val('#condSymbol').toUpperCase(), source: val('#condSource'), buy: $('#condBuy').value, sell: $('#condSell').value,
      maxHold: Number(val('#condHold') || 0), stopLoss: Number(val('#condStop') || 0) / 100, takeProfit: Number(val('#condTake') || 0) / 100,
      feeRate: Number(val('#condFee') || 0) / 100, slippage: Number(val('#condSlip') || 0) / 100,
    };
    out.className = 'dash-empty'; out.textContent = '백테스트 실행 중…';
    const { ok, body } = await getJson('/api/dashboard/conditions/backtest', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!ok) { out.innerHTML = `<b class="down">${esc(body.message || '실행하지 못했습니다.')}</b>`; return; }
    const t = body.trade, st = body.study;
    out.className = '';
    out.innerHTML = `
      <div class="dash-meta">${esc(body.symbol)} · ${esc(body.source)} · ${body.bars}일 (${esc(body.from)} ~ ${esc(body.to)}) · 수급 ${body.flowDays}일 반영</div>
      <div class="tiles">
        <div><small>전략 수익률</small><b class="${dir(t.total_return)}">${pct(t.total_return)}</b></div>
        <div><small>단순보유(첫 봉 시가)</small><b class="${dir(t.buy_and_hold_return)}">${pct(t.buy_and_hold_return)}</b></div>
        <div><small>최대낙폭</small><b class="down">${pct(t.max_drawdown)}</b></div>
        <div><small>거래 · 승률</small><b>${t.trade_count}건 · ${t.win_rate == null ? '–' : (t.win_rate * 100).toFixed(0) + '%'}</b></div>
        <div><small>오늘 신호</small><b>${t.signal_today ? '발생 (다음 시가 매수 후보)' : '없음'}</b></div>
      </div>
      <h3>신호 후 수익률 통계 (신호 다음 시가 → n일 뒤 종가, 신호 ${st.signal_count}회)</h3>
      ${table([{ h: '보유일', f: s => `${s.horizon}일`, cls: 'c' }, { h: '표본', f: s => s.count, cls: 'r' }, { h: '평균', f: s => pct(s.mean), cls: s => 'r ' + dir(s.mean) },
        { h: '중앙값', f: s => pct(s.median), cls: s => 'r ' + dir(s.median) }, { h: '승률', f: s => s.win_rate == null ? '–' : (s.win_rate * 100).toFixed(0) + '%', cls: 'r' },
        { h: '최고', f: s => pct(s.best), cls: 'r up' }, { h: '최저', f: s => pct(s.worst), cls: 'r down' }], st.stats)}
      <h3>매매 내역 (최근 ${t.trades.length}건${t.open_position ? ', 보유 중 포지션 1건 제외' : ''})</h3>
      ${table([{ h: '진입', f: x => esc(x.entry_date) }, { h: '청산', f: x => esc(x.exit_date) }, { h: '보유일', f: x => x.bars_held, cls: 'r' },
        { h: '수익률', f: x => pct(x.return), cls: x => 'r ' + dir(x.return) }, { h: '청산 사유', f: x => esc(x.reason) }], t.trades, '체결된 거래가 없습니다.')}
      <div class="dash-meta">${esc(body.notice)}</div>`;
  }

  /* ── 탭 전환 · 지연 로딩 ─────────────────────────────────────── */
  const loaders = {
    overview: [loadOverview],
    flows: [loadFlows, loadOvertime],
    fundamentals: [loadFundamentals, loadContracts],
    portfolio: [loadHoldings, loadWatchlist],
    conditions: [loadPresets, renderSaved],
    ai: [() => window.initAiPortfolio && window.initAiPortfolio(currentUser)],
  };
  function showTab(name, remember) {
    const key = TABS.includes(name) ? name : 'overview';
    document.querySelectorAll('[data-dash-tab]').forEach(b => { const on = b.dataset.dashTab === key; b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on)); });
    document.querySelectorAll('[data-dash-panel]').forEach(p => { p.hidden = p.dataset.dashPanel !== key; });
    if (remember) { const url = new URL(location.href); url.searchParams.set('tab', key); history.replaceState(null, '', url); }
    if (!loaded.has(key)) { loaded.add(key); loaders[key].forEach(fn => fn()); }
  }
  const refreshers = { overview: [loadOverview], flows: [loadFlows], overtime: [loadOvertime], fundamentals: [loadFundamentals], contracts: [loadContracts], portfolio: [loadHoldings, loadWatchlist] };

  window.initDashboard = async user => {
    currentUser = user;
    document.querySelectorAll('[data-dash-tab]').forEach(b => b.addEventListener('click', () => showTab(b.dataset.dashTab, true)));
    document.querySelectorAll('[data-refresh]').forEach(b => b.addEventListener('click', () => (refreshers[b.dataset.refresh] || []).forEach(fn => fn())));
    $('#flowDays').addEventListener('change', loadFlows);
    $('#contractDays').addEventListener('change', loadContracts);
    $('#flowUpload').addEventListener('click', uploadFlows);
    $('#watchForm').addEventListener('submit', addWatch);
    $('#watchBody').addEventListener('click', async e => {
      const symbol = e.target.dataset?.unwatch;
      if (symbol) { await getJson(`/api/dashboard/watchlist/${encodeURIComponent(symbol)}`, { method: 'DELETE' }); loadWatchlist(); }
    });
    $('#condPreset').addEventListener('change', e => {
      const p = presets[Number(e.target.value)];
      if (p) { $('#condBuy').value = p.buy; $('#condSell').value = p.sell || ''; }
    });
    $('#condRun').addEventListener('click', runCondition);
    $('#condSave').addEventListener('click', () => {
      const buy = $('#condBuy').value.trim();
      if (!buy) return;
      const name = (prompt('저장할 조건 이름', buy.slice(0, 20)) || '').trim();
      if (name) { writeSaved([{ name: name.slice(0, 30), buy, sell: $('#condSell').value.trim() }, ...readSaved()]); renderSaved(); }
    });
    $('#condSaved').addEventListener('click', e => {
      const list = readSaved(), load = e.target.dataset?.loadCond, del = e.target.dataset?.delCond;
      if (load !== undefined && list[load]) { $('#condBuy').value = list[load].buy; $('#condSell').value = list[load].sell || ''; $('#condPreset').value = ''; }
      if (del !== undefined) { list.splice(Number(del), 1); writeSaved(list); renderSaved(); }
    });
    // 수급 CSV 업로드는 관리자에게만 보인다.
    getJson('/api/admin/me').then(r => { if (r.ok && r.body.admin) $('#flowAdmin').hidden = false; });
    showTab(new URLSearchParams(location.search).get('tab'), false);
  };
})();
