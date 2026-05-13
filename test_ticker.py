from research_automation.services.sector_report_service import _get_revenue_segments

for t in ['DHL', 'DHL GY', 'DHL GY Equity']:
    rows, year, source = _get_revenue_segments(t)
    print(f'{t}: {source}, {len(rows)}行')
