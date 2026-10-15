# Employee Attendance & Analytics API

My submission for the HROne Software Engineer Trainee assignment, built
with Python, FastAPI, and MongoDB.

## Current progress

-   FastAPI setup and database health check
-   Create and list employees, including validation, filtering, and
    pagination
-   MongoDB indexes and response serialization helpers
-   Attendance punch-in and listing code added, but still needs
    end-to-end testing

## Setup

Create and activate a virtual environment:

``` bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

``` bash
pip install fastapi "uvicorn[standard]" pymongo python-dotenv
```

Create a `.env` file:

``` dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Run the API from the project root:

``` bash
uvicorn app.main:app --reload --port 8000
```

Open `http://127.0.0.1:8000/docs` to test the API.

## Remaining work

1.  Test the current endpoints with MongoDB.
2.  Implement punch-out and attendance regularization, including history
    tracking.
3.  Add monthly, department, trend, and late-arrival analytics.
4.  Implement the query-plan endpoint.
5.  Test edge cases and verify responses against `openapi.yaml` and
    `DATA_MODEL.md`.

This is a **partial submission**. I'll complete the remaining phases one
by one, test them, and update this README as the project progresses.
