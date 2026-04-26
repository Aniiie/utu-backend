from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
from datetime import datetime
import re

app = FastAPI(title="UTU Attendance API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LOGIN_URL = "https://online.uktech.ac.in/ums/Student/Public/ViewDetail"
CAPTCHA_URL = "https://online.uktech.ac.in/ums/Student/Master/GetCaptchaimage"
ATTENDANCE_PAGE_URL = "https://online.uktech.ac.in/ums/Student/User/ViewAttendance"
ATTENDANCE_API_URL = "https://online.uktech.ac.in/ums/Student/User/ShowStudentAttendanceListByRollNoDOB"

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
        session_clients[session_id] = {"cookies": dict(client.cookies), "hidden": hidden}
        return Response(content=captcha_resp.content, media_type="image/png",
                       headers={"Cache-Control": "no-store, no-cache"})

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

    form_data = {**session["hidden"]}
    form_data["txtUserId"] = data.roll_no
    form_data["txtPassword"] = data.dob
    form_data["txtCaptcha"] = data.captcha

    now = datetime.now()
    month_id = now.month
    year = now.year
    session_year = year - 1  # 2025 for session 2025-26

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for k, v in session["cookies"].items():
            client.cookies.set(k, v)

        # Step 1: Login
        login_resp = await client.post(LOGIN_URL, data=form_data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": LOGIN_URL, "User-Agent": "Mozilla/5.0"
        })
        login_soup = BeautifulSoup(login_resp.text, "html.parser")
        if login_soup.find("input", {"name": "txtCaptcha"}):
            raise HTTPException(status_code=401, detail="Wrong credentials or captcha. Try again.")

        # Step 2: Load attendance page and extract IDs from JavaScript
        att_page = await client.get(ATTENDANCE_PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
        page_text = att_page.text

        # Extract IDs from JS variables in page source
        def extract_js_var(text, var_name):
            patterns = [
                rf'var\s+{var_name}\s*=\s*["\']?(\d+)["\']?',
                rf'{var_name}\s*=\s*["\']?(\d+)["\']?',
                rf'["\'{var_name}["\']\s*:\s*["\']?(\d+)["\']?',
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    return match.group(1)
            return None

        # Try to find IDs in page source
        college_id = extract_js_var(page_text, "CollegeId") or extract_js_var(page_text, "collegeId") or "61"
        course_id = extract_js_var(page_text, "CourseId") or extract_js_var(page_text, "courseId") or "1"
        branch_id = extract_js_var(page_text, "BranchId") or extract_js_var(page_text, "branchId") or "1"
        admission_id = extract_js_var(page_text, "StudentAdmissionId") or extract_js_var(page_text, "admissionId") or ""
        duration_id = extract_js_var(page_text, "CourseBranchDurationId") or extract_js_var(page_text, "durationId") or "2"

        # Also check hidden inputs
        att_soup = BeautifulSoup(page_text, "html.parser")
        for inp in att_soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "").lower()
            val = inp.get("value", "")
            if not val:
                continue
            if "collegeid" in name:
                college_id = val
            elif "courseid" in name and "branch" not in name:
                course_id = val
            elif "branchid" in name and "duration" not in name:
                branch_id = val
            elif "admissionid" in name or "studentadmission" in name:
                admission_id = val
            elif "duration" in name:
                duration_id = val

        # Step 3: Call attendance JSON API
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
            "User-Agent": "Mozilla/5.0",
            "Referer": ATTENDANCE_PAGE_URL,
            "X-Requested-With": "XMLHttpRequest"
        })

        if att_resp.status_code == 401:
            raise HTTPException(status_code=401, detail=f"API auth failed. IDs found: college={college_id}, course={course_id}, branch={branch_id}, admission={admission_id}, duration={duration_id}")

        if att_resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Attendance API error: {att_resp.status_code}")

        try:
            att_data = att_resp.json()
        except:
            raise HTTPException(status_code=502, detail="Could not parse attendance response.")

        if not att_data:
            raise HTTPException(status_code=404, detail="No attendance data. Try a different month.")

        # Step 4: Parse JSON
        subject_map = {}
        for item in att_data:
            # Try all possible key names
            subject = (item.get("PaperName") or item.get("SubjectName") or 
                      item.get("paperName") or item.get("subjectName") or "")
            held = int(item.get("TotalClassesHeld") or item.get("totalClassesHeld") or 0)
            attended = int(item.get("TotalClassesAttended") or item.get("totalClassesAttended") or 0)

            if subject:
                if subject not in subject_map:
                    subject_map[subject] = {"held": 0, "attended": 0}
                if held > subject_map[subject]["held"]:
                    subject_map[subject]["held"] = held
                    subject_map[subject]["attended"] = attended

        subjects = []
        for subj, vals in subject_map.items():
            total = vals["held"]
            present = vals["attended"]
            if total > 0:
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
            raise HTTPException(status_code=404, detail=f"Could not parse. Sample keys: {list(sample.keys())}")

    session_clients.pop(data.session_id, None)
    return {"name": "", "roll_no": data.roll_no, "subjects": subjects}

@app.get("/health")
def health():
    return {"status": "ok"}
