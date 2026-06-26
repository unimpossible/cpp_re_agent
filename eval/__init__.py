"""
Prompt-optimization experiment framework for decompiled-C++ recovery.

Closed loop: compile a corpus C++ program -> strip -> decompile -> improve the
decompiled code with the prompt under test -> score how close the result is to
the original. Built so future experiments are a config + (at most) a small
optimizer adapter, never a harness rewrite. See eval/NOTES.md for the lab
journal and the approved plan for the full design.
"""
