from datetime import timedelta

# ── Kalshi API ─────────────────────────────────────────────────────────────────
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_MLB_SERIES = "KXMLBGAME"
KALSHI_NBA_SERIES = "KXNBAGAME"

# ── Poll intervals ──────────────────────────────────────────────────────────────
DISCOVERY_INTERVAL = timedelta(minutes=60)   # re-discover new markets
PRICE_POLL_INTERVAL = timedelta(seconds=10)  # refresh prices

# ── Fee formulas ────────────────────────────────────────────────────────────────
# Kalshi taker: 0.07 * P * (1 - P)  per contract
KALSHI_FEE_COEFF = 0.07

# Polymarket sports taker: 0.03 * P^2 * (1 - P)  per share
# NOTE: Polymarket CLOB v2 launched 2026-04-28. Fee formula changed to
# C × feeRate × p × (1-p) with dynamic C and feeRate per market via getClobMarketInfo().
# This formula differs in exponent (p not p²) and may have a different rate.
# Re-verify by calling GET /clob.polymarket.com/markets/<market_id> on a live sports
# market post-migration and update this constant + arb_detector.poly_fee() accordingly.
POLY_SPORTS_FEE_COEFF = 0.03

# ── Tax rates (Utah, short-term capital gains assumption) ───────────────────────
FEDERAL_TAX_RATE = 0.24
STATE_TAX_RATE = 0.0455
COMBINED_TAX_RATE = FEDERAL_TAX_RATE + STATE_TAX_RATE   # 0.2855
AFTER_TAX_MULTIPLIER = 1.0 - COMBINED_TAX_RATE          # 0.7145

# ── Arb thresholds ──────────────────────────────────────────────────────────────
MIN_GROSS_SPREAD = 0.03        # 3% minimum gross spread to flag as opportunity
LOG_GROSS_THRESHOLD = 0.02     # 2% minimum gross spread to write to spread_log.csv

# ── Team name normalization ─────────────────────────────────────────────────────
# Maps Kalshi yes_sub_title (city/city+suffix format) → canonical team name.
# Kalshi disambiguates same-city teams with a letter suffix:
#   "Los Angeles D" = Dodgers, "Los Angeles A" = Angels
#   "New York Y" = Yankees,    "New York M" = Mets
#   "Chicago C" = Cubs,        "Chicago WS" = White Sox
KALSHI_TO_CANONICAL = {
    "Los Angeles D": "Dodgers",
    "Los Angeles A": "Angels",
    "New York Y":    "Yankees",
    "New York M":    "Mets",
    "Chicago C":     "Cubs",
    "Chicago WS":    "White Sox",
    "San Francisco": "Giants",
    "Arizona":       "Diamondbacks",
    "Atlanta":       "Braves",
    "Baltimore":     "Orioles",
    "Boston":        "Red Sox",
    "Cincinnati":    "Reds",
    "Cleveland":     "Guardians",
    "Colorado":      "Rockies",
    "Detroit":       "Tigers",
    "Houston":       "Astros",
    "Kansas City":   "Royals",
    "Miami":         "Marlins",
    "Milwaukee":     "Brewers",
    "Minnesota":     "Twins",
    "A's":           "Athletics",
    "Oakland":       "Athletics",
    "Sacramento":    "Athletics",
    "Philadelphia":  "Phillies",
    "Pittsburgh":    "Pirates",
    "San Diego":     "Padres",
    "Seattle":       "Mariners",
    "St. Louis":     "Cardinals",
    "Tampa Bay":     "Rays",
    "Texas":         "Rangers",
    "Toronto":       "Blue Jays",
    "Washington":    "Nationals",
    # Ticker abbreviations (used in event ticker suffix, e.g. KXMLBGAME-...-NYYBOS)
    "ATH":  "Athletics",
    "ATL":  "Braves",
    "AZ":   "Diamondbacks",
    "BAL":  "Orioles",
    "BOS":  "Red Sox",
    "CHC":  "Cubs",
    "CIN":  "Reds",
    "CLE":  "Guardians",
    "COL":  "Rockies",
    "CWS":  "White Sox",
    "DET":  "Tigers",
    "HOU":  "Astros",
    "KC":   "Royals",
    "LAA":  "Angels",
    "LAD":  "Dodgers",
    "MIA":  "Marlins",
    "MIL":  "Brewers",
    "MIN":  "Twins",
    "NYM":  "Mets",
    "NYY":  "Yankees",
    "PHI":  "Phillies",
    "PIT":  "Pirates",
    "SD":   "Padres",
    "SEA":  "Mariners",
    "SF":   "Giants",
    "STL":  "Cardinals",
    "TB":   "Rays",
    "TEX":  "Rangers",
    "TOR":  "Blue Jays",
    "WSH":  "Nationals",
}

