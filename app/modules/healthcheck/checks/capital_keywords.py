"""Capital-expenditure indicator lists for the Revenue vs Capital review
(``capital_item_review``) — from the SOP master list.

A P&L expense line whose description / reference / supplier name contains a
capital keyword (and no revenue-exclusion word) is a possible fixed asset that
should be reviewed for capitalisation instead of being expensed. Matching is
whole-word / phrase (case-insensitive) so "car" doesn't fire inside "cardboard";
the exclusion list suppresses obvious revenue spend (repairs, fuel, insurance).
Supplier names are a soft signal only (a name alone is an indicator, not proof).
"""
from __future__ import annotations

import re

# --- capital indicator words & phrases (SOP master list) --------------------
_CAPITAL_KEYWORDS: tuple[str, ...] = (
    # Core accounting & asset recognition
    "asset", "fixed asset", "capital asset", "capital item", "capital purchase",
    "capital acquisition", "asset acquisition", "asset purchase", "asset addition",
    "asset register", "asset capitalisation", "capitalised cost",
    "capitalised expenditure", "non-current asset", "long-term asset",
    "durable asset", "tangible asset", "intangible asset", "plant",
    "plant and machinery", "machinery", "equipment", "operational equipment",
    "production equipment", "business equipment", "commercial equipment",
    "industrial equipment", "installation", "commissioning", "setup cost",
    "initial setup", "first-time installation", "replacement unit", "upgrade unit",
    "system upgrade", "hardware upgrade", "equipment upgrade", "asset enhancement",
    "asset improvement", "capital improvement", "capacity expansion",
    "new installation", "new equipment", "new machinery", "asset build",
    "asset construction", "asset creation",
    # Property, land & buildings
    "land purchase", "freehold property", "leasehold property",
    "commercial property", "industrial property", "warehouse property",
    "office building", "office premises", "factory building", "shop premises",
    "retail unit", "industrial unit", "business premises", "property acquisition",
    "building acquisition", "property purchase", "building purchase",
    "construction cost", "building construction", "new build", "extension works",
    "structural works", "structural alteration", "structural modification",
    "building works", "major works", "capital works", "refurbishment",
    "renovation", "modernisation", "reconfiguration", "redevelopment", "fit-out",
    "office fit-out", "shop fit-out", "warehouse fit-out", "factory fit-out",
    "leasehold improvements", "tenant improvements", "landscaping works",
    "permanent fixtures", "permanent installation", "roof replacement",
    "roofing works", "flooring installation", "concrete works", "steel works",
    "brickwork", "structural steel", "load-bearing works", "partition installation",
    "false ceiling installation", "suspended ceiling", "fire-rated doors",
    "fire protection installation",
    # Electrical, mechanical & infrastructure
    "electrical installation", "rewiring", "electrical upgrade", "power upgrade",
    "distribution board", "consumer unit", "electrical panel", "switchgear",
    "transformer", "generator", "backup generator", "ups system",
    "power backup system", "mechanical installation", "plumbing installation",
    "pipework installation", "water system installation", "drainage installation",
    "gas installation", "boiler installation", "heating system",
    "central heating system", "hvac system", "air conditioning system",
    "air handling unit", "ventilation system", "extractor system",
    "extraction unit", "extractor fan", "ducting installation",
    "refrigeration system", "cold storage system", "walk-in freezer",
    "walk-in fridge", "fire alarm system", "sprinkler system",
    "fire suppression system", "security system", "alarm system",
    "cctv installation", "cctv", "access control system", "biometric system",
    "turnstile system", "gate automation", "roller shutter installation",
    "air conditioner",
    # Plant, machinery & industrial equipment
    "production line", "manufacturing line", "assembly line", "processing line",
    "industrial oven", "commercial oven", "kiln", "furnace", "compressor",
    "air compressor", "vacuum pump", "hydraulic system", "pneumatic system",
    "lathe machine", "milling machine", "drilling machine", "cutting machine",
    "laser cutter", "cnc machine", "3d printer", "packaging machine",
    "labelling machine", "weighing machine", "conveyor belt",
    "material handling system", "forklift", "pallet truck", "reach truck",
    "stacker", "crane", "hoist", "winch", "loading dock equipment",
    "dock leveller", "racking system", "storage racking", "industrial shelving",
    "heavy duty shelving", "warehouse automation", "robotic arm",
    "automation equipment", "industrial robot", "process control system",
    # IT hardware, technology & digital assets
    "computer", "desktop computer", "desktop", "laptop", "notebook", "workstation",
    "server", "rack server", "blade server", "cloud server hardware",
    "storage server", "network storage", "nas device", "san storage",
    "data centre equipment", "it hardware", "computer hardware", "hardware",
    "network equipment", "network hardware", "router", "firewall appliance",
    "load balancer", "wireless access point", "structured cabling", "cat6 cabling",
    "fibre installation", "patch panel", "server rack", "rack cabinet",
    "it infrastructure", "epos system", "pos system", "till system",
    "payment terminal", "card machine", "biometric reader",
    "time attendance system", "access card system", "printer", "photocopier",
    "scanner", "multifunction printer", "large format printer", "plotter",
    "barcode scanner", "label printer", "monitor", "display screen",
    "digital signage", "video conferencing equipment", "projector",
    "interactive display", "smart board",
    # Office furniture, fixtures & fittings
    "office furniture", "furniture", "fittings", "desk system", "office desk",
    "bench desk", "bench seating", "ergonomic chair", "task chair",
    "executive chair", "conference table", "meeting room table", "boardroom table",
    "filing cabinet", "pedestal drawer", "storage cabinet", "cupboard unit",
    "shelving unit", "bookcase", "mobile storage", "locker system",
    "reception desk", "reception counter", "waiting area seating", "lounge seating",
    "partition system", "screen divider", "acoustic panels", "soundproof panels",
    "lighting installation", "led lighting system", "emergency lighting",
    "fixed lighting", "track lighting", "display shelving", "showroom fittings",
    "retail display units",
    # Vehicles, transport & mobility
    "motor vehicle", "motor", "vehicle", "company car", "car", "commercial vehicle",
    "delivery van", "panel van", "box van", "refrigerated van", "pickup truck",
    "lorry", "truck", "hgv", "fleet vehicle", "vehicle acquisition",
    "vehicle purchase", "fleet purchase", "company van", "van", "electric vehicle",
    "hybrid vehicle", "forklift truck", "warehouse vehicle", "industrial vehicle",
    "construction vehicle", "plant vehicle", "vehicle conversion",
    "vehicle body build", "vehicle fit-out", "vehicle racking",
    "vehicle refrigeration unit", "trailer", "semi-trailer", "flatbed trailer",
    "tow equipment",
    # Kitchen, catering & hospitality
    "commercial kitchen equipment", "catering equipment", "industrial kitchen",
    "commercial fridge", "commercial freezer", "blast freezer", "upright freezer",
    "chest freezer", "refrigeration unit", "display fridge", "wine cooler",
    "ice machine", "ice maker", "range cooker", "commercial hob", "deep fat fryer",
    "combi oven", "food processor", "planetary mixer", "dough mixer",
    "espresso machine", "bar equipment", "beer tap system", "cellar cooling system",
    "extract canopy", "kitchen extraction system",
    # Medical, dental, fitness & specialist
    "medical equipment", "clinical equipment", "diagnostic equipment",
    "testing equipment", "laboratory equipment", "lab installation", "x-ray machine",
    "ultrasound machine", "mri equipment", "dental chair", "dental unit",
    "dental compressor", "sterilisation unit", "autoclave", "physio equipment",
    "rehabilitation equipment", "gym equipment", "fitness equipment", "treadmill",
    "cross trainer", "exercise bike", "weight machines", "therapy equipment",
    # Software & intangibles (capitalisable)
    "software licence", "perpetual", "enterprise software", "erp system",
    "crm system", "custom software development", "bespoke software",
    "software implementation", "software configuration", "system implementation",
    "platform development", "application development", "e-commerce platform build",
    "database development", "software upgrade", "software enhancement",
    "licence acquisition", "intellectual property", "ip acquisition",
    "patent purchase", "trademark acquisition",
    # Behavioural & contextual phrases
    "purchase of", "supply and install", "supply & fit", "installation charges",
    "new unit", "new system", "initial purchase", "first installation",
    "replacement of old", "upgrade of existing", "fit-out works", "equipment supply",
    "system installation", "hardware supply", "machine supply",
    "delivery and installation", "commissioning charges",
)

