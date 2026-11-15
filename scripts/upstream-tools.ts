import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

// Only this trusted extension can start the host bridge; model commands stay in Docker.
export default function (pi: ExtensionAPI) {
  const z = pi.zod;
  let done = false;
  let queue = Promise.resolve();
  const schemas = {
    workspace: z.object({ command: z.string() }),
    fetch_source: z.object({ url: z.string(), path: z.string().optional(), mode: z.enum(["text", "archive"]).default("text"), offset: z.number().int().min(0).default(0) }),
    finish: z.object({ summary: z.string(), recipes: z.array(z.object({ name: z.string(), decision: z.enum(["accept", "reject"]), reason: z.string().min(1) })) }),
  };
  const descriptions = {
    workspace: "Run a shell command in the offline disposable Python container at /work. Only /work persists. Commands time out after 60 seconds and output is limited to 64 KiB.",
    fetch_source: "Download public HTTPS source using validated pinned addresses. Returns a computed SHA256 and workspace path. Archives are not extracted. Text returns up to 64 KiB from offset. Choose a new path for each download, or omit path.",
    finish: "Submit the final batch summary and exactly one justified accept/reject decision per candidate. Call once after all work; no tools are allowed afterward.",
  };
  for (const name of ["workspace", "fetch_source", "finish"] as const) {
    pi.registerTool({
      name, label: name, description: descriptions[name], parameters: schemas[name],
      async execute(_id: string, params: unknown, signal: AbortSignal) {
        const operation = queue.then(async () => {
          if (done) throw new Error("Session is already finished");
          if (signal.aborted) throw new Error("Tool aborted");
          const { promise, resolve, reject } = Promise.withResolvers<string>();
          const child = spawn(process.env.UPSTREAM_PYTHON!, [process.env.UPSTREAM_BRIDGE!, process.env.UPSTREAM_CONFIG!], {
            env: { PATH: "/usr/local/bin:/usr/bin:/bin", LANG: "C.UTF-8" },
            stdio: ["pipe", "pipe", "pipe"],
          });
          let output = "";
          const abort = () => child.kill("SIGTERM");
          signal.addEventListener("abort", abort, { once: true });
          child.stdout.on("data", data => { output += data.toString(); });
          child.stderr.resume();
          child.on("error", reject);
          child.on("close", code => {
            signal.removeEventListener("abort", abort);
            if (code !== 0) reject(new Error(output || "Trusted tool failed"));
            else resolve(output);
          });
          child.stdin.end(JSON.stringify({ name, params }));
          const result = await promise;
          if (name === "finish") done = true;
          return { content: [{ type: "text", text: result }], details: {} };
        });
        queue = operation.then(() => undefined, () => undefined);
        return operation;
      },
    });
  }
}
