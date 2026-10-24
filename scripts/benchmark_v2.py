#!/usr/bin/env python3
"""Offline benchmark: no network attempts."""
import json,sys,time,tracemalloc
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'shared'))
from scanlib.v2.scope import compile_scope
tracemalloc.start();start=time.perf_counter();scope=compile_scope(b'11.0.0.0/12\n')
print(json.dumps({'targets':scope.target_count,'normalized_cidrs':len(scope.allow),'elapsed_seconds':time.perf_counter()-start,'peak_bytes':tracemalloc.get_traced_memory()[1],'network_attempts':0},indent=2))
