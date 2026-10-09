from contextlib import asynccontextmanager

from fastapi import FastAPI
from controllers.upload_controller import router as upload_router
from controllers.relation_controller import router as relation_router
# from controllers.table_controller import router as table_router
from controllers.metadata_controller import router as metadata_router
from controllers.laya_controller import router as table_column_router
from controllers.SQL_generation_controller import router as SQL_generation_router
from controllers.run_SQL_controller import router as run_SQL_router



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm everything once at startup instead of on the first request.
    from tools.table_column_selection import get_selector
    from tools.LLM_provider import warm_up
    get_selector()          # builds the Laya Router once
    warm_up()               # builds the Groq client once (no-op if key missing)
    yield


app = FastAPI(title="Excel -> SQL", lifespan=lifespan)
app.include_router(upload_router)
app.include_router(relation_router)
# app.include_router(table_router)
app.include_router(metadata_router)
app.include_router(table_column_router)
app.include_router(SQL_generation_router)
app.include_router(run_SQL_router)
# run: uvicorn main:app --reload