"""
Caribou workflow for the SeBS benchmark 110.dynamic-html (in EcoLife: "dynamic-100k").

The computation itself is taken unchanged from SeBS (src/sebs_fn/function.py).
This file only adds the Caribou scaffolding.
"""
import json
import time
from typing import Any

from caribou.deployment.client import CaribouWorkflow

workflow = CaribouWorkflow(name="sebs_dynamic_html", version="3.0.0")


@workflow.serverless_function(
    name="dynamic_html",
    entry_point=True,
)
def dynamic_html(event: Any) -> dict[str, Any]:
    if isinstance(event, str):
        event = json.loads(event)

    # Import here so that the local machine does not need the function's
    # dependencies (e.g. torch, igraph) installed when deploying.
    from src.sebs_fn.function import handler as sebs_handler

    start = time.time()
    output = sebs_handler(event)
    duration_s = time.time() - start

    # Return only measurements, not the (sometimes very large) computation result.
    result = {"status": 200, "benchmark": "dynamic-100k", "sebs_duration_s": duration_s}
    if isinstance(output, dict) and "measurement" in output:
        result["measurement"] = output["measurement"]
    print(json.dumps(result))
    return result
