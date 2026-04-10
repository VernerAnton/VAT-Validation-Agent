# Deployment Guide

This project consists of four services, each deployed independently on Railway.

## Services

### file-vault-server
MCP server providing file storage and retrieval tools.
- Runtime: Node.js
- Entry point: `src/index.js`

### websearch-server
MCP server providing web search tools.
- Runtime: Node.js
- Entry point: `src/index.js`

### sandbox-server
MCP server providing code execution sandbox tools.
- Runtime: Node.js
- Entry point: `src/index.js`

### streamlit-app
Frontend application for interacting with the VAT validation agent.
- Runtime: Python
- Entry point: `app.py`

## Deploying to Railway

1. Create a new Railway project.
2. Add a service for each subdirectory (`file-vault-server`, `websearch-server`, `sandbox-server`, `streamlit-app`).
3. Set the root directory of each service to its respective folder.
4. Each folder contains a `railway.toml` with the appropriate build and start configuration.
5. Configure any required environment variables per service in the Railway dashboard.
6. Deploy all services.
