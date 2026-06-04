#!/usr/bin/env python3
"""
Universal bank statement importer.

Auto-detects format and parses:
  - AEB43 / N43 / CSB-43 (text, fixed-width)  -> csb43
  - CSV with Spanish bank headers (date, concept, amount, balance) -> pandas
  - XLS / XLSX bank statement                  -> pandas

Routes the resulting statement lines into the right Odoo journal by IBAN match.

Usage:
    python3 bank_importer.py --file /path/to/extract.csv

Exit codes:
  0  -> imported OK (printed STATEMENT_RESULTS=...)
  10 -> validation failed (no journal match, no parseable rows, etc.)
  30 -> ORM error
  40 -> bad input
"""
# === pipeline isolation guard (auto-injected) ===
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
try:
    import companies as _comp_guard
    if getattr(_comp_guard, "PIPELINE_NAME", None) != 'austral':
        raise RuntimeError(
            f"PIPELINE_MISMATCH: script {__file__} expected pipeline='austral' "
            f"but loaded companies.PIPELINE_NAME={getattr(_comp_guard, 'PIPELINE_NAME', None)!r}"
        )
except ImportError:
    pass  # script sin dependencia de companies.py (e.g. drive_ops)
# === end isolation guard ===
try:
    _AUSTRAL_COMPANY_ID = int(_comp_guard.COMPANIES[0]["odoo_company_id"])
except Exception:
    _AUSTRAL_COMPANY_ID = 4


import argparse
import io
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam_test"

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

import pandas as pd  # noqa: E402

