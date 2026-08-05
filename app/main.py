"""
Power Trading Backend - Main Application Entry Point
FastAPI application with MongoDB integration
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Allow running this file directly with: python app/main.py
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    Contract,
    energy_scheduling,
    health,
    metering,
    outage_requests,
    resource_forecasting,
    sapp,
    telemetry,
)
from app.core.config import settings
from app.db.database import connect_db, disconnect_db
from app.services.scrape_scheduler import start_scrape_scheduler, stop_scrape_scheduler


# ===== LIFECYCLE EVENTS =====

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle manager
    Handles startup and shutdown events
    """
    # Startup: Initialize database connection
    print("\nStarting Power Trading Backend...")
    connect_db()
    start_scrape_scheduler()

    yield

    # Shutdown: Close database connection
    print("\nShutting down Power Trading Backend...")
    stop_scrape_scheduler()
    disconnect_db()


# ===== APPLICATION SETUP =====

# Create FastAPI application with lifecycle management
app = FastAPI(
    title=settings.API_TITLE,
    version=settings.API_VERSION,
    description="Power Trading Backend API with MongoDB and Time Series Support",
    debug=settings.DEBUG,
    lifespan=lifespan,
)


# ===== MIDDLEWARE =====

# Add CORS middleware for cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure based on your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== API ROUTERS =====

# Include route modules
app.include_router(health.router)
app.include_router(telemetry.router)
app.include_router(sapp.router)
app.include_router(Contract.router)
app.include_router(resource_forecasting.router)
app.include_router(energy_scheduling.router)
app.include_router(metering.router)
app.include_router(outage_requests.router)


# ===== ROOT ENDPOINT =====

@app.get("/")
def root():
    """
    Root endpoint - API information
    Provides links to documentation
    """
    return {
        "message": "Power Trading Backend API",
        "version": settings.API_VERSION,
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
    }


# ===== MAIN EXECUTION =====

if __name__ == "__main__":
    import uvicorn

    print(f"""
    Power Trading Backend
    MongoDB + Time Series Architecture

    Server: http://{settings.HOST}:{settings.PORT}
    Docs:   http://{settings.HOST}:{settings.PORT}/docs
    """)

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )

#TODO - YEARLY BUDGET SHOULD COMMUNICATE WITH THE FORECASTED LEVELS TO INDICATE SPILLAGE OR SHORTAGES

#TODO - YEARLY BUDGET - SPILLAGE QTY(in MW) AND SPILLAGE COST
