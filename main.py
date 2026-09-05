import os
import csv
import io
import base64
import json
from contextlib import asynccontextmanager

import openai
from fastapi import FastAPI, Request, File, UploadFile, Form, Body
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from database import init_db, get_db, get_est_now, unix_to_est
from crypto_service import (
    extract_tx_hash, verify_and_price_tx, get_current_price_usd,
    detect_symbol_from_input, symbol_chain_family,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

MANUAL_METHODS = {"CASHAPP", "VENMO", "ZELLE", "CASH", "OTHER"}
PAID_STATUSES = ("PAID", "MANUALLY_PAID")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def db_cursor(conn):
    return conn.cursor()


def safe_json(value) -> str:
    """json.dumps, but with '</' escaped so a contractor/team name can never
    prematurely close the <script> tag it's embedded in."""
    return json.dumps(value, default=str).replace("</", "<\\/")


def money(v) -> float:
    return round(float(v or 0), 2)


# --------------------------------------------------------------------------
# Shared SQL
# --------------------------------------------------------------------------

LEDGER_SELECT = """
    SELECT l.id, l.week_date, c.id AS contractor_id, c.name AS contractor,
           c.credit_balance,
           l.amount_owed_usd,
           COALESCE(l.credit_applied_usd, 0) AS credit_applied_usd,
           (l.amount_owed_usd - COALESCE(l.credit_applied_usd, 0)) AS net_due_usd,
           COALESCE(l.status, 'UNPAID') AS status,
           l.paid_at_est, l.crypto_tx_id, l.crypto_symbol,
           COALESCE(l.payment_method, UPPER(l.crypto_symbol)) AS payment_method,
           l.amount_paid_usd, l.notes
    FROM weekly_ledgers l
    JOIN contractors c ON l.contractor_id = c.id
"""

LEDGER_ORDER = """
    ORDER BY
        CASE WHEN UPPER(COALESCE(l.status, 'UNPAID')) IN ('PAID', 'MANUALLY_PAID') THEN 1 ELSE 0 END ASC,
        c.name ASC
"""


def fetch_ledgers_for_week(cursor, week_date: str):
    cursor.execute(LEDGER_SELECT + " WHERE l.week_date = %s " + LEDGER_ORDER, (week_date,))
    return cursor.fetchall()


# --------------------------------------------------------------------------
# Advance credit helpers
#
# A contractor who overpays builds a credit balance. Every new week, and
# whenever credit is added, that balance is drawn down against their unpaid
# rows (oldest first) until it runs out. Every movement is written to
# credit_transactions so the balance is always explainable.
# --------------------------------------------------------------------------

def _source_sql(source_kind: str):
    """Returns (balance_table, fk_column) for 'contractor' or 'team'."""
    if source_kind == "team":
        return "teams", "team_id"
    return "contractors", "contractor_id"


def add_credit(cursor, source_kind: str, source_id: int, amount: float, reason: str, ledger_id=None):
    """Adds (or, if negative, removes) advance balance for a contractor or a
    team, and writes the movement to credit_transactions."""
    amount = money(amount)
    if amount == 0:
        return
    table, fk = _source_sql(source_kind)
    cursor.execute(f"UPDATE {table} SET credit_balance = COALESCE(credit_balance, 0) + %s WHERE id = %s", (amount, source_id))
    cursor.execute(
        f"""INSERT INTO credit_transactions ({fk}, amount_usd, reason, ledger_id, created_at_est)
            VALUES (%s, %s, %s, %s, %s)""",
        (source_id, amount, reason, ledger_id, get_est_now()),
    )


def get_balance(cursor, source_kind: str, source_id: int) -> float:
    table, _ = _source_sql(source_kind)
    cursor.execute(f"SELECT COALESCE(credit_balance, 0) AS bal FROM {table} WHERE id = %s", (source_id,))
    row = cursor.fetchone()
    return money(row["bal"]) if row else 0.0


def apply_credit_to_rows(cursor, source_kind: str, source_id: int, source_label: str, ledger_ids, max_amount: float = None):
    """Deducts from a contractor's OR a team's advance balance against the
    SPECIFIC unpaid rows the person chose, oldest first, up to max_amount
    (or the whole balance). Nothing here runs on its own. The caller is
    responsible for checking that the rows are eligible for this source
    (same contractor, or all members of the team). Returns
    (applied_total, touched_ids). A row whose net due reaches zero is
    marked PAID with method CREDIT."""
    available = get_balance(cursor, source_kind, source_id)
    if max_amount is not None:
        available = min(available, money(max_amount))
    if available <= 0 or not ledger_ids:
        return 0.0, []

    table, fk = _source_sql(source_kind)
    placeholders = ",".join(["%s"] * len(ledger_ids))
    cursor.execute(
        f"""SELECT id, week_date, amount_owed_usd, COALESCE(credit_applied_usd, 0) AS credit_applied_usd
            FROM weekly_ledgers
            WHERE id IN ({placeholders})
              AND UPPER(COALESCE(status, 'UNPAID')) NOT IN ('PAID', 'MANUALLY_PAID')
            ORDER BY id ASC""",
        list(ledger_ids),
    )
    applied_total, touched = 0.0, []
    for led in cursor.fetchall():
        if available <= 0:
            break
        net_due = money(float(led["amount_owed_usd"]) - float(led["credit_applied_usd"]))
        if net_due <= 0:
            continue
        apply_amt = min(available, net_due)
        new_applied = money(float(led["credit_applied_usd"]) + apply_amt)
        if money(net_due - apply_amt) == 0:
            cursor.execute(
                """UPDATE weekly_ledgers
                   SET credit_applied_usd = %s, status = 'PAID', payment_method = 'CREDIT',
                       amount_paid_usd = amount_owed_usd, paid_at_est = %s,
                       notes = CONCAT_WS(' ', notes, %s)
                   WHERE id = %s""",
                (new_applied, get_est_now(), f"Settled from {source_label} advance balance.", led["id"]),
            )
        else:
            cursor.execute("UPDATE weekly_ledgers SET credit_applied_usd = %s WHERE id = %s", (new_applied, led["id"]))
        cursor.execute(f"UPDATE {table} SET credit_balance = COALESCE(credit_balance, 0) - %s WHERE id = %s", (apply_amt, source_id))
        cursor.execute(
            f"""INSERT INTO credit_transactions ({fk}, amount_usd, reason, ledger_id, created_at_est)
                VALUES (%s, %s, %s, %s, %s)""",
            (source_id, -apply_amt, f"Deducted against {led['week_date']}", led["id"], get_est_now()),
        )
        available = money(available - apply_amt)
        applied_total = money(applied_total + apply_amt)
        touched.append(led["id"])
    return applied_total, touched


def team_covers_rows(cursor, team_id: int, ledger_ids) -> bool:
    """True if every selected row's contractor is a member of the team."""
    placeholders = ",".join(["%s"] * len(ledger_ids))
    cursor.execute(
        f"""SELECT COUNT(*) AS outsiders FROM weekly_ledgers l
            WHERE l.id IN ({placeholders})
              AND l.contractor_id NOT IN (SELECT contractor_id FROM team_members WHERE team_id = %s)""",
        list(ledger_ids) + [team_id],
    )
    return (cursor.fetchone() or {}).get("outsiders", 1) == 0


# --------------------------------------------------------------------------
# Page + read endpoints
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, selected_week: str = None):
    ledgers, contractors, weeks, teams = [], [], [], []

    # Each block is independent on purpose: one failing query must never
    # blank out unrelated data (teams, contractors) further down.
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)

        try:
            cursor.execute("SELECT week_date FROM weekly_ledgers GROUP BY week_date ORDER BY MAX(id) DESC")
            weeks = [w["week_date"] for w in cursor.fetchall()]
        except Exception as e:
            print(f"Error fetching weeks: {e}")

        if not selected_week and weeks:
            selected_week = weeks[0]

        if selected_week:
            try:
                ledgers = fetch_ledgers_for_week(cursor, selected_week)
            except Exception as e:
                print(f"Error fetching ledgers for week '{selected_week}': {e}")

        try:
            cursor.execute("SELECT id, name, COALESCE(credit_balance, 0) AS credit_balance FROM teams ORDER BY name ASC")
            teams = [{"id": t["id"], "name": t["name"], "credit_balance": money(t["credit_balance"]), "members": []} for t in cursor.fetchall()]
            team_index = {t["id"]: t for t in teams}
            cursor.execute("""
                SELECT tm.team_id, c.name AS contractor_name
                FROM team_members tm JOIN contractors c ON tm.contractor_id = c.id
                ORDER BY c.name ASC
            """)
            for row in cursor.fetchall():
                if row["team_id"] in team_index:
                    team_index[row["team_id"]]["members"].append(row["contractor_name"])
        except Exception as e:
            print(f"Error fetching teams: {e}")

        try:
            cursor.execute("SELECT id, name, COALESCE(credit_balance, 0) AS credit_balance FROM contractors ORDER BY name ASC")
            contractors = cursor.fetchall()
        except Exception as e:
            print(f"Error fetching contractors: {e}")
    except Exception as e:
        print(f"Error connecting to database: {e}")
    finally:
        if conn:
            conn.close()

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "ledgers_json": safe_json(ledgers),
            "teams_json": safe_json(teams),
            "contractors_json": safe_json(contractors),
            "weeks_json": safe_json(weeks),
            "selected_week": selected_week or "",
        },
    )