# Maps Polymarket outcome label → canonical team name.
# Polymarket uses full team names in outcome labels (e.g. "New York Yankees").
# Only exceptions to the canonical name need to be listed here.
POLYMARKET_TO_CANONICAL = {
    "New York Yankees":       "Yankees",
    "Boston Red Sox":         "Red Sox",
    "Los Angeles Dodgers":    "Dodgers",
    "Los Angeles Angels":     "Angels",
    "San Francisco Giants":   "Giants",
    "New York Mets":          "Mets",
    "Chicago White Sox":      "White Sox",
    "Chicago Cubs":           "Cubs",
    "Arizona Diamondbacks":   "Diamondbacks",
    "Atlanta Braves":         "Braves",
    "Baltimore Orioles":      "Orioles",
    "Cincinnati Reds":        "Reds",
    "Cleveland Guardians":    "Guardians",
    "Colorado Rockies":       "Rockies",
    "Detroit Tigers":         "Tigers",
    "Houston Astros":         "Astros",
    "Kansas City Royals":     "Royals",
    "Miami Marlins":          "Marlins",
    "Milwaukee Brewers":      "Brewers",
    "Minnesota Twins":        "Twins",
    "Athletics":              "Athletics",
    "Philadelphia Phillies":  "Phillies",
    "Pittsburgh Pirates":     "Pirates",
    "San Diego Padres":       "Padres",
    "Seattle Mariners":       "Mariners",
    "St. Louis Cardinals":    "Cardinals",
    "Tampa Bay Rays":         "Rays",
    "Texas Rangers":          "Rangers",
    "Toronto Blue Jays":      "Blue Jays",
    "Washington Nationals":   "Nationals",
}

# Maps canonical team name → Polymarket slug abbreviation.
# Most match Kalshi abbreviations (lowercased); exceptions listed explicitly.
CANONICAL_TO_POLY_ABBR = {
    "Yankees":       "nyy",
    "Red Sox":       "bos",
    "Dodgers":       "lad",
    "Angels":        "laa",
    "Giants":        "sf",
    "Mets":          "nym",
    "White Sox":     "cws",
    "Cubs":          "chc",
    "Diamondbacks":  "ari",   # Kalshi uses AZ, Polymarket uses ari
    "Braves":        "atl",
    "Orioles":       "bal",
    "Reds":          "cin",
    "Guardians":     "cle",
    "Rockies":       "col",
    "Tigers":        "det",
    "Astros":        "hou",
    "Royals":        "kc",
    "Marlins":       "mia",
    "Brewers":       "mil",
    "Twins":         "min",
    "Athletics":     "oak",   # Kalshi uses ATH, Polymarket uses oak
    "Phillies":      "phi",
    "Pirates":       "pit",
    "Padres":        "sd",
    "Mariners":      "sea",
    "Cardinals":     "stl",
    "Rays":          "tb",
    "Rangers":       "tex",
    "Blue Jays":     "tor",
    "Nationals":     "wsh",
}

# All known Kalshi team abbreviations — used to split concatenated away+home
# from event ticker suffixes (e.g. "NYYBOS" → "NYY" + "BOS").
# Sorted longest-first so greedy matching works correctly.
KALSHI_ABBR_SET = sorted([
    "CWS", "ATH", "CIN", "NYY", "BOS", "LAD", "ATL", "WSH", "MIL", "DET",
    "PIT", "TEX", "MIN", "NYM", "PHI", "CHC", "TOR", "LAA", "BAL", "HOU",
    "CLE", "STL", "MIA", "SEA", "COL", "NYM", "SD", "TB", "SF", "AZ", "KC",
], key=len, reverse=True)

