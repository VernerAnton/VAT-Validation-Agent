# VAT Validation Agent — Deployment Guide

Complete phase-by-phase deployment to Railway.

## Prerequisites

- [Railway CLI](https://docs.railway.com/guides/cli) installed and authenticated
- A [Tavily API key](https://tavily.com/) (free tier gives 1,000 searches/month)
- A [DeepSeek API key](https://platform.deepseek.com/)
- Node.js 20+ and Python 3.11+ (for local testing only)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Railway Project                        │
│                                                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ file-vault   │  │ websearch    │  │ sandbox      │   │
│  │ :3001        │  │ :3002        │  │ :3003        │   │
│  │ MCP Resource │  │ MCP Tool     │  │ MCP Tool     │   │
│  │ (VBA file)   │  │ (Tavily)     │  │ (file I/O)   │   │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘   │
│         │                  │                  │           │
│         └──────────────────┼──────────────────┘           │
│                            │                              │
│                   ┌────────┴────────┐                     │
│                   │  Streamlit App  │                     │
│                   │  :8501          │                     │
│                   │  MCP Client +   │                     │
│                   │  DeepSeek LLM   │                     │
│                   └─────────────────┘                     │
└─────────────────────────────────────────────────────────┘
```

---

## Phase 1 — Create Railway Project

```bash
# Login to Railway (if not already)
railway login

# Create a new project
railway init
# Name it: vat-validation-agent
```

---

## Phase 2 — Deploy file-vault-server

```bash
cd file-vault-server

# Create a new Railway service
railway service create file-vault-server

# Link to it
railway link

# Set environment variables
railway variables set PORT=3001
railway variables set VBA_FILE_PATH=./data/VAT-Database.txt

# Deploy
railway up

# Note the deployed URL — it will look like:
# https://file-vault-server-production-xxxx.up.railway.app
# Save this as FILE_VAULT_URL
```

**Verify:**
```bash
curl https://YOUR_FILE_VAULT_URL/health
# Should return: {"status":"ok","server":"file-vault-server",...}
```

---

## Phase 3 — Deploy websearch-server

```bash
cd ../websearch-server

# Create a new Railway service
railway service create websearch-server

# Link to it
railway link

# Set environment variables
railway variables set PORT=3002
railway variables set TAVILY_API_KEY=tvly-YOUR-KEY-HERE

# Deploy
railway up

# Save the URL as WEBSEARCH_URL
```

**Verify:**
```bash
curl https://YOUR_WEBSEARCH_URL/health
# Should return: {"status":"ok","service":"websearch-server"}
```

---

## Phase 4 — Deploy sandbox-server

```bash
cd ../sandbox-server

# Create a new Railway service
railway service create sandbox-server

# Link to it
railway link

# Set environment variables
railway variables set PORT=3003
railway variables set WORKSPACE_DIR=./workspace

# Deploy
railway up

# Save the URL as SANDBOX_URL
```

**Verify:**
```bash
curl https://YOUR_SANDBOX_URL/health
# Should return: {"status":"ok","workspace":"..."}
```

---

## Phase 5 — Deploy Streamlit App

```bash
cd ../streamlit-app

# Create a new Railway service
railway service create streamlit-app

# Link to it
railway link

# Set environment variables (use the URLs from phases 2-4)
railway variables set PORT=8501
railway variables set DEEPSEEK_API_KEY=sk-YOUR-KEY-HERE
railway variables set FILE_VAULT_URL=https://file-vault-server-production-xxxx.up.railway.app
railway variables set WEBSEARCH_URL=https://websearch-server-production-xxxx.up.railway.app
railway variables set SANDBOX_URL=https://sandbox-server-production-xxxx.up.railway.app

# Deploy
railway up

# The Streamlit app URL is your main entry point
```

**Verify:**
Open the Streamlit URL in your browser. Click "Check All Servers" in the sidebar — all three should show green.

---

## Environment Variables Reference

### file-vault-server
| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PORT` | No | `3001` | HTTP port |
| `VBA_FILE_PATH` | No | `./data/VAT-Database.txt` | Path to the VBA module file |

### websearch-server
| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PORT` | No | `3002` | HTTP port |
| `TAVILY_API_KEY` | **Yes** | — | Tavily Search API key |

### sandbox-server
| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PORT` | No | `3003` | HTTP port |
| `WORKSPACE_DIR` | No | `./workspace` | Directory for draft files |

### streamlit-app
| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PORT` | No | `8501` | Streamlit port |
| `DEEPSEEK_API_KEY` | **Yes** | — | DeepSeek API key |
| `FILE_VAULT_URL` | **Yes** | `http://localhost:3001` | file-vault-server URL |
| `WEBSEARCH_URL` | **Yes** | `http://localhost:3002` | websearch-server URL |
| `SANDBOX_URL` | **Yes** | `http://localhost:3003` | sandbox-server URL |

---

## Local Development

Run all four services locally for testing:

```bash
# Terminal 1 — file-vault-server
cd file-vault-server
npm install && npm run dev

# Terminal 2 — websearch-server
cd websearch-server
export TAVILY_API_KEY=tvly-xxx
npm install && npm run dev

# Terminal 3 — sandbox-server
cd sandbox-server
npm install && npm run dev

# Terminal 4 — streamlit-app
cd streamlit-app
pip install -r requirements.txt
export DEEPSEEK_API_KEY=sk-xxx
streamlit run app.py
```

---

## Troubleshooting

### MCP Connection Errors
- Ensure each server's Railway URL is publicly accessible (no private networking)
- The Streamlit app talks to MCP servers over HTTPS — Railway provides this automatically
- Check Railway logs: `railway logs` in each service directory

### Tavily Rate Limits
- Free tier: 1,000 searches/month
- Use the batch size control in the UI (default 10) to limit concurrent searches
- The delay slider adds pauses between API calls

### DeepSeek Errors
- Verify your API key at https://platform.deepseek.com/
- The app uses `deepseek-chat` model — cheapest option
- Temperature is set to 0.1 for consistent rate extraction

### VBA Parse Errors
- The parser expects the exact format: `countryData(N, 1) = "XX": countryData(N, 2) = "Name": countryData(N, 3) = Rate`
- Special characters in country names (Côte d'Ivoire, São Tomé) are handled correctly
- The output file preserves the exact original formatting

### Railway Deployment Issues
- Each service needs its own `railway.toml` (already included)
- Railway auto-detects Node.js (TypeScript) and Python (Streamlit) via nixpacks
- If builds fail, check that `package.json` / `requirements.txt` are in the service root
