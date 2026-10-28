import asyncio
from backend.aws_worker import run_cloud_test, run_python_script
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.carbon_forecast import forecaster


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "data" / "greensched.db"

ELECTRICITY_MAPS_API_KEY = os.getenv("ELECTRICITY_MAPS_API_KEY")

# Configure an actual EC2 instance type for your intended workload.
# This is a configuration, not a claim that capacity is available.
AWS_INSTANCE_TYPE = os.getenv("AWS_INSTANCE_TYPE", "t3.small")
AWS_PRICING_REGION = os.getenv("AWS_PRICING_REGION", "us-east-1")

app = FastAPI(
    title="GreenSched API",
    description=(
        "Carbon-aware workload scheduling prototype. "
        "Unavailable real-world measurements are not fabricated."
    ),
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Local development only
    allow_methods=["*"],
    allow_headers=["*"],
)


# AWS region IDs must correspond to actual AWS regions.
# Electricity Maps zones represent electricity grids, not AWS regions.
REGIONS = [
    {
        "region": "us-west-2",
        "zone": "US-CAL-CISO",
        "aws_location_name": "US West (Oregon)",
    },
    {
        "region": "eu-central-1",
        "zone": "DE",
        "aws_location_name": "Europe (Frankfurt)",
    },
    {
        "region": "ap-south-1",
        "zone": "IN-WE",
        "aws_location_name": "Asia Pacific (Mumbai)",
    },
]


