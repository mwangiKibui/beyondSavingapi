# AI layout evaluation

Model `openai-gpt-oss-120b`, 2 run(s) per statement, page 1 masked, no guess of ours (the AI reads the layout from scratch).

Run on 2026-11-25 with `scripts/eval_ai_layouts.py` against the dev database (ab-333). This is the hardest case: the model reads each layout **from scratch**, with no guess of ours. In production our own guess comes first and the AI's answer is merged onto it, and banks with a built-in layout (M-Pesa, Equity, NCBA, Mentor) never reach the AI.

## Findings

- **Row grouping:** the model named a grouping that reads the statement in 13 of the 16 runs where that can be scored. In the other 3, Co-operative Bank and the generated "around" statement got a wrong first answer, and our own tries (`read_best_grouping`) found the right grouping. **Every passing read used a correct grouping.**
- **Co-operative Bank** (text centred on the date line): passed in both runs with all 1,040 rows and 15 rows without a description (1.4%). Before ab-333 that read lost 625 descriptions.
- **Column roles:** the model got all five key roles right on Airtel, M-Pesa and the generated statements, and 4/5 on Co-operative Bank (it read the first of two date columns; our date-column swap fixes that).
- **From scratch it fails on two of our real statements.** Both have built-in layouts, so production doesn't depend on the AI for them:
  - **NCBA:** the model read the "Debit" and "Credit" headings, stacked over two lines, as one signed-amount column. With the right roles our reader passes NCBA without any special settings. *Follow-up:* tell the model that stacked headings over different columns are separate columns, then re-run this report.
  - **M-Pesa:** the roles were right, but the statement needs settings the model can't express: a totals-based check (its running balance doesn't add up row to row) and the order of same-time rows. *Follow-up candidates for new tried-not-assumed settings:* the balance check mode and the order of tied rows.
- **Mentor Sacco** has several sub-ledger tables in one statement. That isn't a single-table layout; it has its own parser.
- **Cost and time:** about 1.3–2.6k tokens in and 0.5–1.9k out per statement, which is well under a cent. Each call took 7–26s.

## Results

| Statement | Run | Outcome | Rows read / expected | Rows w/o description | Key roles right | Row grouping said (reads?) | Grouping used | Tokens in/out | Seconds |
|---|---|---|---|---|---|---|---|---|---|
| Airtel Money | 1 | passed | 83 / 83 | 0 | 5/5 | below (yes) | below | 2048/811 | 13 |
| Airtel Money | 2 | passed | 83 / 83 | 0 | 5/5 | below (yes) | below | 2048/688 | 9 |
<!-- Airtel Money: groupings that read it with the known layout: below, nearest -->
| Co-Operative Bank | 1 | passed | 1040 / 1040 | 15 | 4/5 | above (no) | nearest | 1808/1170 | 18 |
| Co-Operative Bank | 2 | passed | 1040 / 1040 | 15 | 4/5 | nearest (yes) | nearest | 1808/1364 | 20 |
<!-- Co-Operative Bank: groupings that read it with the known layout: nearest -->
| Equity Bank | 1 | passed | 6 / 6 | 0 | 3/5 | above (yes) | above | 1293/1057 | 15 |
| Equity Bank | 2 | passed | 6 / 6 | 0 | 3/5 | above (yes) | above | 1293/1866 | 25 |
<!-- Equity Bank: groupings that read it with the known layout: above, below, nearest -->
| M-Pesa | 1 | failed_check | 0 / 44 | - | 5/5 | below (yes) | below | 2191/864 | 12 |
| M-Pesa | 2 | failed_check | 0 / 44 | - | 5/5 | below (yes) | below | 2191/1052 | 17 |
<!-- M-Pesa: groupings that read it with the known layout: above, below, nearest -->
| Mentor Sacco | 1 | failed_check | 0 / 2 | - | - | above (-) | above | 2568/930 | 13 |
| Mentor Sacco | 2 | failed_check | 0 / 2 | - | - | below (-) | below | 2568/760 | 11 |
<!-- Mentor Sacco: groupings that read it with the known layout: n/a -->
| NCBA Bank | 1 | failed_check | 0 / 79 | - | 3/5 | below (yes) | below | 2021/1103 | 15 |
| NCBA Bank | 2 | failed_check | 0 / 79 | - | 3/5 | below (yes) | below | 2021/1261 | 20 |
<!-- NCBA Bank: groupings that read it with the known layout: below, nearest -->
| Generated: text on the date line | 1 | passed | 30 / 30 | 0 | 5/5 | below (yes) | below | 1437/633 | 10 |
| Generated: text on the date line | 2 | passed | 30 / 30 | 0 | 5/5 | below (yes) | below | 1437/543 | 8 |
<!-- Generated: text on the date line: groupings that read it with the known layout: above, below, nearest -->
| Generated: text above the date line | 1 | passed | 30 / 30 | 0 | 5/5 | above (yes) | above | 1537/484 | 7 |
| Generated: text above the date line | 2 | passed | 30 / 30 | 0 | 5/5 | above (yes) | above | 1537/722 | 10 |
<!-- Generated: text above the date line: groupings that read it with the known layout: above, nearest -->
| Generated: text around the date line | 1 | passed | 30 / 30 | 0 | 5/5 | below (no) | nearest | 1396/1691 | 26 |
| Generated: text around the date line | 2 | passed | 30 / 30 | 0 | 5/5 | nearest (yes) | nearest | 1396/1700 | 24 |
<!-- Generated: text around the date line: groupings that read it with the known layout: nearest -->
