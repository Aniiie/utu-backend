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

        # Step 2: Load attendance page
        att_page = await client.get(ATTENDANCE_PAGE_URL, headers={"User-Agent": "Mozilla/5.0"})
        page_text = att_page.text
        att_soup = BeautifulSoup(page_text, "html.parser")

        # Find StudentAdmissionId — it's in a hidden input with id containing "admission"
        admission_id = ""
        college_id = "61"
        course_id = "1"
        branch_id = "1"
        duration_id = "2"
        student_name = ""

        # Search ALL hidden inputs
        for inp in att_soup.find_all("input", {"type": "hidden"}):
            inp_id = (inp.get("id") or "").lower()
            inp_name = (inp.get("name") or "").lower()
            val = inp.get("value", "")
            if not val:
                continue
            if "admission" in inp_id or "admission" in inp_name:
                admission_id = val
            elif "collegeid" in inp_id or "collegeid" in inp_name:
                college_id = val
            elif "courseid" in inp_id or "courseid" in inp_name:
                if "branch" not in inp_id:
                    course_id = val
            elif "branchid" in inp_id or "branchid" in inp_name:
                if "duration" not in inp_id:
                    branch_id = val
            elif "duration" in inp_id or "duration" in inp_name:
                duration_id = val

        # Also search in ALL inputs (not just hidden)
        if not admission_id:
            for inp in att_soup.find_all("input"):
                inp_id = (inp.get("id") or "").lower()
                val = inp.get("value", "")
                if "admission" in inp_id and val:
                    admission_id = val
                    break

        # Search in page source for the value
        if not admission_id:
            patterns = [
                r'id=["\'].*?admission.*?["\'][^>]*value=["\'](\d+)["\']',
                r'value=["\'](\d+)["\'][^>]*id=["\'].*?admission.*?["\']',
                r'admission.*?value.*?["\'](\d+)["\']',
                r'"admission"\s*:\s*"?(\d+)"?',
                r"admission.*?=.*?'(\d+)'",
                r'admission.*?=.*?"(\d+)"',
            ]
            for p in patterns:
                m = re.search(p, page_text, re.IGNORECASE)
                if m:
                    admission_id = m.group(1)
                    break

        # Get student name
        for td in att_soup.find_all("td"):
            text = td.get_text(strip=True)
            if text and len(text) > 5 and text.replace(" ","").isalpha() and text.isupper() and len(text.split()) >= 2:
                student_name = text
                break

        if not admission_id:
            # Return all hidden input ids for debugging
            all_ids = [(inp.get("id",""), inp.get("name",""), inp.get("value","")) 
                      for inp in att_soup.find_all("input", {"type": "hidden"})]
            raise HTTPException(status_code=404, detail=f"Cannot find AdmissionId. Hidden inputs: {all_ids[:10]}")

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
            "User-Agent": "Mozilla/5.0",
            "Referer": ATTENDANCE_PAGE_URL,
            "X-Requested-With": "XMLHttpRequest"
        })

        if att_resp.status_code != 200:
            raise HTTPException(status_code=att_resp.status_code,
                detail=f"API {att_resp.status_code}. IDs: college={college_id},course={course_id},branch={branch_id},admission={admission_id},duration={duration_id}")

        try:
            att_data = att_resp.json()
        except:
            raise HTTPException(status_code=502, detail="Could not parse attendance response.")

        if not att_data:
            raise HTTPException(status_code=404, detail="No attendance data for this month.")

        # Parse JSON
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
