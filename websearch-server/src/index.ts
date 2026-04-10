import express from 'express';
import cors from 'cors';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { z } from 'zod';

// ---------------------------------------------------------------------------
// Environment validation
// ---------------------------------------------------------------------------

const TAVILY_API_KEY = process.env.TAVILY_API_KEY;
if (!TAVILY_API_KEY) {
  console.error('[websearch-server] FATAL: TAVILY_API_KEY environment variable is required but not set.');
  process.exit(1);
}

const PORT = parseInt(process.env.PORT ?? '3002', 10);

// ---------------------------------------------------------------------------
// Tavily API types
// ---------------------------------------------------------------------------

interface TavilyResult {
  title: string;
  url: string;
  content: string;
  score?: number;
}

interface TavilyResponse {
  answer?: string;
  results: TavilyResult[];
  query?: string;
}

// ---------------------------------------------------------------------------
// Tavily search helper
// ---------------------------------------------------------------------------

async function tavilySearch(query: string): Promise<string> {
  const response = await fetch('https://api.tavily.com/search', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      api_key: TAVILY_API_KEY,
      query,
      search_depth: 'advanced',
      max_results: 5,
      include_answer: true,
    }),
  });

  if (!response.ok) {
    const errorText = await response.text().catch(() => 'Unknown error');
    throw new Error(`Tavily API error ${response.status}: ${errorText}`);
  }

  const data = (await response.json()) as TavilyResponse;

  const lines: string[] = [];

  // Include the AI-generated answer if present
  if (data.answer) {
    lines.push('**Answer:**');
    lines.push(data.answer);
    lines.push('');
  }

  // Include individual results
  if (data.results && data.results.length > 0) {
    lines.push('**Search Results:**');
    for (const result of data.results) {
      lines.push(`\n**${result.title}**`);
      lines.push(`URL: ${result.url}`);
      if (result.content) {
        // Trim content to keep results concise
        const snippet = result.content.length > 500
          ? result.content.slice(0, 500) + '…'
          : result.content;
        lines.push(snippet);
      }
    }
  } else {
    lines.push('No results found.');
  }

  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// MCP Server setup
// ---------------------------------------------------------------------------

function createMcpServer(): McpServer {
  const server = new McpServer({
    name: 'websearch-server',
    version: '1.0.0',
  });

  server.tool(
    'search_web',
    'Search the web for current information using Tavily. Useful for VAT rates, tax regulations, and other time-sensitive data.',
    {
      query: z.string().describe('Search query for current VAT rate information'),
    },
    async ({ query }) => {
      console.log(`[websearch-server] search_web called with query: "${query}"`);
      try {
        const result = await tavilySearch(query);
        console.log(`[websearch-server] search_web completed for query: "${query}"`);
        return {
          content: [
            {
              type: 'text',
              text: result,
            },
          ],
        };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`[websearch-server] search_web error for query "${query}": ${message}`);
        return {
          content: [
            {
              type: 'text',
              text: `Error performing web search: ${message}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  return server;
}

// ---------------------------------------------------------------------------
// Express server
// ---------------------------------------------------------------------------

const app = express();
app.use(cors());
app.use(express.json());

// Health check endpoint
app.get('/health', (_req, res) => {
  res.status(200).json({ status: 'ok', service: 'websearch-server' });
});

// MCP endpoint — POST (stateless streamable HTTP transport)
app.post('/mcp', async (req, res) => {
  console.log('[websearch-server] Incoming MCP POST request');
  const server = createMcpServer();
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined, // stateless mode
  });

  res.on('close', () => {
    console.log('[websearch-server] Request closed, cleaning up transport');
    transport.close().catch((err: unknown) => {
      console.error('[websearch-server] Error closing transport:', err);
    });
  });

  try {
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[websearch-server] Error handling MCP request: ${message}`);
    if (!res.headersSent) {
      res.status(500).json({ error: 'Internal server error', details: message });
    }
  }
});

// MCP endpoint — GET (not supported, return 405)
app.get('/mcp', (_req, res) => {
  res.status(405).json({ error: 'Method Not Allowed. Use POST for MCP requests.' });
});

// MCP endpoint — DELETE (not supported, return 405)
app.delete('/mcp', (_req, res) => {
  res.status(405).json({ error: 'Method Not Allowed.' });
});

// ---------------------------------------------------------------------------
// Start server
// ---------------------------------------------------------------------------

app.listen(PORT, () => {
  console.log(`[websearch-server] MCP server listening on port ${PORT}`);
  console.log(`[websearch-server] MCP endpoint: POST http://localhost:${PORT}/mcp`);
  console.log(`[websearch-server] Health check: GET http://localhost:${PORT}/health`);
});
