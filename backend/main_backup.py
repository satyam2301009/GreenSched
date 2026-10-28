
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DB_PATH = ROOT / "data" / "greensched.db"

app = FastAPI(
    title="GreenSched API",
    description="Carbon-aware workload scheduling prototype",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Local development only
    allow_methods=["*"],
    allow_headers=["*"],
)


def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_db():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task_name TEXT NOT NULL,
                runtime_minutes INTEGER NOT NULL,
                energy_kwh REAL NOT NULL,
                region TEXT NOT NULL,
                carbon_intensity REAL NOT NULL,
                data_source TEXT NOT NULL,
                estimated_emissions_g REAL NOT NULL,
                status TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


@app.on_event("startup")
def startup():
    initialize_db()


class TaskRequest(BaseModel):
    task_name: str = Field(min_length=1, max_length=100)
    runtime_minutes: int = Field(default=5, ge=1, le=1440)
    energy_kwh: float = Field(default=0.1, gt=0, le=10000)


# Configure these regions and zones according to the API coverage
# and cloud regions you can actually use. Values below are demo data.
REGIONS = [
    {"region": "us-west-2", "zone": "US-CAL-CISO", "demo_intensity": 180.0},
    {"region": "eu-west-1", "zone": "DE", "demo_intensity": 300.0},
    {"region": "ap-south-1", "zone": "IN-WE", "demo_intensity": 650.0},
]


async def get_intensity(region):
    api_key = os.getenv("ELECTRICITY_MAPS_API_KEY")
    if api_key:
        url = "https://api.electricitymaps.com/v3/carbon-intensity/latest"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(
                    url,
                    params={"zone": region["zone"]},
                    headers={"auth-token": api_key},
                )
                response.raise_for_status()
                data = response.json()
                intensity = data.get("carbonIntensity")
                if intensity is not None:
                    return float(intensity), "LIVE"
        except (httpx.HTTPError, ValueError, TypeError):
            pass

    return region["demo_intensity"], "SIMULATED"


@app.get("/")
def home():
    return {
        "message": "GreenSched API is running",
        "docs": "/docs",
        "database": str(DB_PATH),
    }


@app.get("/api/health")
def health():
    return {"status": "healthy", "database": "SQLite"}


@app.get("/api/regions")
async def list_regions():
    results = []
    for region in REGIONS:
        intensity, source = await get_intensity(region)
        results.append({
            **region,
            "carbon_intensity_gco2_kwh": intensity,
            "source": source,
        })
    return {"regions": results}


@app.post("/api/tasks")
async def create_task(request: TaskRequest):
    candidates = []
    for region in REGIONS:
        intensity, source = await get_intensity(region)
        candidates.append((intensity, source, region))

    intensity, source, selected_region = min(candidates, key=lambda x: x[0])
    task_id = str(uuid.uuid4())[:8]
    emissions = request.energy_kwh * intensity

    # Local demo execution only. No remote cloud task is launched here.
    status = "completed"
    execution_mode = "LOCAL_DEMO"

    created_at = datetime.now(timezone.utc).isoformat()

    with connect_db() as db:
        db.execute("""
            INSERT INTO tasks (
                task_id, task_name, runtime_minutes, energy_kwh,
                region, carbon_intensity, data_source,
                estimated_emissions_g, status, execution_mode, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, request.task_name, request.runtime_minutes,
            request.energy_kwh, selected_region["region"], intensity,
            source, emissions, status, execution_mode, created_at,
        ))

    return {
        "task_id": task_id,
        "task_name": request.task_name,
        "selected_region": selected_region["region"],
        "electricity_zone": selected_region["zone"],
        "carbon_intensity_gco2_kwh": intensity,
        "carbon_data_source": source,
        "estimated_emissions_gco2": round(emissions, 3),
        "status": status,
        "execution_mode": execution_mode,
        "note": "Local demo record only; no remote cloud execution occurred.",
    }


@app.get("/api/tasks")
def list_tasks():
    with connect_db() as db:
        rows = db.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        ).fetchall()
    return {"tasks": [dict(row) for row in rows]}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    with connect_db() as db:
        row = db.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return dict(row)