@app.get("/api/ledgers")
async def api_ledgers(week_date: str = None):
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        if not week_date:
            cursor.execute("SELECT week_date FROM weekly_ledgers GROUP BY week_date ORDER BY MAX(id) DESC LIMIT 1")
            row = cursor.fetchone()
            week_date = row["week_date"] if row else None
        ledgers = fetch_ledgers_for_week(cursor, week_date) if week_date else []
        cursor.execute("SELECT id, name, COALESCE(credit_balance, 0) AS credit_balance FROM contractors ORDER BY name ASC")
        contractors = cursor.fetchall()
        cursor.execute("SELECT id, COALESCE(credit_balance, 0) AS credit_balance FROM teams")
        team_balances = {t["id"]: money(t["credit_balance"]) for t in cursor.fetchall()}
        return {"status": "success", "week_date": week_date, "ledgers": ledgers, "contractors": contractors,
                "team_balances": team_balances}
    finally:
        if conn:
            conn.close()


@app.get("/api/contractor-history/{contractor_id}")
async def contractor_history(contractor_id: int):
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        cursor.execute("SELECT id, name, COALESCE(credit_balance, 0) AS credit_balance FROM contractors WHERE id = %s", (contractor_id,))
        contractor = cursor.fetchone()
        if not contractor:
            return JSONResponse(status_code=404, content={"status": "error", "message": "Contractor not found."})

        cursor.execute(LEDGER_SELECT + " WHERE l.contractor_id = %s ORDER BY l.id DESC", (contractor_id,))
        history = cursor.fetchall()

        cursor.execute(
            """SELECT id, amount_usd, reason, ledger_id, created_at_est
               FROM credit_transactions WHERE contractor_id = %s ORDER BY id DESC LIMIT 50""",
            (contractor_id,),
        )
        credit_history = cursor.fetchall()

        total_owed = sum(float(r["amount_owed_usd"]) for r in history)
        total_paid = sum(float(r["amount_owed_usd"]) for r in history if (r["status"] or "").upper() in PAID_STATUSES)
        return {
            "status": "success",
            "contractor": contractor["name"],
            "contractor_id": contractor["id"],
            "credit_balance": money(contractor["credit_balance"]),
            "total_owed": money(total_owed),
            "total_paid": money(total_paid),
            "history": history,
            "credit_history": credit_history,
        }
    finally:
        if conn:
            conn.close()


