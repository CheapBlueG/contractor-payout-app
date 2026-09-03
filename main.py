import os
import base64
import json
import openai
from fastapi import FastAPI, Request, File, UploadFile, Form, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from database import init_db, get_db, get_est_now
from crypto_service import get_tx_usd_value

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

@app.on_event("startup")
def startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, selected_week: str = None):
    ledgers = []
    teams = []
    contractors = []
    weeks = []
    teams_map = {}
    
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("SELECT week_date FROM weekly_ledgers GROUP BY week_date ORDER BY MAX(id) DESC")
        weeks_res = cursor.fetchall()
        weeks = [w["week_date"] for w in weeks_res]

        if not selected_week and weeks:
            selected_week = weeks[0]

        if selected_week:
            cursor.execute("""
                SELECT l.id, l.week_date, c.name as contractor, l.amount_owed_usd, l.status, l.paid_at_est, l.crypto_tx_id, l.crypto_symbol, l.notes
                FROM weekly_ledgers l
                JOIN contractors c ON l.contractor_id = c.id
                WHERE l.week_date = %s
                ORDER BY 
                    CASE 
                        WHEN UPPER(l.status) LIKE '%PAID%' THEN 1 
                        ELSE 0 
                    END ASC,
                    c.name ASC
            """, (selected_week,))
            ledgers = cursor.fetchall()

        cursor.execute("SELECT * FROM teams")
        teams = cursor.fetchall()

        cursor.execute("""
            SELECT t.name as team_name, c.name as contractor_name
            FROM teams t
            JOIN team_members tm ON t.id = tm.team_id
            JOIN contractors c ON tm.contractor_id = c.id
        """)
        team_rows = cursor.fetchall()
        for row in team_rows:
            t_name = row["team_name"]
            c_name = row["contractor_name"]
            if t_name not in teams_map:
                teams_map[t_name] = []
            teams_map[t_name].append(c_name)

        cursor.execute("SELECT * FROM contractors ORDER BY name ASC")
        contractors = cursor.fetchall()

        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error fetching index data: {e}")

    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={
            "ledgers": ledgers, 
            "teams": teams, 
            "teams_map": teams_map,
            "teams_json": json.dumps(teams_map),
            "contractors": contractors,
            "contractors_json": json.dumps([c["name"] for c in contractors]),
            "weeks": weeks,
            "selected_week": selected_week
        }
    )

@app.post("/api/upload")
async def upload_weekly_image(
    file: UploadFile = File(...), 
    week_date: str = Form(...),
    overwrite: bool = Form(False)
):
    conn = get_db()
    cursor = conn.cursor()
    clean_week_date = week_date.strip()

    cursor.execute("SELECT COUNT(*) as cnt FROM weekly_ledgers WHERE week_date = %s", (clean_week_date,))
    check_res = cursor.fetchone()
    
    if check_res and check_res["cnt"] > 0 and not overwrite:
        cursor.close()
        conn.close()
        return JSONResponse(
            status_code=400,
            content={
                "status": "error", 
                "message": f"Data for '{clean_week_date}' already exists. Check 'Overwrite if week exists' or delete the range below."
            }
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
                        "text": "Extract table data from image. Return JSON format: {\"records\": [{\"contractor\": \"lowercase name\", \"profits\": numeric_amount}]}"
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                    }
                ]
            }
        ]
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

    # Require approval if unknown contractor names are detected
    if unknown_contractors:
        cursor.close()
        conn.close()
        return JSONResponse({
            "status": "approval_required",
            "week_date": clean_week_date,
            "overwrite": overwrite,
            "unknown_contractors": unknown_contractors,
            "existing_contractors": sorted(existing_db_names),
            "records": records
        })

    # Execute insert if all names already exist in DB
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
    cursor.close()
    conn.close()
    return JSONResponse({"status": "success", "extracted_count": len(records), "week_date": clean_week_date})

@app.post("/api/confirm-upload")
async def confirm_upload(payload: dict = Body(...)):
    week_date = payload.get("week_date")
    records = payload.get("records", [])
    mappings = payload.get("mappings", {})  # { "typo_name": "corrected_name" }
    overwrite = payload.get("overwrite", False)

    if not week_date or not records:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing required confirmation payload."})

    conn = get_db()
    cursor = conn.cursor()
    clean_week_date = week_date.strip()

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
    cursor.close()
    conn.close()
    return {"status": "success", "extracted_count": len(records), "week_date": clean_week_date}

