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
templates = Jinja2Templates(directory="templates")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

@app.on_event("startup")
def startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT l.id, l.week_date, c.name as contractor, l.amount_owed_usd, l.status, l.paid_at_est, l.crypto_tx_id, l.crypto_symbol, l.notes
        FROM weekly_ledgers l
        JOIN contractors c ON l.contractor_id = c.id
        ORDER BY l.week_date DESC, c.name ASC
    """)
    ledgers = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT * FROM teams")
    teams = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return templates.TemplateResponse("index.html", {"request": request, "ledgers": ledgers, "teams": teams})

@app.post("/api/upload")
async def upload_weekly_image(file: UploadFile = File(...), week_date: str = Form(...)):
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

    conn = get_db()
    cursor = conn.cursor()
    for r in records:
        name = str(r["contractor"]).strip().lower()
        amount = float(r["profits"])

        cursor.execute("INSERT OR IGNORE INTO contractors (name) VALUES (?)", (name,))
        cursor.execute("SELECT id FROM contractors WHERE name = ?", (name,))
        cid = cursor.fetchone()["id"]

        cursor.execute("""
            INSERT INTO weekly_ledgers (week_date, contractor_id, amount_owed_usd, status)
            VALUES (?, ?, ?, 'UNPAID')
        """, (week_date, cid, amount))

    conn.commit()
    conn.close()
    return JSONResponse({"status": "success", "extracted_count": len(records)})

@app.post("/api/mark-paid-manual")
async def mark_paid_manual(ledger_ids: list[int] = Body(...), notes: str = Body("")):
    conn = get_db()
    cursor = conn.cursor()
    now_est = get_est_now()
    placeholders = ",".join(["?"] * len(ledger_ids))
    cursor.execute(f"""
        UPDATE weekly_ledgers 
        SET status = 'MANUALLY_PAID', paid_at_est = ?, notes = ?
        WHERE id IN ({placeholders})
    """, [now_est, notes] + ledger_ids)
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/reconcile-crypto")
async def reconcile_crypto(
    ledger_ids: list[int] = Body(...),
    tx_id: str = Body(...),
    symbol: str = Body(...)
):
    conn = get_db()
    cursor = conn.cursor()
    placeholders = ",".join(["?"] * len(ledger_ids))
    
    cursor.execute(f"SELECT SUM(amount_owed_usd) as total FROM weekly_ledgers WHERE id IN ({placeholders})", ledger_ids)
    total_owed = cursor.fetchone()["total"] or 0.0

    tx_usd_val = get_tx_usd_value(tx_id, symbol)

    if abs(tx_usd_val - total_owed) <= 5.0:
        now_est = get_est_now()
        cursor.execute(f"""
            UPDATE weekly_ledgers 
            SET status = 'PAID', paid_at_est = ?, crypto_tx_id = ?, crypto_symbol = ?
            WHERE id IN ({placeholders})
        """, [now_est, tx_id, symbol.upper()] + ledger_ids)
        conn.commit()
        conn.close()
        return {"status": "matched", "total_owed": total_owed, "received_usd": tx_usd_val}
    else:
        conn.close()
        return {
            "status": "mismatch", 
            "total_owed": total_owed, 
            "received_usd": tx_usd_val,
            "difference": round(tx_usd_val - total_owed, 2)
        }

@app.post("/api/teams")
async def create_team(team_name: str = Body(...), contractor_names: list[str] = Body(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO teams (name) VALUES (?)", (team_name,))
    cursor.execute("SELECT id FROM teams WHERE name = ?", (team_name,))
    team_id = cursor.fetchone()["id"]

    for name in contractor_names:
        name_clean = name.strip().lower()
        cursor.execute("INSERT OR IGNORE INTO contractors (name) VALUES (?)", (name_clean,))
        cursor.execute("SELECT id FROM contractors WHERE name = ?", (name_clean,))
        cid = cursor.fetchone()["id"]
        cursor.execute("INSERT OR IGNORE INTO team_members (team_id, contractor_id) VALUES (?, ?)", (team_id, cid))

    conn.commit()
    conn.close()
    return {"status": "success"}
