[README.md](https://github.com/user-attachments/files/27080156/README.md)
# UTU Attendance Tracker

Check your UTU attendance directly — built for students of Veer Madho Singh Bhandari Uttarakhand Technical University.

---

## Project Structure

```
utu-attendance/
├── backend/
│   ├── main.py           ← FastAPI backend (scraper)
│   └── requirements.txt
└── frontend/
    └── src/App.jsx       ← React frontend
```

---

## Backend Setup

### 1. Install Python dependencies
```bash
cd backend
pip install -r requirements.txt
```

### 2. Run the server
```bash
uvicorn main:app --reload --port 8000
```

Backend will run at: `http://localhost:8000`

---

## Frontend Setup

### 1. Create React app
```bash
npx create-react-app utu-frontend
cd utu-frontend
```

### 2. Replace `src/App.jsx` with the provided `App.jsx`

### 3. Run the app
```bash
npm start
```

Frontend will run at: `http://localhost:3000`

---

## How It Works

1. App loads → fetches CAPTCHA image from UTU portal (session cookies stored on backend)
2. Student fills: Roll No., DOB (as password), and CAPTCHA text
3. Backend submits the form to UTU portal with the correct session cookies
4. Attendance data is scraped, parsed, and returned as JSON
5. Frontend displays subject-wise attendance with color coding:
   - 🟢 Green = 75%+ (safe)
   - 🟡 Yellow = 65–74% (warning)
   - 🔴 Red = below 65% (shortage)
   - Also shows: how many classes you can bunk OR how many you need to attend

---

## Deploying (for others to use)

**Backend:** Deploy to [Render](https://render.com) or [Railway](https://railway.app) (free tier)
- Set start command: `uvicorn main:app --host 0.0.0.0 --port 8000`

**Frontend:** Deploy to [Vercel](https://vercel.com) or [Netlify](https://netlify.com) (free)
- Update `API_BASE` in `App.jsx` to your deployed backend URL

---

## Notes
- This is a personal project, not affiliated with UTU
- Student credentials (DOB) are only sent to the official UTU portal, not stored anywhere
- CAPTCHA is fetched live from UTU and shown directly to the user
