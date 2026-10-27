
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
from live_carbon import get_carbon_forecast


Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="GreenSched API",
    description="Carbon-aware scheduler for laptop computing tasks",
    version="0.6.0",
)

scheduler = BackgroundScheduler(timezone="UTC")


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


def as_utc(value):
    """Convert a datetime to UTC, treating naive values as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def schedule_job(task_id: str, run_at: datetime):
    """Register a future task execution with APScheduler."""
    run_at = as_utc(run_at)

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

    # Recover future scheduled tasks after an application restart.
    with SessionLocal() as db:
        scheduled_tasks = db.scalars(
            select(Task).where(Task.status == "scheduled")
        ).all()

        for task in scheduled_tasks:
            if not task.scheduled_start:
                continue

            run_at = as_utc(
                datetime.fromisoformat(task.scheduled_start)
            )

            # Do not unexpectedly execute overdue tasks.
            if run_at <= datetime.now(timezone.utc):
                continue

            try:
                schedule_job(task.id, run_at)
            except Exception:
                # Keep the application running if one job cannot be restored.
                continue


@app.on_event("shutdown")
def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/")
def root():
    return {
        "message": "Welcome to GreenSched API",
        "version": "0.6.0",
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
    deadline = as_utc(data.deadline)

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
        tasks = db.scalars(
            select(Task).order_by(Task.created_at.desc())
        ).all()

        return {
            "count": len(tasks),
            "tasks": [serialize_task(task) for task in tasks],
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
    # Get a forecast before changing the database.
    try:
        forecast_data = get_carbon_forecast(
            zone="IN",
            horizon_hours=24,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Carbon forecast unavailable: {exc}",
        ) from None

    forecast_points = forecast_data.get("forecast", [])

    if not forecast_points:
        raise HTTPException(
            status_code=503,
            detail="No usable carbon forecast points are available",
        )

    # Normalize and sort forecast timestamps.
    points = []

    for point in forecast_points:
        try:
            point_time = as_utc(point["datetime"])
            intensity = float(
                point["carbon_intensity_gco2_kwh"]
            )

            if intensity < 0:
                continue

            points.append({
                "datetime": point_time,
                "intensity": intensity,
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            continue

    points.sort(key=lambda point: point["datetime"])

    if not points:
        raise HTTPException(
            status_code=503,
            detail="Forecast contains no valid data points",
        )

    # Build a lookup of forecast values by timestamp.
    intensity_by_time = {
        point["datetime"]: point["intensity"]
        for point in points
    }

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
        deadline = as_utc(datetime.fromisoformat(task.deadline))

        # Require the whole task duration to fit before its deadline.
        duration = timedelta(minutes=task.duration_minutes)

        if deadline <= now:
            raise HTTPException(
                status_code=400,
                detail="Task deadline has already passed",
            )

        # Start no earlier than the next full UTC hour.
        next_hour = (
            now.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1)
        )

        # Reserve intervals belonging to other scheduled tasks.
        existing_tasks = db.scalars(
            select(Task).where(
                Task.status == "scheduled",
                Task.scheduled_start.is_not(None),
                Task.scheduled_finish.is_not(None),
                Task.id != task_id,
            )
        ).all()

        reserved_slots = []

        for existing in existing_tasks:
            reserved_start = as_utc(
                datetime.fromisoformat(existing.scheduled_start)
            )
            reserved_finish = as_utc(
                datetime.fromisoformat(existing.scheduled_finish)
            )
            reserved_slots.append(
                (reserved_start, reserved_finish)
            )

        candidates = []

        # Candidate starts come from the forecast timestamps.
        for start in sorted(intensity_by_time):
            if start < next_hour:
                continue

            finish = start + duration

            if finish > deadline:
                continue

            # Prevent overlapping scheduled tasks.
            overlaps = any(
                start < reserved_finish
                and finish > reserved_start
                for reserved_start, reserved_finish in reserved_slots
            )

            if overlaps:
                continue

            # Compute a duration-weighted average using hourly forecast
            # points. Refuse candidates if any required hour is missing.
            total_weighted_intensity = 0.0
            covered_minutes = 0
            cursor = start

            while cursor < finish:
                intensity = intensity_by_time.get(cursor)

                if intensity is None:
                    break

                segment_end = min(
                    cursor + timedelta(hours=1),
                    finish,
                )
                segment_minutes = (
                    segment_end - cursor
                ).total_seconds() / 60

                total_weighted_intensity += (
                    intensity * segment_minutes
                )
                covered_minutes += segment_minutes
                cursor = segment_end

            if covered_minutes != task.duration_minutes:
                continue

            average_intensity = (
                total_weighted_intensity
                / task.duration_minutes
            )

            candidates.append({
                "start": start,
                "finish": finish,
                "intensity": round(average_intensity, 2),
            })

        if not candidates:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No valid forecast slot fits the task duration "
                    "and deadline without overlapping another task. "
                    "Try a later deadline or a shorter task duration."
                ),
            )

        # Choose the candidate with the lowest forecast intensity.
        best = min(
            candidates,
            key=lambda item: item["intensity"],
        )

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
            "forecast_source": forecast_data.get(
                "source", "Electricity Maps API"
            ),
            "forecast_zone": forecast_data.get("zone", "IN"),
            "forecast_updated_at": forecast_data.get("updated_at"),
            "forecast_granularity": forecast_data.get(
                "temporal_granularity", "unknown"
            ),
            "data_type": "forecast",
            "status": task.status,
        }

        run_at = best["start"]

    # Register only after the database commit. If registration fails,
    # revert the task so it is not left falsely marked as scheduled.
    try:
        registered = schedule_job(task_id, run_at)
    except Exception:
        registered = False

    if not registered:
        with SessionLocal() as db:
            task = db.get(Task, task_id)

            if task is not None and task.status == "scheduled":
                task.status = "pending"
                task.scheduled_start = None
                task.scheduled_finish = None
                task.carbon_intensity = None
                db.commit()

        raise HTTPException(
            status_code=503,
            detail=(
                "Could not register the scheduled job. "
                "The task has been returned to pending status."
            ),
        )

    return result


@app.get("/schedule")
def list_schedules():
    with SessionLocal() as db:
        tasks = db.scalars(
            select(Task).where(
                Task.scheduled_start.is_not(None)
            )
        ).all()

        return {
            "count": len(tasks),
            "schedules": [
                {
                    "task_id": task.id,
                    "task_name": task.name,
                    "scheduled_start": task.scheduled_start,
                    "scheduled_finish": task.scheduled_finish,
                    "carbon_intensity_gco2_kwh": task.carbon_intensity,
                    "status": task.status,
                }
                for task in tasks
            ],
        }


@app.post("/tasks/{task_id}/execute")
def execute_task(task_id: str):
    # Mark the task as running before starting its process.
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

    except OSError:
        stderr = "The Python task process could not be started"

    finished_at = datetime.now(timezone.utc).isoformat()

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
