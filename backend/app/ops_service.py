"""
The ops service: the AIOps features (cluster questions and the health
check) as their own Deployment (k8s/24-ops.yaml), running under the only
service account that can read the cluster.

Run with:  uvicorn app.ops_service:app --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI

from . import common
from . import ops_api
from . import rag_core
from . import store

app = FastAPI(title="OrbitOps Assistant ops service")
common.add_cors(app)
common.add_probes(app, "ops")
app.include_router(ops_api.router)


@app.on_event("startup")
def startup():
    store.init_db()
    rag_core.warm_up()
