(function(){
  function fmt(n){return Number(n||0).toLocaleString('zh-TW');}
  function pct(n){return (Number(n||0)*100).toFixed(1)+'%';}
  function badge(v){return '<span class="risk-badge '+v+'">'+v+'</span>';}
  function renderRisk(data){
    var root=document.getElementById('riskRoot'); if(!root)return;
    var lev=data.leverage||{}, eq=data.equity||{}, stress=data.stress||[];
    root.innerHTML='<div class="section-head"><span class="eyebrow">Risk Exposure</span><h2>合併淨槓桿 ＋ 子籃子壓力敏感度</h2><span class="inv-updated">'+(data.as_of||'')+'</span></div>'+
      '<div class="risk-kpis">'+
      '<div class="metric accent"><div class="label">NLR Gross</div><div class="value num">'+Number(lev.nlr_gross||0).toFixed(2)+'×</div><div class="change">'+badge(lev.verdict||'OK')+'</div></div>'+
      '<div class="metric"><div class="label">Net Bias</div><div class="value num">'+Number(lev.nlr_net||0).toFixed(2)+'×</div><div class="change">淨名目 '+fmt(lev.net_notional)+'</div></div>'+
      '<div class="metric"><div class="label">Combined Equity</div><div class="value num">'+fmt(eq.combined)+'</div><div class="change">期貨權益 '+fmt(eq.futures)+'</div></div>'+
      '<div class="metric"><div class="label">Margin</div><div class="value num">'+fmt(eq.futures_maint_margin)+'</div><div class="change">原始 '+fmt(eq.futures_init_margin)+'</div></div></div>'+
      '<div class="risk-grid"><div class="card"><div class="card-title"><span>NLR 儀表</span><span class="risk-note">紅燈門檻 3.5×</span></div><div id="riskGauge" class="risk-chart"></div></div>'+
      '<div class="card"><div class="card-title"><span>三月重演保證金存活檢定</span></div><div class="risk-signal-list">'+stress.map(function(s){return '<div class="risk-signal"><div><div class="shock num">'+pct(s.shock)+'</div><div class="risk-note">總損益 '+fmt(s.dpnl_total)+'（'+pct(s.dpnl_pct_equity)+'），期貨權益後 '+fmt(s.futures_equity_after)+'</div></div>'+badge(s.verdict)+'</div>';}).join('')+'</div></div></div>'+
      '<div class="card"><div class="card-title"><span>−20% 壓力貢獻</span><span class="risk-note">虧損紅、獲利藍；β 缺值不計入並列於資料品質</span></div><div id="riskBars" class="risk-chart"></div><div class="risk-note">缺價格：'+(data.data_quality?.missing_prices||[]).join(', ')+'　缺 β：'+(data.data_quality?.missing_beta||[]).join(', ')+'</div></div>';
    var gauge=echarts.init(document.getElementById('riskGauge'));
    gauge.setOption({backgroundColor:'transparent',series:[{type:'gauge',min:0,max:5,splitNumber:5,axisLine:{lineStyle:{width:18,color:[[0.4,'#1ed992'],[0.7,'#e0a83c'],[1,'#ff525b']]}},pointer:{width:5},progress:{show:true,width:18},detail:{formatter:function(v){return v.toFixed(2)+'×';},color:'#dde3ec',fontSize:28},data:[{value:Number(lev.nlr_gross||0),name:'NLR Gross'}],title:{color:'#8b95a5'}}]});
    var top=data.top_contributors||[];
    var bar=echarts.init(document.getElementById('riskBars'));
    bar.setOption({grid:{left:90,right:24,top:16,bottom:24},tooltip:{trigger:'axis'},xAxis:{type:'value',axisLabel:{color:'#8b95a5'},splitLine:{lineStyle:{color:'#1e2632'}}},yAxis:{type:'category',data:top.map(function(x){return x.name||x.symbol;}),axisLabel:{color:'#dde3ec'}},series:[{type:'bar',data:top.map(function(x){return x.dpnl_at_-20pct||0;}),itemStyle:{color:function(p){return p.value<0?'#ff525b':'#4d7fff';}}}]});
    window.addEventListener('resize',function(){gauge.resize();bar.resize();});
  }
  fetch('risk.json?v='+Date.now()).then(function(r){if(!r.ok)throw new Error(r.status);return r.json();}).then(renderRisk).catch(function(e){var root=document.getElementById('riskRoot');if(root)root.innerHTML='<div class="card"><div class="card-title">風險資料尚未產生</div><div class="risk-note">請執行 risk_exposure.py 產生 docs/risk.json。'+e.message+'</div></div>';});
}());