@app.post("/api/delete-contractor")
async def delete_contractor(payload: dict = Body(...)):
    contractor_id = payload.get("contractor_id")
    if not contractor_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Contractor ID is required."})

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM team_members WHERE contractor_id = %s", (contractor_id,))
    cursor.execute("DELETE FROM weekly_ledgers WHERE contractor_id = %s", (contractor_id,))
    cursor.execute("DELETE FROM contractors WHERE id = %s", (contractor_id,))
    deleted_count = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    return {"status": "success", "deleted": deleted_count}

@app.post("/api/delete-week")
async def delete_week(payload: dict = Body(...)):
    week_date = payload.get("week_date")
    if not week_date:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Week date parameter is required."})

    clean_week_date = week_date.strip()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM weekly_ledgers WHERE week_date = %s", (clean_week_date,))
    deleted_count = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    return {"status": "success", "deleted_rows": deleted_count}

@app.post("/api/mark-paid-manual")
async def mark_paid_manual(ledger_ids: list[int] = Body(...), notes: str = Body("")):
    conn = get_db()
    cursor = conn.cursor()
    now_est = get_est_now()
    placeholders = ",".join(["%s"] * len(ledger_ids))
    cursor.execute(f"""
        UPDATE weekly_ledgers 
        SET status = 'MANUALLY_PAID', paid_at_est = %s, notes = %s
        WHERE id IN ({placeholders})
    """, [now_est, notes] + ledger_ids)
    conn.commit()
    cursor.close()
    conn.close()
    return {"status": "success"}

@app.post("/api/reconcile-crypto")
async def reconcile_crypto(
    ledger_ids: list[int] = Body(...),
    tx_id: str = Body(...),
    symbol: str = Body(...)
):
    if not ledger_ids or not tx_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing ledger selection or TxID."})

    conn = get_db()
    cursor = conn.cursor()
    placeholders = ",".join(["%s"] * len(ledger_ids))

    cursor.execute(f"SELECT SUM(amount_owed_usd) as total FROM weekly_ledgers WHERE id IN ({placeholders})", ledger_ids)
    res = cursor.fetchone()
    total_owed = float(res["total"]) if res and res["total"] else 0.0

    # Execute blockchain lookup safely
    try:
        tx_usd_val = get_tx_usd_value(tx_id, symbol)
    except ValueError as err:
        cursor.close()
        conn.close()
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(err)}
        )
    except Exception as err:
        cursor.close()
        conn.close()
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Unexpected verification error: {str(err)}"}
        )

    if abs(tx_usd_val - total_owed) <= 5.0:
        now_est = get_est_now()
        cursor.execute(f"""
            UPDATE weekly_ledgers 
            SET status = 'PAID', paid_at_est = %s, crypto_tx_id = %s, crypto_symbol = %s
            WHERE id IN ({placeholders})
        """, [now_est, tx_id, symbol.upper()] + ledger_ids)
        conn.commit()
        cursor.close()
        conn.close()
        return {"status": "matched", "total_owed": total_owed, "received_usd": round(tx_usd_val, 2)}
    else:
        cursor.close()
        conn.close()
        return {
            "status": "mismatch", 
            "total_owed": total_owed, 
            "received_usd": round(tx_usd_val, 2),
            "difference": round(tx_usd_val - total_owed, 2)
        }

@app.post("/api/teams")
async def create_team(team_name: str = Body(...), contractor_names: list[str] = Body(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO teams (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (team_name,))
    cursor.execute("SELECT id FROM teams WHERE name = %s", (team_name,))
    team_id = cursor.fetchone()["id"]

    for name in contractor_names:
        name_clean = name.strip().lower()
        cursor.execute("INSERT INTO contractors (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name_clean,))
        cursor.execute("SELECT id FROM contractors WHERE name = %s", (name_clean,))
        cid = cursor.fetchone()["id"]
        cursor.execute("INSERT INTO team_members (team_id, contractor_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (team_id, cid))

    conn.commit()
    cursor.close()
    conn.close()
    return {"status": "success"}
