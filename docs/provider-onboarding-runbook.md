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

MVP1 ships parsing support for exactly two providers:

- **M-Pesa** (mobile money) — password-protected PDF.
- **Equity Bank** (bank).

Every other provider a user can already create an account for today
(Airtel Money, NCBA Bank, Mentor Sacco, Biashara Sacco — see
`PROVIDERS_BY_TYPE` in `app/api/accounts.py`) supports manual transaction
entry now, but has no statement parser yet. Adding one follows this same
process, starting from its own "collect sample statements" ticket.

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
   provider (e.g. ab-35 for M-Pesa + Equity Bank) — don't start the parser
   itself until sample files exist.
2. **Analyze the format.** File type (PDF, CSV, Excel), whether it's
   password-protected (and what the password is derived from — e.g.
   M-Pesa PDFs are typically locked with the account holder's ID number
   or a PIN they set), how the statement is laid out (a single table, one
   section per day, a running balance column, etc.), and any header/footer
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
- **File format:** e.g. PDF, CSV, Excel
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
- **File format:** PDF (per the product brief — password-protected)
- **Password-protected:** yes — TBD exactly what the password is derived
  from until a real sample statement is in hand (typically the account
  holder's ID number or a self-set PIN for M-Pesa statements, to be
  confirmed against an actual file)
- **Sample files:** none yet — see the "collect sample statements" ticket
- **Field mapping:** TBD — pending a sample statement to analyze
- **Dedupe strategy:** TBD — M-Pesa statements typically include a
  transaction/receipt code per line; use that if present, otherwise fall
  back to the date+amount+direction+description hash
- **Known quirks:** TBD

### Equity Bank (bank)

- **Status:** not started
- **File format:** TBD — pending a sample statement to analyze
- **Password-protected:** TBD
- **Sample files:** none yet — see the "collect sample statements" ticket
- **Field mapping:** TBD — pending a sample statement to analyze
- **Dedupe strategy:** TBD
- **Known quirks:** TBD