@app.get("/api/export-csv")
async def export_csv(week_date: str):
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        rows = fetch_ledgers_for_week(cursor, week_date)
    finally:
        if conn:
            conn.close()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Contractor", "Owed (USD)", "Credit applied (USD)", "Net due (USD)", "Status",
                     "Paid via", "Amount paid (USD)", "Paid at (EST)", "Tx hash", "Notes"])
    for r in rows:
        writer.writerow([
            r["contractor"], f'{money(r["amount_owed_usd"]):.2f}', f'{money(r["credit_applied_usd"]):.2f}',
            f'{money(r["net_due_usd"]):.2f}', r["status"], r["payment_method"] or "",
            f'{money(r["amount_paid_usd"]):.2f}' if r["amount_paid_usd"] is not None else "",
            r["paid_at_est"] or "", r["crypto_tx_id"] or "", r["notes"] or "",
        ])
    buffer.seek(0)
    safe_name = "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in week_date).strip() or "ledger"
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{safe_name}.csv"'})


@app.get("/api/live-price")
async def live_price(symbol: str):
    """Display estimate only — reconciliation always prices at send-time."""
    try:
        return {"status": "success", "symbol": symbol.upper(), "price_usd": get_current_price_usd(symbol)}
    except ValueError as err:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(err)})


