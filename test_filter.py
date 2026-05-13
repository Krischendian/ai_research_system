import sys
sys.path.insert(0, 'src')
from research_automation.services.sector_report_service import (
    _filter_by_ticker_whitelist,
    _sanitize_bracket_tickers,
)

test_text = "\nACN本季度高级AI订单达22亿美元，同比近翻倍，GenAI累计订单约115亿美元。\nIBM软件收入增长10%，HashiCorp并购完成，WatsonX平台新增企业客户超200家。\nBAH受DOGE政策影响，联邦业务收入下滑，预计FY26全年营收持平。\nUPS裁员两万人，年化成本节省约10亿美元，Q4营收同比下降约1%。\nCTSH AI相关营收占比提升至15%，GenAI项目数量环比翻倍，NRR维持在108%。\n"

allowed = {'ACN','AVY','BAH','BT/A LN','CTSH','DG','DHL GY','EL','FRE GY','HCA','IBM','JLL','KBX GY','MDB','PPG','RTO','TGT','UPS','ZM'}

cleaned = _sanitize_bracket_tickers(test_text)
filtered = _filter_by_ticker_whitelist(cleaned, allowed)

print('=== 过滤前 ===')
print(test_text)
print('=== 过滤后 ===')
print(filtered)