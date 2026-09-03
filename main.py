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
                ORDER BY c.name ASC
            """, (selected_week,))
            ledgers = cursor.fetchall()

        cursor.execute("SELECT * FROM teams")
        teams = cursor.fetchall()

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
            "contractors": contractors,
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

    cursor.execute("SELECT COUNT(*) as cnt FROM weekly_ledgers WHERE week_date = %s", (week_date,))
    check_res = cursor.fetchone()
    
    if check_res and check_res["cnt"] > 0:
        if not overwrite:
            cursor.close()
            conn.close()
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error", 
                    "message": f"Data for '{week_date}' already exists. Check 'Overwrite existing week' or delete the week below before uploading."
                }
            )
        else:
            cursor.execute("DELETE FROM weekly_ledgers WHERE week_date = %s", (week_date,))

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

    for r in records:
        name = str(r["contractor"]).strip().lower()
        amount = float(r["profits"])

        cursor.execute("INSERT INTO contractors (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        cursor.execute("SELECT id FROM contractors WHERE name = %s", (name,))
        cid = cursor.fetchone()["id"]

        cursor.execute("""
            INSERT INTO weekly_ledgers (week_date, contractor_id, amount_owed_usd, status)
            VALUES (%s, %s, %s, 'UNPAID')
        """, (week_date, cid, amount))

    conn.commit()
    cursor.close()
    conn.close()
    return JSONResponse({"status": "success", "extracted_count": len(records), "week_date": week_date})

@app.post("/api/delete-week")
async def delete_week(payload: dict = Body(...)):
    week_date = payload.get("week_date")
    if not week_date:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Week date parameter is required."})

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM weekly_ledgers WHERE week_date = %s", (week_date,))
    conn.commit()
    cursor.close
