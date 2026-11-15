"""Clip mode: long video in, ranked 9:16 short clips out.

Each stage is its own module, runnable as `python -m pipeline.clip.<stage>`.
Nothing is re-exported here: importing a stage from this file would load it
twice when it is run that way.
"""
