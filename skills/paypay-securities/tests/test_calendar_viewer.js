// Run with node; execute the actual viewer aggregation and summary code.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(__dirname + '/../paypay_sec/calendar_template.html', 'utf8');
const extract = (a,b) => html.slice(html.indexOf(a), html.indexOf(b));
const js = extract('function worstQuality', '/* ======================= 热力分档') +
  extract('function countableInPeriod', '/* ======================= 年视图') +
  extract('function renderYear()', '// 构建 7 行');
const day = (p, from='2026-11-01', quality='full') => ({pnl:p, comparison_from:from, data_quality:quality});
function run(accounts, records) {
  const ctx = {DATA:{accounts,days:records}, state:{account:'total',cursor:new Date(2026,0,1)}, viewEl:{innerHTML:''}, buildHeatmap:()=>'', bindCellTooltips:()=>{}, fmtJPY:n=>String(n), signClass:()=>'', ymd:d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`};
  vm.createContext(ctx); vm.runInContext(js,ctx); return ctx;
}
let c=run(['leo','lisa'],[{date:'2026-11-02',accounts:{lisa:day(-100)}}]);
assert.equal(c.getDay('2026-11-02','total').data_quality,'partial');
assert.equal(c.getDay('2026-11-02','total').pnl,-100);
assert(c.monthSummary(2026,10).includes('0 个可计入区间'));
assert(!c.monthSummary(2026,10).includes('>-100<'));
c=run(['leo','lisa'],[{date:'2026-11-02',accounts:{leo:day(null),lisa:day(-100)}}]);
assert.equal(c.getDay('2026-11-02','total').data_quality,'partial');
assert(c.monthSummary(2026,10).includes('0 个可计入区间'));
c.renderYear();
assert(c.viewEl.innerHTML.includes('0 个可计入区间'));
assert(!c.viewEl.innerHTML.includes('>-100<'));
c=run(['leo','lisa'],[{date:'2026-11-02',accounts:{leo:day(200),lisa:day(-100)}}]);
assert.equal(c.getDay('2026-11-02','total').pnl,100);
assert.equal(c.getDay('2026-11-02','total').data_quality,'full');
assert(c.monthSummary(2026,10).includes('>100<'));
c=run(['leo','lisa'],[{date:'2026-11-02',accounts:{leo:day(200,'2026-10-31'),lisa:day(-100)}}]);
assert.equal(c.getDay('2026-11-02','total').data_quality,'missing');
assert.equal(c.getDay('2026-11-02','total').pnl,null);
c=run(['leo','lisa'],[{date:'2026-11-02',accounts:{leo:{...day(null),total_assets:500},lisa:{...day(-100),total_assets:300}}}]);
assert.equal(c.getDay('2026-11-02','total').total_assets,800);
assert.equal(c.getDay('2026-11-02','total').assets_partial,false);
c=run(['leo','lisa'],[{date:'2026-11-02',accounts:{lisa:{...day(-100),total_assets:300}}}]);
assert.equal(c.getDay('2026-11-02','total').assets_partial,true);
c=run(['leo'],[{date:'2026-11-02',accounts:{leo:day(123,'2026-10-31')}}]);
c.state.account='leo';
assert(c.monthSummary(2026,10).includes('0 个可计入区间'));
assert(c.countableInPeriod(c.getDay('2026-11-02','leo'),'2026','2026-11-02'));
assert(!c.countableInPeriod(c.getDay('2026-11-02','leo'),'2026-11','2026-11-02'));
c=run(['leo'],[
  {date:'2026-01-01',accounts:{leo:day(10,'2025-12-31')}},
  {date:'2026-02-01',accounts:{leo:day(20,'2026-01-31')}},
  {date:'2026-02-09',accounts:{leo:day(30,'2026-02-06')}}, // Friday to Monday
  {date:'2026-03-02',accounts:{leo:day(999,'2026-02-27')}}, // cross-month weekend
]);
c.state.account='leo';
assert(c.monthSummary(2026,0).includes('>10<'));
assert(c.monthSummary(2026,1).includes('>50<'));
assert(c.monthSummary(2026,1).includes('2 个可计入区间'));
assert(c.monthSummary(2026,1).includes('最佳区间'));
assert(c.monthSummary(2026,1).includes('涨 / 跌 区间数'));
assert(c.monthSummary(2026,2).includes('0 个可计入区间'));
c.renderYear();
assert(c.viewEl.innerHTML.includes('>1059<')); // cross-month weekend stays in year
assert(c.viewEl.innerHTML.includes('4 个可计入区间'));
assert(c.viewEl.innerHTML.includes('区间胜率'));
c=run(['leo'],[{date:'2026-01-02',accounts:{leo:day(99,'2025-12-30')}}]);
c.state.account='leo'; c.renderYear();
assert(c.viewEl.innerHTML.includes('0 个可计入区间'));
c=run(['leo','lisa'],[{date:'2026-11-02',accounts:{leo:day(null),lisa:day(null)}}]);
assert.equal(c.getDay('2026-11-02','total').data_quality,'missing');
console.log('calendar viewer regression tests passed');
