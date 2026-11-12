"""
Caribou-Workflow fuer den SeBS-Benchmark 501.graph-pagerank (in EcoLife: "pagerank-1000k").

Die eigentliche Rechenlogik ist unveraendert aus SeBS uebernommen
(src/sebs_fn/function.py). Diese Datei ergaenzt nur das Caribou-Geruest.
"""
import json
import time
from typing import Any

from caribou.deployment.client import CaribouWorkflow

workflow = CaribouWorkflow(name="sebs_graph_pagerank", version="3.0.0")


@workflow.serverless_function(
    name="graph_pagerank",
    entry_point=True,
)
def graph_pagerank(event: Any) -> dict[str, Any]:
    if isinstance(event, str):
        event = json.loads(event)

    # Erst hier importieren: so braucht der lokale Rechner beim Deploy die
    # Abhaengigkeiten der Funktion (z. B. torch, igraph) nicht installiert.
    from src.sebs_fn.function import handler as sebs_handler

    start = time.time()
    output = sebs_handler(event)
    duration_s = time.time() - start

    # Nur Messwerte zurueckgeben, nicht das (teils sehr grosse) Rechenergebnis.
    result = {"status": 200, "benchmark": "pagerank-1000k", "sebs_duration_s": duration_s}
    if isinstance(output, dict) and "measurement" in output:
        result["measurement"] = output["measurement"]
    print(json.dumps(result))
    return result
