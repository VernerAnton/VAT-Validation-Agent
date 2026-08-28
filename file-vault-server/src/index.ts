import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import express, { Request, Response } from 'express';
import cors from 'cors';
import { readFileSync } from 'fs';
import { resolve } from 'path';

// ─── Configuration ────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.PORT ?? '3001', 10);
const VBA_FILE_PATH = resolve(process.env.VBA_FILE_PATH ?? './data/VAT-Database.txt');

// ─── Load VBA file at startup ─────────────────────────────────────────────────

let fileContent: string;

try {
  fileContent = readFileSync(VBA_FILE_PATH, 'utf-8');
  console.log(`[file-vault-server] Loaded VBA file from: ${VBA_FILE_PATH}`);
  console.log(`[file-vault-server] File size: ${fileContent.length} bytes`);
} catch (err) {
  console.error(`[file-vault-server] FATAL: Cannot read VBA file at ${VBA_FILE_PATH}`, err);
  process.exit(1);
}

// ─── Create MCP Server ────────────────────────────────────────────────────────

// Factory: create a fresh McpServer per incoming request. The stateless
// Streamable HTTP transport connects to one server instance per POST, so
// sharing a single instance across concurrent requests can corrupt transport
// state (the SDK's connect() overwrites the previous transport reference).
function createMcpServer(): McpServer {
  const server = new McpServer({
    name: 'file-vault-server',
    version: '1.0.0',
  });

  // Register the VBA file as a read-only MCP resource
  // resource(name, uri, metadata, readCallback)
  // ResourceMetadata = Omit<Resource, 'uri' | 'name'> — do NOT include name here
  server.resource(
    'VAT Database Module 2',
    'vat-database://module2',
    {
      description: 'VBA module containing hardcoded country VAT data for 185 countries (country code, full name, VAT rate).',
      mimeType: 'text/plain',
    },
    async (_uri: URL) => {
      return {
        contents: [
          {
            uri: 'vat-database://module2',
            mimeType: 'text/plain',
            text: fileContent,
          },
        ],
      };
    }
  );

  // Also register as a tool — some MCP clients prefer tools over resources
  server.tool(
    'get_vat_file',
    'Returns the raw content of the VBA VAT database file (Module 2). Contains hardcoded VAT rates for 185 countries.',
    {},
    async () => {
      return {
        content: [
          {
            type: 'text' as const,
            text: fileContent,
          },
        ],
      };
    }
  );

  return server;
}

// ─── Express App ──────────────────────────────────────────────────────────────

const app = express();

app.use(cors());
app.use(express.json());

// Health check for Railway
app.get('/health', (_req: Request, res: Response) => {
  res.json({
    status: 'ok',
    server: 'file-vault-server',
    version: '1.0.0',
    vbaFilePath: VBA_FILE_PATH,
    fileLoaded: fileContent.length > 0,
  });
});

// MCP Streamable HTTP — POST (stateless: new server + transport per request)
app.post('/mcp', async (req: Request, res: Response) => {
  console.log(`[file-vault-server] POST /mcp — new MCP request`);

  const server = createMcpServer();
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined, // stateless mode
  });

  res.on('close', () => {
    console.log('[file-vault-server] Response closed, cleaning up transport');
    transport.close().catch(() => {
      // Ignore cleanup errors
    });
  });

  try {
    await server.connect(transport);
    // Pass req.body as pre-parsed body since express.json() has already parsed it
    await transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error('[file-vault-server] Error handling MCP request:', err);
    if (!res.headersSent) {
      res.status(500).json({ error: 'Internal server error' });
    }
  }
});

// SSE not supported in stateless mode
app.get('/mcp', (_req: Request, res: Response) => {
  res.status(405).json({
    error: 'SSE not supported in stateless mode. Use POST /mcp.',
    hint: 'Send JSON-RPC requests via POST /mcp',
  });
});

// DELETE not supported in stateless mode
app.delete('/mcp', (_req: Request, res: Response) => {
  res.status(405).json({
    error: 'Session management not supported in stateless mode.',
    hint: 'Each POST /mcp request is independent.',
  });
});

// Root info endpoint
app.get('/', (_req: Request, res: Response) => {
  res.json({
    name: 'file-vault-server',
    description: 'MCP Resource server — exposes VBA VAT database as read-only resource',
    version: '1.0.0',
    endpoints: {
      mcp: 'POST /mcp',
      health: 'GET /health',
    },
    resources: [
      {
        uri: 'vat-database://module2',
        name: 'VAT Database Module 2',
        mimeType: 'text/plain',
      },
    ],
    tools: ['get_vat_file'],
  });
});

// ─── Start Server ─────────────────────────────────────────────────────────────

// Bind dual-stack ('::' accepts both IPv6 and IPv4-mapped connections).
// Railway private networking resolves *.railway.internal over IPv6, so an
// IPv4-only bind ('0.0.0.0') would black-hole internal service-to-service
// traffic while still working over the public edge.
app.listen(PORT, '::', () => {
  console.log(`[file-vault-server] Server running on port ${PORT}`);
  console.log(`[file-vault-server] MCP endpoint: POST http://localhost:${PORT}/mcp`);
  console.log(`[file-vault-server] Health check: GET http://localhost:${PORT}/health`);
  console.log(`[file-vault-server] Resource URI: vat-database://module2`);
});
