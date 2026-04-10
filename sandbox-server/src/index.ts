import express, { Request, Response } from 'express';
import cors from 'cors';
import fs from 'fs';
import path from 'path';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
// Import from zod/v3 for compatibility with the MCP SDK's internal zod-compat layer
import { z } from 'zod/v3';

// ─── Configuration ──────────────────────────────────────────────────────────

const PORT = parseInt(process.env.PORT ?? '3003', 10);
const WORKSPACE_DIR = path.resolve(process.env.WORKSPACE_DIR ?? './workspace');

// Ensure workspace directory exists on startup
if (!fs.existsSync(WORKSPACE_DIR)) {
  fs.mkdirSync(WORKSPACE_DIR, { recursive: true });
  console.log(`[sandbox-server] Created workspace directory: ${WORKSPACE_DIR}`);
}

console.log(`[sandbox-server] Workspace directory: ${WORKSPACE_DIR}`);

// ─── Security helper ────────────────────────────────────────────────────────

/**
 * Validates a filename and returns the safe absolute path within WORKSPACE_DIR.
 * Throws if the filename contains path traversal or is absolute.
 */
function safeWorkspacePath(filename: string): string {
  // Reject absolute paths
  if (path.isAbsolute(filename)) {
    throw new Error(`Invalid filename: absolute paths are not allowed (got "${filename}")`);
  }
  // Reject path traversal sequences
  if (filename.includes('..')) {
    throw new Error(`Invalid filename: path traversal sequences are not allowed (got "${filename}")`);
  }
  // Reject empty filenames
  if (!filename.trim()) {
    throw new Error('Invalid filename: filename must not be empty');
  }

  const resolved = path.resolve(WORKSPACE_DIR, filename);

  // Double-check the resolved path is within the workspace
  if (!resolved.startsWith(WORKSPACE_DIR + path.sep) && resolved !== WORKSPACE_DIR) {
    throw new Error(`Invalid filename: resolves outside workspace (got "${filename}")`);
  }

  return resolved;
}

// ─── Diff implementation ─────────────────────────────────────────────────────

interface DiffHunk {
  type: 'context' | 'remove' | 'add';
  line: string;
}

/**
 * Computes a simplified unified-style diff between two strings.
 * Shows 3 lines of context around each changed region.
 */
function computeDiff(original: string, modified: string): string {
  const CONTEXT = 3;

  const origLines = original.split('\n');
  const modLines = modified.split('\n');

  // Build an LCS-based edit script (Myers-like simple approach)
  // For simplicity we use a line-by-line comparison with a basic LCS.
  const lcs = computeLCS(origLines, modLines);

  // Reconstruct the diff hunks
  const hunks: DiffHunk[] = [];
  let oi = 0; // pointer into origLines
  let mi = 0; // pointer into modLines
  let li = 0; // pointer into lcs

  while (oi < origLines.length || mi < modLines.length) {
    if (
      li < lcs.length &&
      oi < origLines.length &&
      mi < modLines.length &&
      origLines[oi] === lcs[li] &&
      modLines[mi] === lcs[li]
    ) {
      // Common line (context)
      hunks.push({ type: 'context', line: origLines[oi] });
      oi++;
      mi++;
      li++;
    } else {
      // Consume differing lines
      while (oi < origLines.length && (li >= lcs.length || origLines[oi] !== lcs[li])) {
        hunks.push({ type: 'remove', line: origLines[oi] });
        oi++;
      }
      while (mi < modLines.length && (li >= lcs.length || modLines[mi] !== lcs[li])) {
        hunks.push({ type: 'add', line: modLines[mi] });
        mi++;
      }
    }
  }

  if (hunks.length === 0) {
    return '(no differences)';
  }

  // Find which context lines to show (within CONTEXT lines of a change)
  const changedIndices = new Set<number>();
  hunks.forEach((h, i) => {
    if (h.type !== 'context') {
      for (let k = Math.max(0, i - CONTEXT); k <= Math.min(hunks.length - 1, i + CONTEXT); k++) {
        changedIndices.add(k);
      }
    }
  });

  // Render output
  const lines: string[] = ['--- original', '+++ modified'];
  let prevShown = false;
  let hunkOrigLine = 1;
  let hunkModLine = 1;
  let hunkOrigCount = 0;
  let hunkModCount = 0;
  let hunkLines: string[] = [];

  // Count lines to build hunk headers
  let origLine = 1;
  let modLine = 1;
  let hunkStartOrig = 1;
  let hunkStartMod = 1;
  let inHunk = false;

  for (let i = 0; i < hunks.length; i++) {
    const h = hunks[i];
    const show = changedIndices.has(i);

    if (show) {
      if (!inHunk) {
        hunkStartOrig = origLine;
        hunkStartMod = modLine;
        hunkLines = [];
        hunkOrigCount = 0;
        hunkModCount = 0;
        inHunk = true;
      }
      if (h.type === 'context') {
        hunkLines.push(`  ${h.line}`);
        hunkOrigCount++;
        hunkModCount++;
        origLine++;
        modLine++;
      } else if (h.type === 'remove') {
        hunkLines.push(`- ${h.line}`);
        hunkOrigCount++;
        origLine++;
      } else {
        hunkLines.push(`+ ${h.line}`);
        hunkModCount++;
        modLine++;
      }

      // Check if next line is not shown (end of hunk)
      const nextShown = i + 1 < hunks.length && changedIndices.has(i + 1);
      if (!nextShown) {
        lines.push(`@@ -${hunkStartOrig},${hunkOrigCount} +${hunkStartMod},${hunkModCount} @@`);
        lines.push(...hunkLines);
        inHunk = false;
      }
    } else {
      // Lines we skip — still need to advance counters
      if (h.type === 'context') {
        origLine++;
        modLine++;
      } else if (h.type === 'remove') {
        origLine++;
      } else {
        modLine++;
      }
    }
  }

  return lines.join('\n');
}

