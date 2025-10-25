"""
Constants for the energy pricing application.
Includes currency symbols and minor unit mappings.
"""

# localization maps for currencies

CURRENCY_SYMBOL_MAP = {
    "EUR": "€",
    "DKK": "kr",
    "NOK": "kr",
    "SEK": "kr",
    "USD": "$",
    "GBP": "£",
    "CHF": "CHF",
    "CZK": "Kč",
}

CURRENCY_MINOR_UNIT_MAP = {
    "EUR": "ct",
    "DKK": "øre",
    "NOK": "øre",
    "SEK": "öre",
    "USD": "¢",
    "GBP": "p",
    "CHF": "Rp.",
    "CZK": "haléř",
}
