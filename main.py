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

sessions: dict = {}

@app.get("/captcha")
async def get_captcha(session_id: str):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        r = await client.get(BASE_URL)
        cookies = dict(client.cookies)
        captcha_resp = await client.get(CAPTCHA_URL)
        if captcha_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Could not fetch captcha")
        cookies.update(dict(client.cookies))
        sessions[session_id] = cookies
        return Response(
            content=captcha_resp.content,
            media_type="image/png",
            headers={"Cache-Control": "no-store"}
        )

class AttendanceRequest(BaseModel):
    roll_no: str
    dob: str
    captcha: str
    session_id: str

@app.post("/attendance")
async def get_attendance(data: AttendanceRequest):
    cookies = sessions.get(data.session_id)
    if not cookies:
        raise HTTPException(status_code=400, detail="Session expired. Please refresh captcha.")

    form_data = {
        "txtUserId": data.roll_no,
        "txtPassword": data.dob,
        "txtCaptcha": data.captcha,
        "btnLogin": "Login",
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for k, v in cookies.items():
            client.cookies.set(k, v)
        
        page = await client.get(BASE_URL)
        soup_pre = BeautifulSoup(page.text, "html.parser")
        for hidden in soup_pre.find_all("input", {"type": "hidden"}):
            name = hidden.get("name", "")
            val = hidden.get("value", "")
            if name:
                form_data[name] = val

        resp = await client.post(
            BASE_URL,
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": BASE_URL,
            }
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Portal request failed")

    soup = BeautifulSoup(resp.text, "html.parser")
    
    if soup.find("input", {"name": "txtCaptcha"}):
        raise HTTPException(status_code=401, detail="Invalid credentials or captcha. Please try again.")

    subjects = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows[1:]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) >= 3:
                nums = [int(c) for c in cols if c.isdigit()]
                if len(nums) >= 2:
                    present, total = nums[0], nums[1]
                    percentage = round((present / total) * 100, 1) if total > 0 else 0
                    subject_name = next((c for c in cols if not c.isdigit() and len(c) > 2), "")
                    if subject_name:
                        subjects.append({
                            "subject": subject_name,
                            "present": present,
                            "total": total,
                            "percentage": percentage,
                            "safe": percentage >= 75,
                        })

    if not subjects:
        title = soup.title.string if soup.title else "unknown"
        raise HTTPException(status_code=404, detail=f"No data found. Page: {title}")

    name = ""
    for tag in soup.find_all(["td", "span", "div"]):
        text = tag.get_text(strip=True)
        if "name" in text.lower() and ":" in text:
            parts = text.split(":")
            if len(parts) > 1:
                name = parts[1].strip()
                break

    return {"name": name, "roll_no": data.roll_no, "subjects": subjects}

@app.get("/health")
def health():
    return {"status": "ok"}