log = logging.getLogger("bank_importer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

IBAN_RE = re.compile(r"\bES[\s\d]{20,28}\b", re.IGNORECASE)
DATE_HEADER_PATTERNS = [
    r"^fecha", r"^date", r"^día", r"^dia", r"^operacion", r"^operación",
    r"^f\.?\s*operac", r"^f\.?\s*valor", r"^f\.?\s*contable", r"^f\.?\s*mov",
]
AMOUNT_HEADER_PATTERNS = [r"^importe", r"^amount", r"^cantidad", r"^euros"]
CONCEPT_HEADER_PATTERNS = [r"^concepto", r"^movimiento", r"^descripcion", r"^descripción"]
BALANCE_HEADER_PATTERNS = [r"^saldo", r"^balance"]


def _normalize_acc(s: str) -> str:
    return "".join(c for c in (s or "") if c.isalnum()).upper()


def _find_iban_in_text(text: str) -> str | None:
    m = IBAN_RE.search(text)
    if m:
        return _normalize_acc(m.group(0))
    # Fallback: numero de cuenta CCC desnudo (sin prefijo ES), 16-24 digitos
    # (ej. ABANCA "Numero de cuenta 20801202515500000632"). Evita capturar
    # importes/referencias buscando runs largos de digitos contiguos.
    for mm in re.finditer(r"(\d[\d\s]{14,28}\d)", text):
        digits = re.sub(r"\s", "", mm.group(1))
        if 16 <= len(digits) <= 24:
            return digits
    return None


def _parse_amount_es(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return 0.0
    s = s.replace("\xa0", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_date_es(val) -> date | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date()
    s = str(val).strip()
    # Recortar componente horario: "2026-05-29 01:59:13" / "29/05/2026 8:00" / ISO con T
    s = s.replace("T", " ").split(" ")[0].strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _detect_format(file_path: Path, raw: bytes) -> str:
    """Return 'n43', 'xls', 'xlsx', or 'csv'."""
    head = raw[:8]
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    text = raw[:200].decode("utf-8", errors="ignore").strip()
    if re.match(r"^1[14]\s*ES?\d{8,}", text):
        return "n43"
    suffix = file_path.suffix.lower()
    if suffix in (".n43", ".csb"):
        return "n43"
    if suffix in (".xls",):
        return "xls"
    if suffix in (".xlsx",):
        return "xlsx"
    return "csv"


def _detect_column_indices(df: pd.DataFrame) -> tuple[int, int, int, int]:
    """Find the columns for date, concept, amount, balance.
    Returns -1 if not found for that role."""
    date_i = concept_i = amount_i = balance_i = -1

    for header_row in range(min(25, len(df))):
        labels = [str(v).strip().lower() if v is not None else "" for v in df.iloc[header_row].tolist()]
        guess_date = guess_concept = guess_amount = guess_balance = -1
        for i, label in enumerate(labels):
            if guess_date < 0 and any(re.match(p, label) for p in DATE_HEADER_PATTERNS):
                guess_date = i
            elif guess_concept < 0 and any(re.match(p, label) for p in CONCEPT_HEADER_PATTERNS):
                guess_concept = i
            elif guess_amount < 0 and any(re.match(p, label) for p in AMOUNT_HEADER_PATTERNS):
                guess_amount = i
            elif guess_balance < 0 and any(re.match(p, label) for p in BALANCE_HEADER_PATTERNS):
                guess_balance = i
        if guess_date >= 0 and guess_amount >= 0:
            return header_row, guess_date, guess_concept, guess_amount, guess_balance
    return -1, -1, -1, -1, -1


def _read_xls_robusto(raw: bytes):
    """Lee .xls probando xlrd; si falla (muchos export bancarios son HTML
    disfrazado de .xls) prueba read_html y luego openpyxl."""
    import pandas as pd, io
    # 0) xlrd DIRECTO (bypassa el check de version pandas<->xlrd; xlrd 1.2.0 lee .xls binario)
    try:
        import xlrd as _xlrd
        _wb = _xlrd.open_workbook(file_contents=raw)
        _sh = _wb.sheet_by_index(0)
        _rows = []
        for _r in range(_sh.nrows):
            _row = []
            for _c in range(_sh.ncols):
                _cell = _sh.cell(_r, _c)
                _v = _cell.value
                # convertir fechas serial Excel -> ISO
                if _cell.ctype == _xlrd.XL_CELL_DATE:
                    try:
                        _dt = _xlrd.xldate_as_datetime(_v, _wb.datemode)
                        _v = _dt.strftime('%Y-%m-%d')
                    except Exception:
                        _v = str(_v)
                _row.append('' if _v is None else str(_v))
            _rows.append(_row)
        if _rows:
            return pd.DataFrame(_rows, dtype=str)
    except Exception:
        pass
    # 1) xlrd via pandas (xls binario real, si la version casa)
    try:
        return pd.read_excel(io.BytesIO(raw), engine="xlrd", header=None, dtype=str)
    except Exception:
        pass
    # 2) HTML disfrazado de .xls (común en bancos espanoles)
    try:
        tables = pd.read_html(io.BytesIO(raw), header=None)
        if tables:
            return tables[0].astype(str)
    except Exception:
        pass
    # 3) openpyxl (por si es xlsx con extension .xls)
    try:
        return pd.read_excel(io.BytesIO(raw), engine="openpyxl", header=None, dtype=str)
    except Exception as e:
        raise ValueError(f"no se pudo leer .xls (xlrd/html/openpyxl fallaron): {e}")


def _parse_csv_or_xls(file_path: Path, raw: bytes, fmt: str) -> dict:
    """Returns {iban, transactions: [{date, concept, amount, balance}], balance_start, balance_end}."""
    if fmt == "csv":
        df = pd.read_csv(io.BytesIO(raw), header=None, dtype=str, encoding="utf-8", on_bad_lines="skip")
    elif fmt == "xls":
        df = _read_xls_robusto(raw)
    elif fmt == "xlsx":
        df = pd.read_excel(io.BytesIO(raw), engine="openpyxl", header=None, dtype=str)
    else:
        raise ValueError(f"unsupported format {fmt}")

    flat_text = "\n".join(" | ".join(str(v) for v in row if v is not None) for row in df.head(25).values.tolist())
    iban = _find_iban_in_text(flat_text)

    # Detectar exports de facturas (no son extractos bancarios)
    _low = flat_text.lower()
    _factura_signals = sum(s in _low for s in ['factura', 'cliente', 'nif', 'articulos', 'base imp'])
    if _factura_signals >= 3 and 'saldo' not in _low:
        raise ValueError("NO_ES_BANCO: el archivo parece un export de facturas de venta "
                         "(columnas FACTURA/CLIENTE/NIF), no un extracto bancario")

    header_row, date_i, concept_i, amount_i, balance_i = _detect_column_indices(df)
    if header_row < 0 or date_i < 0 or amount_i < 0:
        raise ValueError(f"could not detect columns. Header text: {flat_text[:300]!r}")

    transactions = []
    balance_start = None
    balance_end = None
    last_balance = None

    for idx in range(header_row + 1, len(df)):
        row = df.iloc[idx]
        d = _parse_date_es(row.iloc[date_i])
        if not d:
            continue
        amt = _parse_amount_es(row.iloc[amount_i])
        if amt == 0 and not str(row.iloc[amount_i]).strip().startswith("0"):
            continue
        concept = ""
        if concept_i >= 0:
            concept = str(row.iloc[concept_i] or "").strip()
            if concept_i + 1 < len(row):
                extra_label = str(df.iloc[header_row, concept_i + 1] or "").strip().lower()
                if extra_label and not any(re.match(p, extra_label) for p in (AMOUNT_HEADER_PATTERNS + BALANCE_HEADER_PATTERNS)):
                    extra = str(row.iloc[concept_i + 1] or "").strip()
                    if extra:
                        concept = f"{concept} | {extra}"
        balance_val = _parse_amount_es(row.iloc[balance_i]) if balance_i >= 0 else None
        if balance_val is not None:
            last_balance = balance_val
        transactions.append({
            "date": d,
            "concept": concept,
            "amount": amt,
            "balance": balance_val,
        })

    if transactions and balance_i >= 0:
        first_bal = transactions[-1]["balance"]
        if first_bal is not None:
            balance_start = first_bal - transactions[-1]["amount"]
        balance_end = transactions[0]["balance"]

    return {"iban": iban, "transactions": transactions,
            "balance_start": balance_start, "balance_end": balance_end}


def _parse_n43(raw: bytes) -> dict:
    from csb43 import aeb43
    batch = aeb43.read_batch(io.BytesIO(raw))
    transactions = []
    iban = None
    balance_start = balance_end = None
    for account in batch.accounts:
        if iban is None:
            iban = _normalize_acc(account.account_number or "")
        balance_start = float(account.initial_balance)
        for tx in account.transactions or []:
            txt_bits = []
            for item in tx.optional_items or []:
                t1 = (getattr(item, "item_1", "") or "").strip()
                t2 = (getattr(item, "item_2", "") or "").strip()
                if t1: txt_bits.append(t1)
                if t2: txt_bits.append(t2)
            transactions.append({
                "date": tx.transaction_date,
                "concept": " ".join(txt_bits) or "Movimiento",
                "amount": float(tx.amount),
                "balance": None,
            })
        balance_end = balance_start + sum(t["amount"] for t in transactions)
    return {"iban": iban, "transactions": transactions,
            "balance_start": balance_start, "balance_end": balance_end}


def _find_journal(env, iban_hint: str | None):
    if iban_hint:
        clean = _normalize_acc(iban_hint)
        for j in env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", _AUSTRAL_COMPANY_ID)]):
            if not j.bank_account_id:
                continue
            acc = _normalize_acc(j.bank_account_id.acc_number or "")
            if acc and (acc == clean or acc.endswith(clean[-10:]) or clean.endswith(acc[-10:])):
                return j
    return None


def import_file(file_path: Path) -> dict:
    raw = file_path.read_bytes()
    fmt = _detect_format(file_path, raw)
    log.info(f"detected format: {fmt}")
    if fmt == "n43":
        parsed = _parse_n43(raw)
    else:
        parsed = _parse_csv_or_xls(file_path, raw, fmt)

    iban = parsed.get("iban")
    txs = parsed.get("transactions", [])
    log.info(f"  parsed {len(txs)} transactions; iban hint: {iban}")
    if not txs:
        return {"file": str(file_path), "format": fmt, "error": "no transactions parsed",
                "transactions": 0}

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        journal = _find_journal(env, iban)
        if not journal:
            return {"file": str(file_path), "format": fmt, "error": f"no journal matches IBAN {iban}",
                    "iban_hint": iban, "transactions": len(txs)}

        company_id = journal.company_id.id
        min_date = min(t["date"] for t in txs)
        max_date = max(t["date"] for t in txs)
        balance_start = parsed.get("balance_start") or 0.0
        balance_end = parsed.get("balance_end")
        if balance_end is None:
            balance_end = balance_start + sum(t["amount"] for t in txs)

        statement_name = f"{journal.name} {min_date.isoformat()} a {max_date.isoformat()}"
        # Idempotencia: si ya existe un statement con mismo journal+nombre+nº lineas, no duplicar
        existing = env["account.bank.statement"].search([
            ("company_id", "=", company_id), ("journal_id", "=", journal.id),
            ("name", "=", statement_name),
        ], limit=1)
        if existing and len(existing.line_ids) == len(txs):
            return {"file": str(file_path), "format": fmt, "iban": iban,
                    "journal_id": journal.id, "statement_id": existing.id,
                    "lines": len(existing.line_ids), "duplicate": True,
                    "min_date": min_date.isoformat(), "max_date": max_date.isoformat()}
        statement = env["account.bank.statement"].with_company(company_id).create({
            "name": statement_name,
            "journal_id": journal.id,
            "date": max_date,
            "balance_start": balance_start,
            "balance_end_real": balance_end,
            "company_id": company_id,
        })
        line_ids = []
        for tx in txs:
            line_vals = {
                "statement_id": statement.id,
                "journal_id": journal.id,
                "date": tx["date"],
                "payment_ref": (tx["concept"][:120] or "Movimiento"),
                "amount": tx["amount"],
                "company_id": company_id,
            }
            line = env["account.bank.statement.line"].with_company(company_id).create(line_vals)
            line_ids.append(line.id)

        # Conciliación automática al importar (regla: todo extracto se concilia)
        try:
            import conciliacion_ops
            _rec = conciliacion_ops.cmd_auto_reconcile(env, company_id)
            auto_reconciled = _rec.get("auto_conciliadas", 0)
        except Exception as _e:
            log.warning(f"auto-reconcile post-import fallo: {_e}")
            auto_reconciled = 0

        company_name = journal.company_id.name
        journal_id = journal.id
        statement_id = statement.id
        cr.commit()

    return {
        "file": str(file_path),
        "format": fmt,
        "iban": iban,
        "journal_id": journal_id,
        "company": company_name,
        "statement_id": statement_id,
        "lines": len(line_ids),
        "auto_reconciled": auto_reconciled,
        "min_date": str(min_date),
        "max_date": str(max_date),
        "balance_start": balance_start,
        "balance_end": balance_end,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True)
    args = p.parse_args()

    fp = Path(args.file)
    if not fp.exists():
        log.error(f"file not found: {fp}")
        return 40

    try:
        result = import_file(fp)
    except Exception as e:
        log.exception("import error")
        print(f"ERROR={type(e).__name__}: {e}")
        return 30

    if result.get("error"):
        print(f"VALIDATION_ERROR={result['error']}")
        print(f"STATEMENT_RESULTS={json.dumps(result, ensure_ascii=False, default=str)}")
        return 10

    print(f"STATEMENT_RESULTS={json.dumps(result, ensure_ascii=False, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
