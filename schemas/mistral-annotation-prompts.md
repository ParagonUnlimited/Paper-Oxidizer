# Prompts to paste into the Mistral test UI

## Annotation Prompt (the document-level one)

This PDF is a scan from a personal financial and legal archive: probate, household
bills, taxes, banking, insurance, court papers. It may be ONE document or MANY
unrelated documents scanned together, possibly out of order.

Rules, in priority order:

1. SEGMENT FIRST. Identify each distinct document and list its PDF page numbers in
   correct reading order (which may differ from scan order). Every page must appear
   in exactly one document's pages list or in unassigned_pages with a reason -- the
   page counts must add up to the total.
2. COPY, DON'T INTERPRET. Values exactly as printed: account numbers with their
   dashes, amounts with their $ signs, names letter-for-letter. 'ROBERT N DOBBINS'
   and 'ROBERT ALDEN DOBBINS' are different strings -- never normalize or merge
   names. Dates as YYYY-MM-DD. Use null for anything not printed. Never guess.
3. DATES ARE GRANULAR. Capture EVERY date on the document in dates[], each
   classified by its meaning: posted, due-by, service dates and periods,
   appointments, billing months/years, tax years, transactions, dates paid,
   referenced 'communicated on' dates, proposed dates, renewals, expirations,
   travel and flight dates, court dates, ticket dates, available-on/by dates,
   closings. Keep the exact printed label with each. One document may carry
   many dates -- never collapse them or omit one because another seems more
   important. When the document states a consequence for missing a date
   (late fee, levy, lien, cancellation, foreclosure), record it with that date.
4. MONEY MUST BE COMPUTABLE. For every amount give BOTH the verbatim string
   (with its $ and any CR/DR marks) AND `amount_numeric` -- digits only, one
   optional decimal point, a leading minus for anything that reduces what is
   owed (credits, refunds, payments received). No symbols, no commas. Classify
   each with `amount_role`, and set `sign_as_printed` to 'credit' when the
   document marks it CR, parenthesised, or otherwise as a reduction. If an
   amount cannot be parsed to a single number, leave `amount_numeric` null --
   never guess a figure.
5. EVERY ITEMIZED ROW GOES IN line_items[], one entry per printed row, in
   printed order. Never summarise, never skip rows, never merge two rows.
   Capture the merchant/payee separately from the description when they are
   separable, plus any reference or transaction id, and the running balance
   printed on that row. A 40-row credit-card statement produces 40 entries.
6. RECORD WHO PAID. `payment_instruments[]` for checks, card payments,
   transfers and autopay -- including check number, memo line, and the name
   printed on the instrument. `account_holder_names[]` for every name shown as
   an owner of the account or obligation. `transaction_parties` for who owed
   and who was paid. All names VERBATIM: 'ROBERT N DOBBINS' and 'ROBERT ALDEN
   DOBBINS' are different people until proven otherwise.
   ⚠ NEVER record more than the last 4 digits of a card or bank account in the
   `*_last4` fields. Full account numbers belong only in `account_numbers[]`,
   where they appear exactly as printed.
7. ADDRESSES CARRY ROLES. For each address, say what it IS -- service address,
   mailing address, property taxed, collateral, remit-to, return address. The
   same street can be a service address on one bill and a mailing address on
   another, and only the role makes an expense attributable to a property.
8. TRANSCRIBE ALL HANDWRITING, stamps, and highlights verbatim, with page and
   location. 'illegible' is a valid transcription; an invented one is not.
9. RECORD DUPLICATE INDICATORS: print-shop footer codes, mailing batch numbers,
   COPY stamps.
10. NOTE PAGINATION EVIDENCE ('Page 2 of 6') and say when referenced pages are
   not present.

## Image annotation prompt (if the UI takes one for Annotate Images)

Image regions from scanned financial/legal paperwork. Classify each region,
transcribe every legible word verbatim (including inside logos, stamps, and
handwriting), and name the organization when a logo, letterhead, or seal
identifies one -- that identification often beats the body text. Note dates on
stamps. Say 'illegible' rather than guessing.
