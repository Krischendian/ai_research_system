# test_debug.py
import logging
logging.basicConfig(level=logging.WARNING)

from research_automation.services.sector_report_service import _get_revenue_segments

for t in ['DHL', 'BT/A', 'FRE', 'KBX']:
    data, year, source = _get_revenue_segments(t)
    print(f'{t}: {source}, FY{year}, {len(data)}行')