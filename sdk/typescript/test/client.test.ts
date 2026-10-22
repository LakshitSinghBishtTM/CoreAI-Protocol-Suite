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
