from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
from datetime import datetime

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

async def do_login(client, data, session):
    cookies = session["cookies"]
    hidden = session["hidden"]
    form_data = {**hidden}
    form_data["txtUserId"] = data.roll_no
    form_data["txtPassword"] = data.dob
    form_data["txtCaptcha"] = data.captcha

    for k, v in cookies.items():
        client.cookies.set(k, v)

    login_resp = await client.post(
        LOGIN_URL,
        data=form_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": LOGIN_URL,
            "User-Agent": "Mozilla/5.0"
        }
    )
    login_soup = BeautifulSoup(login_resp.text, "html.parser")
    if login_soup.find("input", {"name": "txtCaptcha"}):
        raise HTTPException(status_code=401, detail="Wrong credentials or captcha. Try again.")
    return login_resp

@app.post("/debug")
async def debug_attendance(data: AttendanceRequest):
    """Returns raw HTML of attendance page for debugging."""
    session = session_clients.get(data.session_id)
    if not session:
        raise HTTPException(status_code=400, detail="Session not found.")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        await do_login(client, data, session)
        
        # Try GET first
        att_resp = await client.get(ATTENDANCE_URL, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(att_resp.text, "html.parser")
        
        # Get all select names and options
        selects_info = {}
        for sel in soup.find_all("select"):
            name = sel.get("name") or sel.get("id") or "unknown"
            opts = [(o.get("value",""), o.get_text(strip=True)) for o in sel.find_all("option")]
            selects_info[name] = opts

        tables = len(soup.find_all("table"))
        title = soup.title.string if soup.title else "no title"
        
        return {
            "page_title": title,
            "tables_found": tables,
            "selects": selects_info,
            "page_url": str(att_resp.url),
            "status": att_resp.status_code
        }

@app.post("/attendance")
async def get_attendance(data: AttendanceRequest):
    session = session_clients.get(data.session_id)
    if not session:
        raise HTTPException(status_code=400, detail="Session not found. Please refresh captcha.")

    now = datetime.now()
    current_month = now.strftime("%B")
    current_year = str(now.year)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        await do_login(client, data, session)

        # Load attendance page
        att_page = await client.get(ATTENDANCE_URL, headers={"User-Agent": "Mozilla/5.0"})
        att_soup = BeautifulSoup(att_page.text, "html.parser")

        # Build form
        att_form = {}
        for inp in att_soup.find_all("input", {"type": "hidden"}):
            n = inp.get("name")
            v = inp.get("value", "")
            if n:
                att_form[n] = v

        for sel in att_soup.find_all("select"):
            name = sel.get("name") or sel.get("id")
            if not name:
                continue
            options = sel.find_all("option")
            selected_val = None
            for opt in options:
                val = opt.get("value", "")
                text = opt.get_text(strip=True)
                if current_month.lower() in text.lower() or current_year in text:
                    selected_val = val
                    break
            if not selected_val:
                for opt in options:
                    val = opt.get("value", "")
                    if val and val not in ("0", ""):
                        selected_val = val
                        break
            if selected_val:
                att_form[name] = selected_val

        submit_btn = att_soup.find("input", {"type": "submit"}) or att_soup.find("button", {"type": "submit"})
        if submit_btn and submit_btn.get("name"):
            att_form[submit_btn.get("name")] = submit_btn.get("value", "View Attendance")

        att_resp = await client.post(
            ATTENDANCE_URL,
            data=att_form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": ATTENDANCE_URL,
                "User-Agent": "Mozilla/5.0"
            }
        )

        result_soup = BeautifulSoup(att_resp.text, "html.parser")
        subjects = []
        table = result_soup.find("table")

        if table:
            rows = table.find_all("tr")
            for row in rows[1:]:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cols) < 3:
                    continue
                subject_name = cols[0].strip()
                if not subject_name or subject_name.isdigit():
                    continue
                try:
                    total = int(cols[-3]) if cols[-3].isdigit() else 0
                    present = int(cols[-2]) if cols[-2].isdigit() else 0
                    pct_str = cols[-1].replace("%", "").strip()
                    pct = float(pct_str) if pct_str.replace(".", "").isdigit() else (
                        round((present / total) * 100, 1) if total > 0 else 0
                    )
                    if total > 0:
                        subjects.append({
                            "subject": subject_name,
                            "present": present,
                            "total": total,
                            "percentage": pct,
                            "safe": pct >= 75,
                        })
                except (ValueError, IndexError):
                    continue

    if not subjects:
        raise HTTPException(status_code=404, detail="No attendance data found.")

    session_clients.pop(data.session_id, None)
    return {"name": "", "roll_no": data.roll_no, "subjects": subjects}

@app.get("/health")
def health():
    return {"status": "ok"}
