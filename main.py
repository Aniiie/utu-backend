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

# Try multiple student info endpoints
STUDENT_INFO_URLS = [
    "https://online.uktech.ac.in/ums/Student/User/GetStudentInfo",
    "https://online.uktech.ac.in/ums/Student/User/GetStudentDetailByRollNo",
    "https://online.uktech.ac.in/ums/Student/User/GetStudentDetail",
    "https://online.uktech.ac.in/ums/Student/Public/GetStudentByRollNoDOB",
    "https://online.uktech.ac.in/ums/Student/User/GetStudentAdmissionDetail",
]

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

        # Step 2: Try to get student info from various APIs
        admission_id = ""
        college_id = "61"
        course_id = "1"
        branch_id = "1"
        duration_id = "2"
        student_name = ""

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": ATTENDANCE_PAGE_URL,
            "X-Requested-With": "XMLHttpRequest"
        }

        # Try each student info URL
        for url in STUDENT_INFO_URLS:
            for params in [
                {"RollNo": data.roll_no, "DOB": data.dob},
                {"rollNo": data.roll_no, "dob": data.dob},
                {"RollNo": data.roll_no},
            ]:
                try:
                    r = await client.get(url, params=params, headers=headers)
                    if r.status_code == 200 and r.text and r.text != "null":
                        d = r.json()
                        if isinstance(d, list) and d:
                            d = d[0]
                        if isinstance(d, dict):
                            aid = (d.get("StudentAdmissionId") or d.get("AdmissionId") or 
                                  d.get("admissionId") or d.get("studentAdmissionId"))
                            if aid:
                                admission_id = str(aid)
                                college_id = str(d.get("CollegeId") or d.get("collegeId") or college_id)
                                course_id = str(d.get("CourseId") or d.get("courseId") or course_id)
                                branch_id = str(d.get("BranchId") or d.get("branchId") or branch_id)
                                duration_id = str(d.get("CourseBranchDurationId") or duration_id)
                                student_name = str(d.get("StudentName") or d.get("studentName") or "")
                                break
                except:
                    continue
            if admission_id:
                break

        # Step 3: If still no ID, try loading ViewDetail page which has student info
        if not admission_id:
            detail_page = await client.get(
                "https://online.uktech.ac.in/ums/Student/User/ViewDetail",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            detail_text = detail_page.text
            
            # Search for admission ID in page source
            patterns = [
                r'StudentAdmissionId["\s:=\']+(\d+)',
                r'AdmissionId["\s:=\']+(\d+)',
                r'"Value"\s*:\s*(\d+).*?"Text"\s*:\s*"[^"]*admission',
                r'hdnStudentAdmission[^>]*value=["\'](\d+)',
            ]
            for p in patterns:
                m = re.search(p, detail_text, re.IGNORECASE)
                if m:
                    admission_id = m.group(1)
                    break

            # Also check the page URL after redirect - sometimes ID is in URL
            if str(detail_page.url) != "https://online.uktech.ac.in/ums/Student/User/ViewDetail":
                url_match = re.search(r'id=(\d+)', str(detail_page.url))
                if url_match:
                    admission_id = url_match.group(1)

        if not admission_id:
            raise HTTPException(status_code=404, 
                detail="Could not find StudentAdmissionId automatically. The portal uses JavaScript to load it.")

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

        att_resp = await client.get(ATTENDANCE_API_URL, params=params, headers=headers)

        if att_resp.status_code != 200:
            raise HTTPException(status_code=att_resp.status_code,
                detail=f"API {att_resp.status_code}. IDs: college={college_id},course={course_id},branch={branch_id},admission={admission_id},duration={duration_id}")

        try:
            att_data = att_resp.json()
        except:
            raise HTTPException(status_code=502, detail="Could not parse attendance response.")

        if not att_data:
            raise HTTPException(status_code=404, detail="No attendance data for this month.")

        # Parse
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
            raise HTTPException(status_code=404, detail=f"Keys found: {list(sample.keys())}")

    session_clients.pop(data.session_id, None)
    return {"name": student_name, "roll_no": data.roll_no, "subjects": subjects}

@app.get("/health")
def health():
    return {"status": "ok"}
