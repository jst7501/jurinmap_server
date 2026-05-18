"""SEC SIC code → sector 매핑.

SEC SIC (Standard Industrial Classification) 4자리 코드 → 12개 sector 그룹.
sector 컬럼이 100% NULL 인 페니 universe 즉시 채우는 fallback.

Reference: SEC EDGAR SIC list (full lookup at
  https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC=ALL)
"""

SIC_RANGES = [
    # (start, end_inclusive, sector, sector_full)
    (100, 999,   "Agriculture",      "Agriculture, Forestry, Fishing"),
    (1000, 1499, "Energy",           "Mining (Metals, Coal, Oil & Gas)"),
    (1500, 1799, "Industrials",      "Construction"),
    (2000, 2099, "Consumer Staples", "Food & Beverages"),
    (2100, 2199, "Consumer Staples", "Tobacco"),
    (2200, 2399, "Consumer Disc.",   "Textile / Apparel"),
    (2400, 2499, "Industrials",      "Lumber & Wood"),
    (2500, 2599, "Consumer Disc.",   "Furniture"),
    (2600, 2699, "Materials",        "Paper"),
    (2700, 2799, "Comm. Services",   "Printing & Publishing"),
    (2800, 2899, "Materials",        "Chemicals"),
    (2830, 2836, "Healthcare",       "Pharmaceuticals"),
    (2900, 2999, "Energy",           "Petroleum Refining"),
    (3000, 3099, "Materials",        "Rubber & Plastics"),
    (3100, 3199, "Consumer Disc.",   "Leather"),
    (3200, 3299, "Materials",        "Stone, Clay, Glass"),
    (3300, 3399, "Materials",        "Primary Metal Industries"),
    (3400, 3499, "Industrials",      "Fabricated Metal"),
    (3500, 3599, "Industrials",      "Industrial Machinery"),
    (3600, 3699, "Technology",       "Electronic Equipment"),
    (3700, 3799, "Industrials",      "Transportation Equipment"),
    (3711, 3713, "Consumer Disc.",   "Motor Vehicles"),
    (3721, 3729, "Industrials",      "Aircraft / Aerospace"),
    (3812, 3812, "Industrials",      "Defense"),
    (3825, 3829, "Healthcare",       "Medical Instruments"),
    (3841, 3845, "Healthcare",       "Medical Instruments"),
    (3826, 3826, "Technology",       "Lab Instruments"),
    (3674, 3674, "Technology",       "Semiconductors"),
    (3669, 3669, "Technology",       "Comm. Equipment"),
    (3661, 3661, "Technology",       "Telephone & Telegraph Apparatus"),
    (3663, 3663, "Technology",       "Radio & TV Broadcasting Equipment"),
    (3812, 3873, "Technology",       "Instruments"),
    (3900, 3999, "Consumer Disc.",   "Misc Manufacturing"),
    (4000, 4099, "Industrials",      "Rail Transport"),
    (4100, 4199, "Industrials",      "Bus / Motor Coach"),
    (4200, 4299, "Industrials",      "Trucking & Warehousing"),
    (4400, 4499, "Industrials",      "Water Transport"),
    (4500, 4599, "Industrials",      "Air Transport"),
    (4600, 4699, "Industrials",      "Pipelines"),
    (4700, 4799, "Industrials",      "Transport Services"),
    (4800, 4899, "Comm. Services",   "Communications"),
    (4812, 4813, "Comm. Services",   "Telecom"),
    (4832, 4833, "Comm. Services",   "Radio / TV"),
    (4841, 4841, "Comm. Services",   "Cable TV"),
    (4899, 4899, "Comm. Services",   "Comm. Services"),
    (4900, 4999, "Utilities",        "Electric, Gas, Sanitary"),
    (5000, 5199, "Consumer Staples", "Wholesale Trade"),
    (5200, 5299, "Consumer Disc.",   "Retail Building Materials"),
    (5300, 5399, "Consumer Disc.",   "Retail General Merchandise"),
    (5400, 5499, "Consumer Staples", "Retail Food"),
    (5500, 5599, "Consumer Disc.",   "Retail Auto"),
    (5600, 5699, "Consumer Disc.",   "Retail Apparel"),
    (5700, 5799, "Consumer Disc.",   "Retail Furniture"),
    (5800, 5899, "Consumer Disc.",   "Retail Eating & Drinking"),
    (5900, 5999, "Consumer Disc.",   "Retail Misc"),
    (5961, 5961, "Consumer Disc.",   "Online Retail"),
    (6000, 6099, "Financials",       "Banks"),
    (6020, 6020, "Financials",       "Commercial Banks"),
    (6021, 6022, "Financials",       "Commercial Banks"),
    (6035, 6036, "Financials",       "Savings Institution"),
    (6099, 6099, "Financials",       "Foreign Banks"),
    (6100, 6199, "Financials",       "Credit / Finance"),
    (6200, 6299, "Financials",       "Securities / Brokers"),
    (6211, 6211, "Financials",       "Brokers & Dealers"),
    (6300, 6399, "Financials",       "Insurance"),
    (6311, 6311, "Financials",       "Life Insurance"),
    (6321, 6321, "Financials",       "Health Insurance"),
    (6331, 6331, "Financials",       "Property & Casualty Insurance"),
    (6500, 6599, "Real Estate",      "Real Estate"),
    (6512, 6512, "Real Estate",      "Real Estate Operators"),
    (6770, 6770, "Financials",       "Blank Checks / SPAC"),
    (6798, 6798, "Real Estate",      "REIT"),
    (6792, 6792, "Energy",           "Oil Royalty Traders"),
    (7000, 7099, "Consumer Disc.",   "Hotels / Lodging"),
    (7200, 7299, "Consumer Disc.",   "Personal Services"),
    (7300, 7399, "Industrials",      "Business Services"),
    (7370, 7372, "Technology",       "Computer Services / Software"),
    (7371, 7371, "Technology",       "Computer Services"),
    (7372, 7372, "Technology",       "Prepackaged Software"),
    (7373, 7374, "Technology",       "Computer Integrated Systems / Data Processing"),
    (7389, 7389, "Industrials",      "Business Services NEC"),
    (7500, 7599, "Consumer Disc.",   "Auto Repair / Rental"),
    (7800, 7899, "Comm. Services",   "Amusement & Recreation"),
    (7812, 7812, "Comm. Services",   "Motion Picture Production"),
    (8000, 8099, "Healthcare",       "Health Services"),
    (8060, 8062, "Healthcare",       "Hospitals"),
    (8071, 8071, "Healthcare",       "Medical Labs"),
    (8082, 8082, "Healthcare",       "Home Health Care"),
    (8200, 8299, "Consumer Disc.",   "Educational Services"),
    (8300, 8399, "Healthcare",       "Social Services"),
    (8700, 8799, "Industrials",      "Engineering / Accounting / R&D"),
    (8731, 8731, "Healthcare",       "Commercial Physical & Biological R&D"),
    (8742, 8742, "Industrials",      "Management Consulting"),
    (9999, 9999, "Other",            "Non-classifiable"),
]

