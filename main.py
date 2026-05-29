from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
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

LOGIN_URL = "https://online.uktech.ac.in/ums/Student/Account/Login"
CAPTCHA_URL = "https://online.uktech.ac.in/ums/Student/Master/GetCaptchaimage"
ATTENDANCE_PAGE_URL = "https://online.uktech.ac.in/ums/Student/User/ViewAttendance"
ATTENDANCE_API_URL = "https://online.uktech.ac.in/ums/Student/User/ShowStudentAttendanceListByRollNoDOB"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Store persistent clients per session
persistent_clients: dict = {}

@app.get("/captcha")
async def get_captcha(session_id: str):
    # Create a NEW persistent client for this session
    client = httpx.AsyncClient(follow_redirects=True, headers=HEADERS)
    
    # Load login page
    page_resp = await client.get(LOGIN_URL)
    soup = BeautifulSoup(page_resp.text, "html.parser")
    
    # Get hidden fields
    hidden = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        n = inp.get("name")
        v = inp.get("value", "")
        if n:
            hidden[n] = v

    # Fetch captcha with same client (same session)
    captcha_resp = await client.get(CAPTCHA_URL)
    if captcha_resp.status_code != 200:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Could not fetch captcha")

    # Store client and hidden fields
    # Close old client if exists
    if session_id in persistent_clients:
        try:
            await persistent_clients[session_id]["client"].aclose()
        except:
            pass
    
    persistent_clients[session_id] = {
        "client": client,
        "hidden": hidden
    }

    return Response(content=captcha_resp.content, media_type="image/png",
                   headers={"Cache-Control": "no-store, no-cache"})

class AttendanceRequest(BaseModel):
    roll_no: str
    dob: str
    captcha: str
    session_id: str

@app.post("/attendance")
async def get_attendance(data: AttendanceRequest):
    session = persistent_clients.get(data.session_id)
    if not session:
        raise HTTPException(status_code=400, detail="Session not found. Please refresh captcha.")

    client = session["client"]
    hidden = session["hidden"]

    # Build form
    form_data = {**hidden}
    form_data["LoginId"] = data.roll_no
    form_data["Password"] = data.dob
    form_data["Captcha"] = data.captcha

    now = datetime.now()
    month_id = now.month
    year = now.year
    session_year = year - 1

    try:
        # Step 1: Login using the SAME client that fetched the captcha
        login_resp = await client.post(LOGIN_URL, data=form_data, headers={
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": LOGIN_URL,
        })

        login_soup = BeautifulSoup(login_resp.text, "html.parser")

        # Check if still on login page
        if login_soup.find("input", {"name": "Captcha"}):
            raise HTTPException(status_code=401, detail="Wrong credentials or captcha. Try again.")

        # Step 2: Load attendance page with same client
        att_page = await client.get(ATTENDANCE_PAGE_URL, headers={
            **HEADERS,
            "Referer": str(login_resp.url),
        })
        att_soup = BeautifulSoup(att_page.text, "html.parser")

        # Check if redirected to login
        if att_soup.find("input", {"name": "Captcha"}):
            raise HTTPException(status_code=401,
                detail=f"Redirected to login after auth. URL: {att_page.url}")

        def get_by_name(soup, *names):
            for name in names:
                el = soup.find("input", {"name": name})
                if el and el.get("value"):
                    return el.get("value")
            return ""

        admission_id = get_by_name(att_soup, "StudentAdmissionId")
        college_id = get_by_name(att_soup, "CollegeId") or "61"
        course_id = get_by_name(att_soup, "CourseId") or "1"
        branch_id = get_by_name(att_soup, "BranchId") or "1"
        duration_id = get_by_name(att_soup, "CourseBranchDurationId") or "2"
        student_name = get_by_name(att_soup, "StudentName")

        if not admission_id:
            all_inputs = [(inp.get("name",""), inp.get("value","")[:20])
                         for inp in att_soup.find_all("input")]
            raise HTTPException(status_code=404,
                detail=f"URL:{att_page.url} Inputs:{all_inputs[:10]}")

        # Step 3: Call attendance API
        params = {
            "CollegeId": college_id,
            "CourseId": course_id,
            "BranchId": branch_id,
            "CourseBranchDurationId": duration_id,
            "StudentAdmissionId": admission_id,
            "DateOfBirth": data.dob,
            "SessionYear": str(session_year),
            "RollNo": data.roll_no,
            "Year": str(year),
            "MonthId": str(month_id),
        }

        att_resp = await client.get(ATTENDANCE_API_URL, params=params, headers={
            **HEADERS,
            "Referer": ATTENDANCE_PAGE_URL,
            "X-Requested-With": "XMLHttpRequest"
        })

        if att_resp.status_code != 200:
            raise HTTPException(status_code=att_resp.status_code,
                detail=f"API {att_resp.status_code}. admission={admission_id}")

        try:
            att_data = att_resp.json()
        except:
            raise HTTPException(status_code=502, detail="Could not parse attendance response.")

        if not att_data:
            raise HTTPException(status_code=404, detail="No attendance data for this month.")

        subject_map = {}
        for item in att_data:
            subject = (item.get("PaperName") or item.get("SubjectName") or
                      item.get("paperName") or item.get("subjectName") or "")
            held = int(item.get("TotalClassesHeld") or item.get("totalClassesHeld") or 0)
            attended = int(item.get("TotalClassesAttended") or item.get("totalClassesAttended") or 0)
            if subject and held > 0:
                if subject not in subject_map or held > subject_map[subject]["held"]:
                    subject_map[subject] = {"held": held, "attended": attended}

        subjects = []
        for subj, vals in subject_map.items():
            total = vals["held"]
            present = vals["attended"]
            pct = round((present / total) * 100, 1)
            subjects.append({
                "subject": subj,
                "present": present,
                "total": total,
                "percentage": pct,
                "safe": pct >= 75,
            })

        if not subjects:
            sample = att_data[0] if att_data else {}
            raise HTTPException(status_code=404, detail=f"Keys: {list(sample.keys())}")

        return {"name": student_name, "roll_no": data.roll_no, "subjects": subjects}

    finally:
        # Clean up client
        try:
            await client.aclose()
        except:
            pass
        persistent_clients.pop(data.session_id, None)

@app.get("/health")
def health():
    return {"status": "ok"}
