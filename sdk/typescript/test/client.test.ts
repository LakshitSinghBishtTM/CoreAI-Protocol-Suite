import { test } from "node:test";
import assert from "node:assert/strict";
import { CaipsClient, CaipsError } from "../src/index.js";

const mk = (h: (url: string, init: any) => Response) =>
  new CaipsClient("http://t/", "k", (async (u: any, i: any) => h(String(u), i)) as any);

test("sends auth header and path", async () => {
  let seen: any;
  const c = mk((u, i) => { seen = { u, auth: i.headers.Authorization }; return Response.json({ choices: [] }); });
  await c.chat("fast", []);
  assert.deepEqual(seen, { u: "http://t/v1/chat/completions", auth: "Bearer k" });
});

test("errors carry status and detail", async () => {
  const c = mk(() => Response.json({ detail: "nope" }, { status: 403 }));
  await assert.rejects(c.listTools(), (e: any) => e instanceof CaipsError && e.status === 403 && e.detail === "nope");
});

test("waitRun polls until terminal", async () => {
  const s = ["running", "completed"];
  const c = mk(() => Response.json({ status: s.shift() }));
  assert.equal((await c.waitRun("r", 1000, 0)).status, "completed");
});

test("run helpers use the right endpoints and bodies", async () => {
  const seen: any[] = [];
  const c = mk((u, i) => {
    seen.push([i.method, u.replace("http://t", ""), i.body]);
    return Response.json({ data: [], ok: true });
  });
  await c.listAgents();
  await c.runEvents("r1", 4);
  await c.pauseRun("r1");
  await c.resumeRun("r1", true);
  await c.killRuns("alice");
  assert.deepEqual(seen, [
    ["GET", "/v1/harness/agents", undefined],
    ["GET", "/v1/runs/r1/events?after=4", undefined],
    ["POST", "/v1/runs/r1/pause", undefined],
    ["POST", "/v1/runs/r1/resume", JSON.stringify({ approve: true })],
    ["POST", "/v1/runs/kill", JSON.stringify({ owner: "alice" })],
  ]);
});

test("runAgent starts a run then waits for it", async () => {
  const c = mk((u, i) =>
    i.method === "POST" ? Response.json({ id: "r9", status: "running" }) : Response.json({ id: "r9", status: "completed", result: "ok" }));
  const run = await c.runAgent("helper", "task", 1000, 0);
  assert.equal(run.result, "ok");
});

test("waitRun can stop when blocked on an approval", async () => {
  const c = mk(() => Response.json({ status: "waiting_approval" }));
  assert.equal((await c.waitRun("r", 1000, 0, true)).status, "waiting_approval");
  await assert.rejects(c.waitRun("r", 30, 5));
});
