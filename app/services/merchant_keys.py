"""Turns one statement row into the key category suggestions use (ab-253).

Pure functions, no I/O. Formats come from the real statements studied in
docs/research/kenya-merchants/parts/descriptor-formats.md.

A row gets one key:
  name       a cleaned-up business name (card payments, utilities, fees)
  paybill    a Paybill number seen on the row (M-Pesa shows it)
  till       a Buy Goods Till number
  reference  a keyed hash of something private: an NCBA account
             reference, or a person on the other side of a transfer.
             Only ever used for that user's own memory.
  None       nothing to learn from (Fuliza overdrafts, loans, own-account
             transfers) - no suggestion.

is_business decides whether a row may use and vote into the shared list.
When unsure (e.g. a Till registered under a personal name) it is False:
private history still works, sharing doesn't.

Raw account references, phone numbers and person names never leave this
module except as a keyed hash (hash_value). The display label for an NCBA
reference row shows the reference, but only to that user, from their own
transaction text.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass

MPESA = "M-Pesa"
NCBA = "NCBA Bank"

_BUSINESS_TOKENS = {
    "LTD", "LIMITED", "PLC", "CO", "COMPANY", "ENTERPRISE", "ENTERPRISES", "SUPERMARKET", "SUPERMARKETS",
    "STORE", "STORES", "SHOP", "SHOPS", "MART", "HOTEL", "RESTAURANT", "CAFE", "PHARMACY", "CHEMIST",
    "HOSPITAL", "CLINIC", "MEDICAL", "SCHOOL", "ACADEMY", "UNIVERSITY", "COLLEGE", "SACCO", "BANK",
    "INSURANCE", "ASSURANCE", "AGENCIES", "AGENCY", "SERVICES", "SOLUTIONS", "KENYA", "INTERNATIONAL",
    "GROUP", "HOLDINGS", "MOTORS", "HARDWARE", "BUTCHERY", "SALON", "SPA", "BARBER", "BARBERSHOP",
    "BEAUTY", "PARLOR", "PARLOUR", "CHURCH", "PARISH", "ARCHDIOCESE", "DIOCESE", "MINISTRY", "COUNTY",
    "AUTHORITY", "WATER", "POWER", "PREPAID", "POSTPAID", "BUNDLES", "AIRTIME", "SAFARICOM", "AIRTEL",
    "TELKOM", "FUND", "CAPITAL", "INVESTMENTS", "TRADERS", "TRADING", "DISTRIBUTORS", "WHOLESALE",
    "PETROL", "STATION", "SERVICENTRE", "ENERGY", "OIL", "GAS", "TRAVEL", "TOURS", "SHUTTLE", "SACCO.",
    "FOODS", "BAKERY", "LOUNGE", "BAR", "PUB", "CINEMA", "STUDIOS", "TECHNOLOGIES", "SYSTEMS", "NETWORKS",
    "CHARGE", "CHARGES", "PURCHASE", "DUTY", "FEE", "FEES",
}
_MONTHS = {
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER",
    "NOVEMBER", "DECEMBER", "JAN", "FEB", "MAR", "APR", "JUN", "JUL", "AUG", "SEP", "SEPT", "OCT", "NOV", "DEC",
}

_FULIZA = re.compile(r"\b(?:FULIZA M-?PESA|WITH FULIZA)\b")


@dataclass(frozen=True)
class RowKey:
    kind: str | None
    value: str | None
    is_business: bool = False
    provider: str | None = None  # set for reference keys
    account_hash: str | None = None  # M-Pesa "Acc." reference, hashed
    number_kind: str | None = None  # 'paybill' | 'till' when a real number is on the row
    number: str | None = None
    name: str | None = None  # cleaned business/biller name when known
    match_text: str = ""  # cleaned text the shared-list patterns are matched against
    display: str = ""  # short label for rows with no readable name
    is_transfer_hint: bool = False


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").upper()).strip()


def strip_reference_codes(text: str) -> str:
    """Drops every token containing a digit (reference codes, phone and
    meter numbers, card masks, amounts) - what's left is the words."""
    return " ".join(t for t in text.split() if not re.search(r"\d", t))


