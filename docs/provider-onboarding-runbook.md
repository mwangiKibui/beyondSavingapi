# Provider onboarding runbook

Supporting a new statement provider (a bank, a mobile money service, a
SACCO) is a repeatable engineering task, not a one-off. This document is
the working detail referenced by the product brief's "Provider
onboarding" section (`product-brief.html`, in the shared cross-repo docs
folder, not this repo) — this file is the source of truth, that section
is just the pointer.

## Scope

MVP1 covers **upload only**: a user downloads a statement themselves and
uploads it. Live API sync (connecting directly to a provider) is MVP2 and
will extend this runbook with the API-connection side when it lands.

MVP1 ships parsing support for every provider a user can already create
an account for today (see `PROVIDERS_BY_TYPE` in `app/api/accounts.py`):

- **M-Pesa** (mobile money)
- **Airtel Money** (mobile money)
- **Equity Bank** (bank)
- **NCBA Bank** (bank)
- **Mentor Sacco** (sacco)
- **Biashara Sacco** (sacco)

**Working assumption until real samples say otherwise:** every
provider's statement is password-protected, and the file is either a
PDF or a Word document (`.doc`/`.docx`) - both need to be unlocked
before parsing, so step 2 below should confirm which format and
password scheme actually apply once a real sample is in hand.

Each one goes through the same process below, starting from its own
"collect sample statements" ticket - they don't have to land together,
but all six are in scope for MVP1, not just a first pair.

## Process

Follow these steps in order for every new provider. Each one has a
matching field in the per-provider template below — fill that in as you
go rather than after the fact.

1. **Collect real sample statement files.** Get several — different
   months, different account types if the provider has more than one,
   and edge cases (a statement with a reversed/refunded transaction, a
   zero-activity period, a password-protected file). No parser should be
   written without real examples in hand: they anchor the field mapping
   and become the parser's test fixtures. This is its own ticket per
   provider (e.g. ab-35 starts this for M-Pesa) — don't start the parser
   itself until sample files exist.
2. **Analyze the format.** Confirm the file type (working assumption is
   PDF or Word for every MVP1 provider, until a real sample says
   otherwise) and how it's password-protected (and what the password is
   derived from — e.g. M-Pesa PDFs are typically locked with the account
   holder's ID number or a PIN they set) — both need unlocking before
   parsing. Then note how the statement is laid out (a single table, one
   section per day, a running balance column, etc.) and any header/footer
   noise to strip.
3. **Map fields to the transaction schema.** Every source column/position
   needs an explicit mapping to the `transactions` table (see
   `beyondSavingdb/migrations/000001_initial_schema.up.sql`, or the
   shared `schema.sql` reference doc):
   - `txn_date` — the transaction's date.
   - `amount` — a positive magnitude (never signed — see `direction`).
   - `direction` — `in` or `out`.
   - `counterparty` — who it was to/from, if the statement says.
   - `description` — the raw statement line text (kept verbatim, not
     reworded, so the original fact is preserved).
   - `balance_after` — the running balance following this transaction, if
     the statement provides one.
4. **Decide the `dedupe_hash` strategy.** Deduplication is scoped per
   account (`UNIQUE (account_id, dedupe_hash)`) and applies to imports
   only. Prefer a stable identifier the provider includes on each line (a
   transaction reference/receipt number) when one exists; fall back to a
   hash of `txn_date + amount + direction + description` when it doesn't.
   **Don't assume a reference number is unique per row without checking —
   confirmed on two providers so far (Mentor Sacco, NCBA), one real-world
   event routinely fans out to several related postings (a fee, the main
   entry, an excise duty line) that all share the exact same reference
   code.** Combine the reference with the amount (and description, if two
   related postings can share both reference and amount) rather than
   trusting the reference alone. Document which one this provider uses
   and why.
5. **Build and test the parser** against the sample files from step 1.
   Cover the edge cases collected there, not just the happy path.
6. **Validate against the running balance.** Replay the parsed
   transactions in order and confirm the computed running balance matches
   the statement's own `balance_after` column at every row, not just the
   final one. A mismatch means a transaction was missed, mis-signed, or
   mis-ordered.
7. **Wire into the parse-job queue.** Once the parser is proven standalone,
   connect it to the upload → queue → worker pipeline (separate BE
   tickets) rather than building the two together.
