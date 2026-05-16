"""
Health Check Endpoints
Used to verify API and database status
"""

from fastapi import APIRouter
from app.db.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check():
    """Basic API health check"""
    return {"status": "ok", "service": "Power Trading API"}


@router.get("/health/db")
def db_health_check():
    """
    Check MongoDB connection status
    Returns connection info if successful
    """
    try:
        db = get_db()
        # Test connection with a ping
        db.command('ping')
        
        # Get database statistics
        return {
            "status": "ok",
            "database": "connected",
            "database_name": db.name
        }
    except Exception as e:
        return {
            "status": "error",
            "database": "disconnected",
            "detail": str(e)
        }