# ── NBA team name normalization ─────────────────────────────────────────────────
# Separate from MLB — many abbreviations overlap (BOS, DET, CLE, PHI, etc.)

# Maps Kalshi yes_sub_title → canonical NBA team name.
# Kalshi disambiguates same-city teams:
#   "Los Angeles L" = Lakers, "Los Angeles C" = Clippers
NBA_KALSHI_TO_CANONICAL = {
    # City names (yes_sub_title format)
    "Atlanta":        "Hawks",
    "Boston":         "Celtics",
    "Brooklyn":       "Nets",
    "Charlotte":      "Hornets",
    "Chicago":        "Bulls",
    "Cleveland":      "Cavaliers",
    "Dallas":         "Mavericks",
    "Denver":         "Nuggets",
    "Detroit":        "Pistons",
    "Golden State":   "Warriors",
    "Houston":        "Rockets",
    "Indiana":        "Pacers",
    "Los Angeles C":  "Clippers",
    "Los Angeles L":  "Lakers",
    "Memphis":        "Grizzlies",
    "Miami":          "Heat",
    "Milwaukee":      "Bucks",
    "Minnesota":      "Timberwolves",
    "New Orleans":    "Pelicans",
    "New York":       "Knicks",
    "Oklahoma City":  "Thunder",
    "Orlando":        "Magic",
    "Philadelphia":   "76ers",
    "Phoenix":        "Suns",
    "Portland":       "Trail Blazers",
    "Sacramento":     "Kings",
    "San Antonio":    "Spurs",
    "Toronto":        "Raptors",
    "Utah":           "Jazz",
    "Washington":     "Wizards",
    # Ticker abbreviations
    "ATL":  "Hawks",
    "BOS":  "Celtics",
    "BKN":  "Nets",
    "CHA":  "Hornets",
    "CHI":  "Bulls",
    "CLE":  "Cavaliers",
    "DAL":  "Mavericks",
    "DEN":  "Nuggets",
    "DET":  "Pistons",
    "GSW":  "Warriors",
    "HOU":  "Rockets",
    "IND":  "Pacers",
    "LAC":  "Clippers",
    "LAL":  "Lakers",
    "MEM":  "Grizzlies",
    "MIA":  "Heat",
    "MIL":  "Bucks",
    "MIN":  "Timberwolves",
    "NOP":  "Pelicans",
    "NYK":  "Knicks",
    "OKC":  "Thunder",
    "ORL":  "Magic",
    "PHI":  "76ers",
    "PHX":  "Suns",
    "POR":  "Trail Blazers",
    "SAC":  "Kings",
    "SAS":  "Spurs",
    "TOR":  "Raptors",
    "UTA":  "Jazz",
    "WAS":  "Wizards",
}

# Maps Polymarket outcome label → canonical NBA team name.
# Polymarket uses short team nicknames for NBA (e.g. "Timberwolves", not full names).
NBA_POLYMARKET_TO_CANONICAL = {
    "Hawks":          "Hawks",
    "Celtics":        "Celtics",
    "Nets":           "Nets",
    "Hornets":        "Hornets",
    "Bulls":          "Bulls",
    "Cavaliers":      "Cavaliers",
    "Mavericks":      "Mavericks",
    "Nuggets":        "Nuggets",
    "Pistons":        "Pistons",
    "Warriors":       "Warriors",
    "Rockets":        "Rockets",
    "Pacers":         "Pacers",
    "Clippers":       "Clippers",
    "Lakers":         "Lakers",
    "Grizzlies":      "Grizzlies",
    "Heat":           "Heat",
    "Bucks":          "Bucks",
    "Timberwolves":   "Timberwolves",
    "Pelicans":       "Pelicans",
    "Knicks":         "Knicks",
    "Thunder":        "Thunder",
    "Magic":          "Magic",
    "76ers":          "76ers",
    "Suns":           "Suns",
    "Trail Blazers":  "Trail Blazers",
    "Kings":          "Kings",
    "Spurs":          "Spurs",
    "Raptors":        "Raptors",
    "Jazz":           "Jazz",
    "Wizards":        "Wizards",
}

