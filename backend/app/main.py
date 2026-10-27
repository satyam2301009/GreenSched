from fastapi import FastAPI

app = FastAPI(
    title="GreenSched API",
    description="Carbon-aware workload scheduling prototype",
    version="0.1.0"
)

@app.get("/")
def home():
    return {
        "project": "GreenSched",
        "status": "running",
        "message": "Backend is ready"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}
