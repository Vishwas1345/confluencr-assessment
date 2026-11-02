from fastapi import APIRouter

from app.schemas import HealthResponse
from app.timestamps import to_iso_z, utc_now

router = APIRouter(tags=["health"])


@router.get("/", response_model=HealthResponse)
def health() -> HealthResponse:
    current_time = to_iso_z(utc_now())
    assert current_time is not None
    return HealthResponse(status="HEALTHY", current_time=current_time)
