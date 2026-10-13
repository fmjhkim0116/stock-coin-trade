/* AI 포트폴리오 탭 — Sniper(상관관계·효율적 투자선) / Shield(켈리공식·월 1회 리밸런싱).
   숫자는 모두 서버가 계산한 값이며, 'AI 해설'은 그 결과를 풀어 쓴 글이다. 서버 문자열은 esc()를 거쳐 출력한다. */
(() => {
  const $ = selector => document.querySelector(selector);
  let started = false, user = null;
  const U = () => window.dashUtil;
  const won = v => v == null ? '–' : `${Number(v).toLocaleString('ko-KR')}원`;
  const p1 = v => v == null ? '–' : `${(v * 100).toFixed(1)}%`;
  const split = text => String(text || '').split(/[\s,;]+/).filter(Boolean);
  const last = { sniper: null, kelly: null };

  /* ── 공통: AI 클로드 해설 ───────────────────────────────────── */
  async function explain(kind, target) {
    const { esc, getJson } = U();
    const box = $(target);
    if (!user?.loggedIn) { box.innerHTML = '<span class="dim">AI 해설은 로그인 후 이용할 수 있습니다.</span>'; return; }
    box.textContent = 'AI 클로드가 결과를 해석하는 중…';
    const { body } = await getJson('/api/dashboard/ai/explain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind, result: last[kind] }) });
    box.innerHTML = body.available ? `<div class="ai-explain"><b>AI 클로드 해설</b><p>${esc(body.text).replace(/\n/g, '<br>')}</p><small>${esc(body.note)}</small></div>`
      : `<span class="dim">${esc(body.reason || body.message || '해설을 만들 수 없습니다.')}</span>`;
  }

  /* ── AI Sniper ──────────────────────────────────────────────── */
  const heat = c => c == null ? '' : `background:${c >= 0 ? `rgba(225,29,72,${(Math.abs(c) * 0.55).toFixed(2)})` : `rgba(37,99,235,${(Math.abs(c) * 0.55).toFixed(2)})`}`;

  function frontierSvg(frontier) {
    const pts = frontier.frontier, assets = frontier.assets, chosen = Object.values(frontier.portfolios);
    const xs = [...pts.map(p => p.volatility), ...assets.map(a => a.volatility), ...chosen.map(c => c.volatility)];
    const ys = [...pts.map(p => p.expectedReturn), ...assets.map(a => a.expectedReturn), ...chosen.map(c => c.expectedReturn)];
    const W = 560, H = 260, m = 36, x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const X = v => m + (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0)) * (W - 2 * m), Y = v => H - m - (y1 === y0 ? 0.5 : (v - y0) / (y1 - y0)) * (H - 2 * m);
    const colors = { minVol: '#059669', maxSharpe: '#2962FF', target: '#E11D48' };
    const line = pts.map(p => `${X(p.volatility).toFixed(1)},${Y(p.expectedReturn).toFixed(1)}`).join(' ');
    return `<svg viewBox="0 0 ${W} ${H}" class="ai-chart" role="img" aria-label="효율적 투자선: 변동성 대비 기대수익">
      <line x1="${m}" y1="${H - m}" x2="${W - m}" y2="${H - m}" stroke="#cbd5e1"/><line x1="${m}" y1="${m}" x2="${m}" y2="${H - m}" stroke="#cbd5e1"/>
      <text x="${W / 2}" y="${H - 6}" text-anchor="middle" fill="#787B86" font-size="11">변동성(위험, 연) ${(x0 * 100).toFixed(0)}% ~ ${(x1 * 100).toFixed(0)}%</text>
      <text x="10" y="${H / 2}" fill="#787B86" font-size="11" transform="rotate(-90 10 ${H / 2})" text-anchor="middle">기대수익 ${(y0 * 100).toFixed(0)}% ~ ${(y1 * 100).toFixed(0)}%</text>
      ${assets.map(a => `<circle cx="${X(a.volatility)}" cy="${Y(a.expectedReturn)}" r="3" fill="#94a3b8"><title>${U().esc(a.symbol)}</title></circle>`).join('')}
      <polyline points="${line}" fill="none" stroke="#64748b" stroke-width="2"/>
      ${Object.entries(frontier.portfolios).map(([k, c]) => `<circle cx="${X(c.volatility)}" cy="${Y(c.expectedReturn)}" r="6" fill="${colors[k] || '#000'}"><title>${U().esc(c.label)}</title></circle>`).join('')}
    </svg>
    <div class="ai-legend"><span><i style="background:#64748b"></i>효율적 투자선</span><span><i style="background:#94a3b8"></i>개별 종목</span><span><i style="background:#059669"></i>최소 변동성</span><span><i style="background:#2962FF"></i>최대 샤프</span><span><i style="background:#E11D48"></i>목표 수익대</span></div>`;
  }

  function portfolioCard(p, names) {
    const { esc } = U();
    const bars = Object.entries(p.weights).sort((a, b) => b[1] - a[1]).map(([s, w]) =>
      `<div class="wbar"><span>${esc(names[s] || s)} <small>${esc(s)}</small></span><div><i style="width:${(w * 100).toFixed(1)}%"></i></div><b>${(w * 100).toFixed(1)}%</b></div>`).join('');
    return `<div class="pcard"><h4>${esc(p.label)}</h4>${bars}
      <div class="pstats"><span>기대수익(보정) <b>${p1(p.expectedReturn)}</b></span><span>변동성 <b>${p1(p.volatility)}</b></span><span>샤프 <b>${p.sharpe}</b></span>
      <span>과거 실현수익 <b>${p1(p.historicalReturn)}</b></span><span>과거 최대낙폭 <b class="down">${p1(p.historicalMaxDrawdown)}</b></span></div></div>`;
  }

  async function runSniper() {
    const { esc, getJson } = U(), out = $('#aiSniperResult');
    const v = id => $(id).value.trim();
    const payload = { symbols: split(v('#aiSymbols')), base: split(v('#aiBase')), source: v('#aiSource'), maxWeight: Number(v('#aiMaxW')) / 100,
      targetLow: Number(v('#aiLo')) / 100, targetHigh: Number(v('#aiHi')) / 100, rf: Number(v('#aiRf')) / 100, shrink: Number(v('#aiShrink')) / 100 };
    out.className = 'dash-empty'; out.textContent = '가격 데이터를 불러와 상관관계와 효율적 투자선을 계산하는 중… (종목이 많으면 수십 초 걸릴 수 있습니다)';
    const { ok, body } = await getJson('/api/dashboard/ai/sniper', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!ok) { out.innerHTML = `<b class="down">${esc(body.message || '계산하지 못했습니다.')}</b>`; return; }
    last.sniper = { period: body.period, correlation: { lowestPairs: body.correlation.lowestPairs, diversifiers: body.correlation.diversifiers.slice(0, 6), base: body.correlation.base },
      portfolios: body.frontier.portfolios, notes: body.frontier.notes, names: body.names };
    const c = body.correlation, names = body.names, label = s => `${esc(names[s] || s)} <small>${esc(s)}</small>`;
    out.className = '';
    const labels = c.labels;
    const heatTable = `<div class="dash-table"><table class="heat"><thead><tr><th></th>${labels.map(s => `<th class="c" title="${esc(names[s] || s)}">${esc(s)}</th>`).join('')}</tr></thead><tbody>${c.matrix.map((row, i) =>
      `<tr><th>${esc(names[labels[i]] || labels[i])}</th>${row.map((x, j) => `<td class="c" style="${i === j ? '' : heat(x)}">${i === j ? '–' : x.toFixed(2)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    const diversifiers = U().table([
      { h: '#', f: r => r.rank, cls: 'c' }, { h: '종목', f: r => label(r.symbol) },
      { h: c.mode === 'base' ? '내 바구니와의 상관' : '다른 종목과의 평균 상관', f: r => (c.mode === 'base' ? r.corrWithBase : r.avgCorr).toFixed(2), cls: 'r' },
      { h: '전체 평균 상관', f: r => r.avgCorr.toFixed(2), cls: 'r' },
    ], c.diversifiers.map((r, i) => ({ ...r, rank: i + 1 })));
    out.innerHTML = `
      <div class="dash-meta">${esc(body.source)} · ${body.period.days}거래일 (${esc(body.period.from)} ~ ${esc(body.period.to)}) · 사용 종목 ${labels.length}개${body.dropped.length ? ` · 데이터 부족으로 제외: ${esc(body.dropped.join(', '))}` : ''}${Object.keys(body.failed).length ? ` · 수신 실패: ${esc(Object.keys(body.failed).join(', '))}` : ''}</div>
      <h3>① 상관관계가 낮은 자산 ${c.mode === 'base' ? '(내 바구니 ' + esc(c.base.join(', ')) + ' 기준, 낮은 순)' : '(전체 평균 상관 낮은 순)'}</h3>${diversifiers}
      <h3>가장 상관이 낮은 종목 쌍</h3>${U().table([{ h: '종목 A', f: r => label(r.a) }, { h: '종목 B', f: r => label(r.b) }, { h: '상관계수', f: r => r.corr.toFixed(2), cls: r => 'r ' + (r.corr < 0 ? 'down' : '') }], c.lowestPairs)}
      <details class="dash-more"><summary>상관계수 행렬 보기</summary>${heatTable}</details>
      <h3>② 효율적 투자선 — 위험 대비 수익이 가장 좋은 조합</h3>${frontierSvg(body.frontier)}
      ${body.frontier.notes.map(n => `<div class="dash-empty warn">${esc(n)}</div>`).join('')}
      <div class="pgrid">${Object.values(body.frontier.portfolios).map(p => portfolioCard(p, names)).join('')}</div>
      <div class="dash-meta">${esc(body.notice)} 기대수익은 종목 간 평균으로 ${Math.round(body.frontier.shrink * 100)}% 보정했고, 종목당 최대 비중 ${Math.round(body.frontier.maxWeight * 100)}%를 적용했습니다.</div>
      <div class="cond-actions"><button type="button" class="dash-btn" id="aiSniperExplain">AI 클로드 해설 보기</button></div><div id="aiSniperExplainBox"></div>`;
    $('#aiSniperExplain').addEventListener('click', () => explain('sniper', '#aiSniperExplainBox'));
  }

  /* ── AI Shield: 급등주 · 켈리 ───────────────────────────────── */
  async function runSurge() {
    const { esc, getJson } = U(), out = $('#aiSurgeResult');
    out.className = 'dash-empty'; out.textContent = '급등주를 찾는 중…';
    const symbols = split($('#kSymbols').value).join(',');
    const { ok, body } = await getJson(`/api/dashboard/ai/shield/candidates?symbols=${encodeURIComponent(symbols)}`);
    if (!ok) { out.innerHTML = `<b class="down">${esc(body.message || '조회하지 못했습니다.')}</b>`; return; }
    out.className = '';
    out.innerHTML = U().table([{ h: '종목', f: r => `${esc(r.name)} <small>${esc(r.symbol)}</small>` }, { h: '현재가', f: r => U().num(r.price), cls: 'r' },
      { h: '5일 등락', f: r => U().rate(r.chg5 * 100), cls: 'r up' }, { h: '거래량 / 20일 평균', f: r => r.volumeRatio == null ? '–' : `${r.volumeRatio.toFixed(1)}배`, cls: 'r' }, { h: '기준일', f: r => esc(r.date) }],
      body.rows, `오늘 조건을 충족한 종목이 없습니다 (확인 ${body.checked}종목).`) + `<div class="dash-meta">${esc(body.note)}${Object.keys(body.failed).length ? ` · 수신 실패 ${Object.keys(body.failed).length}종목` : ''}</div>`;
  }

  const statusClass = s => s === '베팅 가능' ? 'ok' : 'bad';
  async function runKelly() {
    const { esc, getJson } = U(), out = $('#aiKellyResult'), v = id => $(id).value.trim();
    const payload = { mode: 'pooled', capital: Number(v('#kCapital')), fraction: Number(v('#kFraction')), cap: Number(v('#kCap')) / 100, totalCap: Number(v('#kTotal')) / 100,
      symbols: split(v('#kSymbols')), buy: $('#kBuy').value, sell: $('#kSell').value, stopLoss: Number(v('#kStop')) / 100, maxHold: Number(v('#kHold') || 0) };
    out.className = 'dash-empty'; out.textContent = '과거 신호 거래를 모아 승률·손익비와 켈리 비율을 계산하는 중…';
    const { ok, body } = await getJson('/api/dashboard/ai/shield/kelly', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!ok) { out.innerHTML = `<b class="down">${esc(body.message || '계산하지 못했습니다.')}</b>`; return; }
    const s = body.stats; last.kelly = { stats: s, candidates: body.candidates, params: body.params, symbolsUsed: body.symbolsUsed };
    out.className = '';
    const dd = s.drawdownProb ? Object.entries(s.drawdownProb).map(([c, pr]) => `<tr><td class="c">${c}배 켈리</td><td class="r">${(pr * 100).toFixed(1)}%</td></tr>`).join('') : '';
    out.innerHTML = `
      <div class="kstatus ${statusClass(s.status)}"><b>${esc(s.status)}</b> ${s.reason ? `— ${esc(s.reason)}` : `— 권장 베팅 비율 <b>${p1(s.recommended)}</b> (투자금의 ${won(Math.round(payload.capital * s.recommended))} / 종목당)`}</div>
      <div class="tiles">
        <div><small>거래 표본</small><b>${s.trades}건 (${body.symbolsUsed}종목)</b></div>
        <div><small>승률 (95% 신뢰하한)</small><b>${s.winRate == null ? '–' : `${p1(s.winRate)} (${p1(s.winRateLow)})`}</b></div>
        <div><small>평균 수익 / 평균 손실</small><b>${s.avgWin == null ? '–' : `${p1(s.avgWin)} / ${p1(s.avgLoss)}`}</b></div>
        <div><small>손익비</small><b>${s.payoffRatio ?? '–'}</b></div>
        <div><small>풀 켈리 f*</small><b>${s.fullKelly == null ? '–' : p1(s.fullKelly)}</b></div>
        <div><small>보수 켈리(신뢰하한)</small><b>${s.conservativeKelly == null ? '–' : p1(s.conservativeKelly)}</b></div>
      </div>
      ${dd ? `<details class="dash-more"><summary>켈리 배수별 자산이 절반이 될 확률 (이상적 모형 근사)</summary><div class="dash-table"><table><thead><tr><th class="c">켈리 배수</th><th class="r">자산 50% 하락 도달 확률</th></tr></thead><tbody>${dd}</tbody></table></div></details>` : ''}
      <h3>오늘 급등주 후보별 베팅 금액</h3>
      ${U().table([{ h: '종목', f: r => `${esc(r.name)} <small>${esc(r.symbol)}</small>` }, { h: '비율', f: r => p1(r.fraction), cls: 'r' }, { h: '베팅 금액', f: r => won(r.amount), cls: 'r' }],
        body.candidates, s.recommended > 0 ? '오늘 조건을 충족한 종목이 없어 베팅 대상이 없습니다.' : '권장 비율이 0이라 베팅 대상이 없습니다.')}
      <div class="dash-meta">${esc(body.notice)}${body.candidates.length ? ` · 합계 ${won(body.totalBet)}` : ''}</div>
      ${body.warnings.map(w => `<div class="dash-empty warn">${esc(w)}</div>`).join('')}
      <div class="cond-actions"><button type="button" class="dash-btn" id="aiKellyExplain">AI 클로드 해설 보기</button></div><div id="aiKellyExplainBox"></div>`;
    $('#aiKellyExplain').addEventListener('click', () => explain('kelly', '#aiKellyExplainBox'));
  }

  async function runManual() {
    const { esc, getJson } = U(), out = $('#aiManualResult');
    const payload = { mode: 'manual', p: Number($('#mP').value) / 100, b: Number($('#mB').value), capital: Number($('#kCapital').value), fraction: Number($('#kFraction').value), cap: Number($('#kCap').value) / 100 };
    const { ok, body } = await getJson('/api/dashboard/ai/shield/kelly', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    out.innerHTML = ok ? `<div class="kstatus ${statusClass(body.status)}"><b>${esc(body.status)}</b> — 풀 켈리 ${p1(body.fullKelly)}, 권장 ${p1(body.recommended)} (${won(body.amount)}). ${esc(body.warnings.at(-1))}</div>` : `<b class="down">${esc(body.message || '계산하지 못했습니다.')}</b>`;
  }

  /* ── AI Shield: 월 1회 리밸런싱 (켈리공식 카테고리) ───────────── */
  async function loadAccount() {
    const { esc, getJson } = U(), box = $('#aiShieldAccount');
    const { ok, status, body } = await getJson('/api/dashboard/ai/shield/account');
    if (status === 401) { box.className = 'dash-empty'; box.innerHTML = '<a href="/member/login.html">로그인</a> 후 이용할 수 있습니다.'; return; }
    if (!ok) { box.className = 'dash-empty'; box.textContent = '장부를 불러오지 못했습니다.'; return; }
    box.className = '';
    if (!body.exists) {
      box.innerHTML = `<p>켈리 베팅에 쓰는 <b>베팅 계좌</b>와 수익을 옮겨 두는 <b>안전 계좌</b>를 모의 장부로 관리합니다. 시작 금액을 정하세요.</p>
        <div class="cond-actions"><input id="aiStart" type="number" min="1" step="100000" value="10000000" aria-label="시작 금액"><button type="button" class="dash-btn primary" id="aiStartBtn">장부 만들기</button><span id="aiStartMsg" class="dash-meta"></span></div><div class="dash-meta">${esc(body.note || '실제 주문이나 이체는 일어나지 않습니다.')}</div>`;
      $('#aiStartBtn').addEventListener('click', async () => {
        const r = await getJson('/api/dashboard/ai/shield/account', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ capital: Number($('#aiStart').value) }) });
        if (r.ok) loadAccount(); else $('#aiStartMsg').textContent = r.body.message || '만들지 못했습니다.';
      });
      return;
    }
    const ledger = U().table([{ h: '월', f: r => esc(r.month) }, { h: '리밸런싱 전 베팅 계좌', f: r => won(r.riskyBefore), cls: 'r' }, { h: '수익', f: r => won(r.gain), cls: 'r up' },
      { h: '안전 계좌로 이전', f: r => won(r.swept), cls: 'r' }, { h: '베팅 계좌', f: r => won(r.riskyAfter), cls: 'r' }, { h: '안전 계좌', f: r => won(r.safeAfter), cls: 'r' }], body.ledger, '아직 리밸런싱 기록이 없습니다.');
    box.innerHTML = `
      <div class="tiles"><div><small>베팅 계좌 (장부)</small><b>${won(body.risky)}</b></div><div><small>안전 계좌 (장부)</small><b>${won(body.safe)}</b></div>
        <div><small>수익 기준선 (최고 이익 기준)</small><b>${won(body.baseline)}</b></div><div><small>이번 달(${esc(body.currentMonth)})</small><b>${body.doneThisMonth ? '리밸런싱 완료' : '미실행'}</b></div></div>
      <div class="cond-form">
        <label>현재 베팅 계좌 평가금액 (원)<input id="aiEquity" type="number" min="0" step="10000" value="${body.risky}"></label>
        <label>수익 중 안전 계좌로 옮길 비율<select id="aiRatio"><option value="0.3">30%</option><option value="0.5" selected>50%</option><option value="0.7">70%</option><option value="1">100%</option></select></label>
      </div>
      <div class="cond-actions"><button type="button" class="dash-btn" id="aiFillEquity">보유종목 평가금액 가져오기</button><button type="button" class="dash-btn" id="aiPreview">미리보기</button>
        <button type="button" class="dash-btn primary" id="aiRebalance" ${body.doneThisMonth ? 'disabled' : ''}>이달 리밸런싱 실행 (월 1회)</button></div>
      <div id="aiPlan" class="dash-empty">미리보기로 이전 금액을 확인한 뒤 실행하세요. 수익이 기준선을 넘은 부분만 옮기며, 손실 구간에서는 옮기지 않습니다.</div>
      <h3>리밸런싱 기록</h3>${ledger}<div class="dash-meta">${esc(body.note)}</div>
      <details class="dash-more"><summary>장부 다시 시작하기</summary><div class="cond-actions"><input id="aiResetAmt" type="number" min="1" step="100000" value="10000000" aria-label="새 시작 금액"><button type="button" class="dash-btn" id="aiReset">기록을 지우고 새로 시작</button></div></details>`;
    const body_ = () => JSON.stringify({ riskyEquity: Number($('#aiEquity').value), ratio: Number($('#aiRatio').value) });
    const show = (plan, extra = '') => { $('#aiPlan').className = ''; $('#aiPlan').innerHTML = `<div class="kstatus ${plan.swept > 0 ? 'ok' : 'bad'}">${esc(plan.message)}</div>
      <div class="tiles"><div><small>수익(기준선 초과)</small><b>${won(plan.gain)}</b></div><div><small>안전 계좌로 이전</small><b>${won(plan.swept)}</b></div><div><small>이전 후 베팅 계좌</small><b>${won(plan.riskyAfter)}</b></div><div><small>이전 후 안전 계좌</small><b>${won(plan.safeAfter)}</b></div></div>${extra}`; };
    $('#aiFillEquity').addEventListener('click', async () => {
      const r = await getJson('/api/dashboard/holdings');
      if (r.ok && r.body.totalEval != null) { $('#aiEquity').value = Math.round(r.body.totalEval); $('#aiPlan').textContent = `모의 보유종목 평가금액 ${won(Math.round(r.body.totalEval))}을 채웠습니다. 베팅 계좌와 같은 돈일 때만 사용하세요.`; }
      else $('#aiPlan').textContent = '보유종목 평가금액을 가져오지 못했습니다.';
    });
    $('#aiPreview').addEventListener('click', async () => {
      const r = await getJson('/api/dashboard/ai/shield/rebalance/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body_() });
      if (r.ok) show(r.body.plan, r.body.doneThisMonth ? '<div class="dash-meta">이번 달은 이미 실행했습니다.</div>' : ''); else $('#aiPlan').innerHTML = `<b class="down">${esc(r.body.message || '미리보기에 실패했습니다.')}</b>`;
    });
    $('#aiRebalance').addEventListener('click', async () => {
      if (!confirm('이달 리밸런싱을 실행합니다. 월 1회만 가능하며 되돌릴 수 없습니다(모의 장부). 진행할까요?')) return;
      const r = await getJson('/api/dashboard/ai/shield/rebalance', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body_() });
      if (r.ok) { await loadAccount(); show(r.body.plan); } else $('#aiPlan').innerHTML = `<b class="down">${esc(r.body.message || '실행하지 못했습니다.')}</b>`;
    });
    $('#aiReset').addEventListener('click', async () => {
      if (!confirm('리밸런싱 기록을 모두 지우고 새로 시작합니다. 진행할까요?')) return;
      const r = await getJson('/api/dashboard/ai/shield/account', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ capital: Number($('#aiResetAmt').value), reset: true }) });
      if (r.ok) loadAccount(); else $('#aiPlan').innerHTML = `<b class="down">${esc(r.body.message || '실패했습니다.')}</b>`;
    });
  }

  /* ── 카테고리 전환 ──────────────────────────────────────────── */
  function showCat(name, remember) {
    const key = ['sniper', 'shield'].includes(name) ? name : 'sniper';
    document.querySelectorAll('[data-ai-cat]').forEach(b => { const on = b.dataset.aiCat === key; b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on)); });
    document.querySelectorAll('[data-ai-pane]').forEach(p => { p.hidden = p.dataset.aiPane !== key; });
    if (remember) { const url = new URL(location.href); url.searchParams.set('cat', key); history.replaceState(null, '', url); }
    if (key === 'shield' && !showCat.loaded) { showCat.loaded = true; loadAccount(); }
  }

  window.initAiPortfolio = u => {
    if (started) return;
    started = true; user = u;
    $('#kBuy').value = 'close > hh(20) and volume > 2 * vma(20) and chg(5) > 0.05';
    $('#kSell').value = 'close < ma(10)';
    document.querySelectorAll('[data-ai-cat]').forEach(b => b.addEventListener('click', () => showCat(b.dataset.aiCat, true)));
    $('#aiSniperRun').addEventListener('click', runSniper);
    $('#aiSurgeRun').addEventListener('click', runSurge);
    $('#aiKellyRun').addEventListener('click', runKelly);
    $('#aiManualRun').addEventListener('click', runManual);
    showCat(new URLSearchParams(location.search).get('cat'), false);
  };
})();
