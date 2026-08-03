"""
Auto-assigns Brand and Category to products based on product name analysis.

Built from direct analysis of all 494 products in Book1.xlsx —
rules are specific to Samanala Super Mart's actual product catalogue,
not generic keyword matching.

USAGE — called from ItemMasterUploadView after creating a new product:

    from inventory.services.auto_categorise import classify_product

    category_name, brand_name = classify_product(product_name)

    # Get or create Category and Brand objects
    from products.models import Category, Brand
  

    product.category = category
    product.brand    = brand
   

APPROACH:
    Two-pass rule engine:
    Pass 1 — Brand detection: match against known brand prefixes/keywords
              derived from the actual product list (494 products analysed).
    Pass 2 — Category detection: match against product name keywords.
    Both passes are case-insensitive and use substring matching, not exact match.

    Falls back to 'Unclassified' for brand and 'General' for category
    if no rules match — never crashes, always returns something.

WHY NOT FUZZY MATCHING (fuzzywuzzy):
    The chatbot uses fuzzywuzzy (80% threshold) because user input is
    unpredictable free text. Product names from the Item Master are
    structured and consistent (ALL CAPS, easyAcc format) — simple
    substring rules are more reliable and faster here.

WHY NOT AI/ML:
    Would require an external API call per product during upload,
    adding latency and a failure mode. Rule-based works offline,
    is deterministic (same input always gives same output), and
    can be audited easily. Upgrade to AI classification later if
    the catalogue grows beyond what rules can handle.
"""

from typing import Tuple


BRAND_RULES = [
    ('MD',              ['MD ']),
    ('Pears',           ['PEARS', 'PERAS BED TIME']),
    ('Ritzbury',        ['RITZBURY']),
    ('Sunquick',        ['SUNQUICK']),
    ('Lifebuoy',        ['LIFEBUOY', 'LIFEBOUY']),
    ('Signal',          ['SIGNAL']),
    ('Revello',         ['REVELLO']),
    ('Sunsilk',         ['SUNSILK']),
    ('KVC',             ['KVC ']),
    ('Sunlight',        ['SUNLIGHT']),
    ('Delish',          ['DELISH']),
    ('Maldini',         ['MALDINI']),
    ('Tiara',           ['TIARA']),
    ('Ayush',           ['AYUSH']),
    ('Lux',             ['LUX ']),
    ('Vaseline',        ['VASLINE', 'VASELINE']),
    ('Clear',           ['CLEAR ']),
    ('Dove',            ['DOVE ']),
    ('Ponds',           ['PONDS']),
    ('Kotagala',        ['KOTAGALA']),
    ('Scan',            ['SCAN ', 'SACN ']),
    ('Comfort',         ['COMFORT']),
    ('Vim',             ['VIM ']),
    ('Rexona',          ['REXONA']),
    ('Wonderlight',     ['WONDERLIGHT', 'WONDER LIGHT']),
    ('Closeup',         ['CLOSEUP']),
    ('Pringles',        ['PRINGLES']),
    ('Cadbury',         ['CADBURY']),
    ('Chunky',          ['CHUNKY CHOC']),
    ('Batook',          ['BATOOK']),
    ('Glow & Handsome', ['GLOW & HANDSOME', 'G&L', 'G& L']),
    ('Marmite',         ['MARMITE']),
    ('Knorr',           ['KNORR', 'KNOR ']),
    ('Surf Excel',      ['SURF EXCEL']),
    ('Rin',             ['RIN ']),
    ('N Joy',           ['N JOY', 'N- CEYLON']),
    ('Red Bull',        ['RED BULL']),
    ('Toblerone',       ['TOBLERONE']),
    ('Mars',            ['MARS ']),
    ('Viva',            ['VIVA ']),
    ('Samanala',        ['SAMANALA']),
    ('Ajinomoto',       ['AJINOMOTO']),
    ('Sunqick',         ['SUNQICK']),
]


