"""
themes.py — Curated trade-theme taxonomy for the UOA dashboard.

Polygon only exposes SIC (market structure). Unusual options flow, though,
clusters by THEME — photonics, uranium, GLP-1, AI data-center power — and
those cut across GICS sectors. This is the second layer: a hand-curated
ticker -> theme(s) map. A ticker can belong to several themes (TSLA: Autos
+ Robotics). Meant to be maintained by hand, not auto-derived.

Gate for adding a theme (per the dashboard owner): a real cluster of
liquid optionable names with a shared catalyst that move together, where
the theme explains the flow better than the GICS sector would.
"""

THEME_MAP = {
    "Memory": [
        "MU", "SNDK", "STX", "WDC"],
    "Photonics / Optical Networking": [
        "LITE", "CIEN", "COHR", "AXTI", "TSEM", "APH", "VICR", "LASR",
        "OPTX", "AAOI", "LPTH", "VIAV", "COMM", "VSAT", "AVGO", "MRVL"],
    "Aerospace & Defense": [
        "FTAI", "KRMN", "ATI", "GE", "HWM", "ATRO", "CRS", "MRCY",
        "LMT", "RTX", "NOC", "GD", "LHX"],
    "Drones": [
        "AVAV", "KTOS", "ONDS", "UMAC", "RCAT", "ZENA", "DPRO", "UAVS",
        "JOBY", "AIRO"],
    "Space": [
        "RKLB", "ASTS", "PL", "LUNR", "FLY", "AIRO", "DXYZ", "VELO",
        "BKSY", "SIDU", "SATS", "FEIM", "RDW"],
    "Robotics & Automation": [
        "TSLA", "TER", "SYM", "RR", "SERV", "ROK", "XPEV"],
    "Homebuilders": [
        "TOL", "BLDR", "DFH", "DHI", "GRBK", "HOV", "KBH", "LEN",
        "MHO", "MTH", "PHM", "TPH"],
    "Autos & Auto Tech": [
        "RIVN", "TM", "GM", "F", "DAN", "HSAI", "ALV", "AEVA", "OUST",
        "APTV", "TSLA"],
    "Uranium": [
        "CCJ", "UEC", "UUUU", "LEU", "NXE", "DNN"],
    "Small Modular Reactors": [
        "SMR", "OKLO", "NNE", "BWXT", "LTBR", "GEV", "FLR", "CEG"],
    "Alternative Energy & Power": [
        "BE", "FSLR", "NXT", "SEDG", "FLNC", "NVT", "SEI", "BW", "EOSE"],
    "Data-Center Power & Cooling": [
        "VRT", "VST", "CEG", "TLN", "GEV", "ETN", "NRG", "PWR", "CWEN", "POWL"],
    "Coal": [
        "BTU", "AMR", "HCC", "CEIX", "ARLP", "METC"],
    "Retail & Apparel": [
        "TPR", "VSCO", "URBN", "DBI", "ROST", "RVLV", "REAL", "ANF",
        "AEO", "DG", "DLTR", "RH", "W", "JMIA", "ULTA"],
    "LatAm": [
        "CIB", "NU", "MELI", "SE", "DLO", "STNE", "BAP", "BCH"],
    "China ADRs": [
        "BIDU", "BABA", "FUTU", "TIGR", "VNET", "GDS", "BILI", "PDD"],
    "Chemicals": [
        "SQM", "ALB", "HUN", "PRM", "MOS", "CF"],
    "Software": [
        "AMTM", "APP", "SOUN", "MDB", "ZETA", "U", "FROG", "PLTR", "CRWV",
        "BBAI", "RZLV", "DOCN", "SNOW", "TTAN", "COMP", "NBIS", "PATH", "PGY"],
    "Bitcoin Miners & AI Data Centers": [
        "APLD", "HUT", "CIFR", "IREN", "BITF", "GLXY", "CLSK", "WULF",
        "BKKT", "MARA", "RIOT"],
    "Mortgage & Lending": [
        "FIGR", "RKT", "TREE", "BETR", "FNMA", "LC", "AFRM", "OPEN"],
    "Quantum Computing": [
        "IONQ", "QBTS", "RGTI", "QUBT", "ARQQ"],
    "Cybersecurity": [
        "CRWD", "RBRK", "PANW", "ZS", "S", "FTNT", "NET", "OKTA",
        "TENB", "CYBR", "VRNS", "QLYS"],
    "Banks": [
        "GS", "MS", "WFC", "JPM", "C", "BAC", "USB", "PNC", "TFC"],
    "Semiconductors": [
        "LRCX", "INTC", "AMKR", "KLAC", "AMD", "NVDA", "TSM", "ASML",
        "AVGO", "ADI", "SKYT", "GFS", "AEHR", "PLAB", "ONTO", "MKSI",
        "AMAT", "ACMR", "ALMU", "NVTS", "MRVL", "QCOM", "MCHP"],
    "Contract Manufacturing / EMS": [
        "CLS", "TTMI", "FN", "JBL", "SANM", "FLEX", "GLW"],
    "Brokers & Exchanges": [
        "HOOD", "SCHW", "IBKR", "COIN", "CBOE", "ICE", "NDAQ", "MKTX"],
    "Machinery": [
        "CAT", "TEX", "FLS", "XMTR", "DE", "PCAR"],
    "Internet & Content": [
        "GOOGL", "RDDT", "META", "NFLX", "PINS", "SNAP", "SPOT"],
    "Medical & Biotech": [
        "LLY", "ALMS", "LQDA", "PGEN", "RVMD", "NVO", "TEVA", "TMDX",
        "GH", "GRAL", "NTRA", "ISRG"],
    "GLP-1 / Obesity": [
        "LLY", "NVO", "VKTX", "AMGN", "HIMS", "TERN"],
    "Rare Earth & Critical Minerals": [
        "CRML", "AREC", "UAMY", "TMC", "MP", "ABAT", "USAR"],
    "Metals & Miners": [
        "CENX", "AEM", "EGO", "FSM", "FNV", "GFI", "HMY", "KGC", "NEM",
        "WPM", "PAAS", "CDE", "SBSW", "HL", "AG", "PPTA", "RIO", "FCX",
        "AA", "SCCO", "TECK", "VALE"],
    "Oil & Gas": [
        "HAL", "CVX", "XOM", "SLB", "FTI", "WFRD", "FRO", "INSW"],
    "Airlines": [
        "DAL", "AAL", "LUV", "UAL", "ALK"],
    "AI Infrastructure": [
        "NVDA", "AVGO", "MRVL", "AMD", "TSM", "SMCI", "ANET", "DELL",
        "CRWV", "NBIS", "ALAB", "CRDO", "VRT", "MU", "AMAT"],
    "Semiconductor Equipment": [
        "LRCX", "AMAT", "KLAC", "ASML", "ONTO", "MKSI", "ACMR", "AEHR",
        "PLAB", "KLIC", "UCTT", "COHU", "AEIS", "ICHR", "NVMI", "CAMT"],
    "Power Semis": [
        "NVTS", "MCHP", "ADI", "ON", "WOLF", "POWI", "MPWR", "DIOD", "SLAB"],
    "Crypto Infrastructure": [
        "COIN", "MSTR", "HOOD", "GLXY", "MARA", "RIOT", "CLSK", "BITF",
        "IREN", "WULF", "CIFR", "HUT", "BMNR"],
    "Defense Tech": [
        "PLTR", "AVAV", "KTOS", "RKLB", "ONDS", "RCAT", "AXON", "BBAI",
        "MRCY", "DRS", "KRMN", "AIRO"],
}

# ── reverse index: ticker -> set(themes) ─────────────────────────────────────
_TICKER_THEMES = {}
for _theme, _tickers in THEME_MAP.items():
    for _t in _tickers:
        _TICKER_THEMES.setdefault(_t.upper(), set()).add(_theme)

ALL_THEMES = sorted(THEME_MAP.keys())


def themes_for(ticker):
    """Sorted list of themes a ticker belongs to ([] if none)."""
    return sorted(_TICKER_THEMES.get((ticker or "").upper(), ()))


if __name__ == "__main__":
    print(f"{len(THEME_MAP)} themes, "
          f"{len(_TICKER_THEMES)} unique tickers mapped")
    multi = {t: th for t, th in _TICKER_THEMES.items() if len(th) > 1}
    print(f"{len(multi)} tickers in multiple themes, e.g.:")
    for t in sorted(multi)[:8]:
        print(f"  {t}: {', '.join(sorted(multi[t]))}")
