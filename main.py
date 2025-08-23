import os
from datetime import datetime, timedelta
from typing import List, Dict, Any
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import motor.motor_asyncio

MONGODB_URI = os.getenv("MONGODB_URI")  # mongodb+srv://...
DB_NAME = os.getenv("DB_NAME", "TimesheetDB")

app = FastAPI()

# Allow your Vercel frontend domain + localhost
ALLOWED_ORIGINS = [
    os.getenv("FRONTEND_ORIGIN", "http://localhost:3000"),
    # e.g. https://your-frontend.vercel.app
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI) if MONGODB_URI else None
db = client[DB_NAME] if client else None

@app.get("/health")
async def health():
    return {"ok": True, "db": bool(db)}

def day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)

@app.get("/api/summary")
async def summary():
    users_coll = db["users"]
    tasks_coll = db["tasks"]
    timesheets = db["timesheets"]

    # users by role
    pipeline_users = [
        {"$group": {"_id": "$role", "count": {"$sum": 1}}},
        {"$project": {"_id": 0, "role": "$_id", "count": 1}},
    ]
    roles = [r async for r in users_coll.aggregate(pipeline_users)]
    usersByRole = {r["role"]: r["count"] for r in roles}

    # tasks status
    pipeline_tasks = [
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        {"$project": {"_id": 0, "status": "$_id", "count": 1}},
    ]
    tstats = {r["status"] or "Unknown": r["count"] for r in [r async for r in tasks_coll.aggregate(pipeline_tasks)]}
    tasks_summary = {
        "total": sum(tstats.values()),
        "completed": tstats.get("Completed", 0),
        "in_progress": tstats.get("In Progress", 0),
        "pending": tstats.get("Pending", 0),
    }

    # hours today
    start = day_start(datetime.utcnow())
    pipeline_today = [
        {"$match": {"check_in": {"$gte": start}}},
        {"$project": {
            "duration": {"$divide": [{"$subtract": ["$check_out", "$check_in"]}, 3600000]}
        }},
        {"$group": {"_id": None, "hours": {"$sum": {"$ifNull": ["$duration", 0]}}}},
        {"$project": {"_id": 0, "hours": {"$round": ["$hours", 1]}}},
    ]
    hours_today_docs = [r async for r in timesheets.aggregate(pipeline_today)]
    hours_today = hours_today_docs[0]["hours"] if hours_today_docs else 0

    # last 7 days hours
    start7 = day_start(datetime.utcnow() - timedelta(days=6))
    pipeline_7 = [
        {"$match": {"check_in": {"$gte": start7}}},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$check_in"}},
            "duration": {"$divide": [{"$subtract": ["$check_out", "$check_in"]}, 3600000]},
        }},
        {"$group": {"_id": "$date", "hours": {"$sum": {"$ifNull": ["$duration", 0]}}}},
        {"$project": {"_id": 0, "date": "$_id", "hours": {"$round": ["$hours", 1]}}},
        {"$sort": {"date": 1}},
    ]
    last7 = [r async for r in timesheets.aggregate(pipeline_7)]

    return {
        "usersByRole": usersByRole,
        "tasks": tasks_summary,
        "hours": {"today": hours_today, "last7Days": last7},
    }

@app.get("/api/timesheets/daily")
async def daily(from_: str = Query(..., alias="from"), to: str = Query(...)):
    timesheets = db["timesheets"]
    from_dt = datetime.fromisoformat(from_)
    to_dt = datetime.fromisoformat(to) + timedelta(days=1)

    pipeline = [
        {"$match": {"check_in": {"$gte": from_dt, "$lt": to_dt}}},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$check_in"}},
            "duration": {"$divide": [{"$subtract": ["$check_out", "$check_in"]}, 3600000]},
        }},
        {"$group": {"_id": "$date", "hours": {"$sum": {"$ifNull": ["$duration", 0]}}}},
        {"$project": {"_id": 0, "date": "$_id", "hours": {"$round": ["$hours", 1]}}},
        {"$sort": {"date": 1}},
    ]
    docs = [r async for r in timesheets.aggregate(pipeline)]
    return docs

@app.get("/api/tasks/status-distribution")
async def status_distribution():
    tasks = db["tasks"]
    pipeline = [
        {"$group": {"_id": "$status", "value": {"$sum": 1}}},
        {"$project": {"_id": 0, "name": "$_id", "value": 1}},
    ]
    docs = [r async for r in tasks.aggregate(pipeline)]
    # Normalize names to match the frontend pie labels
    name_map = {"Completed": "Completed", "In Progress": "In Progress", "Pending": "Pending"}
    for d in docs:
        d["name"] = name_map.get(d["name"] or "Pending", d["name"] or "Pending")
    return docs

@app.get("/api/timesheets/recent")
async def recent(limit: int = 20):
    timesheets = db["timesheets"]
    cursor = timesheets.find({}, {"username": 1, "check_in": 1, "check_out": 1, "notes": 1, "completed_today": 1}) \
                       .sort("check_in", -1).limit(min(limit, 100))
    out = []
    async for t in cursor:
        dur = 0.0
        if t.get("check_in") and t.get("check_out"):
            dur = round((t["check_out"] - t["check_in"]).total_seconds() / 3600.0, 1)
        out.append({
            "_id": str(t.get("_id")),
            "username": t.get("username"),
            "check_in": t.get("check_in").isoformat() if t.get("check_in") else None,
            "check_out": t.get("check_out").isoformat() if t.get("check_out") else None,
            "duration_hours": dur,
            "completed_today": t.get("completed_today", 0),
            "notes": t.get("notes"),
        })
    return out
