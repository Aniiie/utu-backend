from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
from datetime import datetime
import json

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
BRANCH_API_URL = "https://online.uktech.ac.in/ums/Student/User/GetCourseBranchDurationForAttendance"

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

    now = datetime.now()
    month_id = now.month  # April = 4
    year = now.year       # 2026

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
                "User-Agent": "Mozilla/5.0"
            }
        )
        login_soup = BeautifulSoup(login_resp.text, "html.parser")
        if login_soup.find("input", {"name": "txtCaptcha"}):
            raise HTTPException(status_code=401, detail="Wrong credentials or captcha. Try again.")

        # Step 2: Load attendance page to get student IDs
        att_page = await client.get(
            ATTENDANCE_PAGE_URL,
            headers={"User-Agent": "Mozilla/5.0", "Referer": LOGIN_URL}
        )
        att_soup = BeautifulSoup(att_page.text, "html.parser")

        # Extract student details from the info table
        student_name = ""
        college_id = "61"
        course_id = "1"
        branch_id = "1"
        admission_id = ""
        course_branch_duration_id = "2"
        session_year = str(year - 1)  # e.g. 2025 for session 2025-26

        # Try to get hidden fields with student data
        for inp in att_soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "").lower()
            val = inp.get("value", "")
            if "college" in name:
                college_id = val
            elif "course" in name and "branch" not in name and "duration" not in name:
                course_id = val
            elif "branch" in name and "duration" not in name:
                branch_id = val
            elif "admission" in name or "student" in name:
                admission_id = val
            elif "duration" in name:
                course_branch_duration_id = val

        # Also check select fields for session year
        for sel in att_soup.find_all("select"):
            name = (sel.get("name") or sel.get("id") or "").lower()
            if "session" in name:
                opts = sel.find_all("option")
                for opt in opts:
                    val = opt.get("value", "")
                    text = opt.get_text(strip=True)
                    if str(year-1) in text:  # e.g. "2025-26"
                        session_year = val if val else str(year-1)
                        break

        # Get student name from table
        for td in att_soup.find_all("td"):
            text = td.get_text(strip=True)
            if text and len(text) > 3 and text.isupper() and len(text.split()) >= 2:
                if not any(c.isdigit() for c in text):
                    student_name = text
                    break

        # Step 3: Get branch duration (to get CourseBranchDurationId)
        branch_resp = await client.get(
            BRANCH_API_URL,
            params={
                "BranchId": branch_id,
                "CourseId": course_id,
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": ATTENDANCE_PAGE_URL}
        )
        
        if branch_resp.status_code == 200:
            try:
                branch_data = branch_resp.json()
                if isinstance(branch_data, list) and len(branch_data) > 0:
                    course_branch_duration_id = str(branch_data[0].get("CourseBranchDurationId", course_branch_duration_id))
            except:
                pass

        # Step 4: Call the direct attendance JSON API
        params = {
            "CollegeId": college_id,
            "CourseId": course_id,
            "BranchId": branch_id,
            "CourseBranchDurationId": course_branch_duration_id,
            "StudentAdmissionId": admission_id,
            "DateOfBirth": data.dob,
            "SessionYear": session_year,
            "RollNo": data.roll_no,
            "Year": str(year),
            "MonthId": str(month_id),
        }

        att_resp = await client.get(
            ATTENDANCE_API_URL,
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": ATTENDANCE_PAGE_URL,
                "X-Requested-With": "XMLHttpRequest"
            }
        )

        if att_resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Attendance API error: {att_resp.status_code}")

        try:
            att_data = att_resp.json()
        except:
            raise HTTPException(status_code=502, detail="Could not parse attendance response.")

        if not att_data:
            raise HTTPException(status_code=404, detail="No attendance data returned. Try a different month.")

        # Step 5: Parse JSON response
        # Group by subject and sum up
        subject_map = {}
        for item in att_data:
            subject = item.get("PaperName") or item.get("SubjectName") or item.get("paperName") or ""
            held = item.get("TotalClassesHeld") or item.get("totalClassesHeld") or 0
            attended = item.get("TotalClassesAttended") or item.get("totalClassesAttended") or 0
            pct = item.get("AttendedPercentage") or item.get("attendedPercentage") or 0

            if subject:
                if subject not in subject_map:
                    subject_map[subject] = {"held": 0, "attended": 0}
                subject_map[subject]["held"] = max(subject_map[subject]["held"], int(held) if held else 0)
                subject_map[subject]["attended"] = max(subject_map[subject]["attended"], int(attended) if attended else 0)

        subjects = []
        for subj, vals in subject_map.items():
            total = vals["held"]
            present = vals["attended"]
            if total > 0:
                percentage = round((present / total) * 100, 1)
                subjects.append({
                    "subject": subj,
                    "present": present,
                    "total": total,
                    "percentage": percentage,
                    "safe": percentage >= 75,
                })

    if not subjects:
        # Return raw keys for debugging
        sample = att_data[0] if att_data else {}
        raise HTTPException(status_code=404, detail=f"Could not parse. Keys: {list(sample.keys())}")

    session_clients.pop(data.session_id, None)
    return {"name": student_name, "roll_no": data.roll_no, "subjects": subjects}

@app.get("/health")
def health():
    return {"status": "ok"}
