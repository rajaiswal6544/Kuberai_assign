from fastapi import APIRouter, Request

from app.db import check_db

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict:
    state = request.app.state
    db_up = check_db(state.engine)
    price = state.price_service.status()
    llm = state.llm.status() if state.llm is not None else {"status": "not_configured"}
    # Chat keeps working without the LLM (rule-based fallback), so only the DB and the
    # price source decide whether the service is "ok" or "degraded".
    ok = db_up and price["status"] == "up"
    return {
        "status": "ok" if ok else "degraded",
        "db": "up" if db_up else "down",
        "price_source": price,
        "llm": llm,
    }
