"""Pipeline orchestration: run_single (one clip), run_batch (several
creators), and auto_finish (the post-script stages chained together).

Run them as modules, e.g. `python -m pipeline.orchestrate.run_single`.
Nothing is re-exported here: importing run_single at package import time
made every `python -m pipeline.orchestrate.run_single` call print a
RuntimeWarning about the module already being loaded.
"""
