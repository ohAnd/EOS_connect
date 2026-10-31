"""Optimization backends: the external EOS server, EVopt, and the built-in solver.

Carries an ``__init__`` like every sibling package under ``interfaces`` - without
one this stayed a namespace package, and an absolute import of a module inside it
could not be resolved by the tooling.
"""
