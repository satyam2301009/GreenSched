
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4
import subprocess
import tempfile
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from apscheduler.schedulers.background import BackgroundScheduler

from app.db import Base, engine, SessionLocal
from app.models import Task


# Create database tables if they do not exist.
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="GreenSched API",
    description="Carbon-aware scheduler for laptop computing tasks",
    version="0.5.0",
)

scheduler = BackgroundScheduler(timezone="UTC")

# SIMULATED DEMO DATA. These are not real electricity measurements.
CARBON_FORECAST = [
    420, 380, 300, 220, 180, 240, 310, 400,
    350, 260, 190, 230, 320, 410, 370, 280,
    200, 170, 250, 330, 390, 300, 220, 180,
]


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    task_type: Literal[
        "python", "coding", "ml_training", "data_processing"
    ]
    duration_minutes: int = Field(gt=0, le=1440)
    deadline: datetime
    description: str = Field(default="", max_length=500)
    script_code: str = Field(default="", max_length=20000)


def serialize_task(task):
    """Return task details without exposing executable source code."""
    return {
        "id": task.id,
        "name": task.name,
        "task_type": task.task_type,
        "duration_minutes": task.duration_minutes,
        "deadline": task.deadline,
        "description": task.description,
        "status": task.status,
        "created_at": task.created_at,
        "scheduled_start": task.scheduled_start,
        "scheduled_finish": task.scheduled_finish,
        "carbon_intensity_gco2_kwh": task.carbon_intensity,
        "stdout": task.stdout,
        "stderr": task.stderr,
        "exit_code": task.exit_code,
    }


def schedule_job(task_id: str, run_at: datetime):
    """Register one future execution with APScheduler."""
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)

    if run_at <= datetime.now(timezone.utc):
        return False

    scheduler.add_job(
        execute_task,
        trigger="date",
        run_date=run_at,
        args=[task_id],
        id=f"task_{task_id}",
        replace_existing=True,
        misfire_grace_time=60,
    )
    return True


@app.on_event("startup")
def start_scheduler():
    if not scheduler.running:
        scheduler.start()

    # Recover future scheduled tasks from SQLite after a restart.
    with SessionLocal() as db:
        scheduled_tasks = db.scalars(
            select(Task).where(Task.status == "scheduled")
        ).all()

        for task in scheduled_tasks:
            if not task.scheduled_start:
                continue

            run_at = datetime.fromisoformat(task.scheduled_start)

            # Avoid unexpectedly running jobs whose time has passed.
            if run_at <= datetime.now(timezone.utc):
                continue

            schedule_job(task.id, run_at)


@app.on_event("shutdown")
def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/")
def root():
    return {
        "message": "Welcome to GreenSched API",
        "version": "0.5.0",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "GreenSched",
        "scheduler_running": scheduler.running,
    }


@app.post("/tasks", status_code=201)
def create_task(data: TaskCreate):
    now = datetime.now(timezone.utc)
    deadline = data.deadline

    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)

    if deadline <= now:
        raise HTTPException(
            status_code=400,
            detail="Deadline must be in the future",
        )

    task = Task(
        id=str(uuid4())[:8],
        name=data.name,
        task_type=data.task_type,
        duration_minutes=data.duration_minutes,
        deadline=deadline.isoformat(),
        description=data.description,
        script_code=data.script_code,
        status="pending",
        created_at=now.isoformat(),
    )

    with SessionLocal() as db:
        db.add(task)
        db.commit()
        db.refresh(task)
        return serialize_task(task)


@app.get("/tasks")
def list_tasks():
    with SessionLocal() as db:
        result = db.scalars(
            select(Task).order_by(Task.created_at.desc())
        ).all()

        return {
            "count": len(result),
            "tasks": [serialize_task(task) for task in result],
        }


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    with SessionLocal() as db:
        task = db.get(Task, task_id)

        if task is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found",
            )

        return serialize_task(task)