# ---------------------------------------------------------
# Database
# ---------------------------------------------------------

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
                created_at TEXT NOT NULL,
                execution_result TEXT
            )
        """)
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "execution_result" not in columns:
            db.execute("ALTER TABLE tasks ADD COLUMN execution_result TEXT")


@app.on_event("startup")
def startup():
    initialize_db()


# ---------------------------------------------------------
# Request validation
# ---------------------------------------------------------

class TaskRequest(BaseModel):
    task_name: str = Field(min_length=1, max_length=100)
    runtime_minutes: int = Field(default=5, ge=1, le=1440)
    energy_kwh: float = Field(default=0.1, gt=0, le=10000)
    # Only submit Python code you trust. It runs as ec2-user on your EC2 worker.
    python_script: str = Field(
        default="print('Hello from GreenSched on AWS EC2')",
        min_length=1,
        max_length=20000,
    )


# ---------------------------------------------------------
# Electricity Maps: live carbon and renewable data
# ---------------------------------------------------------

async def fetch_electricity_signal(zone: str, signal: str):
    """
    Returns (value, source, error).

    No synthetic fallback is used. Missing API access or missing
    signals result in None, with an explanatory error.
    """
    if not ELECTRICITY_MAPS_API_KEY:
        return None, "UNAVAILABLE", "API key not configured"

    endpoints = {
        "carbon": "carbon-intensity",
        "renewable": "renewable-energy",
    }

    if signal not in endpoints:
        return None, "UNAVAILABLE", "Unsupported signal"

    url = (
        "https://api.electricitymaps.com/v4/"
        f"{endpoints[signal]}/latest"
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                url,
                params={"zone": zone},
                headers={"auth-token": ELECTRICITY_MAPS_API_KEY},
            )
            response.raise_for_status()
            payload = response.json()

        # Support common response shapes without inventing values.
        raw_value = payload.get("value")

        if raw_value is None:
            raw_value = payload.get("carbonIntensity")

        if raw_value is None and isinstance(payload.get("data"), list):
            data = payload["data"]
            if data:
                raw_value = data[-1].get("value")

        if raw_value is None:
            return None, "UNAVAILABLE", "Signal missing from API response"

        value = float(raw_value)

        if signal == "renewable" and not 0 <= value <= 100:
            return None, "UNAVAILABLE", "Renewable percentage out of range"

        if value < 0:
            return None, "UNAVAILABLE", "Negative value returned"

        return value, "LIVE", None

    except httpx.HTTPStatusError as exc:
        return (
            None,
            "UNAVAILABLE",
            f"Electricity Maps HTTP {exc.response.status_code}",
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        return None, "UNAVAILABLE", str(exc)


# ---------------------------------------------------------
# AWS: instance offering and on-demand price
# ---------------------------------------------------------

def get_aws_instance_offering(region: str):
    """
    Checks whether the configured instance type is offered
    in a region. This is NOT a real-time capacity guarantee.
    """
    try:
        ec2 = boto3.client("ec2", region_name=region)
        response = ec2.describe_instance_type_offerings(
            LocationType="region",
            Filters=[
                {
                    "Name": "instance-type",
                    "Values": [AWS_INSTANCE_TYPE],
                }
            ],
        )

        offerings = response.get("InstanceTypeOfferings", [])
        offered = any(
            item.get("InstanceType") == AWS_INSTANCE_TYPE
            for item in offerings
        )

        return {
            "instance_type": AWS_INSTANCE_TYPE,
            "offered_in_region": offered,
            "source": "AWS_EC2_API",
            "availability_note": (
                "Offering status only; actual capacity is not confirmed."
            ),
            "error": None,
        }

    except (BotoCoreError, ClientError) as exc:
        return {
            "instance_type": AWS_INSTANCE_TYPE,
            "offered_in_region": None,
            "source": "UNAVAILABLE",
            "availability_note": (
                "EC2 offering status could not be verified."
            ),
            "error": str(exc),
        }


@lru_cache(maxsize=64)
def get_aws_hourly_price(region: str, instance_type: str):
    """
    Queries the AWS Price List API for Linux, shared-tenancy,
    On-Demand EC2 pricing.

    Returns USD per instance-hour, or None if the price cannot
    be resolved. Pricing is not a live market measurement.
    """
    location_names = {
        "us-west-2": "US West (Oregon)",
        "eu-central-1": "EU (Frankfurt)",
        "ap-south-1": "Asia Pacific (Mumbai)",
    }

    location = location_names.get(region)
    if location is None:
        return {
            "usd_per_hour": None,
            "source": "UNAVAILABLE",
            "error": "AWS pricing location mapping is missing",
        }

    try:
        pricing = boto3.client(
            "pricing",
            region_name=AWS_PRICING_REGION,
        )

        response = pricing.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode",
                 "Value": region},
                {"Type": "TERM_MATCH", "Field": "instanceType",
                 "Value": instance_type},
                {"Type": "TERM_MATCH", "Field": "operatingSystem",
                 "Value": "Linux"},
                {"Type": "TERM_MATCH", "Field": "tenancy",
                 "Value": "Shared"},
                {"Type": "TERM_MATCH", "Field": "preInstalledSw",
                 "Value": "NA"},
                {"Type": "TERM_MATCH", "Field": "capacitystatus",
                 "Value": "Used"},
                {"Type": "TERM_MATCH", "Field": "location",
                 "Value": location},
            ],
            MaxResults=10,
        )

        for product_json in response.get("PriceList", []):
            product = json.loads(product_json)
            terms = product.get("terms", {}).get("OnDemand", {})

            for term in terms.values():
                for dimension in term.get(
                    "priceDimensions", {}
                ).values():
                    price = dimension.get("pricePerUnit", {}).get("USD")
                    unit = dimension.get("unit")

                    if price is not None and unit == "Hrs":
                        return {
                            "usd_per_hour": float(price),
                            "source": "AWS_PRICE_LIST_API",
                            "error": None,
                        }

        return {
            "usd_per_hour": None,
            "source": "UNAVAILABLE",
            "error": "No matching On-Demand hourly price found",
        }

    except (BotoCoreError, ClientError, ValueError, TypeError) as exc:
        return {
            "usd_per_hour": None,
            "source": "UNAVAILABLE",
            "error": str(exc),
        }


# ---------------------------------------------------------
# Green Score
# ---------------------------------------------------------


def calculate_green_scores(regions):
    """
    GreenSched scoring:
      Full score: Carbon 40%, Renewables 30%,
                  Resources 20%, Cost 10%.
      Partial score: Carbon 50%, Renewables 37.5%,
                     Cost 12.5%.

    Partial scores are explicitly labelled PARTIAL.
    Missing resource values are never fabricated.
    """

    if not regions:
        return regions

    def normalize(values, value, inverse=False):
        low, high = min(values), max(values)
        if high == low:
            return 1.0
        score = (value - low) / (high - low)
        return 1.0 - score if inverse else score

    carbon_field = "carbon_intensity_gco2_kwh"
    renewable_field = "renewable_percent"
    resource_field = "resource_score_input"
    cost_field = "cost_usd_per_hour"

    partial_fields = (
        carbon_field,
        renewable_field,
        cost_field,
    )
    full_fields = partial_fields + (resource_field,)

    full_available = all(
        all(region.get(field) is not None for field in full_fields)
        for region in regions
    )

    partial_available = all(
        all(region.get(field) is not None for field in partial_fields)
        for region in regions
    )

    if not partial_available:
        for region in regions:
            missing = [
                field for field in partial_fields
                if region.get(field) is None
            ]
            if region.get(resource_field) is None:
                missing.append(resource_field)

            region["green_score"] = None
            region["green_score_status"] = "UNAVAILABLE"
            region["green_score_missing_factors"] = missing

        return regions

    carbons = [r[carbon_field] for r in regions]
    renewables = [r[renewable_field] for r in regions]
    costs = [r[cost_field] for r in regions]

    if full_available:
        resources = [r[resource_field] for r in regions]

    for region in regions:
        c = normalize(
            carbons, region[carbon_field], inverse=True
        )
        r = normalize(
            renewables, region[renewable_field]
        )
        k = normalize(
            costs, region[cost_field], inverse=True
        )

        if full_available:
            s = normalize(resources, region[resource_field])
            score = 100 * (
                0.40 * c
                + 0.30 * r
                + 0.20 * s
                + 0.10 * k
            )
            region["green_score_status"] = "AVAILABLE"
            region["green_score_missing_factors"] = []
        else:
            score = 100 * (
                0.50 * c
                + 0.375 * r
                + 0.125 * k
            )
            region["green_score_status"] = "PARTIAL"
            region["green_score_missing_factors"] = [
                resource_field
            ]

        region["green_score"] = round(score, 2)

    return sorted(
        regions,
        key=lambda region: region["green_score"],
        reverse=True,
    )


    def normalize(values, value, inverse=False):
        low, high = min(values), max(values)
        if high == low:
            return 1.0
        score = (value - low) / (high - low)
        return 1.0 - score if inverse else score

    carbons = [r["carbon_intensity_gco2_kwh"] for r in regions]
    renewables = [r["renewable_percent"] for r in regions]
    resources = [r["resource_score_input"] for r in regions]
    costs = [r["cost_usd_per_hour"] for r in regions]

    for region in regions:
        c = normalize(
            carbons, region["carbon_intensity_gco2_kwh"], inverse=True
        )
        r = normalize(renewables, region["renewable_percent"])
        s = normalize(resources, region["resource_score_input"])
        k = normalize(costs, region["cost_usd_per_hour"], inverse=True)

        region["green_score"] = round(
            100 * (0.40 * c + 0.30 * r + 0.20 * s + 0.10 * k),
            2,
        )
        region["green_score_status"] = "AVAILABLE"
        region["green_score_missing_factors"] = []

    return sorted(
        regions,
        key=lambda region: region["green_score"],
        reverse=True,
    )


# ---------------------------------------------------------
# Region assessment
# ---------------------------------------------------------

async def get_scored_regions():
    results = []

    for config in REGIONS:
        carbon, carbon_source, carbon_error = (
            await fetch_electricity_signal(config["zone"], "carbon")
        )
        renewable, renewable_source, renewable_error = (
            await fetch_electricity_signal(config["zone"], "renewable")
        )

        offering = get_aws_instance_offering(config["region"])
        price = get_aws_hourly_price(
            config["region"], AWS_INSTANCE_TYPE
        )

        # AWS does not expose general real-time free EC2 capacity
        # through this offering API. Do not convert offered=True
        # into a fabricated resource-availability percentage.
        resource_score_input = None

        results.append({
            "region": config["region"],
            "electricity_zone": config["zone"],
            "carbon_intensity_gco2_kwh": carbon,
            "carbon_data_source": carbon_source,
            "carbon_data_error": carbon_error,
            "renewable_percent": renewable,
            "renewable_data_source": renewable_source,
            "renewable_data_error": renewable_error,
            "aws_instance_type": offering["instance_type"],
            "aws_instance_offered": offering["offered_in_region"],
            "aws_offering_source": offering["source"],
            "aws_offering_error": offering["error"],
            "aws_availability_note": offering["availability_note"],
            "cost_usd_per_hour": price["usd_per_hour"],
            "cost_data_source": price["source"],
            "cost_data_error": price["error"],
            "resource_score_input": resource_score_input,
            "resource_availability": None,
            "resource_availability_status": "UNAVAILABLE",
            "resource_availability_note": (
                "AWS instance offering does not confirm real-time capacity."
            ),
            "green_score": None,
            "green_score_status": "UNAVAILABLE",
            "green_score_missing_factors": [],
        })

    return calculate_green_scores(results)


def choose_region(regions):
    """
    Prefer the full Green Score if all required data exists.
    Otherwise use a clearly labelled lowest-carbon fallback.
    """
    scored = [
        region for region in regions
        if region.get("green_score") is not None
    ]

    if scored:
        return max(scored, key=lambda r: r["green_score"]), "GREEN_SCORE"

    carbon_candidates = [
        region for region in regions
        if region.get("carbon_intensity_gco2_kwh") is not None
    ]

    if not carbon_candidates:
        raise HTTPException(
            status_code=503,
            detail=(
                "No live carbon-intensity data is available. "
                "Scheduling decision cannot be made."
            ),
        )

    return (
        min(
            carbon_candidates,
            key=lambda r: r["carbon_intensity_gco2_kwh"],
        ),
        "LOWEST_CARBON_FALLBACK",
    )


# ---------------------------------------------------------
# API routes
# ---------------------------------------------------------

@app.get("/")
def home():
    return {
        "message": "GreenSched API is running",
        "docs": "/docs",
        "database": str(DB_PATH),
    }


@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "database": "SQLite",
    }


@app.get("/api/regions")
async def list_regions():
    return {"regions": await get_scored_regions()}


@app.post("/api/tasks")
async def create_task(request: TaskRequest):
    regions = await get_scored_regions()
    selected, selection_method = choose_region(regions)

    intensity = selected["carbon_intensity_gco2_kwh"]
    emissions = request.energy_kwh * intensity

    task_id = str(uuid.uuid4())[:8]
    created_at = datetime.now(timezone.utc).isoformat()
    execution_mode = "AWS_EC2_SSH"
    status = "running"
    cloud_result = None
    error_message = None

    with connect_db() as db:
        db.execute(
            """
            INSERT INTO tasks (
                task_id, task_name, runtime_minutes, energy_kwh,
                region, carbon_intensity, data_source,
                estimated_emissions_g, status, execution_mode, created_at,
                execution_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                request.task_name,
                request.runtime_minutes,
                request.energy_kwh,
                selected["region"],
                intensity,
                selected["carbon_data_source"],
                emissions,
                status,
                execution_mode,
                created_at,
                None,
            ),
        )

    # Executes user-provided Python on the configured AWS worker.
    # Treat this as trusted-code execution: it is not a sandbox.
    try:
        cloud_result = await asyncio.to_thread(
            run_python_script, request.python_script, task_id
        )
        status = cloud_result.get("status", "failed")
        if status not in ("completed", "failed"):
            status = "failed"
        if status == "failed":
            error_message = cloud_result.get("error") or cloud_result.get(
                "stderr", "Remote task failed"
            )
    except Exception as exc:
        status = "failed"
        error_message = f"{type(exc).__name__}: {exc}"

    result_json = json.dumps(cloud_result, ensure_ascii=False) if cloud_result else None
    with connect_db() as db:
        db.execute(
            """
            UPDATE tasks
            SET status = ?, execution_mode = ?, execution_result = ?
            WHERE task_id = ?
            """,
            (status, execution_mode, result_json, task_id),
        )

    return {
        "task_id": task_id,
        "task_name": request.task_name,
        "selected_region": selected["region"],
        "electricity_zone": selected["electricity_zone"],
        "selection_method": selection_method,
        "green_score": selected["green_score"],
        "green_score_status": selected["green_score_status"],
        "carbon_intensity_gco2_kwh": intensity,
        "carbon_data_source": selected["carbon_data_source"],
        "renewable_percent": selected["renewable_percent"],
        "cost_usd_per_hour": selected["cost_usd_per_hour"],
        "resource_availability": None,
        "estimated_emissions_gco2": round(emissions, 3),
        "status": status,
        "execution_mode": execution_mode,
        "cloud_execution": cloud_result,
        "error": error_message,
        "note": (
            "Python code was executed on the AWS Sydney worker. Carbon emissions "
            "are estimates based on the submitted energy assumption, not direct "
            "power measurements. The selected region is a scheduling recommendation; "
            "the current worker itself runs in Sydney."
        ),
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
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return dict(row)


@app.get("/api/forecast")
def carbon_forecast(hours_ahead: int = 1):
    if not 1 <= hours_ahead <= 24:
        raise HTTPException(
            status_code=422,
            detail="hours_ahead must be between 1 and 24",
        )

    # This is the existing demo forecaster, not a validated
    # real-world carbon forecast.
    return forecaster.forecast(hours_ahead)


@app.post("/api/compare")
async def compare_scheduling(request: TaskRequest):
    regions = await get_scored_regions()
    selected, selection_method = choose_region(regions)

    # Fixed comparison baseline: the configured India electricity zone.
    # This is a comparison policy, not an AWS default placement policy.
    baseline = next(
        (r for r in regions if r["region"] == "ap-south-1"),
        None,
    )

    if baseline is None:
        raise HTTPException(
            status_code=500,
            detail="Configured comparison baseline is missing",
        )

    if baseline["carbon_intensity_gco2_kwh"] is None:
        raise HTTPException(
            status_code=503,
            detail="Live baseline carbon data is unavailable",
        )

    selected_intensity = selected["carbon_intensity_gco2_kwh"]
    baseline_intensity = baseline["carbon_intensity_gco2_kwh"]

    selected_emissions = request.energy_kwh * selected_intensity
    baseline_emissions = request.energy_kwh * baseline_intensity

    reduction = None
    if baseline_emissions > 0:
        reduction = round(
            (
                (baseline_emissions - selected_emissions)
                / baseline_emissions
            ) * 100,
            2,
        )

    return {
        "task_name": request.task_name,
        "energy_kwh_assumption": request.energy_kwh,
        "carbon_aware": {
            "region": selected["region"],
            "selection_method": selection_method,
            "green_score": selected["green_score"],
            "green_score_status": selected["green_score_status"],
            "intensity_gco2_kwh": selected_intensity,
            "data_source": selected["carbon_data_source"],
            "renewable_percent": selected["renewable_percent"],
            "cost_usd_per_hour": selected["cost_usd_per_hour"],
            "estimated_emissions_gco2": round(selected_emissions, 3),
        },
        "baseline": {
            "policy": "Fixed configured baseline",
            "region": baseline["region"],
            "intensity_gco2_kwh": baseline_intensity,
            "data_source": baseline["carbon_data_source"],
            "estimated_emissions_gco2": round(baseline_emissions, 3),
        },
        "estimated_reduction_percent": reduction,
        "comparison_note": (
            "Both emissions values use the same supplied energy assumption. "
            "They are estimates, not power-meter measurements. "
            "No workload is executed by the comparison endpoint. Both emissions "
            "values use the same supplied energy assumption; they are estimates, "
            "not power-meter measurements."
        ),
    }
