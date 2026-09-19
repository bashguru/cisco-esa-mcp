"""Cisco docs hybrid-search MCP server.

A local, free, Docker-deployable MCP server that ingests a folder of product
PDFs into a ParadeDB (Postgres + BM25 + pgvector) backend and serves accurate,
version-aware hybrid search plus figure retrieval over the Model Context Protocol.
"""

__version__ = "0.1.0"