CATEGORY_RULES = [
    ('Soaps',           ['PEARS ACTIVE FLORAL 350g', 'PEARS BED TIME 350g', 'PEARS P&G 350g ']),
    ('Hair Care',       ['SHAMPOO', 'CONDITIONER', ' COND ', 'COND 1', ' CON ',
                         'HAIR FALL', 'HAIR RESCUE', 'ANTI DANDRUFF', 'ANTI DRANDRUFF',
                         'THICK & LONG', 'THICK & STRONG', 'DAMAGE RESTORE', 'DAMAGE RESTRO',
                         'OXYGEN MOISTURE', 'OXIGEN MOISTURE', 'INTENCE REPAIR',
                         'INTENSE REPAIR', 'HERBAL RESCUE', 'SMOOTH & NOURISHING',
                         'LONG & HEALTHY GROWTH', 'S&S JAFFNA', 'SUNSILK JAFFNA',
                         'BLACK SHINE', 'CLEAR ICE COOL', 'CLEAR CSC', 'CLEAR MEN',
                         'LIFEBUOY AYUDA', 'LIFEBUOY THICK', 'LIFEBUOY STRONG & LONG',
                         'LIFEBUOY ANTI']),
    ('Oral Care',       ['TOOTHBRUSH', 'TOOTH BRUSH', 'TOOTHPASTE', 'TOOTH PASTE',
                         'SIGNAL HERBAL', 'SIGNAL ORANGE', 'SIGNAL STRONG', 'SIGNAL FIGHTER',
                         'SIGNAL DEEP', 'SIGNAL JUNIOR', 'SIGNAL TRIPLE', 'SIGNAL STRAWBERRY',
                         'CLOSEUP', 'AYUSH ANTI CAVITY', 'AYUSH WHITENING']),
    ('Skin Care',       ['FACE WASH', 'FACE CREAM', 'BODY LOTION', 'TALC', 'CREAM ',
                         'COLOGNE', 'MAGIC DROPS', 'BED TIME CREAM', 'BABY OIL', 'BABY CREAM',
                         'ALOE CREAM', 'PIMPLE', 'BRIGHT BEAUTY', 'PURE DETOX', 'BEAUTY SERUM',
                         'TONER', 'PONDS LIGHT', 'PONDS MAGIC', 'PONDS PURE',
                         'GLOW & HANDSOME', 'G&L', 'G& L', 'VENIVAL', 'VASLINE', 'VASELINE',
                         'AYUSH TERMARIC', 'AYUSH TURMERIC', 'PEARS BLUE', 'PEARS PINK',
                         'PEARS RED 100', 'PEARS GREEN', 'PEARS P&G 50ml',
                         'PEARS ACTIVE FLORAL 200', 'LIFEBUOY HANDWASH', 'LIFEBUOY KITCHEN',
                         'LIFEBOUY TURMERIC']),
    ('Deodorant',       ['DEODORANT', 'REXONA', 'POWDER DRY', 'ALOEVERA 25ml',
                         'ALOEVERA 50ml', 'SPORTS 50ml', 'SHOWER FRESH']),
    ('Soaps',           ['SOAP', 'LUX ', 'LIFEBUOY KHOMBA', 'LIFEBUOY VENIVAL',
                         'LIFEBUOY TOTAL', 'LIFEBUOY MILD', 'LIFEBUOY COOL', 'LIFEBUOY SAVE',
                         'LIFEBUOY TUMERIC', 'PERAS BED TIME SOAP', 'PEARS VENIVAL 90g',
                         'PEARS SOAP', 'PEARS ALOE & NEEM SOAP', 'PEARS P&G SOAP',
                         'PEARS FLORAL SOAP']),
    ('Dishwashing',     ['VIM ', 'DISHWASH']),
    ('Laundry',         ['SUNLIGHT CLEAN', 'SUNLIGHT ROYAL', 'SUNLIGHT YELLOW', 'SUNLIGHT YELOW',
                         'SUNLIGHT MATIC', 'SUNLIGHT NATURAL', 'SUNLIGHT LEMON',
                         'SUNLIGHT LAVENDER', 'SUNLIGHT LEMON & ROSE', 'SUNLIGHT CLEAN & SAKURA',
                         'SURF EXCEL', 'RIN ', 'COMFORT ', 'WONDER LIGHT', 'WONDERLIGHT',
                         'SAMANALA DETERGENT', 'DETERGENT']),
    ('Chocolate',       ['RITZBURY', 'REVELLO', 'CHUNKY CHOC', 'CHOCO ', 'CADBURY',
                         'TOBLERONE', 'MARS ', 'OAT CHOCO', 'CHOCO BEENS', 'CHOCO A NUT',
                         'CHOCO LA', 'CHOCO MO', 'LOLLY POP', 'BIG BOM', 'KINGO', 'CAFFETTO',
                         'HEART TOFEE', 'TAMARIND TOFEE', 'GOFRET', 'HONEY CUP', 'BATOOK',
                         'NIK NAK', 'CHIT CHAT', 'POPIT', 'VIP 25G', 'VIBER 25G',
                         'LIMITED EDITION']),
    ('Biscuits',        ['BISCUIT', 'WINE BISCUIT', 'JEM BISCUIT', 'CREAM CRACKER']),
    ('Snacks',          ['PRINGLES', 'CHIPS', 'SCAN ', 'SACN ', 'PEANUT', 'OLIVES', 'CASAVA']),
    ('Cakes & Bakery',  ['SWISS ROLL', 'SWISS ROALL', 'SPONGE', 'BUTTER SPONGE',
                         'BUTTER RAISIN', 'SPONGE LAYER']),
    ('Beverages',       ['SUNQUICK', 'RED BULL', 'JUICE', 'CORDIAL', 'NECTAR', 'DELITE',
                         'RTD ', 'FALUDA MIX', 'MIXED FRUIT CORDIAL', 'ORANGE CORDIAL',
                         'KOTAGALA TEA', 'KOTAGALA KAHATA', 'MD ALOE VERA NECTA',
                         'MD ALOE VERA NECTAR']),
    ('Jams & Spreads',  ['JAM', 'MARMALADE', 'PEANUT BUTTER', 'MARMITE', 'MD PINEAPPLE',
                         'ORANGE MARMALADE']),
    ('Jellies',         ['JELLY', 'JELATINE', 'GELATIN', 'FALUDA']),
    ('Condiments',      ['CHUTNEY', 'SAUCE', 'PICKLE', 'PASTE', 'TREACLE', 'MUSTARD',
                         'CHILLI PASTE', 'TOMATO SAUCE', 'KVC ']),
    ('Cooking Oils',    ['OIL', 'N JOY COCONUT', 'COCONUT OIL']),
    ('Baking',          ['BAKING POWDER', 'CORN FLOUR', 'CORN FLOWER', 'COCOA POWDER',
                         'CUSTARD POWDER', 'COCOVA POWDER', 'DELISH CORN', 'DELISH COCOVA']),
    ('Spices',          ['MALDINI CHILLI', 'MALDINI PEPPER', 'MALDINI TURMERIC',
                         'MALDINI ROASTED', 'GAMMIRIS', 'SUUDURU', 'MAHADURU', 'ULUHAAL',
                         'ABA ', 'KURUNDU', 'KAHA ', 'GORAKA', 'KARABU NATI', 'ENASAAL',
                         'N- CEYLON GINGER', 'GINGER POWDER', 'KOTHTHAMALLI']),
    ('Dry Groceries',   ['SUGAR', 'PARIPPU', 'WATANA', 'KADALA', 'SAW HAAL', 'RATA INDI',
                         'MUDDARAM', 'BATHALA PETHI', 'AJINOMOTO', 'KNORR', 'KNOR ',
                         'VIVA ', 'WHITE SUGAR']),
    ('Baby Products',   ['BABY OIL', 'BABY CREAM', 'BABY ', 'PEARS BABY', 'PEARS VENIVAL BABY',
                         'PEARS BED TIME', 'BED TIME 100ml', 'BED TIME 50ml']),
    ('Papadams',        ['PAPADAM', 'THEERU PAPADAM']),
    ('Shopping Bags',   ['SHOPPING BAG']),
]