8. **Update this runbook.** Fill in the provider's template entry
   properly (replacing any "TBD" placeholders) as part of the parser
   ticket's own acceptance criteria — a parser ships with its
   documentation, not before it.

## Per-provider template

Copy this block for each new provider.

```markdown
### <Provider name> (<account type: bank | mobile_money | sacco>)

- **Status:** not started | samples collected | parser in progress | parser shipped
- **File format:** e.g. PDF, Word (`.doc`/`.docx`) — MVP1's working
  assumption is that every provider's statement is one of these two,
  password-protected, until a real sample says otherwise
- **Password-protected:** yes/no — if yes, how the password is derived
- **Sample files:** where they live (e.g. `tests/fixtures/statements/<provider>/`)
- **Field mapping:**
  | Source column/position | Maps to           | Notes |
  |-------------------------|-------------------|-------|
  | ...                      | `txn_date`        |       |
  | ...                      | `amount`          |       |
  | ...                      | `direction`       |       |
  | ...                      | `counterparty`    |       |
  | ...                      | `description`     |       |
  | ...                      | `balance_after`   |       |
- **Dedupe strategy:** provider reference number, or hash of date+amount+direction+description
- **Known quirks:** anything that broke a naive parse — multi-line
  descriptions, reversed transactions, non-chronological ordering, etc.
```

## Providers

### M-Pesa (mobile_money)

- **Status:** parser built (ab-39) — `app/parsers/mpesa.py`, registered
  in `app/parsers/PARSERS`. Not yet wired to actually write transactions
  or flip the import to `parsed` — that's ab-44.