def hash_value(provider: str, value: str, *, secret: str) -> str:
    digest = hmac.new(secret.encode(), f"{provider}|{value}".encode(), hashlib.sha256).hexdigest()
    return "h:" + digest[:32]


def looks_like_business(name: str) -> bool:
    words = re.findall(r"[A-Z]+", normalize_text(name))
    return any(w in _BUSINESS_TOKENS for w in words)


def _clean_name(name: str) -> str:
    name = normalize_text(name).rstrip(" .")
    return re.sub(r"\s+", " ", name).strip()


def _words_only(text: str) -> str:
    words = [w for w in strip_reference_codes(text).split() if w not in _MONTHS]
    return " ".join(words).strip(" .-")


def _number_row(kind: str, number: str, name: str, *, account: str | None, provider: str, secret: str) -> RowKey:
    name = _clean_name(name)
    account_hash = hash_value(provider, normalize_text(account), secret=secret) if account else None
    return RowKey(
        kind=kind,
        value=number,
        is_business=looks_like_business(name),
        account_hash=account_hash,
        number_kind=kind,
        number=number,
        name=name or None,
        match_text=name,
        display=f"{'Paybill' if kind == 'paybill' else 'Till'} {number}",
    )


def _person_row(provider: str, identity: str, *, secret: str, match_text: str = "") -> RowKey:
    # match_text stays empty for person transfers, so a person's name can
    # never hit a short shared-list pattern.
    return RowKey(
        kind="reference",
        value=hash_value(provider, normalize_text(identity), secret=secret),
        provider=provider,
        display="Transfer",
        match_text=match_text,
    )


def _name_row(name: str, *, match_text: str | None = None) -> RowKey:
    name = _clean_name(name)
    if not name:
        return RowKey(kind=None, value=None)
    return RowKey(
        kind="name", value=name, is_business=looks_like_business(name), name=name, match_text=match_text or name
    )


# --- M-Pesa ---------------------------------------------------------------

_MPESA_PAYBILL = re.compile(r"^PAY BILL(?: ONLINE)? TO (\d{4,8}) - (.+?)(?:\.? ACC\.? ?(.*))?$")
_MPESA_TILL = re.compile(r"^MERCHANT PAYMENT TO (\d{4,8}) - (.+)$")
_MPESA_B2C = re.compile(r"^BUSINESS PAYMENT FROM (\d{4,8}) - (.+?) VIA API\b")
_MPESA_BUNDLE = re.compile(r"^(?:RECHARGE FOR CUSTOMER|CUSTOMER BUNDLE PURCHASE)(?: WITH)? TO (\d{4,8}) ?([A-Z].*?)(?: BY - .*)?$")
_MPESA_PERSON = re.compile(r"^(?:CUSTOMER TRANSFER(?! OF FUNDS CHARGE)|CUSTOMER SEND MONEY|FUNDS RECEIVED FROM|SEND MONEY)\b.*?(?:TO|FROM)? ?(\d{9,12})? ?-? ?(.*)$")
_MPESA_NO_KEY = re.compile(r"^(?:OVERDRAFT OF CREDIT PARTY|OD LOAN REPAYMENT|AGENT |CUSTOMER WITHDRAWAL|WITHDRAW)")


def _mpesa(text: str, secret: str) -> RowKey:
    cleaned = normalize_text(_FULIZA.sub("", text))
    if _MPESA_NO_KEY.match(cleaned):
        return RowKey(kind=None, value=None)
    if cleaned.startswith("AIRTIME PURCHASE"):
        return RowKey(kind="name", value="AIRTIME PURCHASE", is_business=True, name="AIRTIME PURCHASE", match_text="AIRTIME PURCHASE")
    if m := _MPESA_PAYBILL.match(cleaned):
        return _number_row("paybill", m.group(1), m.group(2), account=m.group(3), provider=MPESA, secret=secret)
    if m := _MPESA_TILL.match(cleaned):
        return _number_row("till", m.group(1), m.group(2), account=None, provider=MPESA, secret=secret)
    if m := _MPESA_B2C.match(cleaned):
        return _number_row("paybill", m.group(1), m.group(2), account=None, provider=MPESA, secret=secret)
    if m := _MPESA_BUNDLE.match(cleaned):
        return _number_row("paybill", m.group(1), m.group(2), account=None, provider=MPESA, secret=secret)
    if cleaned.endswith("CHARGE"):
        return _name_row(cleaned)
    if m := _MPESA_PERSON.match(cleaned):
        identity = m.group(1) or m.group(2) or cleaned
        return _person_row(MPESA, identity, secret=secret)
    return _fallback(cleaned)


