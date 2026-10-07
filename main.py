from fastapi import FastAPI
from controllers.upload_controller import router as upload_router
from controllers.relation_controller import router as relation_router

app = FastAPI(title="Excel -> SQL")
app.include_router(upload_router)
app.include_router(relation_router)
# run: uvicorn main:app --reload