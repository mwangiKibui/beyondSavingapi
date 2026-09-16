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
   only. Prefer a stable identifier the provider includes on each line
   (a transaction reference/receipt number) when one exists; fall back to
   a hash of `txn_date + amount + direction + description` when it
   doesn't. Document which one this provider uses and why.
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

- **Status:** not started
- **File format:** assumed PDF or Word (`.doc`/`.docx`) — TBD which,
  pending a sample statement (product brief calls out PDF specifically
  for M-Pesa, so PDF is the stronger guess here)
- **Password-protected:** assumed yes — TBD exactly what the password is
  derived from until a real sample statement is in hand (typically the
  account holder's ID number or a self-set PIN for M-Pesa statements, to
  be confirmed against an actual file)
- **Sample files:** none yet — see the "collect sample statements" ticket
- **Field mapping:** TBD — pending a sample statement to analyze
- **Dedupe strategy:** TBD — M-Pesa statements typically include a
  transaction/receipt code per line; use that if present, otherwise fall
  back to the date+amount+direction+description hash
- **Known quirks:** TBD

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

- **Status:** not started
- **File format:** assumed PDF or Word (`.doc`/`.docx`) — TBD which,
  pending a sample statement
- **Password-protected:** assumed yes — TBD exactly what the password is
  derived from, pending a sample statement
- **Sample files:** none yet — see the "collect sample statements" ticket
- **Field mapping:** TBD — pending a sample statement to analyze
- **Dedupe strategy:** TBD
- **Known quirks:** TBD

### NCBA Bank (bank)

- **Status:** not started
- **File format:** assumed PDF or Word (`.doc`/`.docx`) — TBD which,
  pending a sample statement
- **Password-protected:** assumed yes — TBD exactly what the password is
  derived from, pending a sample statement
- **Sample files:** none yet — see the "collect sample statements" ticket
- **Field mapping:** TBD — pending a sample statement to analyze
- **Dedupe strategy:** TBD
- **Known quirks:** TBD

### Mentor Sacco (sacco)

- **Status:** samples collected (ab-35)
- **File format:** PDF — "MEMBER STATEMENT" from Mentor Sacco Society Ltd
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
  | `Debit` / `Credit` | `amount` + `direction` | which column is populated decides `direction` — see quirks below for the CR/DR-section caveat |
  | `Balance` | `balance_after` | running balance for that sub-ledger only, not the whole statement |
- **Dedupe strategy:** `Document No` is **not** unique per row — the same
  code (e.g. `ATRC-07315`) appears on multiple rows across different
  sub-ledgers because one real-world event (e.g. a loan repayment) posts
  simultaneously to several sub-ledgers. Use a composite hash of
  `Document No + sub-ledger name + Transaction Details + amount` instead
  of `Document No` alone. Rows with a genuinely unique per-row code (the
  `Balance Enquiry Charges-SSPSP...` lines) are unambiguous either way.
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
  - **CR/DR marks the sub-ledger's normal balance side, not the
    transaction's direction on its own.** Ordinary Deposit, Savings
    Account, and Share Capital are CR-normal (Credit = money in, Debit =
    money out, matching a member's savings). Instant Loan is DR-normal
    (Debit = loan disbursed/liability increases, Credit = repayment).
    Map `direction` per sub-ledger accordingly rather than assuming
    Debit always means "out."

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