/**
 * Simple LCS (Longest Common Subsequence) for arrays of strings.
 */
function computeLCS(a: string[], b: string[]): string[] {
  const m = a.length;
  const n = b.length;

  // For large files, fall back to a simpler direct diff
  if (m * n > 1_000_000) {
    return simpleLCS(a, b);
  }

  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));

  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (a[i - 1] === b[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }

  // Backtrack to find the LCS
  const result: string[] = [];
  let i = m;
  let j = n;
  while (i > 0 && j > 0) {
    if (a[i - 1] === b[j - 1]) {
      result.unshift(a[i - 1]);
      i--;
      j--;
    } else if (dp[i - 1][j] > dp[i][j - 1]) {
      i--;
    } else {
      j--;
    }
  }

  return result;
}

/**
 * Fallback: extract only exact consecutive matches for large files.
 */
function simpleLCS(a: string[], b: string[]): string[] {
  const setB = new Set(b);
  return a.filter((line) => setB.has(line));
}

// ─── MCP Server ──────────────────────────────────────────────────────────────

function createMcpServer(): McpServer {
  const server = new McpServer({
    name: 'sandbox-server',
    version: '1.0.0',
  });

  // Tool 1: write_draft
  server.registerTool(
    'write_draft',
    {
      description: 'Write content to a file in the workspace sandbox directory. Never modifies files outside the workspace.',
      inputSchema: {
        filename: z.string().describe('Name of file to write in workspace'),
        content: z.string().describe('Full file content to write'),
      },
    },
    async ({ filename, content }) => {
      let filePath: string;
      try {
        filePath = safeWorkspacePath(filename);
      } catch (err) {
        return {
          content: [
            {
              type: 'text' as const,
              text: `Error: ${err instanceof Error ? err.message : String(err)}`,
            },
          ],
          isError: true,
        };
      }

      try {
        // Ensure parent directory exists (supports subdirectories within workspace)
        const dir = path.dirname(filePath);
        if (!fs.existsSync(dir)) {
          fs.mkdirSync(dir, { recursive: true });
        }

        fs.writeFileSync(filePath, content, 'utf8');
        console.log(`[write_draft] Wrote file: ${filePath}`);

        return {
          content: [
            {
              type: 'text' as const,
              text: `Successfully wrote ${content.length} characters to: ${filePath}`,
            },
          ],
        };
      } catch (err) {
        console.error(`[write_draft] Error writing file: ${err}`);
        return {
          content: [
            {
              type: 'text' as const,
              text: `Error writing file: ${err instanceof Error ? err.message : String(err)}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  // Tool 2: get_diff
  server.registerTool(
    'get_diff',
    {
      description: 'Compute a line-by-line diff between two strings. Returns a unified diff format showing additions and removals.',
      inputSchema: {
        original: z.string().describe('Original file content'),
        modified: z.string().describe('Modified file content'),
      },
    },
    async ({ original, modified }) => {
      try {
        const diff = computeDiff(original, modified);
        return {
          content: [
            {
              type: 'text' as const,
              text: diff,
            },
          ],
        };
      } catch (err) {
        return {
          content: [
            {
              type: 'text' as const,
              text: `Error computing diff: ${err instanceof Error ? err.message : String(err)}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  // Tool 3: read_draft
  server.registerTool(
    'read_draft',
    {
      description: 'Read content from a file in the workspace sandbox directory.',
      inputSchema: {
        filename: z.string().describe('Name of file to read from workspace'),
      },
    },
    async ({ filename }) => {
      let filePath: string;
      try {
        filePath = safeWorkspacePath(filename);
      } catch (err) {
        return {
          content: [
            {
              type: 'text' as const,
              text: `Error: ${err instanceof Error ? err.message : String(err)}`,
            },
          ],
          isError: true,
        };
      }

      try {
        if (!fs.existsSync(filePath)) {
          return {
            content: [
              {
                type: 'text' as const,
                text: `Error: File not found: ${filename}`,
              },
            ],
            isError: true,
          };
        }

        const content = fs.readFileSync(filePath, 'utf8');
        console.log(`[read_draft] Read file: ${filePath}`);

        return {
          content: [
            {
              type: 'text' as const,
              text: content,
            },
          ],
        };
      } catch (err) {
        console.error(`[read_draft] Error reading file: ${err}`);
        return {
          content: [
            {
              type: 'text' as const,
              text: `Error reading file: ${err instanceof Error ? err.message : String(err)}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  // Tool 4: list_drafts
  server.registerTool(
    'list_drafts',
    {
      description: 'List all files currently in the workspace sandbox directory.',
      inputSchema: {},
    },
    async () => {
      try {
        if (!fs.existsSync(WORKSPACE_DIR)) {
          return {
            content: [
              {
                type: 'text' as const,
                text: JSON.stringify({ files: [] }),
              },
            ],
          };
        }

        const files = fs.readdirSync(WORKSPACE_DIR);
        console.log(`[list_drafts] Listed ${files.length} files in workspace`);

        return {
          content: [
            {
              type: 'text' as const,
              text: JSON.stringify({ files }),
            },
          ],
        };
      } catch (err) {
        console.error(`[list_drafts] Error listing files: ${err}`);
        return {
          content: [
            {
              type: 'text' as const,
              text: `Error listing files: ${err instanceof Error ? err.message : String(err)}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  return server;
}

// ─── Express App ─────────────────────────────────────────────────────────────

const app = express();
app.use(cors());
app.use(express.json());

// Health check
app.get('/health', (_req: Request, res: Response) => {
  res.status(200).json({ status: 'ok', workspace: WORKSPACE_DIR });
});

// List workspace files via REST
app.get('/workspace', (_req: Request, res: Response) => {
  try {
    if (!fs.existsSync(WORKSPACE_DIR)) {
      res.json({ files: [] });
      return;
    }
    const files = fs.readdirSync(WORKSPACE_DIR);
    res.json({ files });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

// Download a specific draft file via REST (for Streamlit to fetch)
app.get('/workspace/:filename', (req: Request, res: Response) => {
  const { filename } = req.params;
  let filePath: string;
  try {
    filePath = safeWorkspacePath(filename);
  } catch (err) {
    res.status(400).json({ error: String(err) });
    return;
  }

  if (!fs.existsSync(filePath)) {
    res.status(404).json({ error: `File not found: ${filename}` });
    return;
  }

  res.sendFile(filePath);
});

// MCP endpoint — POST only (stateless Streamable HTTP transport)
app.post('/mcp', async (req: Request, res: Response) => {
  const server = createMcpServer();
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
  });

  res.on('close', () => {
    transport.close();
  });

  try {
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error('[/mcp] Error handling MCP request:', err);
    if (!res.headersSent) {
      res.status(500).json({ error: 'Internal server error' });
    }
  }
});

// Reject GET /mcp and DELETE /mcp
app.get('/mcp', (_req: Request, res: Response) => {
  res.status(405).json({ error: 'Method Not Allowed. Use POST /mcp for MCP requests.' });
});

app.delete('/mcp', (_req: Request, res: Response) => {
  res.status(405).json({ error: 'Method Not Allowed.' });
});

// ─── Start Server ─────────────────────────────────────────────────────────────

app.listen(PORT, () => {
  console.log(`[sandbox-server] Listening on port ${PORT}`);
  console.log(`[sandbox-server] MCP endpoint: POST http://localhost:${PORT}/mcp`);
  console.log(`[sandbox-server] Health check: GET  http://localhost:${PORT}/health`);
  console.log(`[sandbox-server] Workspace:    ${WORKSPACE_DIR}`);
});