# --- NCBA -----------------------------------------------------------------

_NCBA_IB_OUT = re.compile(r"^IB OUTGOING MOBILE PAYMENTS (PAYBILL|BUYGOODS) \S+ (\d{4,12})$")
_NCBA_UTILITY = re.compile(r"^UTILITY PAYMENT (.+)$")
_NCBA_CARD = re.compile(r"^MERCHANT ATM TRANSACTION (.+)$")
_NCBA_PERSON_IN = re.compile(r"^MPESA PAYBILL TRANSFER MPESA REF: \S+ (.+)$")
_NCBA_NO_KEY = re.compile(r"^(?:AA LOAN REPAYMENT|INWARD CLEARING DIRECT DEBIT|IB OUTGOING MOBILE PAYMENTS (?!PAYBILL|BUYGOODS))")
_NCBA_CHARGES = re.compile(r"^(MOBILE PAYMENTS CHARGES|KE EXCISE DUTY|EXCISE DUTY|DEBIT CARD CHARGE|TRANSFER CHARGES|INWARD DIRECT DEBIT COMMISSION)\b")


def _ncba(text: str, secret: str) -> RowKey:
    if text.startswith("TRF TO M-PESA"):
        return RowKey(kind=None, value=None, is_transfer_hint=True)
    if m := _NCBA_IB_OUT.match(text):
        kind, number = m.groups()
        if kind == "BUYGOODS":
            return RowKey(kind="till", value=number, number_kind="till", number=number, display=f"Till {number}")
        # NCBA prints the account reference the payer typed, not the Paybill.
        return RowKey(
            kind="reference",
            value=hash_value(NCBA, number, secret=secret),
            provider=NCBA,
            display=f"Paybill payment · ref {number}",
        )
    if _NCBA_NO_KEY.match(text):
        return RowKey(kind=None, value=None)
    if m := _NCBA_CHARGES.match(text):
        return _name_row(m.group(1))
    if m := _NCBA_UTILITY.match(text):
        return _name_row(_words_only(m.group(1)))
    if m := _NCBA_CARD.match(text):
        # Keep the prefix in match_text: some shared patterns include it
        # (e.g. 'MERCHANT ATM TRANSACTION * GOOGLE ONE').
        return _name_row(_words_only(m.group(1)), match_text=_words_only(text))
    if m := _NCBA_PERSON_IN.match(text):
        return _person_row(NCBA, _words_only(m.group(1)), secret=secret)
    if text.startswith("IB INCOMING MOBILE PAYMENTS"):
        # Narrative may name an employer or a person: keep the key private.
        # Reading the shared list by text is still fine (nothing is shared
        # by a lookup), e.g. 'IB INCOMING MOBILE PAYMENTS * DLOCAL'.
        words = _words_only(text)
        return _person_row(NCBA, words, secret=secret, match_text=words)
    return _fallback(text)


# --- everything else ------------------------------------------------------


def _fallback(text: str) -> RowKey:
    words = _words_only(text)
    if not words:
        return RowKey(kind=None, value=None)
    # Private name key; only shared-list matches or business-looking
    # names count as business.
    return RowKey(kind="name", value=words, is_business=looks_like_business(words), name=words, match_text=words)


def extract_key(*, provider: str, description: str | None, direction: str, secret: str) -> RowKey:
    text = normalize_text(description)
    if not text:
        return RowKey(kind=None, value=None)
    if provider == MPESA:
        return _mpesa(text, secret)
    if provider == NCBA:
        return _ncba(text, secret)
    return _fallback(text)
