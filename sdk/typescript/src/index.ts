export class CaipsError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(`HTTP ${status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
}

export interface Run { id?: string; status: string; [k: string]: unknown }
export interface Tool { name: string; description: string; version: string; owner: string; permission: string; parameters: unknown }
export type Message = { role: string; content: unknown; [k: string]: unknown };

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export class CaipsClient {
  private base: string;
  constructor(
    baseUrl = "http://localhost:8000",
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
    return this.req<any>("POST", "/v1/chat/completions", { model, messages, ...params });
  }
  async models() { return (await this.req<{ data: any[] }>("GET", "/v1/models")).data; }
  async listTools(): Promise<Tool[]> { return (await this.req<{ data: Tool[] }>("GET", "/v1/tools")).data; }

  async callTool(name: string, args: Record<string, unknown> = {}) {
    const r = await this.req<any>("POST", "/mcp", {
      jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args },
    });
    if (r.error) throw new CaipsError(200, r.error);
    return r.result;
  }

  startRun(agent: string, task: string) { return this.req<Run>("POST", "/v1/runs", { agent, task }); }
  getRun(id: string) { return this.req<Run>("GET", `/v1/runs/${id}`); }
  cancelRun(id: string) { return this.req<Run>("POST", `/v1/runs/${id}/cancel`); }

  async waitRun(id: string, timeoutMs = 300_000, pollMs = 1000): Promise<Run> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const run = await this.getRun(id);
      if (TERMINAL.has(run.status)) return run;
      if (Date.now() >= deadline) throw new Error(`run ${id} still ${run.status}`);
      await new Promise((res) => setTimeout(res, pollMs));
    }
  }

  async pendingApprovals() {
    const r = await this.req<any>("GET", "/v1/tools/approvals");
    return Array.isArray(r) ? r : r.data;
  }
  decide(approvalId: string, approve: boolean) {
    return this.req<any>("POST", `/v1/tools/approvals/${approvalId}`, { approve });
  }
}
