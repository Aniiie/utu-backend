from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
import re

app = FastAPI(title="UTU Attendance API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_URL = "https://online.uktech.ac.in/ums/Student/Public/ViewDetail"
CAPTCHA_URL = "https://online.uktech.ac.in/ums/Student/Master/GetCaptchaimage"

# Store session cookies per client (simple in-memory, good for dev)
sessions: dict = {}

class AttendanceRequest(BaseModel):
    roll_no: str
    dob: str         # DD/MM/YYYY
    captcha: str
    session_id: str

@app.get("/captcha")
async def get_captcha(session_id: str):
    """Fetch captcha image from UTU portal and return it with cookies stored."""
    async with httpx.AsyncClient() as client:
        # First hit the main page to get session cookies
        await client.get(BASE_URL)
        # Now fetch captcha
        resp = await client.get(CAPTCHA_URL)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Could not fetch captcha")
        # Store cookies for this session
        sessions[session_id] = dict(client.cookies)
        return Response(content=resp.content, media_type="image/png")

@app.post("/attendance")
async def get_attendance(data: AttendanceRequest):
    """Submit login form and scrape attendance data."""
    cookies = sessions.get(data.session_id, {})

    form_data = {
        "txtUserId": data.roll_no,
        "txtPassword": data.dob,
        "txtCaptcha": data.captcha,
    }

    async with httpx.AsyncClient(cookies=cookies, follow_redirects=True) as client:
        resp = await client.post(BASE_URL, data=form_data)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Portal request failed")

    soup = BeautifulSoup(resp.text, "html.parser")

    # Check for error messages
    error_div = soup.find(string=re.compile(r"invalid|incorrect|wrong|captcha", re.I))
    if error_div:
        raise HTTPException(status_code=401, detail="Invalid credentials or captcha")

    # Parse attendance table
    subjects = []
    table = soup.find("table")
    if not table:
        raise HTTPException(status_code=404, detail="No attendance data found. Check credentials.")

    rows = table.find_all("tr")
    for row in rows[1:]:  # skip header
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) >= 4:
            try:
                present = int(cols[2]) if cols[2].isdigit() else 0
                total = int(cols[3]) if cols[3].isdigit() else 0
                percentage = round((present / total) * 100, 1) if total > 0 else 0
                subjects.append({
                    "subject": cols[1] if len(cols) > 1 else cols[0],
                    "present": present,
                    "total": total,
                    "percentage": percentage,
                    "safe": percentage >= 75,
                })
            except Exception:
                continue

    if not subjects:
        raise HTTPException(status_code=404, detail="Could not parse attendance. Portal structure may have changed.")

    # Get student name if available
    name = ""
    name_tag = soup.find(string=re.compile(r"Name\s*:", re.I))
    if name_tag:
        name = name_tag.find_next().get_text(strip=True) if name_tag.find_next() else ""

    return {"name": name, "roll_no": data.roll_no, "subjects": subjects}


@app.get("/health")
def health():
    return {"status": "ok"}
