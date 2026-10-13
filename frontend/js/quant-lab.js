const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[char]);
const sqlFor = kind => ({
  market: `SELECT symbol, trade_time, open, high, low, close, volume, adjusted_close\nFROM market_data WHERE symbol = :symbol\nORDER BY trade_time DESC LIMIT :limit;`,
  signal: `WITH ma AS (SELECT trade_time, adjusted_close,\n AVG(adjusted_close) OVER (ORDER BY trade_time ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) fast_ma,\n AVG(adjusted_close) OVER (ORDER BY trade_time ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) slow_ma FROM market_data WHERE symbol = :symbol)\nSELECT *, CASE WHEN fast_ma > slow_ma THEN 'BUY' ELSE 'SELL' END signal FROM ma;`,
  trades: `SELECT trade_id, strategy_id, symbol, trade_time, side, price, quantity, fee, slippage, pnl\nFROM trade_logs ORDER BY trade_id DESC LIMIT 50;`
})[kind];
const strategyExamples = {
  trend: { name:'단기 추세추종 · MA 5 / 20', rule:'5일 이동평균선이 20일선을 위로 넘으면 매수하고, 아래로 내려가면 매도합니다.', why:'샘플 구간에 완만한 상승 흐름이 이어져 짧은 추세를 따라가는 규칙이 유리했습니다.', risk:'가격이 옆으로 움직이는 횡보장에서는 매수·매도가 잦아져 수수료 손실이 커질 수 있습니다.' },
  pullback: { name:'상승추세 눌림목 · MA 20 / 60', return:'실제 계산', mdd:'실제 계산', rule:'20일선이 60일선 위에 있을 때만, 조정 뒤 종가가 20일선을 다시 회복하면 매수합니다. 20일선이 60일선 아래로 내려가면 매도합니다.', why:'큰 상승 추세 안에서 조정이 끝나고 추세가 다시 이어지는 시점만 기다리는 규칙입니다.', risk:'강한 하락 전환에서는 이동평균선이 늦게 반응할 수 있어 손절 규칙을 따로 둬야 합니다.' },
  rsi: { name:'RSI 반등 · 과매도 회복', rule:'RSI가 30 아래로 내려갔다가 다시 30 위로 회복할 때 매수하고, 70 아래로 내려오면 매도합니다.', why:'샘플 데이터의 단기 과매도 뒤 반등 구간을 포착한 학습용 사례입니다.', risk:'하락 추세에서는 RSI가 오래 낮게 머물 수 있습니다. 추세 필터 없이 사용하면 계속 물릴 수 있습니다.' },
  breakout: { name:'거래량 돌파 · 20일 고점', rule:'종가가 최근 20일 고점을 넘고 거래량이 평균보다 클 때 매수 후보로 봅니다.', why:'상승이 시작되는 구간에서 거래량이 함께 늘어난 샘플 조건이라 돌파 전략이 잘 작동한 예시입니다.', risk:'뉴스성 급등은 다음 날 되돌릴 수 있습니다. 돌파만 보고 추격 매수하지 않도록 손절 기준이 필요합니다.' },
  momentum: { name:'중기 모멘텀 · 60일 수익률', rule:'최근 60일 수익률이 플러스이고 가격이 60일선 위에 있을 때만 보유합니다.', why:'상승한 자산이 한동안 상대적으로 강한 흐름을 유지한 샘플 구간을 반영한 사례입니다.', risk:'추세가 갑자기 바뀌면 이미 오른 가격에 진입할 수 있습니다. 여러 종목으로 분산해 검증해야 합니다.' }
};
async function showStrategy(key) {
  const item=strategyExamples[key]; if (!item) return;
  document.querySelectorAll('[data-strategy]').forEach(button => button.classList.toggle('active', button.dataset.strategy === key));
  const output=$('[data-strategy-result]'); output.textContent=`${item.name} 전략을 실제 데이터로 백테스트 중…`;
  await run(key, output, item);
}
async function runFactorAnalysis() {
  const symbol=$('[data-symbol]').value.trim().toUpperCase() || '005930', output=$('[data-factor-result]'); output.textContent='알파 · 베타와 팩터 노출도를 계산 중…';
  try { const response=await fetch(`/api/quant/factor-analysis?symbol=${encodeURIComponent(symbol)}`), data=await response.json(); if (!response.ok) throw new Error(data.message || '분석 실패'); const capm=data.capm; const exposures=data.multiFactor.exposures.map(item => `<li><span>${item.label}</span><b>${item.loading >= 0 ? '+' : ''}${item.loading}</b></li>`).join(''); output.innerHTML=`<div class="factor-metrics"><span><small>연환산 알파</small><b>${capm.alphaAnnual >= 0 ? '+' : ''}${capm.alphaAnnual}%</b></span><span><small>시장 베타</small><b>${capm.beta}</b></span><span><small>CAPM 설명력</small><b>${(capm.rSquared * 100).toFixed(1)}%</b></span><span><small>관측치</small><b>${capm.observations}일</b></span></div><p><b>쉽게 해석하면:</b> 베타가 1이면 시장과 비슷하게 움직인다는 뜻이며, 1보다 크면 시장 움직임에 더 민감합니다. 알파는 시장 요인을 뺀 뒤 남은 수익의 추정치입니다.</p><ul class="factor-exposures">${exposures}</ul><small>${esc(data.notice)}</small>`; } catch (error) { output.innerHTML=`<span class="error">${esc(error.message)}</span>`; }
}
const exitSymbol = () => $('[data-symbol]').value.trim().toUpperCase() || '005930';
const exitBadge = latest => `<span class="exit-badge ${esc(latest.action)}">${esc(latest.action)} · ${esc(latest.score)}점</span> ${esc((latest.reasons || []).join(' · ') || '발동한 규칙 없음')}`;
async function exitCall(url, options, output, busy) {
  output.textContent = busy;
  try { const response = await fetch(url, options), data = await response.json(); if (!response.ok) throw new Error(data.message || '요청 실패'); return data; }
  catch (error) { output.innerHTML = `<span class="error">${esc(error.message)}</span>`; return null; }
}
async function uploadFlows() {
  const output = $('[data-exit-result]'), file = $('[data-exit-file]').files[0];
  if (!file) { output.innerHTML = '<span class="error">CSV 파일을 먼저 선택하세요.</span>'; return; }
  const body = new FormData(); body.append('file', file);
  const data = await exitCall(`/api/quant/investor-flows?symbol=${encodeURIComponent(exitSymbol())}`, { method: 'POST', body }, output, '수급 CSV를 저장하는 중…');
  if (data) output.innerHTML = `<p><b>${esc(data.symbol)}</b> 수급 ${data.rows}일 저장 (${esc(data.start)} ~ ${esc(data.end)})</p><small>${esc(data.message)}</small>`;
}
async function showExitScore() {
  const output = $('[data-exit-result]');
  const data = await exitCall(`/api/quant/exit-score?symbol=${encodeURIComponent(exitSymbol())}&limit=10`, {}, output, '매도 점수를 계산하는 중…');
  if (!data) return;
  const rows = data.rows.slice().reverse().map(row => `<tr><td>${esc(row.date)}</td><td>${Number(row.close).toLocaleString()}</td><td>${row.score}</td><td>${esc(row.action)}</td><td>${esc(row.reasons.join(', '))}</td></tr>`).join('');
  output.innerHTML = `<p>${exitBadge(data.latest)}</p><div class="table-wrap"><table><thead><tr><th>일자</th><th>종가(수정)</th><th>점수</th><th>신호</th><th>규칙</th></tr></thead><tbody>${rows}</tbody></table></div><small>가격 ${data.barDays}일 · 수급 ${data.flowDays}일 사용. ${esc(data.notice)}</small>`;
}
async function runExitBacktest() {
  const output = $('[data-exit-result]');
  const data = await exitCall('/api/quant/exit-backtest', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbol: exitSymbol() }) }, output, '매도 점수 전략을 백테스트 중…');
  if (!data) return;
  const trades = data.trades.slice(-8).map(t => `<tr><td>${esc(t.date)}</td><td>${esc(t.side)}</td><td>${Number(t.quantity).toLocaleString()}</td><td>${Number(t.price).toLocaleString(undefined, { maximumFractionDigits: 1 })}</td><td>${esc(t.reason)}</td></tr>`).join('');
  output.innerHTML = `<div class="factor-metrics"><span><small>전략 수익률</small><b>${data.totalReturn}%</b></span><span><small>단순보유(첫 봉 시가)</small><b>${data.buyAndHoldReturn}%</b></span><span><small>최대낙폭</small><b>${data.maxDrawdown}%</b></span><span><small>체결</small><b>${data.tradeCount}건</b></span></div><p>최신 신호: ${exitBadge(data.latest)}</p><div class="table-wrap"><table><thead><tr><th>일자</th><th>구분</th><th>수량</th><th>체결가</th><th>사유</th></tr></thead><tbody>${trades}</tbody></table></div><small>가격 ${data.barDays}일 · 수급 ${data.flowDays}일. 단순보유는 진입 시점이 달라 직접 비교하면 전략에 유리하게 보일 수 있습니다.</small>`;
}
function table(rows, columns) {
  if (!rows?.length) return '<p class="empty">조회 결과가 없습니다.</p>';
  return `<div class="table-wrap"><table><thead><tr>${columns.map(([key,label]) => `<th>${label}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${columns.map(([key]) => `<td>${esc(typeof row[key] === 'number' ? row[key].toLocaleString(undefined,{maximumFractionDigits:4}) : row[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
async function load(kind) {
  const symbol=$('[data-symbol]').value.trim().toUpperCase() || '005930';
  $('[data-sql]').textContent=sqlFor(kind); $('[data-result]').textContent='PostgreSQL에서 조회 중…';
  try {
    const url=kind==='market'?`/api/quant/market-data?symbol=${encodeURIComponent(symbol)}&limit=20`:kind==='signal'?`/api/quant/signals?symbol=${encodeURIComponent(symbol)}&fast=20&slow=50&limit=30`:'/api/quant/results';
    const response=await fetch(url), data=await response.json(); if (!response.ok) throw new Error(data.message || '조회 실패');
    $('[data-result]').innerHTML=kind==='market'?table(data.rows,[['symbol','종목'],['trade_time','시간'],['open','시가'],['high','고가'],['low','저가'],['close','종가'],['volume','거래량']]):kind==='signal'?table(data.rows,[['trade_time','시간'],['adjusted_close','수정종가'],['fast_ma','MA20'],['slow_ma','MA50'],['signal','신호']]):table(data.trades,[['trade_id','ID'],['strategy_id','전략'],['symbol','종목'],['trade_time','시간'],['side','구분'],['price','체결가'],['quantity','수량'],['pnl','손익']]);
  } catch (error) { $('[data-result]').innerHTML=`<p class="error">${esc(error.message)}. PostgreSQL 연결 설정을 확인하세요.</p>`; }
}
async function run(strategy='ma2050', output=$('[data-backtest]'), detail=null) {
  const symbol=$('[data-symbol]').value.trim().toUpperCase() || '005930'; output.textContent='백테스트 실행 및 거래 로그 저장 중…';
  try { const response=await fetch('/api/quant/backtests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,strategy,fast:20,slow:50,quantity:10,feeRate:.00015,slippage:.0005})}), data=await response.json(); if (!response.ok) throw new Error(data.message || '실행 실패'); const positive=Number(data.totalReturn) > 0; const label=detail?.name || '이동평균 교차 MA 20/50'; const explanation=positive ? '이 샘플 기간·조건에서는 플러스 결과를 보였습니다. 다른 기간과 종목에서도 같은 결과가 나오는지 재검증하세요.' : '이 샘플 기간·조건에서는 좋은 결과가 아니었습니다. 실제 매수 추천이 아니라, 전략이 손실을 낼 수도 있음을 확인하는 백테스트 결과입니다.'; output.innerHTML=`<div class="strategy-result-head"><span>${label} · 실제 계산 결과</span><b>수익률 ${data.totalReturn}%</b><em>MDD ${data.maxDrawdown}%</em></div><p>전략 #${data.strategyId} 저장 · 체결 ${data.tradeCount}건 · 실현 손익 ${Number(data.realizedPnl).toLocaleString()} · Sharpe ${data.sharpeRatio ?? '계산 불가'}</p>${detail ? `<dl><dt>매매 규칙</dt><dd>${detail.rule}</dd><dt>주의할 점</dt><dd>${detail.risk}</dd></dl>` : ''}<p class="backtest-meaning"><b>결과 해석:</b> ${explanation}</p>`; } catch (error) { output.innerHTML=`<span class="error">${esc(error.message)}</span>`; }
}
document.addEventListener('DOMContentLoaded', async () => {
  await initPage();
  const tab=new URLSearchParams(location.search).get('tab') || 'simulation';
  const tabSections={
    simulation:['quant-overview','quant-quickstart','quant-strategies','quant-factors','quant-explorer','quant-backtest'],
    schema:['quant-dataset','quant-schema'],
    algorithm:['quant-guide']
  };
  document.querySelectorAll('[data-quant-tab]').forEach(section => { section.hidden=section.dataset.quantTab !== tab; });
  Object.values(tabSections).flat().forEach(id => { const section=document.getElementById(id); if (section) section.hidden=!(tabSections[tab] || tabSections.simulation).includes(id); });
  const subtabs={ start:['quant-quickstart'], strategy:['quant-strategies','quant-exit'], analysis:['quant-factors','quant-explorer','quant-backtest'] };
  const subnav=document.getElementById('quant-subnav');
  const applySubtab=(name, remember) => {
    if (!subnav || subnav.hidden) return;
    const active=subtabs[name] ? name : 'start';
    Object.entries(subtabs).forEach(([key, ids]) => ids.forEach(id => { const section=document.getElementById(id); if (section) section.hidden=key !== active; }));
    subnav.querySelectorAll('[data-subtab]').forEach(button => { const on=button.dataset.subtab === active; button.classList.toggle('active', on); button.setAttribute('aria-selected', String(on)); });
    if (remember) { const url=new URL(location.href); url.searchParams.set('sub', active); history.replaceState(null, '', url); }
  };
  if (subnav) subnav.querySelectorAll('[data-subtab]').forEach(button => button.addEventListener('click', () => applySubtab(button.dataset.subtab, true)));
  applySubtab(new URLSearchParams(location.search).get('sub'), false);
  if (tab === 'algorithm') {
    const guide=document.querySelector('#quant-guide .guide-body');
    if (guide && !guide.querySelector('[data-algorithm-summary]')) guide.insertAdjacentHTML('afterbegin', `<section class="algorithm-summary" data-algorithm-summary><h3>이 화면에서 실제로 계산하는 알고리즘</h3><div><article><b>추세</b><p>MA 5/20과 MA 20/60의 교차·회복으로 상승 흐름을 따릅니다.</p></article><article><b>반전</b><p>RSI가 과매도 구간에서 회복하는 시점을 매수 후보로 봅니다.</p></article><article><b>돌파·모멘텀</b><p>20일 고점·거래량 또는 60일 수익률로 강한 흐름을 찾습니다.</p></article></div><p><strong>공통 원칙:</strong> 각 날짜의 종가와 과거 데이터만 사용하고, 수수료·슬리피지를 넣은 뒤 BUY/SELL 체결과 손익을 저장합니다.</p></section>`);
  }
  if (tab === 'schema') {
    const schema=document.getElementById('quant-schema');
    if (schema && !schema.querySelector('[data-stack-summary]')) schema.insertAdjacentHTML('afterend', `<section class="quant-card stack-summary" data-stack-summary><header><b>03</b><div><h2>기술 스택 · Docker 구성</h2><p>퀀트 데이터는 웹 애플리케이션의 회원·주문 DB와 분리된 PostgreSQL에 저장합니다.</p></div></header><div class="guide-body"><div class="stack-flow"><span>Browser</span><i>→</i><span>Nginx</span><i>→</i><span>Flask API</span><i>→</i><span>PostgreSQL 16</span></div><div class="schema-details"><article><h3>Frontend · Nginx</h3><p>정적 화면을 제공하고 <code>/api/quant/*</code> 요청을 Flask로 프록시합니다. 브라우저가 DB에 직접 연결하지 않습니다.</p></article><article><h3>Backend · Flask + SQLAlchemy</h3><p>시계열 조회, 전략 실행, 알파·베타 회귀, 체결·성과 저장을 수행합니다. 입력값은 SQL 바인딩으로 처리합니다.</p></article><article><h3>Data · PostgreSQL 16</h3><p>날짜 파티션 OHLCV, BRIN 인덱스, JSONB 전략 파라미터, 거래 로그·팩터 노출도를 저장합니다.</p></article></div><div class="guide-warning"><b>AWS VM 운영 방식</b><span>Docker Compose의 <code>postgres:16-alpine</code> 컨테이너를 내부 네트워크에만 두고, 5432 포트는 외부에 열지 않습니다. 데이터는 <code>postgres-quant-data</code> Docker 볼륨에 유지하며, <code>QUANT_DATABASE_URL</code>로 Flask와 연결합니다.</span></div><p class="guide-note">상세 기동, 보안 그룹, 백업·복구 명령은 저장소 README의 “PostgreSQL Quant on AWS VM” 절을 확인하세요.</p></div></section>`);
  }
  document.querySelectorAll('[data-load]').forEach(button => button.addEventListener('click', () => load(button.dataset.load))); $('[data-run]').addEventListener('click', () => run());
  document.querySelectorAll('[data-strategy]').forEach(button => button.addEventListener('click', () => showStrategy(button.dataset.strategy)));
  $('[data-factor-run]').addEventListener('click', runFactorAnalysis);
  $('[data-exit-upload]').addEventListener('click', uploadFlows); $('[data-exit-score]').addEventListener('click', showExitScore); $('[data-exit-backtest]').addEventListener('click', runExitBacktest);
  try { const response=await fetch('/api/quant/overview'), data=await response.json(); if (!response.ok) throw new Error(data.message); $('[data-quant-stats]').innerHTML=`<b>연결됨</b><span>OHLCV ${Number(data.market_rows).toLocaleString()}건</span><span>전략 ${data.strategy_count}개</span><span>거래 로그 ${data.trade_count}건</span><span>${esc((data.symbols || []).join(' · '))}</span>`; } catch (error) { $('[data-quant-stats]').innerHTML=`<b class="error">${esc(error.message || 'PostgreSQL에 연결할 수 없습니다.')}</b>`; }
});
