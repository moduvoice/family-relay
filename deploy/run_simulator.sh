#!/bin/bash
# Lambda Web Adapter entry: the simulator page + assistant API on :8080
exec python -m uvicorn simulator.backend:app --host 0.0.0.0 --port 8080
