from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup

app = FastAPI(title="UTU Attendance API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LOGIN_URL = "https://online.uktech.ac.in/ums/Student/Public/ViewDetail"
CAPTCHA_URL = "https://online.uktech.ac.in/ums/Student/Master/GetCaptchaimage"
ATTENDANCE_URL = "https://online.uktech.ac.in/ums/Student/User/ViewAttendance"

session_clients: dict = {}

@app.get("/captcha")
async def get_captcha(session_id: str):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        page_resp = await client.get(LOGIN_URL)
        soup = BeautifulSoup(page_resp.text, "html.parser")
        hidden = {}
        for inp in soup.find_all("input", {"type": "hidden"}):
            n = inp.get("name")
            v = inp.get("value", "")
            if n:
                hidden[n] = v
        captcha_resp = await client.get(CAPTCHA_URL)
        if captcha_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Could not fetch captcha")
        session_clients[session_id] = {
            "cookies": dict(client.cookies),
            "hidden": hidden
        }
        return Response(
            content=captcha_resp.content,
            media_type="image/png",
            headers={"Cache-Control": "no-store, no-cache"}
        )

class AttendanceRequest(BaseModel):
    roll_no: str
    dob: str
    captcha: str
    session_id: str

@app.post("/attendance")
async def get_attendance(data: AttendanceRequest):
    session = session_clients.get(data.session_id)
    if not session:
        raise HTTPException(status_code=400, detail="Session not found. Please refresh captcha.")

    cookies = session["cookies"]
    hidden = session["hidden"]

    form_data = {**hidden}
    form_data["txtUserId"] = data.roll_no
    form_data["txtPassword"] = data.dob
    form_data["txtCaptcha"] = data.captcha

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for k, v in cookies.items():
            client.cookies.set(k, v)

        # Step 1: Login
        login_resp = await client.post(
            LOGIN_URL,
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": LOGIN_URL,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )

        # Check if still on login page
        login_soup = BeautifulSoup(login_resp.text, "html.parser")
        if login_soup.find("input", {"name": "txtCaptcha"}):
            raise HTTPException(status_code=401, detail="Wrong credentials or captcha. Try again.")

        # Step 2: Go to attendance page
        att_resp = await client.get(
            ATTENDANCE_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )

        att_soup = BeautifulSoup(att_resp.text, "html.parser")

        # Step 3: Parse the attendance table
        subjects = []
        table = att_soup.find("table")
        if not table:
            raise HTTPException(status_code=404, detail="Attendance table not found. Try again.")

        rows = table.find_all("tr")
        for row in rows[1:]:  # skip header
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 3:
                continue
            
            # Subject name is first column
            subject_name = cols[0].strip()
            if not subject_name:
                continue

            # Last 3 cols are: Total Held, Total Attended, Attended%
            try:
                total = int(cols[-3]) if cols[-3].isdigit() else 0
                present = int(cols[-2]) if cols[-2].isdigit() else 0
                pct_str = cols[-1].replace("%", "").strip()
                pct = float(pct_str) if pct_str.replace(".", "").isdigit() else (
                    round((present / total) * 100, 1) if total > 0 else 0
                )
            except (ValueError, IndexError):
                continue

            if subject_name and total > 0:
                subjects.append({
                    "subject": subject_name,
                    "present": present,
                    "total": total,
                    "percentage": pct,
                    "safe": pct >= 75,
                })

    if not subjects:
        raise HTTPException(status_code=404, detail="No attendance data found. Make sure attendance is available for this month.")

    session_clients.pop(data.session_id, None)

    return {"name": "", "roll_no": data.roll_no, "subjects": subjects}

@app.get("/health")
def health():
    return {"status": "ok"}