# --------------------------------------------------------------------------
# Upload / OCR ingestion
# --------------------------------------------------------------------------

def insert_week_rows(cursor, week_date: str, records, mappings=None):
    mappings = mappings or {}
    for r in records:
        raw_name = str(r["contractor"]).strip().lower()
        final_name = mappings.get(raw_name, raw_name).strip().lower()
        amount = float(r["profits"])
        cursor.execute("INSERT INTO contractors (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (final_name,))
        cursor.execute("SELECT id FROM contractors WHERE name = %s", (final_name,))
        cid = cursor.fetchone()["id"]
        cursor.execute(
            "INSERT INTO weekly_ledgers (week_date, contractor_id, amount_owed_usd, status, credit_applied_usd) VALUES (%s, %s, %s, 'UNPAID', 0)",
            (week_date, cid, amount),
        )


@app.post("/api/upload")
async def upload_weekly_image(file: UploadFile = File(...), week_date: str = Form(...), overwrite: bool = Form(False)):
    clean_week_date = week_date.strip()
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)

        cursor.execute("SELECT COUNT(*) AS cnt FROM weekly_ledgers WHERE week_date = %s", (clean_week_date,))
        if (cursor.fetchone() or {}).get("cnt", 0) > 0 and not overwrite:
            return JSONResponse(status_code=400, content={
                "status": "error",
                "message": f"'{clean_week_date}' already has data. Tick 'Replace existing week' to overwrite it.",
            })

        contents = await file.read()
        base64_image = base64.b64encode(contents).decode("utf-8")
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "Extract table data from image. Return JSON format: {\"records\": [{\"contractor\": \"lowercase name\", \"profits\": numeric_amount}]}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
            ]}],
        )
        records = json.loads(response.choices[0].message.content).get("records", [])

        cursor.execute("SELECT name FROM contractors")
        existing = [row["name"].lower() for row in cursor.fetchall()]
        unknown = []
        for r in records:
            n = str(r["contractor"]).strip().lower()
            if n not in existing and n not in unknown:
                unknown.append(n)
        if unknown:
            return JSONResponse({
                "status": "approval_required", "week_date": clean_week_date, "overwrite": overwrite,
                "unknown_contractors": unknown, "existing_contractors": sorted(existing), "records": records,
            })

        if overwrite:
            cursor.execute("DELETE FROM weekly_ledgers WHERE week_date = %s", (clean_week_date,))
        insert_week_rows(cursor, clean_week_date, records)
        conn.commit()
        return JSONResponse({"status": "success", "extracted_count": len(records), "week_date": clean_week_date})
    finally:
        if conn:
            conn.close()


@app.post("/api/confirm-upload")
async def confirm_upload(payload: dict = Body(...)):
    week_date = payload.get("week_date")
    records = payload.get("records", [])
    if not week_date or not records:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing confirmation payload."})
    clean_week_date = week_date.strip()
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        if payload.get("overwrite", False):
            cursor.execute("DELETE FROM weekly_ledgers WHERE week_date = %s", (clean_week_date,))
        insert_week_rows(cursor, clean_week_date, records, payload.get("mappings", {}))
        conn.commit()
        return {"status": "success", "extracted_count": len(records), "week_date": clean_week_date}
    finally:
        if conn:
            conn.close()


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------

def selection_totals(cursor, ledger_ids):
    placeholders = ",".join(["%s"] * len(ledger_ids))
    cursor.execute(
        f"""SELECT id, contractor_id, amount_owed_usd, COALESCE(credit_applied_usd, 0) AS credit_applied_usd,
                   COALESCE(status, 'UNPAID') AS status
            FROM weekly_ledgers WHERE id IN ({placeholders})""",
        ledger_ids,
    )
    rows = cursor.fetchall()
    unpaid = [r for r in rows if (r["status"] or "").upper() not in PAID_STATUSES]
    net_due = money(sum(float(r["amount_owed_usd"]) - float(r["credit_applied_usd"]) for r in unpaid))
    contractor_ids = {r["contractor_id"] for r in unpaid}
    return unpaid, net_due, contractor_ids


