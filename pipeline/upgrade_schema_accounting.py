# Adds the accounting/estate fields to mistral-annotation-schema.json.
# Idempotent: re-running just rewrites the same definitions.
# Backup written to mistral-annotation-schema.json.bak-preaccounting
import io
import json
import shutil

P_FILE = "mistral-annotation-schema.json"
shutil.copy(P_FILE, P_FILE + ".bak-preaccounting")
schema = json.load(io.open(P_FILE, encoding="utf-8"))
DOC = schema["properties"]["documents"]["items"]
P = DOC["properties"]

NUM = (
    "Normalized for arithmetic: digits with at most one decimal point, optional "
    "leading minus for credits, refunds and payments out. NO currency symbol, NO "
    "thousands separators. Example: -1234.56 . Null if not a single parseable amount."
)


def obj(props, desc=None):
    o = {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }
    if desc:
        o["description"] = desc
    return o


def arr(desc, props):
    return {"type": "array", "description": desc, "items": obj(props)}


# 1 - amounts become summable
P["amounts"] = arr(
    "Every labeled monetary amount. Give BOTH the verbatim string and a normalized "
    "numeric, so totals can be computed without re-parsing text.",
    {
        "label": {"type": "string", "description": "Verbatim label as printed."},
        "value": {
            "type": "string",
            "description": "Exactly as printed, including currency symbol and any CR/DR marks or parentheses.",
        },
        "amount_numeric": {"type": ["string", "null"], "description": NUM},
        "currency": {
            "type": ["string", "null"],
            "description": "ISO code if determinable, e.g. USD. Null if not shown.",
        },
        "amount_role": {
            "type": "string",
            "enum": [
                "total_due", "minimum_due", "previous_balance", "ending_balance",
                "payment_received", "credit", "refund", "new_charges", "subtotal",
                "tax", "late_fee", "interest", "principal", "escrow", "insurance",
                "penalty", "adjustment", "discount", "deposit", "withdrawal",
                "fee_other", "amount_paid", "amount_enclosed", "past_due",
                "credit_limit", "available_credit", "payoff_amount", "assessed_value",
                "gross_pay", "net_pay", "withholding", "other",
            ],
            "description": "What this amount represents. Use other only when none fit.",
        },
        "sign_as_printed": {
            "type": "string",
            "enum": ["debit", "credit", "unspecified"],
            "description": "credit if marked CR, shown in parentheses, or otherwise indicated as a reduction.",
        },
    },
)

# 2 - line items
P["line_items"] = arr(
    "Individual transaction or charge rows from statements, invoices, itemized bills "
    "and ledgers. One entry per printed row, in printed order. Empty array if the "
    "document has no itemized rows.",
    {
        "line_date": {
            "type": ["string", "null"],
            "description": "Date on the row. ISO 8601 if unambiguous, else verbatim. Null if none.",
        },
        "posted_date": {
            "type": ["string", "null"],
            "description": "Separate posting or settlement date, if the row shows two dates.",
        },
        "description": {"type": "string", "description": "Row description exactly as printed."},
        "merchant_or_payee": {
            "type": ["string", "null"],
            "description": "Merchant, payee or counterparty if separable from the description.",
        },
        "amount_as_printed": {"type": ["string", "null"], "description": "Row amount exactly as printed."},
        "amount_numeric": {"type": ["string", "null"], "description": NUM},
        "line_type": {
            "type": "string",
            "enum": [
                "charge", "payment", "credit", "refund", "fee", "interest",
                "transfer", "withdrawal", "deposit", "adjustment", "tax", "unknown",
            ],
        },
        "running_balance_after": {
            "type": ["string", "null"],
            "description": "Balance printed on this row, if the statement carries one.",
        },
        "reference_or_transaction_id": {
            "type": ["string", "null"],
            "description": "Transaction, confirmation or reference number printed on the row.",
        },
        "card_or_account_last4": {
            "type": ["string", "null"],
            "description": "Last 4 digits only, if the row identifies which card or account was used. Never record more than 4 digits here.",
        },
    },
)

