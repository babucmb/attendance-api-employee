"""Opt-in HTTP and MongoDB integration tests using a disposable database.

Run with RUN_MONGODB_INTEGRATION=1 after configuring MONGO_URI in .env or the
environment. The test database has a unique name and is dropped after the run.
"""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from pymongo import MongoClient


ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))


def request_json(base_url: str, path: str, method: str = "GET", body: dict | None = None):
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"{base_url}{path}",
        data=payload,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method=method,
    )
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


@unittest.skipUnless(
    os.getenv("RUN_MONGODB_INTEGRATION") == "1",
    "set RUN_MONGODB_INTEGRATION=1 to run isolated MongoDB/HTTP tests",
)
class MongoDBApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_dotenv(ROOT / ".env")
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        cls.database_name = f"attendance_api_test_{uuid.uuid4().hex}"
        cls.mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000, tz_aware=True)
        cls.mongo_client.admin.command("ping")

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        cls.base_url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment["MONGO_URI"] = mongo_uri
        environment["MONGO_DB"] = cls.database_name
        cls.server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if cls.server.poll() is not None:
                    raise RuntimeError("Uvicorn exited before the integration API became ready")
                try:
                    status, _ = request_json(cls.base_url, "/health")
                    if status == 200:
                        return
                except (OSError, URLError, TimeoutError):
                    time.sleep(0.2)
            raise RuntimeError("Timed out waiting for the integration API to become ready")
        except BaseException:
            if cls.server.poll() is None:
                cls.server.terminate()
                cls.server.wait(timeout=5)
            cls.mongo_client.drop_database(cls.database_name)
            cls.mongo_client.close()
            raise

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None) is not None and cls.server.poll() is None:
            cls.server.terminate()
            cls.server.wait(timeout=5)
        if getattr(cls, "mongo_client", None) is not None:
            cls.mongo_client.drop_database(cls.database_name)
            cls.mongo_client.close()

    def setUp(self):
        database = self.mongo_client[self.database_name]
        database.employees.delete_many({})
        database.attendance_logs.delete_many({})

    def create_employee(self, emp_code: str, name: str):
        return request_json(
            self.base_url,
            "/employees",
            "POST",
            {
                "emp_code": emp_code,
                "name": name,
                "email": f"{emp_code.lower()}@example.com",
                "department": "Research",
                "shift_start": "09:00",
                "shift_end": "17:00",
                "joined_on": "2026-07-01",
            },
        )

    def test_concurrent_writes_punch_out_and_regularization_responses(self):
        employee_request = lambda: self.create_employee("EMP1001", "Employee One")
        with ThreadPoolExecutor(max_workers=2) as pool:
            create_results = list(pool.map(lambda _: employee_request(), range(2)))
        self.assertEqual(sorted(status for status, _ in create_results), [201, 409])
        employee = next(body for status, body in create_results if status == 201)
        self.assertIsInstance(employee["created_at"], int)
        self.assertNotIn("_id", employee)
        self.assertEqual(request_json(self.base_url, "/employees?department=Research")[1]["total"], 1)

        punch_time = timestamp_ms(datetime(2026, 7, 6, 9, 0, tzinfo=IST))
        punch_request = lambda: request_json(
            self.base_url,
            "/attendance/punch-in",
            "POST",
            {"emp_code": "EMP1001", "punched_at": punch_time, "status": "PRESENT"},
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            punch_results = list(pool.map(lambda _: punch_request(), range(2)))
        self.assertEqual(sorted(status for status, _ in punch_results), [201, 409])
        attendance = next(body for status, body in punch_results if status == 201)
        self.assertEqual(attendance["date"], "2026-07-06")
        self.assertEqual(attendance["punch_in"], punch_time)
        self.assertEqual(attendance["history"], [])
        self.assertNotIn("_id", attendance)

        punch_out = timestamp_ms(datetime(2026, 7, 6, 17, 45, tzinfo=IST))
        status, attendance = request_json(
            self.base_url,
            "/attendance/punch-out",
            "POST",
            {"emp_code": "EMP1001", "punched_at": punch_out},
        )
        self.assertEqual(status, 200)
        self.assertEqual(attendance["work_hours"], 8.75)
        self.assertEqual(attendance["overtime_minutes"], 45)

        corrected_out = timestamp_ms(datetime(2026, 7, 6, 18, 0, tzinfo=IST))
        status, attendance = request_json(
            self.base_url,
            "/attendance/EMP1001/2026-07-06",
            "PATCH",
            {
                "punch_out": corrected_out,
                "reason": "Corrected from approved record",
                "regularized_by": "reviewer",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(attendance["work_hours"], 9.0)
        self.assertEqual(attendance["overtime_minutes"], 60)
        self.assertEqual(len(attendance["history"]), 1)
        self.assertEqual(attendance["history"][0]["changes"]["punch_out"]["to"], corrected_out)

        status, _ = request_json(
            self.base_url,
            "/attendance/punch-out",
            "POST",
            {"emp_code": "EMP1001", "punched_at": corrected_out + 60_000},
        )
        self.assertEqual(status, 409)

    def test_analytics_values_gap_filling_and_indexed_explain(self):
        for emp_code, name in (("EMP2001", "Employee Two"), ("EMP2002", "Employee Three")):
            status, _ = self.create_employee(emp_code, name)
            self.assertEqual(status, 201)

        def attendance(emp_code, day, status, hours, late, overtime=0, half_day=False):
            return {
                "emp_code": emp_code,
                "date": day,
                "status": status,
                "punch_in": datetime.fromisoformat(f"{day}T03:30:00+00:00"),
                "punch_out": datetime.fromisoformat(f"{day}T12:30:00+00:00"),
                "work_hours": hours,
                "late_minutes": late,
                "overtime_minutes": overtime,
                "half_day": half_day,
                "history": [],
            }

        self.mongo_client[self.database_name].attendance_logs.insert_many(
            [
                attendance("EMP2001", "2026-07-06", "PRESENT", 8.0, 35),
                attendance("EMP2001", "2026-07-07", "PRESENT", 8.0, 0, 60),
                attendance("EMP2002", "2026-07-06", "WFH", 4.49, 35, half_day=True),
            ]
        )

        status, monthly = request_json(
            self.base_url,
            "/analytics/employees/EMP2001/monthly?month=2026-07",
        )
        self.assertEqual(status, 200)
        self.assertEqual(monthly["working_days"], 23)
        self.assertEqual(monthly["present_days"], 2.0)
        self.assertEqual(monthly["late_count"], 1)
        self.assertEqual(monthly["total_overtime_minutes"], 60)

        status, summary = request_json(
            self.base_url,
            "/analytics/departments/summary?month=2026-07&department=Research",
        )
        self.assertEqual(status, 200)
        research = summary["items"][0]
        self.assertEqual(research["headcount"], 2)
        self.assertEqual(research["present_days"], 2.5)
        self.assertEqual(research["avg_work_hours"], 6.83)
        self.assertEqual(research["total_late_minutes"], 70)

        status, leaderboard = request_json(
            self.base_url,
            "/analytics/leaderboard/late?month=2026-07&department=Research",
        )
        self.assertEqual(status, 200)
        self.assertEqual({item["rank"] for item in leaderboard["items"]}, {1})
        self.assertEqual(len(leaderboard["items"]), 2)

        status, trend = request_json(
            self.base_url,
            "/analytics/departments/Research/trend?"
            + urlencode({"from": "2026-07-06", "to": "2026-07-08"}),
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["date"] for item in trend["items"]], [
            "2026-07-06",
            "2026-07-07",
            "2026-07-08",
        ])
        self.assertEqual(trend["items"][2]["present_count"], 0)

        status, explain = request_json(
            self.base_url,
            "/admin/explain/attendance_list?emp_code=EMP2001",
        )
        self.assertEqual(status, 200)
        stages = []

        def collect_stages(value):
            if isinstance(value, dict):
                if isinstance(value.get("stage"), str):
                    stages.append(value["stage"])
                for nested in value.values():
                    collect_stages(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect_stages(nested)

        collect_stages(explain["explain"])
        self.assertIn("IXSCAN", stages)
        self.assertNotIn("COLLSCAN", stages)


if __name__ == "__main__":
    unittest.main()