def allocate_paid_amounts(rows, total_paid: float):
    """Splits a payment across rows in proportion to each row's net due, so
    per-row 'amount paid' is meaningful even for a batch or a shortfall."""
    dues = [money(float(r["amount_owed_usd"]) - float(r["credit_applied_usd"])) for r in rows]
    total_due = sum(dues) or 1.0
    return {r["id"]: money(total_paid * (d / total_due)) for r, d in zip(rows, dues)}


@app.post("/api/mark-paid-manual")
async def mark_paid_manual(payload: dict = Body(...)):
    ledger_ids = [int(x) for x in payload.get("ledger_ids", [])]
    method = str(payload.get("method", "OTHER")).strip().upper()
    notes = str(payload.get("notes", "") or "").strip()
    override = bool(payload.get("override", False))
    bank_excess = bool(payload.get("bank_excess", False))
    try:
        amount_paid = money(payload.get("amount_paid"))
    except (TypeError, ValueError):
        amount_paid = 0.0

    if not ledger_ids:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Select at least one row first."})
    if method not in MANUAL_METHODS:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Choose how it was paid (Cash App, Venmo, Zelle, cash, or other)."})
    if amount_paid <= 0:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Enter the dollar amount that was received."})

    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        rows, total_due, contractor_ids = selection_totals(cursor, ledger_ids)
        if not rows:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Every selected row is already paid."})

        difference = money(amount_paid - total_due)
        tolerance = 0.01

        if difference < -tolerance and not override:
            return {"status": "mismatch", "total_owed": total_due, "received_usd": amount_paid,
                    "difference": difference, "method": method}

        excess_credited = 0.0
        if difference > tolerance:
            if bank_excess and len(contractor_ids) == 1:
                cid = next(iter(contractor_ids))
                add_credit(cursor, "contractor", cid, difference, f"Overpaid via {method} ({notes})" if notes else f"Overpaid via {method}")
                excess_credited = difference
            # With several contractors in one payment there's no fair way to
            # attribute the excess, so it's recorded in the notes instead.

        now_est = get_est_now()
        shares = allocate_paid_amounts(rows, amount_paid)
        for r in rows:
            row_notes = notes
            if difference < -tolerance:
                row_notes = f"Underpaid via {method} by ${abs(difference):.2f} across this payment — accepted anyway." + (f" Note: {notes}" if notes else "")
            elif difference > tolerance and not excess_credited:
                row_notes = f"Overpaid via {method} by ${difference:.2f} (not banked: multiple contractors in one payment)." + (f" Note: {notes}" if notes else "")
            elif excess_credited:
                row_notes = f"Overpaid via {method} by ${difference:.2f} — banked as advance credit." + (f" Note: {notes}" if notes else "")
            cursor.execute(
                """UPDATE weekly_ledgers
                   SET status = 'MANUALLY_PAID', paid_at_est = %s, notes = %s,
                       payment_method = %s, amount_paid_usd = %s
                   WHERE id = %s""",
                (now_est, row_notes or None, method, shares[r["id"]], r["id"]),
            )

        conn.commit()
        return {"status": "success", "method": method, "amount_paid": amount_paid, "total_owed": total_due,
                "difference": difference, "excess_credited": excess_credited, "paid_at_est": now_est}
    finally:
        if conn:
            conn.close()