# 더 구체적 (좁은 범위) 부터 매칭 — Python sorted by range width ascending
_SORTED_RANGES = sorted(SIC_RANGES, key=lambda r: r[1] - r[0])


def sic_to_sector(sic_code) -> tuple[str | None, str | None]:
    """SIC 4자리 코드 → (sector, sector_full).

    매칭 안 되면 (None, None).
    """
    try:
        sic = int(sic_code)
    except (TypeError, ValueError):
        return None, None
    if not (100 <= sic <= 9999):
        return None, None
    # 좁은 범위 우선 매칭
    for start, end, sector, full in _SORTED_RANGES:
        if start <= sic <= end:
            return sector, full
    # 천의 자리 fallback
    base = (sic // 100) * 100
    for start, end, sector, full in _SORTED_RANGES:
        if start <= base <= end:
            return sector, full
    return None, None


if __name__ == "__main__":
    samples = [
        (2834, "Pharmaceutical Preparations"),
        (3674, "Semiconductors"),
        (3845, "Electromedical Apparatus"),
        (6770, "Blank Checks"),
        (7372, "Prepackaged Software"),
        (5961, "Catalog Mail-Order Retail"),
        (6021, "National Commercial Banks"),
        (3711, "Motor Vehicles"),
        (8731, "Commercial Physical & Biological R&D"),
        (4813, "Telephone Communications"),
        (1311, "Crude Petroleum & Natural Gas"),
    ]
    for sic, desc in samples:
        sector, full = sic_to_sector(sic)
        print(f"SIC {sic:4} ({desc}) → {sector} ({full})")