- **File format:** PDF — official "M-PESA STATEMENT" from Safaricom,
  with a SUMMARY section (totals per transaction type) followed by a
  DETAILED STATEMENT section (the actual transaction table). Extracted
  via `pdfplumber`'s table detection, not `pypdf`'s raw text — the raw
  text interleaves columns unpredictably (e.g. a page number merging
  into the next cell's word) in a way that makes reliable column-based
  parsing impractical.
- **Password-protected:** yes, confirmed. Not derivable from anything
  visible in the statement itself (checked against the mobile number —
  no match); likely the account holder's ID number or a self-set PIN,
  matching the general MVP1 assumption. TBD until confirmed directly or
  against a second sample.
- **Sample files:** `tests/fixtures/statements/mpesa/statement_sample.pdf`
  (PII redacted — customer name, mobile number, email address, and
  every counterparty name attached to a masked phone number replaced
  with placeholders via exact word-level PDF redaction. Safaricom's own
  partial phone masking, e.g. `254791***999`, is left as-is — realistic
  test data for how the provider already obscures numbers itself).
  **Re-locked with a placeholder password (`0000000`), not the real
  one** — same reasoning as NCBA: the real password isn't derivable from
  the document and may be tied to real personal info.
- **Field mapping:**
  | Source column/position | Maps to | Notes |
  |---|---|---|
  | `Receipt No.` | not directly mapped | used for dedupe (see below) — not unique per row |
  | `Completion Time` | `txn_date` | `YYYY-MM-DD HH:MM:SS` |
  | `Details` | `description` | often wraps across 2-4 lines — keep the full multi-line text as one field |
  | `Transaction Status` | not mapped for MVP1 | every row in this sample was `Completed`; the parser defensively excludes any row whose status isn't exactly `Completed` rather than posting it, though this is still untested against a real sample that actually has one |
  | `Paid In` / `Withdrawn` | `amount` + `direction` | separate columns, only one populated per row |
  | `Balance` | `balance_after` | one running balance for the whole account — no sub-ledgers |
- **Dedupe strategy:** same pattern as Mentor Sacco and NCBA — `Receipt
  No.` is stable but **not unique per row**. M-Pesa's Fuliza (overdraft)
  mechanic is the clearest example yet: a single real-world payment
  routinely posts as 2-3 rows sharing one receipt (the actual payment, an
  "OverDraft of Credit Party" credit covering the shortfall, and
  sometimes a separate charge line) — e.g. receipt `UIFJH6C7O7` covers
  an overdraft credit, the Pay Bill payment itself, and its charge, all
  as three distinct rows. Use `Receipt No. + Details + amount`.
- **Known quirks:**
  - **Rows are newest-first, not chronological.** The detailed
    statement lists the most recent transaction at the top and the
    oldest at the bottom — the opposite order from Mentor Sacco and
    NCBA. Running-balance validation (step 6) needs to process this
    provider's rows bottom-to-top, or explicitly sort by
    `Completion Time` ascending first.
  - **Fuliza (overdraft) transactions always split into multiple rows**
    sharing one receipt number — see the dedupe strategy above. Don't
    treat the "OverDraft of Credit Party" line as a separate real
    transaction from the payment it's covering; both need to be
    understood together to make sense of the balance movement, even
    though they're stored as separate `transactions` rows.
  - **Counterparty phone numbers are pre-masked by Safaricom itself**
    (e.g. `254791***999`), unlike NCBA/Mentor Sacco which show full
    numbers. Pass through as-is.
  - **One statement = one account here**, same as NCBA — no sub-ledger
    split needed for this provider.
  - **`pdfplumber`'s table detection isn't consistent page to page on
    this document** — one page's transaction table has 2 extra empty
    columns splitting "Details" from the rest, the other doesn't. The
    parser locates columns by the header row's own text rather than a
    fixed index to handle both shapes.
  - **A wrapped counterparty name at the end of a multi-line "Details"
    cell sometimes spills into a phantom extra table row** with no
    Receipt No./Completion Time of its own (occasionally the name is
    even truncated by a character in that phantom row). The real text
    is already part of the row above's cell, so the parser discards any
    row missing both a Receipt No. and a Completion Time rather than
    merging it in and risking a duplicated/garbled name.
  - **A bare page number occasionally bleeds into the last row's
    "Details" cell on a page** (e.g. a trailing `"4"` on its own line).
    The parser strips any line that's only digits before joining a
    cell's wrapped lines into the final `description`.
  - **The uploaded file must be stored decrypted, not as originally
    uploaded** — ab-37's endpoint now runs a password-protected PDF
    through `decrypt_if_needed()` before it ever reaches MinIO, since
    the password itself is intentionally never persisted and a parser
    has no other way to read the file later.

### Airtel Money (mobile_money)

- **Status:** not started
- **File format:** assumed PDF or Word (`.doc`/`.docx`) — TBD which,
  pending a sample statement
- **Password-protected:** assumed yes — TBD exactly what the password is
  derived from, pending a sample statement
- **Sample files:** none yet — see the "collect sample statements" ticket
- **Field mapping:** TBD — pending a sample statement to analyze
- **Dedupe strategy:** TBD — check for a per-line transaction/receipt
  code first, same approach as M-Pesa
- **Known quirks:** TBD

### Equity Bank (bank)

- **Status:** parser built (ab-40) — `app/parsers/equity_bank.py`,
  registered in `app/parsers.PARSERS`. Not yet wired to write
  transactions or flip the import to `parsed` — that's ab-44.
- **File format:** PDF — "Account Statement". Parsed from raw
  `pdfplumber` text (not table extraction) - `extract_tables()`
  fragments this document into inconsistent single-row tables that
  miss several real transaction rows entirely, unlike M-Pesa's cleaner
  table structure.
- **Password-protected:** yes, confirmed. **Derivation confirmed on this
  sample**: the password is the last 4 digits of the account number
  (account `...1195`, password `1195`). Worth double-checking against a
  second sample before fully trusting it as the general rule, but this
  is the first provider where the derivation is actually confirmed
  rather than guessed.
- **Sample files:**
  `tests/fixtures/statements/equity_bank/account_statement_sample.pdf`
  (PII redacted — customer name, phone numbers, email, and account
  number replaced with placeholders via exact word-level PDF redaction).
  **Re-locked with a placeholder password (`0000`)**, not the real one —
  even though this provider's derivation is now known, the real value is
  still tied to this specific real account number, so it isn't reused.
- **Field mapping:**
  | Source column/position | Maps to | Notes |
  |---|---|---|
  | `Transaction Details` | `description` | multi-line; often packs a phone number, an M-Pesa-style transaction code, a counterparty name, and a fragment of the account number into one wrapped block - keep the whole block as `description` for MVP1 rather than trying to parse out the counterparty separately. The parser's line-boundary heuristic occasionally attaches a transaction's trailing wrapped fragment to the *next* transaction's description instead of its own - an accepted MVP1 imprecision, doesn't affect amount/direction/balance/dedupe |
  | `Payment reference` | not directly mapped | used for dedupe (see below) — not unique per row |
  | `Value Date` | `txn_date` | `DD/MM/YYYY` |
  | `Credit (Money In)` / `Debit (Money Out)` | `amount` + `direction` | **not extracted positionally** - only one column is ever populated per row, and it renders in text as a single bare number with no way to tell which column it came from. Derived instead from the **balance delta** against the previous row (seeded from an opening balance computed from the closing `Total` row - see below). Verified by hand against every transaction in the real sample. |
  | `Balance` | `balance_after` | one running balance for the whole account |
- **Dedupe strategy:** same pattern as every other provider so far —
  `Payment reference` is stable but **not unique per row** (e.g.
  reference `5492302` covers both a debit and its own SMS charge as two
  separate rows in this sample). Use `Payment reference + amount`.
- **Known quirks:**
  - Small amounts render oddly zero-padded (e.g. `02.26` for KES 2.26) —
    strip leading zeros rather than treating them as a formatting error.
  - A `Total` row closes out the transaction table (total credits, total
    debits, closing balance) — not a transaction, must be skipped, same
    as Mentor Sacco/NCBA's non-transaction rows. **This statement has no
    explicit opening balance anywhere** - derive it from this same Total
    row instead: `opening = closing_balance - total_credit + total_debit`
    (verified: gives exactly 340.99, which correctly reconstructs every
    transaction's balance delta from the first row onward).
  - **Some template text (headers, this Total row) renders with every
    character doubled** - a faux-bold trick (e.g. `"TToottaall"` for
    "Total", `"4422,,666600..0000"` for "42,660.00"). Actual transaction
    data rows are never affected, only certain label/total text. Undo it
    per-word (split on whitespace, collapse a word back down if every
    adjacent character pair matches) rather than treating it as
    corruption - the same trick was later confirmed on NCBA's own
    "IMPORTANT NOTICE" footer text too, so it's provider-agnostic.
  - `pdfplumber`'s `extract_tables()` is unreliable on this document -
    it detects each transaction row as its own separate single-row
    "table" inconsistently, and misses some rows outright. Raw
    `extract_text()` plus a regex matching each transaction's numeric
    line (`reference date amount balance`) is what the actual parser
    uses instead.
  - One account = one statement, no sub-ledger split needed.

### NCBA Bank (bank)

- **Status:** parser built (ab-40) — `app/parsers/ncba_bank.py`,
  registered in `app/parsers.PARSERS`. Not yet wired to write
  transactions or flip the import to `parsed` — that's ab-44.
- **File format:** PDF — "e-Statement of Account", "GO BANKING - PAY AS
  YOU GO CURRENT" account type. Real sample spans 5 pages, all parsed
  and verified (see below).
- **Password-protected:** yes, confirmed. Not derivable from any field
  visible in the statement itself (checked against the account number
  and every counterparty phone number in the sample — none match); likely
  the account holder's ID number or a self-set PIN. TBD until confirmed
  with NCBA directly or against a second sample.
- **Sample files:**
  `tests/fixtures/statements/ncba_bank/e_statement_sample.pdf` (PII
  redacted — account holder name, account number, and 11 distinct
  counterparty phone numbers replaced with placeholders via exact
  word-level PDF redaction; transaction rows, amounts, references, and
  running balances are the real values from the source statement).
  **Re-locked with a placeholder password (`0000000`), not the real
  one** — the real password may itself be derived from real personal
  info (e.g. an ID number) not otherwise visible in the document, so it
  isn't committed even though the visible content is now anonymized.
- **Field mapping:**
  | Source column/position | Maps to | Notes |
  |---|---|---|
  | `Date` | `txn_date` | `DD/MM/YYYY` |
  | `Value Date` | not directly mapped | equalled `Date` on every row in this sample — likely redundant for this account type, but map it in case a future sample shows it differing (e.g. a cheque clearing after the transaction date) |
  | `Transaction Type and Details` (2 lines: a type line, then a reference/counterparty line below it, though some wrap onto a 3rd) | `description` | keep all lines — the reference line often carries the only counterparty info (phone number, PayBill/BuyGoods till, MPESA name) |
  | `Debit` / `Credit` | `amount` + `direction` | **not extracted positionally** - `pdfplumber`'s table detection merges every row on a page into one multi-line cell per column, losing per-row Debit/Credit alignment entirely (17 debit values and 2 credit values, no way to tell which of 19 transactions each belongs to). Derived instead from the **balance delta** against the previous row, seeded from the header's explicit "Opening Balance" - verified against every one of 79 transactions across all 5 pages, and the running total matches the statement's own "Payments In"/"Payments Out"/"Closing Balance" figures exactly. |
  | `Balance` | `balance_after` | one running balance for the whole account — no sub-ledgers, unlike Mentor Sacco |
- **Dedupe strategy:** the reference code in the detail line (e.g.
  `FT26216QCX1Y`) is stable but **not unique per row** — confirmed in
  this sample: `1009866765 AA261354HDCY FT26216QCX1Y` appears identically
  on three consecutive rows (a commission fee, the main clearing entry,
  and an excise duty line, all one real-world event). Since there's no
  separate reference column extracted for this provider (the reference
  is embedded in the multi-line `description` text, not isolated), the
  actual implementation hashes `description + amount` - achieving the
  same "not reference alone" requirement without needing to isolate the
  specific reference substring.
- **Known quirks:**
  - **No "Opening Balance" row in the table.** Unlike Mentor Sacco, the
    starting balance comes only from the statement's summary header
    ("Opening Balance: 107,374.39"), not from a row inside the
    transaction table. Running-balance validation (step 6) needs to seed
    from that header field.
  - **Header/summary repeats identically on every page** (account
    number, holder name, Opening/Payments In/Payments Out/Available/
    Closing Balance) — these are the whole statement period's totals,
    not per-page figures. Don't re-parse them as new data each page.
  - **The header disclaimer and "Page N" footer appear twice in a
    naive linear text extraction** even though each renders once,
    visually at the bottom of the page — a PDF content-stream ordering
    artifact, not duplicated content. Don't assume linear extraction
    order matches visual top-to-bottom order for header/footer text;
    rely on the transaction table's own row structure instead.
  - **One statement = one account here**, unlike Mentor Sacco — no
    sub-ledger split needed for this provider.
  - Card numbers appear pre-masked by the bank itself (e.g.
    `425199......9399`) — pass through as-is, don't attempt to unmask.
  - **`pdfplumber`'s table detection merges every transaction on a page
    into one giant multi-line cell per column** - there are no
    horizontal rules between individual rows, only the outer/column
    borders. Real per-row boundaries are reconstructed from the `Date`
    column's own word y-positions instead (a date renders on exactly
    one line per transaction, unlike `description`, which can wrap onto
    extras) - see `app/parsers/ncba_bank.py`'s geometry-based row
    reconstruction. This is the same underlying limitation Equity Bank
    hits, just manifesting differently (Equity fragments into too many
    tables, NCBA collapses into too few).

### Mentor Sacco (sacco)

- **Status:** parser built (ab-115) — `app/parsers/mentor_sacco.py`.
  **Not registered in `app/parsers.PARSERS`** - its output
  (`list[SubLedgerStatement]`, one entry per sub-ledger *instance*) is
  fundamentally multi-account and doesn't fit `StatementParser`'s
  single-list interface. Standalone for now, ready for ab-116 to wire
  in once it resolves how a multi-account import actually maps to
  beyondSaving accounts.
- **File format:** PDF — "MEMBER STATEMENT" from Mentor Sacco Society Ltd.
  Parsed from raw `pdfplumber` text (not table extraction) - same
  fragmentation problem as Equity Bank (`extract_tables()` found real
  transaction rows on only 1 of the sample's 2 pages).
- **Password-protected:** no (contrary to the general MVP1 assumption —
  the sample opened directly, no password prompt)
- **Sample files:** `tests/fixtures/statements/mentor_sacco/member_statement_sample.pdf`
  (PII redacted — member name, phone numbers, member no., and account no.
  are replaced with placeholders; transaction rows, amounts, and running
  balances are the real values from the source statement)
- **Field mapping:**
  | Source column/position | Maps to | Notes |
  |---|---|---|
  | `Date` | `txn_date` | `DD-MM-YYYY` |
  | `Document No` | not directly mapped | used for dedupe (see below) — repeats across related postings, not a unique row ID by itself |
  | `Transaction Details` | `description` | raw line text, kept verbatim |
  | `Debit` / `Credit` | `amount` + `direction` | **not extracted positionally** - same as every bank provider, only one column is ever populated per row and it renders as a single bare number in text. Derived instead from the **balance delta** against the previous row within that same sub-ledger, seeded from the sub-ledger's own opening balance (explicit where stated, otherwise backed out from the first real row - see quirks). |
  | `Balance` | `balance_after` | running balance for that sub-ledger only, not the whole statement |
- **Dedupe strategy:** `Document No` is **not** unique per row — the same
  code (e.g. `ATRC-07315`) appears on multiple rows across different
  sub-ledgers because one real-world event (e.g. a loan repayment) posts
  simultaneously to several sub-ledgers (confirmed: 5 rows share this
  exact code across 3 different sub-ledgers in the real sample, two of
  them even sharing identical description text and amount). Since
  `Document No` isn't cleanly isolated from the rest of the line's text
  either (it's just the first word), the actual implementation hashes
  `sub-ledger name + full description + amount` - achieving the same
  practical uniqueness without needing to extract it separately.
- **Known quirks:**
  - **One statement, four sub-ledgers.** A single Mentor Sacco member
    statement bundles four independent accounts, each with its own
    "Opening Balance" and running balance: **Ordinary Deposit**,
    **Savings Account**, **Share Capital**, and **Instant Loan**. These
    are not sections of one ledger — they're four separate balance
    sequences that happen to share one PDF.
  - **Design decision (2026-09-16):** beyondSaving models one `account` =
    one nickname/provider/currency, so one Mentor Sacco statement maps to
    **multiple beyondSaving accounts**, one per sub-ledger (e.g. "Mentor
    Sacco - Savings", "Mentor Sacco - Instant Loan"). The upload flow
    needs to handle "this one file produced transactions for N accounts,"
    not just one — this is a real scoping requirement for the Statement
    upload API (ab-37) and the Mentor Sacco parser ticket, not an edge
    case to special-case away.
  - **"Instant Loan" appears twice.** A new loan disbursement shows up as
    its own one-row "Instant Loan" section (opening balance = the
    disbursed amount), immediately followed by a second "Instant Loan"
    section that carries the running amortization ledger (interest and
    principal recovery postings) down to zero. Both share the same
    section header text — distinguish them by opening balance/position,
    not by name alone.
  - **Section header rows and "Opening Balance" rows are not
    transactions.** Lines like "Savings Account" (a bare sub-ledger
    label) and "Opening Balance: 31-Jul-2025" must be skipped when
    extracting transactions — the opening balance seeds that sub-ledger's
    running-balance validation (step 6) rather than being transaction #1.
  - **Negative balances are parenthesized**, e.g. `(100.00) CR` — standard
    accounting notation for a negative number, not a formatting error.
    The sub-ledger legitimately went negative there; strip the
    parentheses and treat as negative when validating the running
    balance.
  - **CR/DR marks whether the *resulting balance* sits in a credit or
    debit position, not that specific transaction's own direction.**
    Confirmed against the real sample: a row that clearly *decreases* a
    CR-normal sub-ledger's balance (e.g. a loan repayment deducted from
    Savings) still carries a "CR" suffix, because the balance is still
    positive/in-credit after it - not because that transaction was
    itself a credit. Since direction is derived from the balance delta
    (not from Debit/Credit column position, which isn't reliably
    extractable anyway), the CR/DR mark ends up not being needed for
    computing `direction` at all - only the parenthesization matters,
    for the balance's numeric sign.
  - **Two of the four sub-ledger instances have no explicit "Opening
    Balance" line at all** - Savings Account, and a fresh Instant Loan
    disbursement (as opposed to the separate, pre-existing Instant Loan
    amortization ledger, which does state one). For these, the opening
    balance is backed out from the first real row instead, assuming
    that row is an increase (e.g. `0.00` balance after a `100.00`
    deposit implies an opening of `-100.00`) - confirmed correct by
    hand against the real sample.

### Biashara Sacco (sacco)

- **Status:** not started
- **File format:** assumed PDF or Word (`.doc`/`.docx`) — TBD which,
  pending a sample statement
- **Password-protected:** assumed yes — TBD exactly what the password is
  derived from, pending a sample statement
- **Sample files:** none yet — see the "collect sample statements" ticket
- **Field mapping:** TBD — pending a sample statement to analyze
- **Dedupe strategy:** TBD
- **Known quirks:** TBD