# 3 - how money actually moved
P["payment_instruments"] = arr(
    "How money actually moved, where the document shows it: checks, card payments, "
    "transfers, autopay. This is the evidence of WHO paid. Empty array if none shown.",
    {
        "method": {
            "type": "string",
            "enum": [
                "check", "ach_transfer", "wire", "credit_card", "debit_card", "cash",
                "money_order", "cashiers_check", "autopay", "online_payment",
                "payroll_deduction", "escrow_disbursement", "other", "unknown",
            ],
        },
        "check_number": {"type": ["string", "null"], "description": "Check number, if a check or check copy."},
        "identifier_last4": {
            "type": ["string", "null"],
            "description": "Last 4 of the card or account used. Never more than 4 digits.",
        },
        "bank_or_issuer": {"type": ["string", "null"], "description": "Bank or card issuer name as printed."},
        "memo_line": {
            "type": ["string", "null"],
            "description": "Memo or FOR line, verbatim. Often states what the payment was for.",
        },
        "amount_numeric": {"type": ["string", "null"], "description": NUM},
        "payer_name_as_printed": {
            "type": ["string", "null"],
            "description": "Name printed on the instrument, verbatim. NEVER normalize or merge similar names.",
        },
    },
)

# 4 - who holds the account (estate vs personal hinge)
P["account_holder_names"] = {
    "type": "array",
    "description": (
        "Every name shown as an owner or holder of the account, policy or obligation "
        "on this document, VERBATIM and never normalized or merged. This is the "
        "estate-versus-personal hinge: it distinguishes the decedent's accounts from "
        "the family's."
    ),
    "items": {"type": "string"},
}

# 5 - who owed, who was paid
P["transaction_parties"] = obj(
    {
        "payer_name": {
            "type": ["string", "null"],
            "description": "Who owes or pays, verbatim. On a bill this is usually the addressee; on a check, the account holder.",
        },
        "payee_name": {
            "type": ["string", "null"],
            "description": "Who is owed or paid, verbatim. On a bill this is usually the issuer.",
        },
        "on_behalf_of": {
            "type": ["string", "null"],
            "description": "Third party the payment is for, if stated, e.g. Estate of ...",
        },
    },
    desc="Who owed and who was paid, as printed. Null where the document does not say.",
)

# 6 - addresses gain a role, so expenses can attach to a property
P["properties_or_addresses_referenced"] = arr(
    "Every property or address referenced, with the ROLE it plays. The role is what "
    "allows an expense to be attributed to a specific property.",
    {
        "address_as_printed": {"type": "string", "description": "Verbatim, including unit or suite."},
        "role": {
            "type": "string",
            "enum": [
                "service_address", "mailing_address", "property_taxed",
                "collateral_property", "remit_to_address", "return_address",
                "decedent_residence", "insured_property", "property_sold",
                "other", "unclear",
            ],
        },
    },
)

for key in (
    "amounts", "line_items", "payment_instruments", "account_holder_names",
    "transaction_parties", "properties_or_addresses_referenced",
):
    if key not in DOC["required"]:
        DOC["required"].append(key)

json.dump(schema, io.open(P_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

# strict structured-output dialect check
problems = []


def check(node, path="root"):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                problems.append(path + ": additionalProperties not false")
            props = set(node.get("properties", {}))
            req = set(node.get("required", []))
            if props != req:
                problems.append("%s: required != properties (missing %s)" % (path, sorted(props - req)))
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                check(v, path + "." + k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            check(v, "%s[%d]" % (path, i))


check(schema)
print("strict-dialect problems :", problems if problems else "NONE")
print("doc-level fields        :", len(DOC["properties"]))
print("doc-level required      :", len(DOC["required"]))
print("file size               :", len(io.open(P_FILE, encoding="utf-8").read()), "bytes")
