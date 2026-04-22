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
POLY_SPORTS_FEE_COEFF = 0.03

# ── Tax rates (Utah, short-term capital gains assumption) ───────────────────────
FEDERAL_TAX_RATE = 0.24
STATE_TAX_RATE = 0.0455
COMBINED_TAX_RATE = FEDERAL_TAX_RATE + STATE_TAX_RATE   # 0.2855
AFTER_TAX_MULTIPLIER = 1.0 - COMBINED_TAX_RATE          # 0.7145

# ── Arb thresholds ──────────────────────────────────────────────────────────────
MIN_GROSS_SPREAD = 0.04        # 4% minimum gross spread to flag as opportunity
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
