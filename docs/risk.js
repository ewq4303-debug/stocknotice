(function(){
  var riskData = null;
  var gauge = null;
  var bar = null;

  function fmt(n){return n==null?'—':Number(n).toLocaleString('zh-TW');}
  function pct(n){return n==null?'—':(Number(n)*100).toFixed(1)+'%';}
  function badge(v){return '<span class="risk-badge '+v+'">'+v+'</span>';}
  function isRiskTabVisible(){
    var tab = document.getElementById('tab-risk');
    return !!(tab && tab.classList.contains('active'));
  }
  function qualityList(data, key){
    var quality = data && data.data_quality ? data.data_quality : {};
    var values = Array.isArray(quality[key]) ? quality[key] : [];
    return values.length ? values.join(', ') : '無';
  }
  function resizeRiskCharts(){
    if (gauge) gauge.resize();
    if (bar) bar.resize();
  }
  function renderCharts(){
    if (!riskData || !isRiskTabVisible() || !window.echarts) return;
    var gaugeEl = document.getElementById('riskGauge');
    var barEl = document.getElementById('riskBars');
    if (!gaugeEl || !barEl) return;
    if (!gauge) gauge = echarts.init(gaugeEl);
    if (!bar) bar = echarts.init(barEl);
    var lev = riskData.leverage || {};
    var contrib = Array.isArray(riskData.top_contributors) ? riskData.top_contributors : [];
    gauge.setOption({series:[{type:'gauge',min:0,max:5,progress:{show:true,width:12},axisLine:{lineStyle:{width:12,color:[[0.7,'#1ed992'],[1,'#ff525b']]}},axisTick:{show:false},splitLine:{lineStyle:{color:'#5d6675'}},axisLabel:{color:'#8b95a5'},detail:{formatter:function(v){return v.toFixed(2)+'×';},color:'#dde3ec',fontSize:24},data:[{value:lev.nlr_net||0,name:lev.verdict||''}]}]}, true);
    bar.setOption({grid:{left:80,right:28,top:20,bottom:30},xAxis:{type:'value',axisLabel:{color:'#8b95a5',formatter:function(v){return (v/10000).toFixed(0)+'萬';}},splitLine:{lineStyle:{color:'#1e2632'}}},yAxis:{type:'category',data:contrib.map(function(r){return r.symbol+' '+r.name;}),axisLabel:{color:'#8b95a5'}},series:[{type:'bar',data:contrib.map(function(r){return r['dpnl_at_-20pct']||0;}),itemStyle:{color:function(p){return p.value<0?'#ff525b':'#4d7fff';}},label:{show:true,position:'right',color:'#dde3ec',formatter:function(p){return fmt(p.value);}}}]}, true);
    setTimeout(resizeRiskCharts, 60);
  }
  function renderRisk(data){
    riskData = data || {};
    var root=document.getElementById('riskRoot'); if(!root)return;
    var eq=riskData.equity||{}, lev=riskData.leverage||{}, stress=Array.isArray(riskData.stress)?riskData.stress:[];
    root.innerHTML='<div class="section-head"><span class="eyebrow">Risk Exposure</span><h2>合併淨槓桿 ＋ 子籃子壓力敏感度</h2><span class="inv-updated">'+(riskData.as_of||'')+'</span></div>'+
      '<div class="risk-kpis">'+
      '<div class="metric '+(lev.verdict==='OVER_LEVERED'?'up':'accent')+'"><div class="label">Net Leverage Ratio</div><div class="value num">'+(lev.nlr_net||0).toFixed(2)+'×</div><div class="change">'+(lev.verdict||'')+'</div></div>'+
      '<div class="metric"><div class="label">Gross Notional</div><div class="value num">'+fmt(lev.gross_notional)+'</div><div class="change">淨曝險 '+fmt(lev.net_notional)+'</div></div>'+
      '<div class="metric"><div class="label">Combined Equity</div><div class="value num">'+fmt(eq.combined)+'</div><div class="change">期貨權益 '+fmt(eq.futures)+'</div></div>'+
      '<div class="metric"><div class="label">Margin</div><div class="value num">'+fmt(eq.futures_maint_margin)+'</div><div class="change">原始 '+fmt(eq.futures_init_margin)+'</div></div></div>'+
      '<div class="risk-grid"><div class="card"><div class="card-title"><span>NLR 儀表</span><span class="risk-note">紅燈門檻 3.5×</span></div><div id="riskGauge" class="risk-chart"></div></div>'+
      '<div class="card"><div class="card-title"><span>三月重演保證金存活檢定</span></div><div class="risk-signal-list">'+stress.map(function(s){return '<div class="risk-signal"><div><div class="shock num">'+pct(s.shock)+'</div><div class="risk-note">總損益 '+fmt(s.dpnl_total)+'（'+pct(s.dpnl_pct_equity)+'），期貨權益後 '+fmt(s.futures_equity_after)+'</div></div>'+badge(s.verdict)+'</div>';}).join('')+'</div></div></div>'+
      '<div class="card"><div class="card-title"><span>−20% 壓力貢獻</span><span class="risk-note">虧損紅、獲利藍；β 缺值不計入並列於資料品質</span></div><div id="riskBars" class="risk-chart"></div><div class="risk-note">缺價格：'+qualityList(riskData,'missing_prices')+'　缺 β：'+qualityList(riskData,'missing_beta')+'</div></div>';
    gauge = null;
    bar = null;
    renderCharts();
  }
  function loadRisk(){
    fetch('risk.json?v='+Date.now()).then(function(r){if(!r.ok)throw new Error(r.status);return r.json();}).then(renderRisk).catch(function(e){var root=document.getElementById('riskRoot');if(root)root.innerHTML='<div class="card"><div class="card-title">風險資料尚未產生</div><div class="risk-note">請執行 Daily Futures Risk Exposure action 產生 docs/risk.json。'+e.message+'</div></div>';});
  }

  window.renderRiskCharts = renderCharts;
  window.addEventListener('resize', resizeRiskCharts);
  document.addEventListener('click', function(e){
    var btn = e.target && e.target.closest ? e.target.closest('[data-tab-target="tab-risk"]') : null;
    if (btn) setTimeout(renderCharts, 80);
  });
  loadRisk();
}());
