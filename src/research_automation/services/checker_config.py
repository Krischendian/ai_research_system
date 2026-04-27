TICKER_KEYWORDS = {
    "AVY": ["Avery Dennison", "AVY"],
    "BAH": ["Booz Allen", "BAH"],
    "CTSH": ["Cognizant", "CTSH"],
    "DG": ["Dollar General", "DG"],
    "EL": ["Estee Lauder", "EL", "雅诗兰黛"],
    "HCA": ["HCA Healthcare", "HCA"],
    "JLL": ["Jones Lang", "JLL"],
    "MDB": ["MongoDB", "MDB", "Atlas"],
    "PPG": ["PPG Industries", "PPG"],
    "RTO": ["Rentokil", "RTO"],
    "TGT": ["Target", "TGT"],
    "UPS": ["UPS", "United Parcel"],
    "ZM": ["Zoom", "ZM"],
}

METRIC_KEYWORDS = {
    "revenue": ["营收", "收入", "revenue", "net sales", "销售额", "总收入"],
    "net_income": ["净利润", "净利", "net income", "净亏损"],
    "gross_margin": ["毛利率", "gross margin"],
    "ebitda": ["EBITDA", "息税折旧"],
    "capex": ["资本支出", "capex", "CAPEX"],
    "net_debt_to_equity": ["净债务", "杠杆", "net debt"],
    "yoy_growth": ["同比", "YoY", "year-over-year", "增长", "下降", "增速"],
    "eps": ["EPS", "每股收益", "每股", "摊薄"],
}

# 允许净利率超过 35% 阈值的公司（高毛利软件/SaaS）
SOFTWARE_EXCEPTIONS = {"MDB", "ZM", "CTSH"}

# 容差配置
DEFAULT_TOLERANCE = 0.02  # 2%
WARNING_THRESHOLD = 0.10  # 10%
