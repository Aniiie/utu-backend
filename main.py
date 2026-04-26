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
STUDENT_INFO_API = "https://online.uktech.ac.in/ums/Student/User/GetStudentDetailByRollNoDOB"

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
    session_year = year - 1

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

        # Step 2: Load attendance page and extract correct IDs from JS
        att_page = await client.get(ATTENDANCE_PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
        page_text = att_page.text

        # The correct IDs are embedded in JavaScript function calls on the page
        # Look for patterns like: GetCourseBranchDurationForAttendance?BranchId=1&CourseId=1
        # and StudentAdmissionId in the page JS

        college_id = "61"
        course_id = "1"
        branch_id = "1"
        duration_id = "2"
        admission_id = ""
        student_name = ""

        # Search for StudentAdmissionId in page JS
        patterns = [
            r'StudentAdmissionId["\s]*[:=]["\s]*["\']?(\d+)',
            r'admissionId["\s]*[:=]["\s]*["\']?(\d+)',
            r'AdmissionId["\s]*[:=]["\s]*["\']?(\d+)',
            r'"StudentAdmissionId"\s*:\s*(\d+)',
            r"'StudentAdmissionId'\s*:\s*(\d+)",
        ]
        for p in patterns:
            m = re.search(p, page_text, re.IGNORECASE)
            if m:
                admission_id = m.group(1)
                break

        # Search for CollegeId
        for p in [r'"CollegeId"\s*:\s*(\d+)', r"CollegeId\s*=\s*(\d+)", r'CollegeId["\s]*[:=]["\s]*(\d+)']:
            m = re.search(p, page_text, re.IGNORECASE)
            if m and m.group(1) != "0":
                college_id = m.group(1)
                break

        # Search for CourseBranchDurationId
        for p in [r'"CourseBranchDurationId"\s*:\s*(\d+)', r'CourseBranchDurationId\s*=\s*(\d+)', r'DurationId["\s]*[:=]["\s]*(\d+)']:
            m = re.search(p, page_text, re.IGNORECASE)
            if m and m.group(1) != "0":
                duration_id = m.group(1)
                break

        # Get student name from table
        att_soup = BeautifulSoup(page_text, "html.parser")
        for td in att_soup.find_all("td"):
            text = td.get_text(strip=True)
            if text and len(text) > 5 and text.replace(" ", "").isalpha() and text.isupper():
                student_name = text
                break

        # Step 3: If still no admission_id, try the student detail API
        if not admission_id:
            detail_resp = await client.get(
                STUDENT_INFO_API,
                params={"RollNo": data.roll_no, "DOB": data.dob},
                headers={"User-Agent": "Mozilla/5.0", "Referer": ATTENDANCE_PAGE_URL,
                        "X-Requested-With": "XMLHttpRequest"}
            )
            if detail_resp.status_code == 200:
                try:
                    detail = detail_resp.json()
                    if isinstance(detail, list) and detail:
                        detail = detail[0]
                    admission_id = str(detail.get("StudentAdmissionId") or detail.get("admissionId") or "")
                    college_id = str(detail.get("CollegeId") or college_id)
                    course_id = str(detail.get("CourseId") or course_id)
                    branch_id = str(detail.get("BranchId") or branch_id)
                    duration_id = str(detail.get("CourseBranchDurationId") or duration_id)
                    student_name = detail.get("StudentName") or student_name
                except:
                    pass

        if not admission_id:
            raise HTTPException(status_code=404, detail="Could not find StudentAdmissionId. Please contact support.")

        # Step 4: Call attendance API
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

        if att_resp.status_code != 200:
            raise HTTPException(status_code=att_resp.status_code,
                detail=f"API error {att_resp.status_code}. IDs: college={college_id}, course={course_id}, branch={branch_id}, admission={admission_id}, duration={duration_id}")

        try:
            att_data = att_resp.json()
        except:
            raise HTTPException(status_code=502, detail="Could not parse attendance response.")

        if not att_data:
            raise HTTPException(status_code=404, detail="No attendance data for this month.")

        # Step 5: Parse
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
            raise HTTPException(status_code=404, detail=f"Could not parse. Keys: {list(sample.keys())}")

    session_clients.pop(data.session_id, None)
    return {"name": student_name, "roll_no": data.roll_no, "subjects": subjects}

@app.get("/health")
def health():
    return {"status": "ok"}