@app.post("/api/reconcile-crypto")
async def reconcile_crypto(
    ledger_ids: list[int] = Body(...),
    tx_id: str = Body(...),
    symbol: str = Body(...),
    override: bool = Body(False),
    override_note: str = Body(""),
    bank_excess: bool = Body(False),
):
    if not ledger_ids or not tx_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Select rows and paste a transaction first."})

    # Detect the coin from the pasted link when it's unambiguous. Only
    # override across chain families: ETH/USDT/USDC/DAI all share
    # identical etherscan links, so a plain link can't distinguish them —
    # keep the specific token the dropdown already has in that case.
    detected = detect_symbol_from_input(tx_id)
    current = symbol.strip().upper()
    auto_corrected = bool(detected and symbol_chain_family(detected) != symbol_chain_family(current))
    effective_symbol = detected if auto_corrected else current

    try:
        clean_hash = extract_tx_hash(tx_id, effective_symbol)
    except ValueError as err:
        print(f"reconcile-crypto: hash extraction failed for '{tx_id}' ({effective_symbol}): {err}")
        return JSONResponse(status_code=400, content={"status": "error", "message": str(err)})

    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        placeholders = ",".join(["%s"] * len(ledger_ids))

        # Duplicate-tx guard: one hash may cover several rows in THIS call,
        # but must not already be attached to rows outside this selection.
        cursor.execute(
            f"SELECT DISTINCT week_date FROM weekly_ledgers WHERE crypto_tx_id = %s AND id NOT IN ({placeholders})",
            [clean_hash] + ledger_ids,
        )
        dup_weeks = [r["week_date"] for r in cursor.fetchall()]
        if dup_weeks:
            return JSONResponse(status_code=400, content={
                "status": "error",
                "message": f"This transaction was already used for {', '.join(dup_weeks)}. Each on-chain payment can only be applied once.",
            })

        rows, total_due, contractor_ids = selection_totals(cursor, ledger_ids)
        if not rows:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Every selected row is already paid."})

        try:
            verification = verify_and_price_tx(clean_hash, effective_symbol)
        except ValueError as err:
            print(f"reconcile-crypto: verification failed for {effective_symbol} tx {clean_hash}: {err}")
            return JSONResponse(status_code=400, content={"status": "error", "message": str(err)})
        except Exception as err:
            print(f"reconcile-crypto: UNEXPECTED error verifying {effective_symbol} tx {clean_hash}: {err}")
            return JSONResponse(status_code=500, content={"status": "error", "message": f"Unexpected verification error: {err}"})

        received = money(verification["usd_value"])
        difference = money(received - total_due)
        tolerance = max(1.0, total_due * 0.01)  # 1%, $1 floor
        is_match = abs(difference) <= tolerance

        common = {
            "total_owed": total_due, "received_usd": received, "difference": difference,
            "price_usd_used": round(verification["price_usd_used"], 6),
            "confirmations": verification["confirmations"], "explorer_url": verification["explorer_url"],
            "tx_hash": verification["tx_hash"], "symbol_used": effective_symbol, "symbol_auto_corrected": auto_corrected,
            "crypto_amount": verification["amount"],
        }

        if not (is_match or override):
            return {"status": "mismatch", **common}

        paid_at_est = unix_to_est(verification["timestamp"])
        override_applied = override and not is_match
        excess_credited = 0.0
        notes_value = None
        if override_applied:
            if difference > 0:
                if bank_excess and len(contractor_ids) == 1:
                    cid = next(iter(contractor_ids))
                    add_credit(cursor, "contractor", cid, difference, f"Overpaid via {effective_symbol} tx {clean_hash[:12]}…")
                    excess_credited = difference
                    notes_value = f"Overpaid by ${difference:.2f} (received ${received:.2f} of ${total_due:.2f}) — banked as advance credit."
                else:
                    notes_value = f"Overpaid by ${difference:.2f} (received ${received:.2f} of ${total_due:.2f}) — accepted."
            else:
                notes_value = f"Underpaid by ${abs(difference):.2f} (received ${received:.2f} of ${total_due:.2f}) — accepted anyway."
            if override_note.strip():
                notes_value += f" Note: {override_note.strip()}"

        shares = allocate_paid_amounts(rows, received)
        for r in rows:
            cursor.execute(
                """UPDATE weekly_ledgers
                   SET status = 'PAID', paid_at_est = %s, crypto_tx_id = %s, crypto_symbol = %s,
                       payment_method = %s, amount_paid_usd = %s,
                       notes = COALESCE(%s, notes)
                   WHERE id = %s""",
                (paid_at_est, clean_hash, effective_symbol, effective_symbol, shares[r["id"]], notes_value, r["id"]),
            )
        conn.commit()
        return {"status": "matched", "override_applied": override_applied, "excess_credited": excess_credited,
                "paid_at_est": paid_at_est, **common}
    finally:
        if conn:
            conn.close()


# --------------------------------------------------------------------------
# Advance credit endpoints
# --------------------------------------------------------------------------