# Maps canonical NBA team → Polymarket slug abbreviation (lowercase).
NBA_CANONICAL_TO_POLY_ABBR = {
    "Hawks":          "atl",
    "Celtics":        "bos",
    "Nets":           "bkn",
    "Hornets":        "cha",
    "Bulls":          "chi",
    "Cavaliers":      "cle",
    "Mavericks":      "dal",
    "Nuggets":        "den",
    "Pistons":        "det",
    "Warriors":       "gsw",
    "Rockets":        "hou",
    "Pacers":         "ind",
    "Clippers":       "lac",
    "Lakers":         "lal",
    "Grizzlies":      "mem",
    "Heat":           "mia",
    "Bucks":          "mil",
    "Timberwolves":   "min",
    "Pelicans":       "nop",
    "Knicks":         "nyk",
    "Thunder":        "okc",
    "Magic":          "orl",
    "76ers":          "phi",
    "Suns":           "phx",
    "Trail Blazers":  "por",
    "Kings":          "sac",
    "Spurs":          "sas",
    "Raptors":        "tor",
    "Jazz":           "uta",
    "Wizards":        "was",
}

# NBA Kalshi abbreviations sorted longest-first for greedy split.
NBA_KALSHI_ABBR_SET = sorted([
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
], key=len, reverse=True)

# ── NHL team name normalization ─────────────────────────────────────────────────
KALSHI_NHL_SERIES = "KXNHLGAME"

# Maps Kalshi yes_sub_title (label format: "{ABBR} {Nickname}") → canonical NHL team name.
# Utah Hockey Club uses "UTA Mammoth" label but canonical = "Utah" (Polymarket outcome label).
NHL_KALSHI_TO_CANONICAL = {
    # Label format (yes_sub_title)
    "ANA Ducks":          "Ducks",
    "BOS Bruins":         "Bruins",
    "BUF Sabres":         "Sabres",
    "CAR Hurricanes":     "Hurricanes",
    "CGY Flames":         "Flames",
    "CBJ Blue Jackets":   "Blue Jackets",
    "COL Avalanche":      "Avalanche",
    "DAL Stars":          "Stars",
    "DET Red Wings":      "Red Wings",
    "EDM Oilers":         "Oilers",
    "FLA Panthers":       "Panthers",
    "LA Kings":           "Kings",
    "MIN Wild":           "Wild",
    "MTL Canadiens":      "Canadiens",
    "NJD Devils":         "Devils",
    "NSH Predators":      "Predators",
    "NYI Islanders":      "Islanders",
    "NYR Rangers":        "Rangers",
    "OTT Senators":       "Senators",
    "PHI Flyers":         "Flyers",
    "PIT Penguins":       "Penguins",
    "SEA Kraken":         "Kraken",
    "SJ Sharks":          "Sharks",
    "STL Blues":          "Blues",
    "TB Lightning":       "Lightning",
    "TOR Maple Leafs":    "Maple Leafs",
    "UTA Mammoth":        "Utah",
    "VAN Canucks":        "Canucks",
    "VGK Golden Knights": "Golden Knights",
    "WPG Jets":           "Jets",
    "WSH Capitals":       "Capitals",
    # Ticker abbreviations (used in event ticker suffix, e.g. KXNHLGAME-...-EDMDAL)
    "ANA": "Ducks",
    "BOS": "Bruins",
    "BUF": "Sabres",
    "CAR": "Hurricanes",
    "CGY": "Flames",
    "CBJ": "Blue Jackets",
    "COL": "Avalanche",
    "DAL": "Stars",
    "DET": "Red Wings",
    "EDM": "Oilers",
    "FLA": "Panthers",
    "LA":  "Kings",
    "MIN": "Wild",
    "MTL": "Canadiens",
    "NJD": "Devils",
    "NSH": "Predators",
    "NYI": "Islanders",
    "NYR": "Rangers",
    "OTT": "Senators",
    "PHI": "Flyers",
    "PIT": "Penguins",
    "SEA": "Kraken",
    "SJ":  "Sharks",
    "STL": "Blues",
    "TB":  "Lightning",
    "TOR": "Maple Leafs",
    "UTA": "Utah",
    "VAN": "Canucks",
    "VGK": "Golden Knights",
    "WPG": "Jets",
    "WSH": "Capitals",
}

