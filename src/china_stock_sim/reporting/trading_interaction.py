"""Known, network-free trading-page interactions shared by local and cloud output."""

PUBLIC_SECTIONS = frozenset({
    'financial-history', 'cash', 'corporate-actions', 'runtime', 'benchmarks',
    'history-no-plan', 'trade-history', 'buy-lots',
})

TRADING_SCRIPT = r"""(()=>{
const key='simulate-china-stocks-trading-ui';
const publicSections=new Set(['financial-history','cash','corporate-actions','runtime','benchmarks','history-no-plan','trade-history','buy-lots']);
let saved={};try{const raw=sessionStorage.getItem(key);if(raw&&raw.length<=20000){const value=JSON.parse(raw);if(value&&typeof value==='object'&&!Array.isArray(value))saved=value;}}catch(_){}
const entries=value=>value&&typeof value==='object'&&!Array.isArray(value)?Object.entries(value):[];
const days=new Map(entries(saved.days));const opened=new Map(entries(saved.opened));const history=new Map(entries(saved.history));
const account=document.getElementById('account');
const sections=Array.from(document.querySelectorAll('section[data-account]'));
const persist=()=>{try{sessionStorage.setItem(key,JSON.stringify({account:account?account.value:null,days:Object.fromEntries(days),opened:Object.fromEntries(opened),history:Object.fromEntries(history)}));}catch(_){}};
const validValue=(select,value)=>typeof value==='string'&&Array.from(select.options).some(option=>option.value===value);
const historyControllers=new Map();
const historyStatuses=new Map([['HELD','当前持有'],['CLOSED','股份已清仓'],['CLOSED_PENDING','股份已清仓但权益／税款待处理'],['UNKNOWN','分类待核']]);
const plain=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const safeText=(value,limit)=>typeof value==='string'&&value.length<=limit;
const count=value=>Number.isSafeInteger(value)&&value>=0;
const historyPage=value=>Number.isSafeInteger(value)&&value>0?value:1;
const historyRows=(chunks,columns,total)=>{
 if(!Array.isArray(chunks)||chunks.length>10000||!Array.isArray(columns)||!columns.length||columns.length>32||!columns.every(c=>safeText(c,120))||!count(total))throw Error('history');
 const rows=[];
 for(const chunk of chunks){
  if(!Array.isArray(chunk)||chunk.length>1000)throw Error('history');
  for(const row of chunk){
   if(!plain(row)||!safeText(row.code,16)||!/^(SH|SZ|BJ)\d{6}$/.test(row.code)||!historyStatuses.has(row.status)||!Array.isArray(row.cells)||row.cells.length!==columns.length||!row.cells.every(cell=>cell===null||safeText(cell,4096)))throw Error('history');
   rows.push(row);
  }
 }
 if(rows.length!==total)throw Error('history');
 return rows;
};
const nodeText=(tag,value)=>{const node=document.createElement(tag);node.textContent=value===null?'—':String(value);return node;};
const activateHistory=section=>{
 if(!section.dataset.historyId||historyControllers.has(section))return;
 const panel=section.querySelector('[data-trade-history]');if(!panel)return;
 historyControllers.set(section,true);
 const find=name=>panel.querySelector('[data-history-'+name+']');
 const fields={stock:find('stock'),status:find('status'),rows:find('rows'),summary:find('summary'),head:find('head'),summaryHead:find('summary-head'),prev:find('prev'),next:find('next'),page:find('page'),summaryPrev:find('summary-prev'),summaryNext:find('summary-next'),summaryPage:find('summary-page'),info:find('info')};
 const message=text=>{
  if(fields.info)fields.info.textContent=text;
  for(const key of ['rows','summary','head','summaryHead'])if(fields[key])fields[key].replaceChildren();
  for(const key of ['stock','status','prev','next','summaryPrev','summaryNext'])if(fields[key])fields[key].disabled=true;
  for(const key of ['page','summaryPage'])if(fields[key])fields[key].textContent='历史待核';
 };
 try{
  if(Object.values(fields).some(value=>!value))throw Error('history');
  const identity=section.dataset.account,id=section.dataset.historyId;
  if(!/^history-[A-Za-z0-9_-]{1,80}$/.test(id))throw Error('history');
  const source=document.getElementById(id);
  if(!source||source.type!=='application/json'||!safeText(source.textContent,10000000))throw Error('history');
  const data=JSON.parse(source.textContent);
  if(!plain(data)||data.schema_version!==1||data.scope!=='all_time'||data.account_id!==identity||typeof data.complete!=='boolean'||!['READY','UNAVAILABLE'].includes(data.state)||data.complete!==(data.state==='READY')||!plain(data.row_counts)||!safeText(data.snapshot_at,80)||!/(Z|[+-]\d{2}:\d{2})$/.test(data.snapshot_at)||!Number.isFinite(Date.parse(data.snapshot_at))||!count(data.account_revision)||!count(data.report_revision))throw Error('history');
  if(!data.complete){message('完整交易历史待核'+(safeText(data.reason,1000)&&data.reason?'：'+data.reason:''));return;}
  const trades=historyRows(data.trade_chunks,data.trade_columns,data.row_counts.trades);
  const summaries=historyRows(data.summary_chunks,data.summary_columns,data.row_counts.summaries);
  const stocks=new Map();
  for(const row of summaries){if(stocks.has(row.code))throw Error('history');stocks.set(row.code,row);}
  if(trades.some(row=>!stocks.has(row.code)||stocks.get(row.code).status!==row.status))throw Error('history');
  fields.head.replaceChildren(...data.trade_columns.map(label=>{const th=nodeText('th',label);th.scope='col';return th;}));
  fields.summaryHead.replaceChildren(...data.summary_columns.map(label=>{const th=nodeText('th',label);th.scope='col';return th;}));
  const option=(value,label)=>{const node=nodeText('option',label);node.value=value;return node;};
  fields.stock.replaceChildren(option('','全部股票'),...Array.from(stocks,([code,row])=>option(code,row.cells[0]&&row.cells[0]!==code?row.cells[0]+' · '+code:code)));
  fields.status.replaceChildren(option('','全部分类'),...Array.from(historyStatuses,([value,label])=>option(value,label)));
  const old=plain(history.get(identity))?history.get(identity):{};
  const selection={stock:stocks.has(old.stock)?old.stock:'',status:historyStatuses.has(old.status)?old.status:'',page:historyPage(old.page),summaryPage:historyPage(old.summaryPage)};
  fields.stock.value=selection.stock;fields.status.value=selection.status;
  let filteredTrades=[],filteredSummary=[];
  const filter=()=>{const accepts=row=>(!selection.stock||row.code===selection.stock)&&(!selection.status||row.status===selection.status);filteredTrades=trades.filter(accepts);filteredSummary=summaries.filter(accepts);};
  const draw=(rows,columns,target,pageKey,label,previous,next)=>{
   const pages=Math.ceil(rows.length/20);selection[pageKey]=Math.min(selection[pageKey],Math.max(1,pages));
   const nodes=rows.slice((selection[pageKey]-1)*20,selection[pageKey]*20).map(row=>{const tr=document.createElement('tr');tr.append(...row.cells.map(cell=>nodeText('td',cell)));return tr;});
   if(!nodes.length){const tr=document.createElement('tr'),td=nodeText('td','没有符合当前筛选的记录');td.colSpan=columns.length;tr.append(td);nodes.push(tr);}
   target.replaceChildren(...nodes);label.textContent='第 '+(pages?selection[pageKey]:0)+' / '+pages+' 页 · 共 '+rows.length+' 条';previous.disabled=!pages||selection[pageKey]<=1;next.disabled=!pages||selection[pageKey]>=pages;
  };
  const render=()=>{
   draw(filteredTrades,data.trade_columns,fields.rows,'page',fields.page,fields.prev,fields.next);
   draw(filteredSummary,data.summary_columns,fields.summary,'summaryPage',fields.summaryPage,fields.summaryPrev,fields.summaryNext);
   fields.info.textContent='完整已记录成交 '+data.row_counts.trades+' 笔，涉及 '+data.row_counts.summaries+' 只股票；当前筛选 '+filteredTrades.length+' 笔。快照时间见上方（北京时间）。当前分类按该快照，不随上方历史交易日改变。';
   history.set(identity,{...selection});persist();
  };
  for(const key of ['stock','status'])fields[key].addEventListener('change',()=>{selection[key]=fields[key].value;selection.page=selection.summaryPage=1;filter();render();});
  for(const [key,pageKey,delta] of [['prev','page',-1],['next','page',1],['summaryPrev','summaryPage',-1],['summaryNext','summaryPage',1]])fields[key].addEventListener('click',()=>{selection[pageKey]=Math.max(1,selection[pageKey]+delta);render();});
  filter();render();
 }catch(_){message('完整交易历史待核：展示数据缺失或未通过核对，请查看本机记录。');}
};
let displays=[];try{const node=document.getElementById('account-display');if(node){const value=JSON.parse(node.textContent);if(Array.isArray(value))displays=value;}}catch(_){}
for(const section of sections){
 const identity=section.dataset.account;
 const selector=section.querySelector('[data-trade-selector]');
 if(selector){
  if(validValue(selector,days.get(identity)))selector.value=days.get(identity);
  const updateDay=()=>{section.querySelectorAll('[data-trade-day]').forEach(day=>{day.hidden=day.dataset.tradeDay!==selector.value;});days.set(identity,selector.value);persist();};
  selector.addEventListener('change',updateDay);updateDay();
 }
 const occurrences=new Map();
 section.querySelectorAll('details[data-public-section]').forEach(detail=>{
  const kind=detail.dataset.publicSection;if(!publicSections.has(kind))return;
  let discriminator;
  if(kind==='buy-lots'){
   discriminator=detail.dataset.publicKey;
   if(typeof discriminator!=='string'||!/^(SH|SZ|BJ)\d{6}$/.test(discriminator))return;
  }else{discriminator=occurrences.get(kind)||0;occurrences.set(kind,discriminator+1);}
  const id=JSON.stringify([identity,kind,discriminator]);
  if(typeof opened.get(id)==='boolean')detail.open=opened.get(id);
  detail.addEventListener('toggle',()=>{opened.set(id,detail.open);persist();});
 });
}
if(account){
 let previous=saved.account;try{if(typeof previous!=='string')previous=sessionStorage.getItem('cloud-account');}catch(_){}
 if(validValue(account,previous))account.value=previous;
 const updateAccount=()=>{
  sections.forEach(section=>{section.hidden=section.dataset.account!==account.value;});
  const selected=sections.find(section=>section.dataset.account===account.value);if(selected)activateHistory(selected);
  const display=displays.find(item=>item&&item.account_id===account.value);
  if(display)for(const [id,field] of [['initial','initial'],['cash','cash'],['equity','cash'],['formula','formula']]){const node=document.getElementById(id);if(node&&display[field]!=null)node.textContent=String(display[field]);}
  persist();
 };
 account.addEventListener('change',updateAccount);updateAccount();
}
const updateTimes=()=>{
 const age=document.getElementById('age');const updated=document.getElementById('updated');
 if(age&&updated){const time=Date.parse(updated.dateTime);const minutes=Math.floor((Date.now()-time)/60000);age.textContent=!Number.isFinite(time)?'页面生成时间待核':minutes<0?'本机时钟早于页面生成时间，请核对时钟。':`距页面生成 ${minutes} 分钟；页面每60秒刷新，行情按已采集时点展示。`;}
 const fresh=document.getElementById('cloud-freshness');if(fresh){const due=Date.parse(fresh.dataset.nextDue);fresh.textContent=Number.isFinite(due)?(Date.now()>due?'等待本机更新':'已同步'):'同步期限未知';}
};
updateTimes();setInterval(updateTimes,60000);setTimeout(()=>location.reload(),60000);
})();"""
