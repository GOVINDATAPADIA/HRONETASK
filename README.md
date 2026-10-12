# Employee Attendance & Analytics API

A FastAPI + MongoDB backend for employee attendance tracking and analytics.

## Prerequisites
- Python 3.10+
- MongoDB 6.0+ (MongoDB 7.0 recommended; Atlas or local Docker `docker run -d -p 27017:27017 mongo:7`)

## Running Locally

1. Create and activate a virtual environment:
```bash
python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows:
.\.venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. (Optional) Set environment variables or use a local `.env`:
```bash
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*Note: Real environment variables take precedence over `.env` defaults.*

4. Seed the database with sample data:
```bash
python sample_seed.py
```

5. Start the API service:
```bash
uvicorn app.main:app --port 8000
```
API documentation is available at `http://localhost:8000/docs`.

## Completed Deliverables
- `app/main.py`: Complete implementation with all helpers, endpoints, aggregation pipelines, and startup index creation.
- `REVIEW.md`: Detailed defect analysis and fixes for the starter code.
- `DECISIONS.md`: Design rationale for indexes, concurrency safety, ties, headcount, and scalability.