def classify_product(product_name: str) -> Tuple[str, str]:
    upper = product_name.upper().strip()

    detected_brand = 'Unbranded'
    for brand_name, keywords in BRAND_RULES:
        if any(kw.upper() in upper for kw in keywords):
            detected_brand = brand_name
            break

    detected_category = 'General'
    for category_name, keywords in CATEGORY_RULES:
        if any(kw.upper() in upper for kw in keywords):
            detected_category = category_name
            break

    return detected_category, detected_brand


def classify_all_products(products_qs):
    from products.models import Category, Brand

    classified           = 0
    already_had_category = 0
    errors               = 0

    for product in products_qs:
        if product.category is not None and product.brand is not None:
            already_had_category += 1
            continue

        try:
            category_name, brand_name = classify_product(product.product_name)

            update_fields = []

            if product.category is None:
                category, _ = Category.objects.get_or_create(
                    category_name=category_name,
                    defaults={'default_zone': None}
                )
                product.category = category
                update_fields.append('category')

            if product.brand is None and brand_name != 'Unbranded':
                brand, _ = Brand.objects.get_or_create(
                    brand_name=brand_name,
                    defaults={'manufacturer': ''}
                )
                product.brand = brand
                update_fields.append('brand')

            if update_fields:
                product.save(update_fields=update_fields)
                classified += 1

        except Exception:
            errors += 1
            continue

    return {
        'classified':           classified,
        'already_had_category': already_had_category,
        'errors':               errors,
        'total_processed':      classified + already_had_category + errors,
    }