@app.post("/schedule/{task_id}")
def schedule_task(task_id: str):
    with SessionLocal() as db:
        task = db.get(Task, task_id)

        if task is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found",
            )

        if task.status in ("running", "completed"):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot schedule task with status {task.status}",
            )

        now = datetime.now(timezone.utc)
        deadline = datetime.fromisoformat(task.deadline)

        next_hour = (
            now.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1)
        )

        candidates = []

        # Find hourly start times that fit before the deadline.
        for hour in range(24):
            start = next_hour + timedelta(hours=hour)
            finish = start + timedelta(minutes=task.duration_minutes)

            if finish <= deadline:
                candidates.append({
                    "start": start,
                    "finish": finish,
                    "intensity": CARBON_FORECAST[start.hour],
                })

        if not candidates:
            raise HTTPException(
                status_code=400,
                detail="No available hourly slot fits before the deadline",
            )

        best = min(candidates, key=lambda item: item["intensity"])

        task.scheduled_start = best["start"].isoformat()
        task.scheduled_finish = best["finish"].isoformat()
        task.carbon_intensity = best["intensity"]
        task.status = "scheduled"

        db.commit()

        result = {
            "task_id": task.id,
            "task_name": task.name,
            "scheduled_start": task.scheduled_start,
            "scheduled_finish": task.scheduled_finish,
            "carbon_intensity_gco2_kwh": task.carbon_intensity,
            "forecast_source": "simulated_demo_data",
            "status": task.status,
        }

        run_at = best["start"]

    # Register only after the database transaction has committed.
    schedule_job(task_id, run_at)

    return result


@app.get("/schedule")
def list_schedules():
    with SessionLocal() as db:
        result = db.scalars(
            select(Task).where(Task.scheduled_start.is_not(None))
        ).all()

        return {
            "count": len(result),
            "schedules": [
                {
                    "task_id": task.id,
                    "task_name": task.name,
                    "scheduled_start": task.scheduled_start,
                    "scheduled_finish": task.scheduled_finish,
                    "carbon_intensity_gco2_kwh": task.carbon_intensity,
                    "status": task.status,
                }
                for task in result
            ],
        }


@app.post("/tasks/{task_id}/execute")
def execute_task(task_id: str):
    # Read the task and mark it as running before launching the process.
    with SessionLocal() as db:
        task = db.get(Task, task_id)

        if task is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found",
            )

        if task.task_type != "python":
            raise HTTPException(
                status_code=400,
                detail="Only Python tasks are supported by this executor",
            )

        if not task.script_code.strip():
            raise HTTPException(
                status_code=400,
                detail="No Python script provided for this task",
            )

        if task.status in ("running", "completed"):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot execute task with status {task.status}",
            )

        code = task.script_code
        task.status = "running"
        db.commit()

    started_at = datetime.now(timezone.utc).isoformat()
    stdout = ""
    stderr = ""
    exit_code = None
    final_status = "failed"

    try:
        with tempfile.TemporaryDirectory(
            prefix="greensched_"
        ) as temp_dir:
            script_path = Path(temp_dir) / "task_script.py"
            script_path.write_text(code, encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "-I", str(script_path)],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )

            stdout = result.stdout[-10000:]
            stderr = result.stderr[-10000:]
            exit_code = result.returncode

            final_status = (
                "completed" if exit_code == 0 else "failed"
            )

    except subprocess.TimeoutExpired:
        stderr = "Execution timed out after 30 seconds"

    except OSError as exc:
        stderr = str(exc)

    finished_at = datetime.now(timezone.utc).isoformat()

    # Save execution results to SQLite.
    with SessionLocal() as db:
        task = db.get(Task, task_id)

        if task is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found",
            )

        task.status = final_status
        task.stdout = stdout
        task.stderr = stderr
        task.exit_code = exit_code
        db.commit()

        return {
            "task_id": task.id,
            "task_name": task.name,
            "status": task.status,
            "stdout": task.stdout,
            "stderr": task.stderr,
            "exit_code": task.exit_code,
            "started_at": started_at,
            "finished_at": finished_at,
        }
