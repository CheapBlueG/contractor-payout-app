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

from database import init_db, get_db, get_est_now
from crypto_service import extract_tx_hash, verify_and_price_tx, get_current_price_usd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def db_cursor(conn):
    """Small helper so every endpoint reliably closes its connection, even
    on an exception, instead of leaking it (the original code only closed
    connections on the success path)."""
    return conn.cursor()


def safe_json(value) -> str:
    """json.dumps, but with '</' escaped so a contractor/team name can never
    prematurely close the <script> tag it's embedded in."""
    return json.dumps(value, default=str).replace("</", "<\\/")


# --------------------------------------------------------------------------
# Page + read endpoints
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, selected_week: str = None):
    ledgers, contractors, weeks = [], [], []
    teams_map = {}

    # Each block below is independent on purpose. The previous version ran
    # every query in one try/except, so if ANY single query failed (a bad
    # week_date, a transient DB blip, anything) execution stopped right
    # there and everything after it — including teams and contractors —
    # silently stayed empty, even though that data was totally fine. Now a
    # failure in one section can't blank out the others.
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
                cursor.execute("""
                    SELECT l.id, l.week_date, c.id as contractor_id, c.name as contractor,
                           l.amount_owed_usd, COALESCE(l.status, 'UNPAID') as status, l.paid_at_est, l.crypto_tx_id,
                           l.crypto_symbol, l.notes
                    FROM weekly_ledgers l
                    JOIN contractors c ON l.contractor_id = c.id
                    WHERE l.week_date = %s
                    ORDER BY
                        CASE WHEN UPPER(COALESCE(l.status, 'UNPAID')) LIKE '%PAID%' THEN 1 ELSE 0 END ASC,
                        c.name ASC
                """, (selected_week,))
                ledgers = cursor.fetchall()
            except Exception as e:
                print(f"Error fetching ledgers for week '{selected_week}': {e}")

        try:
            cursor.execute("""
                SELECT t.name as team_name, c.name as contractor_name
                FROM teams t
                JOIN team_members tm ON t.id = tm.team_id
                JOIN contractors c ON tm.contractor_id = c.id
                ORDER BY t.name ASC, c.name ASC
            """)
            for row in cursor.fetchall():
                teams_map.setdefault(row["team_name"], []).append(row["contractor_name"])
        except Exception as e:
            print(f"Error fetching teams: {e}")

        try:
            cursor.execute("SELECT id, name FROM contractors ORDER BY name ASC")
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
            "teams_json": safe_json(teams_map),
            "contractors_json": safe_json([{"id": c["id"], "name": c["name"]} for c in contractors]),
            "weeks_json": safe_json(weeks),
            "selected_week": selected_week or "",
        }
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

        ledgers = []
        if week_date:
            cursor.execute("""
                SELECT l.id, l.week_date, c.id as contractor_id, c.name as contractor,
                       l.amount_owed_usd, COALESCE(l.status, 'UNPAID') as status, l.paid_at_est, l.crypto_tx_id,
                       l.crypto_symbol, l.notes
                FROM weekly_ledgers l
                JOIN contractors c ON l.contractor_id = c.id
                WHERE l.week_date = %s
                ORDER BY
                    CASE WHEN UPPER(COALESCE(l.status, 'UNPAID')) LIKE '%PAID%' THEN 1 ELSE 0 END ASC,
                    c.name ASC
            """, (week_date,))
            ledgers = cursor.fetchall()

        return {"status": "success", "week_date": week_date, "ledgers": ledgers}
    finally:
        if conn:
            conn.close()


@app.get("/api/contractor-history/{contractor_id}")
async def contractor_history(contractor_id: int):
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)

        cursor.execute("SELECT id, name FROM contractors WHERE id = %s", (contractor_id,))
        contractor = cursor.fetchone()
        if not contractor:
            return JSONResponse(status_code=404, content={"status": "error", "message": "Contractor not found."})

        cursor.execute("""
            SELECT id, week_date, amount_owed_usd, status, paid_at_est, crypto_tx_id, crypto_symbol, notes
            FROM weekly_ledgers
            WHERE contractor_id = %s
            ORDER BY id DESC
        """, (contractor_id,))
        history = cursor.fetchall()

        total_paid = sum(float(r["amount_owed_usd"]) for r in history if "PAID" in (r["status"] or "").upper())
        total_owed = sum(float(r["amount_owed_usd"]) for r in history)

        return {
            "status": "success",
            "contractor": contractor["name"],
            "total_paid": round(total_paid, 2),
            "total_owed": round(total_owed, 2),
            "history": history,
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
        cursor.execute("""
            SELECT c.name as contractor, l.amount_owed_usd, l.status, l.paid_at_est,
                   l.crypto_tx_id, l.crypto_symbol, l.notes
            FROM weekly_ledgers l
            JOIN contractors c ON l.contractor_id = c.id
            WHERE l.week_date = %s
            ORDER BY c.name ASC
        """, (week_date,))
        rows = cursor.fetchall()
    finally:
        if conn:
            conn.close()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Contractor", "Amount (USD)", "Status", "Paid At (EST)", "Tx Hash", "Symbol", "Notes"])
    for r in rows:
        writer.writerow([
            r["contractor"],
            f'{float(r["amount_owed_usd"]):.2f}',
            r["status"],
            r["paid_at_est"] or "",
            r["crypto_tx_id"] or "",
            r["crypto_symbol"] or "",
            r["notes"] or "",
        ])
    buffer.seek(0)

    safe_name = "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in week_date).strip() or "ledger"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.csv"'},
    )


