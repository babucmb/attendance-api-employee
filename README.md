# Employee Attendance & Analytics API

This is my Software Engineer Trainee assignment project. It is a small API for
recording employee attendance and getting monthly attendance reports. It uses
Python, FastAPI, and MongoDB.

## What the API can do

- Add employees and view the employee list.
- Record punch-in and punch-out times, following the assignment's attendance
  rules.
- Correct an attendance record while keeping an audit trail of the change.
- Show monthly employee and department reports, late-arrival rankings, and
  department attendance trends.
- Explain the MongoDB query plans for selected endpoints.

The API contract and attendance rules are described in `openapi.yaml`. The
MongoDB document shapes are described in `DATA_MODEL.md`.

## Run the project on Windows

You need Python 3.11 or newer and a running MongoDB 6.0 or newer database.

1. Open PowerShell in the project folder.
2. Create and activate a Python virtual environment, then install the packages:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Configure the database connection. Create a local `.env` file with your own
   database settings:

   ```text
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_assignment
   ```

   Use the URI for your own MongoDB instance if it is not running locally. Do
   not upload `.env` or database credentials to GitHub.

4. Start the API:

   ```powershell
   uvicorn app.main:app --port 8000 --reload
   ```

   Keep this terminal open while using the API.

## How to check that it works

1. Open `http://127.0.0.1:8000/health` in a browser. A healthy response means
   the API can reach MongoDB.
2. Open `http://127.0.0.1:8000/docs` to see the interactive API page.
3. In `/docs`, try `GET /employees` first. To try creating or changing data, use
   only your disposable assignment database. The page shows each endpoint's
   required fields and example response.
4. Run the unit tests from another PowerShell terminal in the project folder:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   python -m unittest discover -s tests -v
   ```

   The unit tests check business rules and query construction. They do not seed
   or change MongoDB. To also run the live HTTP and MongoDB integration tests,
   make sure MongoDB is running and configured through `MONGO_URI`, then run:

   ```powershell
   $env:RUN_MONGODB_INTEGRATION = "1"
   python -m unittest tests.test_mongodb_integration -v
   Remove-Item Env:RUN_MONGODB_INTEGRATION
   ```

   The integration tests start the API on a temporary local port and use a
   uniquely named temporary database on the configured MongoDB server. They
   drop that test database when finished; they do not write to `MONGO_DB`.
   `WORKED.md` records the test coverage and latest run results.

## Sample data safety

`sample_seed.py` loads the assignment's sample data into MongoDB. It refuses to
overwrite collections that already contain data unless `--replace` is provided.
Only use it with an empty, disposable assignment database:

```powershell
python sample_seed.py
```

The replacement option clears and reloads both collections. Do not use it if
you need to keep the data in that database:

```powershell
python sample_seed.py --replace
```

## Assignment status

This submission implements the requested employee attendance API, including
punch-in and punch-out rules, audited manual corrections, MongoDB aggregation
analytics, and query-plan explanations. No assignment feature is intentionally
left incomplete.

The nine unit tests pass. Two additional live API and MongoDB integration tests
also pass when run against a disposable test database; see the test instructions
above. The integration suite does not use or clear the configured assignment
database.
