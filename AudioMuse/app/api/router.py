"""聚合并挂载 API v1 的录音和任务路由。"""

from fastapi import APIRouter

from app.api.recordings import router as recordings_router
from app.api.tasks import router as tasks_router

api_router = APIRouter(prefix="/v1")
api_router.include_router(recordings_router)
api_router.include_router(tasks_router)