# Maps Polymarket outcome label → canonical NHL team name.
# Polymarket uses short team nicknames for NHL (e.g. "Oilers", "Golden Knights").
# Utah Hockey Club appears as "Utah" on Polymarket.
NHL_POLYMARKET_TO_CANONICAL = {
    "Ducks":          "Ducks",
    "Bruins":         "Bruins",
    "Sabres":         "Sabres",
    "Hurricanes":     "Hurricanes",
    "Flames":         "Flames",
    "Blue Jackets":   "Blue Jackets",
    "Avalanche":      "Avalanche",
    "Stars":          "Stars",
    "Red Wings":      "Red Wings",
    "Oilers":         "Oilers",
    "Panthers":       "Panthers",
    "Kings":          "Kings",
    "Wild":           "Wild",
    "Canadiens":      "Canadiens",
    "Devils":         "Devils",
    "Predators":      "Predators",
    "Islanders":      "Islanders",
    "Rangers":        "Rangers",
    "Senators":       "Senators",
    "Flyers":         "Flyers",
    "Penguins":       "Penguins",
    "Kraken":         "Kraken",
    "Sharks":         "Sharks",
    "Blues":          "Blues",
    "Lightning":      "Lightning",
    "Maple Leafs":    "Maple Leafs",
    "Utah":           "Utah",
    "Canucks":        "Canucks",
    "Golden Knights": "Golden Knights",
    "Jets":           "Jets",
    "Capitals":       "Capitals",
    "Mammoth":        "Utah",   # fallback if Polymarket uses full nickname
}

# Maps canonical NHL team → Polymarket slug abbreviation (lowercase).
# Confirmed from live Polymarket API: lak (not la), mon (not mtl), las (not vgk), utah (not uta).
NHL_CANONICAL_TO_POLY_ABBR = {
    "Ducks":          "ana",
    "Bruins":         "bos",
    "Sabres":         "buf",
    "Hurricanes":     "car",
    "Flames":         "cal",
    "Blue Jackets":   "cbj",
    "Avalanche":      "col",
    "Stars":          "dal",
    "Red Wings":      "det",
    "Oilers":         "edm",
    "Panthers":       "fla",
    "Kings":          "lak",
    "Wild":           "min",
    "Canadiens":      "mon",
    "Devils":         "njd",
    "Predators":      "nsh",
    "Islanders":      "nyi",
    "Rangers":        "nyr",
    "Senators":       "ott",
    "Flyers":         "phi",
    "Penguins":       "pit",
    "Kraken":         "sea",
    "Sharks":         "sj",
    "Blues":          "stl",
    "Lightning":      "tb",
    "Maple Leafs":    "tor",
    "Utah":           "utah",
    "Canucks":        "van",
    "Golden Knights": "las",
    "Jets":           "wpg",
    "Capitals":       "wsh",
}

# NHL Kalshi abbreviations sorted longest-first for greedy split.
# 3-letter abbrevs before 2-letter (LA, SJ, TB) to prevent false prefix matches.
NHL_KALSHI_ABBR_SET = sorted([
    "ANA", "BOS", "BUF", "CAR", "CGY", "CBJ", "COL", "DAL", "DET", "EDM",
    "FLA", "MIN", "MTL", "NJD", "NSH", "NYI", "NYR", "OTT", "PHI", "PIT",
    "SEA", "STL", "TOR", "UTA", "VAN", "VGK", "WPG", "WSH",
    "LA", "SJ", "TB",
], key=len, reverse=True)