@app.get("/api/live-price")
async def live_price(symbol: str):
    """Live price for the frontend's '≈ amount owed in crypto' estimate
    only. Never used for reconciliation — actual payments are priced at
    the moment they were sent (see crypto_service.get_price_usd_at)."""
    try:
        price = get_current_price_usd(symbol)
        return {"status": "success", "symbol": symbol.upper(), "price_usd": price}
    except ValueError as err:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(err)})


# --------------------------------------------------------------------------
# Upload / OCR ingestion
# --------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_weekly_image(
    file: UploadFile = File(...),
    week_date: str = Form(...),
    overwrite: bool = Form(False),
):
    clean_week_date = week_date.strip()
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)

        cursor.execute("SELECT COUNT(*) as cnt FROM weekly_ledgers WHERE week_date = %s", (clean_week_date,))
        check_res = cursor.fetchone()

        if check_res and check_res["cnt"] > 0 and not overwrite:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": f"Data for '{clean_week_date}' already exists. Check 'Overwrite if week exists' or delete the range below.",
                },
            )

        contents = await file.read()
        base64_image = base64.b64encode(contents).decode("utf-8")

        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Extract table data from image. Return JSON format: {\"records\": [{\"contractor\": \"lowercase name\", \"profits\": numeric_amount}]}",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                        },
                    ],
                }
            ],
        )

        data = json.loads(response.choices[0].message.content)
        records = data.get("records", [])

        cursor.execute("SELECT name FROM contractors")
        existing_db_names = [row["name"].lower() for row in cursor.fetchall()]

        unknown_contractors = []
        for r in records:
            c_name = str(r["contractor"]).strip().lower()
            if c_name not in existing_db_names and c_name not in unknown_contractors:
                unknown_contractors.append(c_name)

        if unknown_contractors:
            return JSONResponse({
                "status": "approval_required",
                "week_date": clean_week_date,
                "overwrite": overwrite,
                "unknown_contractors": unknown_contractors,
                "existing_contractors": sorted(existing_db_names),
                "records": records,
            })

        if overwrite:
            cursor.execute("DELETE FROM weekly_ledgers WHERE week_date = %s", (clean_week_date,))

        for r in records:
            name = str(r["contractor"]).strip().lower()
            amount = float(r["profits"])

            cursor.execute("INSERT INTO contractors (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
            cursor.execute("SELECT id FROM contractors WHERE name = %s", (name,))
            cid = cursor.fetchone()["id"]

            cursor.execute("""
                INSERT INTO weekly_ledgers (week_date, contractor_id, amount_owed_usd, status)
                VALUES (%s, %s, %s, 'UNPAID')
            """, (clean_week_date, cid, amount))

        conn.commit()
        return JSONResponse({"status": "success", "extracted_count": len(records), "week_date": clean_week_date})
    finally:
        if conn:
            conn.close()


@app.post("/api/confirm-upload")
async def confirm_upload(payload: dict = Body(...)):
    week_date = payload.get("week_date")
    records = payload.get("records", [])
    mappings = payload.get("mappings", {})
    overwrite = payload.get("overwrite", False)

    if not week_date or not records:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing required confirmation payload."})

    clean_week_date = week_date.strip()
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)

        if overwrite:
            cursor.execute("DELETE FROM weekly_ledgers WHERE week_date = %s", (clean_week_date,))

        for r in records:
            raw_name = str(r["contractor"]).strip().lower()
            final_name = mappings.get(raw_name, raw_name).strip().lower()
            amount = float(r["profits"])

            cursor.execute("INSERT INTO contractors (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (final_name,))
            cursor.execute("SELECT id FROM contractors WHERE name = %s", (final_name,))
            cid = cursor.fetchone()["id"]

            cursor.execute("""
                INSERT INTO weekly_ledgers (week_date, contractor_id, amount_owed_usd, status)
                VALUES (%s, %s, %s, 'UNPAID')
            """, (clean_week_date, cid, amount))

        conn.commit()
        return {"status": "success", "extracted_count": len(records), "week_date": clean_week_date}
    finally:
        if conn:
            conn.close()


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------

@app.post("/api/mark-paid-manual")
async def mark_paid_manual(ledger_ids: list[int] = Body(...), notes: str = Body("")):
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        now_est = get_est_now()
        placeholders = ",".join(["%s"] * len(ledger_ids))
        cursor.execute(f"""
            UPDATE weekly_ledgers
            SET status = 'MANUALLY_PAID', paid_at_est = %s, notes = %s
            WHERE id IN ({placeholders})
        """, [now_est, notes] + ledger_ids)
        conn.commit()
        return {"status": "success"}
    finally:
        if conn:
            conn.close()


@app.post("/api/reconcile-crypto")
async def reconcile_crypto(
    ledger_ids: list[int] = Body(...),
    tx_id: str = Body(...),
    symbol: str = Body(...),
):
    if not ledger_ids or not tx_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing ledger selection or TxID."})

    # Accept a raw hash OR a pasted explorer link (any explorer) up front,
    # so the duplicate check and the on-chain lookup both key off the same
    # normalized hash.
    try:
        clean_hash = extract_tx_hash(tx_id, symbol)
    except ValueError as err:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(err)})

    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        placeholders = ",".join(["%s"] * len(ledger_ids))

        # Duplicate-tx guard: the same hash can legitimately cover multiple
        # ledger rows in *this* call (one payment split across several
        # contractors), but must not already be attached to rows outside
        # this selection from an earlier reconciliation.
        cursor.execute(
            f"SELECT DISTINCT week_date FROM weekly_ledgers WHERE crypto_tx_id = %s AND id NOT IN ({placeholders})",
            [clean_hash] + ledger_ids,
        )
        dup_weeks = [r["week_date"] for r in cursor.fetchall()]
        if dup_weeks:
            return JSONResponse(status_code=400, content={
                "status": "error",
                "message": f"This transaction has already been used to mark payment for: {', '.join(dup_weeks)}. Each on-chain payment can only be applied once.",
            })

        cursor.execute(f"SELECT SUM(amount_owed_usd) as total FROM weekly_ledgers WHERE id IN ({placeholders})", ledger_ids)
        res = cursor.fetchone()
        total_owed = float(res["total"]) if res and res["total"] else 0.0

        try:
            verification = verify_and_price_tx(clean_hash, symbol)
        except ValueError as err:
            return JSONResponse(status_code=400, content={"status": "error", "message": str(err)})
        except Exception as err:
            return JSONResponse(status_code=500, content={"status": "error", "message": f"Unexpected verification error: {str(err)}"})

        tx_usd_val = verification["usd_value"]
        # 1% tolerance with a $1 floor, instead of a flat $5 that doesn't
        # scale with the size of the payment.
        tolerance = max(1.0, total_owed * 0.01)

        if abs(tx_usd_val - total_owed) <= tolerance:
            now_est = get_est_now()
            cursor.execute(f"""
                UPDATE weekly_ledgers
                SET status = 'PAID', paid_at_est = %s, crypto_tx_id = %s, crypto_symbol = %s
                WHERE id IN ({placeholders})
            """, [now_est, clean_hash, symbol.upper()] + ledger_ids)
            conn.commit()
            return {
                "status": "matched",
                "total_owed": round(total_owed, 2),
                "received_usd": round(tx_usd_val, 2),
                "price_usd_used": round(verification["price_usd_used"], 6),
                "confirmations": verification["confirmations"],
                "explorer_url": verification["explorer_url"],
                "tx_hash": verification["tx_hash"],
            }
        else:
            return {
                "status": "mismatch",
                "total_owed": round(total_owed, 2),
                "received_usd": round(tx_usd_val, 2),
                "difference": round(tx_usd_val - total_owed, 2),
                "price_usd_used": round(verification["price_usd_used"], 6),
                "confirmations": verification["confirmations"],
                "explorer_url": verification["explorer_url"],
                "tx_hash": verification["tx_hash"],
            }
    finally:
        if conn:
            conn.close()


# --------------------------------------------------------------------------
# Contractors / Teams / Weeks management
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
        deleted_count = cursor.rowcount
        conn.commit()
        return {"status": "success", "deleted": deleted_count}
    finally:
        if conn:
            conn.close()


@app.post("/api/delete-week")
async def delete_week(payload: dict = Body(...)):
    week_date = payload.get("week_date")
    if not week_date:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Week date parameter is required."})

    clean_week_date = week_date.strip()
    conn = None
    try:
        conn = get_db()
        cursor = db_cursor(conn)
        cursor.execute("DELETE FROM weekly_ledgers WHERE week_date = %s", (clean_week_date,))
        deleted_count = cursor.rowcount
        conn.commit()
        return {"status": "success", "deleted_rows": deleted_count}
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
            name_clean = name.strip().lower()
            cursor.execute("INSERT INTO contractors (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name_clean,))
            cursor.execute("SELECT id FROM contractors WHERE name = %s", (name_clean,))
            cid = cursor.fetchone()["id"]
            cursor.execute(
                "INSERT INTO team_members (team_id, contractor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (team_id, cid),
            )

        conn.commit()
        return {"status": "success"}
    finally:
        if conn:
            conn.close()