def _parse_source(payload):
    """A credit source is either {"contractor_id": n} or {"team_id": n}."""
    if payload.get("team_id"):
        return "team", int(payload["team_id"])
    if payload.get("contractor_id"):
        return "contractor", int(payload["contractor_id"])
    return None, None


def _source_label(cursor, kind, source_id):
    table, _ = _source_sql(kind)
    cursor.execute(f"SELECT name FROM {table} WHERE id = %s", (source_id,))
    row = cursor.fetchone()
    return (row["name"] if row else kind)


@app.post("/api/credits/add")
async def credits_add(payload: dict = Body(...)):
    kind, source_id = _parse_source(payload)
    note = str(payload.get("note", "") or "").strip()
    try:
        amount = money(payload.get("amount"))
    except (TypeError, ValueError):
        amount = 0.0
    if not kind or amount <= 0:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Pick a contractor or team and enter an amount above $0."})
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        add_credit(cursor, kind, source_id, amount, f"Extra received{': ' + note if note else ''}")
        bal = get_balance(cursor, kind, source_id)
        conn.commit()
        return {"status": "success", "credit_balance": bal, "source": kind, "source_id": source_id}
    finally:
        if conn:
            conn.close()


@app.post("/api/credits/adjust")
async def credits_adjust(payload: dict = Body(...)):
    """Manual correction of a balance (e.g. contractor was refunded in cash)."""
    kind, source_id = _parse_source(payload)
    note = str(payload.get("note", "") or "").strip()
    try:
        amount = money(payload.get("amount"))  # may be negative
    except (TypeError, ValueError):
        amount = 0.0
    if not kind or amount == 0:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Enter a non-zero adjustment."})
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        add_credit(cursor, kind, source_id, amount, f"Manual adjustment{': ' + note if note else ''}")
        bal = get_balance(cursor, kind, source_id)
        conn.commit()
        return {"status": "success", "credit_balance": bal}
    finally:
        if conn:
            conn.close()


@app.post("/api/credits/apply")
async def credits_apply(payload: dict = Body(...)):
    """Explicitly deduct from a contractor's or a team's advance balance
    against the rows the person selected. For a contractor source every
    row must be theirs; for a team source every row's contractor must be a
    member of that team."""
    ledger_ids = [int(x) for x in payload.get("ledger_ids", [])]
    max_amount = payload.get("amount")
    kind, source_id = _parse_source(payload)
    if not ledger_ids:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Select the rows to deduct against first."})
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        rows, total_due, contractor_ids = selection_totals(cursor, ledger_ids)
        if not rows:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Every selected row is already settled."})

        if not kind:
            # Default: the single contractor in the selection.
            if len(contractor_ids) != 1:
                return JSONResponse(status_code=400, content={"status": "error", "message": "Choose a team balance, or select rows for just one contractor."})
            kind, source_id = "contractor", next(iter(contractor_ids))
        if kind == "contractor" and (len(contractor_ids) != 1 or source_id not in contractor_ids):
            return JSONResponse(status_code=400, content={"status": "error", "message": "That contractor's balance can only pay their own rows."})
        if kind == "team" and not team_covers_rows(cursor, source_id, [r["id"] for r in rows]):
            return JSONResponse(status_code=400, content={"status": "error", "message": "Some selected rows belong to people who aren't on that team."})

        label = _source_label(cursor, kind, source_id)
        cap = money(max_amount) if max_amount not in (None, "") else None
        applied, touched = apply_credit_to_rows(cursor, kind, source_id, label, [r["id"] for r in rows], cap)
        bal = get_balance(cursor, kind, source_id)
        conn.commit()
        if applied <= 0:
            return JSONResponse(status_code=400, content={"status": "error", "message": f"No advance balance available on {label}."})
        return {"status": "success", "applied": applied, "rows_touched": len(touched), "credit_balance": bal,
                "remaining_due": money(total_due - applied), "source": kind, "source_id": source_id, "source_label": label}
    finally:
        if conn:
            conn.close()


