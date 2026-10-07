from fastapi import FastAPI
from controllers.upload_controller import router as upload_router
from controllers.relation_controller import router as relation_router
# from controllers.table_controller import router as table_router
from controllers.metadata_controller import router as metadata_router

app = FastAPI(title="Excel -> SQL")
app.include_router(upload_router)
app.include_router(relation_router)
# app.include_router(table_router)
app.include_router(metadata_router)
# run: uvicorn main:app --reload