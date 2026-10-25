export class CaipsError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(`HTTP ${status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
}

export type RunStatus = "running" | "paused" | "waiting_approval" | "completed" | "failed" | "cancelled";
export interface Run {
  id: string; agent: string; owner: string; status: RunStatus;
  stop_reason: string | null; step: number; cost_usd: number;
  input_tokens: number; output_tokens: number; control: string;
  pending: { calls: Record<string, unknown>[] } | null; result: string | null;
  created: number; updated: number; deadline: number | null;
}
export interface RunEvent { seq: number; ts: number; type: string; [k: string]: unknown }
export interface Agent {
  name: string; allowed_tools: string[]; approval_tools: string[];
  max_steps: number; max_cost_usd: number | null; model: string | null;
}
export interface Tool { name: string; description: string; version: string; owner: string; permission: string; parameters: Record<string, unknown> }
export interface ToolResult { content: { type: string; text: string }[]; isError: boolean }
export interface Approval {
  id: string; caller: string; tool: string; arguments: Record<string, unknown>;
  status: "pending" | "approved" | "denied" | "expired"; decided_by: string | null;
}
export interface ChatCompletion {
  id?: string; model?: string; usage?: Record<string, number>;
  choices: { index?: number; message: Record<string, unknown>; finish_reason?: string | null }[];
}
export type Message = { role: string; content: unknown; [k: string]: unknown };

const TERMINAL = new Set<string>(["completed", "failed", "cancelled"]);
const BLOCKED = new Set<string>(["paused", "waiting_approval"]);

export class CaipsClient {
  private base: string;
  constructor(
    baseUrl = "http://localhost:6969",
    private apiKey?: string,
    private fetchImpl: typeof fetch = fetch,
  ) {
    this.base = baseUrl.replace(/\/+$/, "");
  }

  private async req<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = {};
    if (this.apiKey) headers["Authorization"] = `Bearer ${this.apiKey}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const r = await this.fetchImpl(this.base + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await r.text();
    let data: any;
    try { data = text ? JSON.parse(text) : {}; } catch { data = text; }
    if (!r.ok) throw new CaipsError(r.status, data?.detail ?? data);
    return data as T;
  }

  chat(model: string, messages: Message[], params: Record<string, unknown> = {}) {
    return this.req<ChatCompletion>("POST", "/v1/chat/completions", { model, messages, ...params });
  }
  async models() { return (await this.req<{ data: any[] }>("GET", "/v1/models")).data; }
  async listTools(): Promise<Tool[]> { return (await this.req<{ data: Tool[] }>("GET", "/v1/tools")).data; }

  async callTool(name: string, args: Record<string, unknown> = {}): Promise<ToolResult> {
    const r = await this.req<any>("POST", "/mcp", {
      jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args },
    });
    if (r.error) throw new CaipsError(200, r.error);
    return r.result;
  }

  async listAgents(): Promise<Agent[]> { return (await this.req<{ data: Agent[] }>("GET", "/v1/harness/agents")).data; }
  async listRuns(): Promise<Run[]> { return (await this.req<{ data: Run[] }>("GET", "/v1/runs")).data; }
  async runEvents(id: string, after = 0): Promise<RunEvent[]> {
    return (await this.req<{ data: RunEvent[] }>("GET", `/v1/runs/${id}/events?after=${after}`)).data;
  }
  startRun(agent: string, task: string) { return this.req<Run>("POST", "/v1/runs", { agent, task }); }
  getRun(id: string) { return this.req<Run>("GET", `/v1/runs/${id}`); }
  cancelRun(id: string) { return this.req<{ cancelled: boolean }>("POST", `/v1/runs/${id}/cancel`); }
  pauseRun(id: string) { return this.req<{ pausing: boolean }>("POST", `/v1/runs/${id}/pause`); }
  /** Continue a paused run, or answer an approval request (needs admin). */
  resumeRun(id: string, approve?: boolean) { return this.req<Run>("POST", `/v1/runs/${id}/resume`, { approve: approve ?? null }); }
  /** Kill switch (admin): stop all active runs, or one owner's. */
  killRuns(owner?: string) { return this.req<{ killed: number; owner: string | null }>("POST", "/v1/runs/kill", { owner: owner ?? null }); }
  /** Start a run and wait for it to end. */
  async runAgent(agent: string, task: string, timeoutMs = 300_000, pollMs = 1000): Promise<Run> {
    return this.waitRun((await this.startRun(agent, task)).id, timeoutMs, pollMs);
  }

  /** Poll until the run ends; with stopWhenBlocked also when it is paused or waiting for approval. */
  async waitRun(id: string, timeoutMs = 300_000, pollMs = 1000, stopWhenBlocked = false): Promise<Run> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const run = await this.getRun(id);
      if (TERMINAL.has(run.status) || (stopWhenBlocked && BLOCKED.has(run.status))) return run;
      if (Date.now() >= deadline) throw new Error(`run ${id} still ${run.status}`);
      await new Promise((res) => setTimeout(res, pollMs));
    }
  }

  async pendingApprovals(): Promise<Approval[]> {
    const r = await this.req<any>("GET", "/v1/tools/approvals");
    return Array.isArray(r) ? r : r.data;
  }
  decide(approvalId: string, approve: boolean) {
    return this.req<Approval>("POST", `/v1/tools/approvals/${approvalId}`, { approve });
  }
}