@app.get("/api/team-credits/{team_id}")
async def team_credits(team_id: int):
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        cursor.execute("SELECT id, name, COALESCE(credit_balance, 0) AS credit_balance FROM teams WHERE id = %s", (team_id,))
        team = cursor.fetchone()
        if not team:
            return JSONResponse(status_code=404, content={"status": "error", "message": "Team not found."})
        cursor.execute(
            """SELECT id, amount_usd, reason, ledger_id, created_at_est FROM credit_transactions
               WHERE team_id = %s ORDER BY id DESC LIMIT 50""", (team_id,))
        return {"status": "success", "team": team["name"], "credit_balance": money(team["credit_balance"]), "credit_history": cursor.fetchall()}
    finally:
        if conn:
            conn.close()


# --------------------------------------------------------------------------
# Contractors / Teams / Weeks
# --------------------------------------------------------------------------

@app.post("/api/delete-contractor")
async def delete_contractor(payload: dict = Body(...)):
    contractor_id = payload.get("contractor_id")
    if not contractor_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Contractor ID is required."})
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        cursor.execute("DELETE FROM team_members WHERE contractor_id = %s", (contractor_id,))
        cursor.execute("DELETE FROM weekly_ledgers WHERE contractor_id = %s", (contractor_id,))
        cursor.execute("DELETE FROM contractors WHERE id = %s", (contractor_id,))
        deleted = cursor.rowcount
        conn.commit()
        return {"status": "success", "deleted": deleted}
    finally:
        if conn:
            conn.close()


@app.post("/api/delete-week")
async def delete_week(payload: dict = Body(...)):
    week_date = payload.get("week_date")
    if not week_date:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Week date is required."})
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        cursor.execute("DELETE FROM weekly_ledgers WHERE week_date = %s", (week_date.strip(),))
        deleted = cursor.rowcount
        conn.commit()
        return {"status": "success", "deleted_rows": deleted}
    finally:
        if conn:
            conn.close()


@app.post("/api/teams")
async def create_team(team_name: str = Body(...), contractor_names: list[str] = Body(...)):
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        cursor.execute("INSERT INTO teams (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (team_name,))
        cursor.execute("SELECT id FROM teams WHERE name = %s", (team_name,))
        team_id = cursor.fetchone()["id"]
        for name in contractor_names:
            n = name.strip().lower()
            cursor.execute("INSERT INTO contractors (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (n,))
            cursor.execute("SELECT id FROM contractors WHERE name = %s", (n,))
            cid = cursor.fetchone()["id"]
            cursor.execute("INSERT INTO team_members (team_id, contractor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (team_id, cid))
        conn.commit()
        return {"status": "success", "team_id": team_id, "credit_balance": 0.0}
    finally:
        if conn:
            conn.close()


@app.post("/api/teams/add-member")
async def add_team_member(payload: dict = Body(...)):
    team_id, name = payload.get("team_id"), payload.get("contractor_name")
    if not team_id or not name:
        return JSONResponse(status_code=400, content={"status": "error", "message": "team_id and contractor_name are required."})
    n = str(name).strip().lower()
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        cursor.execute("INSERT INTO contractors (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (n,))
        cursor.execute("SELECT id FROM contractors WHERE name = %s", (n,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Could not resolve contractor."})
        cursor.execute("INSERT INTO team_members (team_id, contractor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (team_id, row["id"]))
        conn.commit()
        return {"status": "success"}
    finally:
        if conn:
            conn.close()


@app.post("/api/teams/remove-member")
async def remove_team_member(payload: dict = Body(...)):
    team_id, name = payload.get("team_id"), payload.get("contractor_name")
    if not team_id or not name:
        return JSONResponse(status_code=400, content={"status": "error", "message": "team_id and contractor_name are required."})
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        cursor.execute("SELECT id FROM contractors WHERE name = %s", (str(name).strip().lower(),))
        row = cursor.fetchone()
        if not row:
            return {"status": "success", "removed": 0}
        cursor.execute("DELETE FROM team_members WHERE team_id = %s AND contractor_id = %s", (team_id, row["id"]))
        removed = cursor.rowcount
        conn.commit()
        return {"status": "success", "removed": removed}
    finally:
        if conn:
            conn.close()


@app.post("/api/delete-team")
async def delete_team(payload: dict = Body(...)):
    team_id = payload.get("team_id")
    if not team_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "team_id is required."})
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        cursor.execute("DELETE FROM teams WHERE id = %s", (team_id,))  # team_members cascade
        deleted = cursor.rowcount
        conn.commit()
        return {"status": "success", "deleted": deleted}
    finally:
        if conn:
            conn.close()