# Revenue spend that must NOT be flagged even above the threshold.
_EXCLUSION_KEYWORDS: tuple[str, ...] = (
    "repair", "servicing", "service", "maintenance", "amc", "annual maintenance",
    "fuel", "insurance", "spare part", "spares", "consumable",
)

# Known capital suppliers — a soft signal only (name alone is an indicator).
_CAPITAL_SUPPLIERS: tuple[str, ...] = (
    "currys", "ikea", "john lewis", "screwfix", "toolstation", "wickes", "b&q",
    "rs components", "costco", "dell", "hp", "apple", "lenovo", "canon", "ricoh",
    "xerox", "cisco", "siemens", "bosch", "jcb", "ford", "mercedes", "volkswagen",
    "toyota",
)


def _compile(words: tuple[str, ...]) -> re.Pattern[str]:
    """Whole-word / phrase match, plural tolerated ("laptops" hits "laptop"). The
    word boundary is what stops "car" firing inside "cardboard"; the optional "s"
    is what keeps a real "3 laptops" line from being missed."""
    ordered = sorted(set(words), key=len, reverse=True)  # longest phrase wins
    return re.compile(
        r"\b(" + "|".join(re.escape(w) for w in ordered) + r")s?\b", re.IGNORECASE)


_CAPITAL_RE = _compile(_CAPITAL_KEYWORDS)
_EXCLUSION_RE = _compile(_EXCLUSION_KEYWORDS)
_SUPPLIER_RE = _compile(_CAPITAL_SUPPLIERS)


def matched_capital_keyword(text: str) -> str | None:
    m = _CAPITAL_RE.search(text or "")
    return m.group(1).lower() if m else None


def has_revenue_exclusion(text: str) -> bool:
    return bool(_EXCLUSION_RE.search(text or ""))


def matched_capital_supplier(name: str) -> str | None:
    m = _SUPPLIER_RE.search(name or "")
    return m.group(1).lower() if m else None
