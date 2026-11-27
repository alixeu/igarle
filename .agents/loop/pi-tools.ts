import { randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { DatabaseSync } from "node:sqlite";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

interface LoopContext {
  token: string;
  root: string;
  files: string[];
  editable: string[];
  evidence: Array<{ id: number }>;
  evidence_db?: string;
  task_id?: string;
}

const MAX_FILE_BYTES = 1_000_000;

function reply(text: string) {
  return { content: [{ type: "text" as const, text }], details: {} };
}

function resolveFile(root: string, relative: string): string {
  const parts = relative.split("/");
  if (parts.some((part) => !part || part === "." || part === ".." || part.includes("\\"))) {
    throw new Error("Use a canonical repository-relative path.");
  }
  let target = root;
  for (const part of parts) {
    target = path.join(target, part);
    const stat = fs.lstatSync(target, { throwIfNoEntry: false });
    if (stat?.isSymbolicLink()) throw new Error("Symlinks are outside this tool's scope.");
  }
  return target;
}

function readText(target: string): string {
  const metadata = fs.statSync(target);
  if (!metadata.isFile() || metadata.size > MAX_FILE_BYTES) {
    throw new Error("Expected a regular text file under 1 MB.");
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(fs.readFileSync(target));
}

function replaceFile(target: string, contents: string, mode: number) {
  if (Buffer.byteLength(contents) > MAX_FILE_BYTES) throw new Error("The resulting file exceeds 1 MB.");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const temporary = path.join(path.dirname(target), ".pi-loop-" + randomUUID());
  try {
    fs.writeFileSync(temporary, contents, { mode, flag: "wx" });
    fs.renameSync(temporary, target);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
}

export default function loopTools(pi: ExtensionAPI) {
  const config = process.env.PI_LOOP_CONTEXT;
  if (!config) throw new Error("The Pi Loop action context is missing.");
  const context: LoopContext = JSON.parse(fs.readFileSync(config, "utf-8"));
  const root = fs.realpathSync(context.root);
  if (root !== fs.realpathSync(process.cwd())) throw new Error("Pi started in the wrong checkout.");

  const editable = new Set(context.editable);
  const readable = new Set([...context.files, ...editable]);
  const names = [...readable].sort();
  let submitted = false;

  pi.registerCommand("pi-loop-protocol-v1", {
    description: "Identify the scoped Pi Loop adapter.",
    handler: async () => {},
  });

  pi.registerTool({
    name: "list_files",
    label: "List project files",
    description: "List authorized repository paths. Filter by a substring and use offset for further results.",
    parameters: Type.Object({
      contains: Type.Optional(Type.String({ maxLength: 160 })),
      offset: Type.Optional(Type.Integer({ minimum: 0 })),
    }),
    async execute(_id, { contains, offset = 0 }) {
      const matches = contains ? names.filter((name) => name.includes(contains)) : names;
      const page = matches.slice(offset, offset + 180);
      return reply(JSON.stringify({ paths: page, next: offset + page.length < matches.length
        ? offset + page.length : null }));
    },
  });

  pi.registerTool({
    name: "read_file",
    label: "Read project file",
    description: "Read an authorized UTF-8 file by line. Paths must be repository relative.",
    parameters: Type.Object({
      path: Type.String(),
      start_line: Type.Optional(Type.Integer({ minimum: 1 })),
      lines: Type.Optional(Type.Integer({ minimum: 1, maximum: 250 })),
    }),
    async execute(_id, args) {
      if (!readable.has(args.path)) throw new Error("The file is outside the assigned read scope.");
      const contents = readText(resolveFile(root, args.path)).split("\n");
      const start = args.start_line ?? 1;
      const count = args.lines ?? 160;
      return reply(contents.slice(start - 1, start + count - 1)
        .map((line, index) => (start + index) + " " + line).join("\n").slice(0, 22_000));
    },
  });

  pi.registerTool({
    name: "search_evidence",
    label: "Search task evidence",
    description: "Search this task's saved events by text and optional event kind, with pagination.",
    parameters: Type.Object({
      query: Type.Optional(Type.String({ maxLength: 200 })),
      kind: Type.Optional(Type.String({ maxLength: 80 })),
      offset: Type.Optional(Type.Integer({ minimum: 0 })),
    }),
    async execute(_id, args) {
      if (!context.evidence_db || !context.task_id) return reply("[]");
      const db = new DatabaseSync(context.evidence_db, { readOnly: true });
      try {
        const events = db.prepare(
          "SELECT id,kind,revision,created_at,substr(data,1,400) AS excerpt FROM events " +
          "WHERE task_id=? AND instr(lower(data),lower(?))>0 AND (?='' OR kind=?) " +
          "ORDER BY id DESC LIMIT 25 OFFSET ?",
        ).all(context.task_id, args.query ?? "", args.kind ?? "", args.kind ?? "", args.offset ?? 0);
        return reply(JSON.stringify(events));
      } finally {
        db.close();
      }
    },
  });

  pi.registerTool({
    name: "read_evidence",
    label: "Read task evidence",
    description: "Load a saved event for this task by evidence ID. Use next_offset to read long events.",
    parameters: Type.Object({
      id: Type.Integer({ minimum: 1 }),
      offset: Type.Optional(Type.Integer({ minimum: 0 })),
    }),
    async execute(_id, args) {
      if (context.evidence_db && context.task_id) {
        const db = new DatabaseSync(context.evidence_db, { readOnly: true });
        try {
          const event = db.prepare(
            "SELECT id,kind,revision,created_at,data FROM events WHERE task_id=? AND id=?",
          ).get(context.task_id, args.id);
          if (!event) throw new Error("The evidence is outside this task.");
          const serialized = JSON.stringify(event);
          const start = args.offset ?? 0;
          const chunk = serialized.slice(start, start + 20_000);
          return reply(JSON.stringify({ text: chunk, next_offset: start + chunk.length < serialized.length
            ? start + chunk.length : null }));
        } finally {
          db.close();
        }
      }
      const item = context.evidence.find((event) => event.id === args.id);
      if (!item) throw new Error("The evidence was not supplied for this task.");
      const serialized = JSON.stringify(item);
      const start = args.offset ?? 0;
      const chunk = serialized.slice(start, start + 20_000);
      return reply(JSON.stringify({ text: chunk, next_offset: start + chunk.length < serialized.length
        ? start + chunk.length : null }));
    },
  });

  if (editable.size) {
    pi.registerTool({
      name: "change_file",
      label: "Change project file",
      description: "Create a file, replace exactly one occurrence of old text, or delete an assigned file.",
      parameters: Type.Object({
        path: Type.String(),
        operation: Type.Union([Type.Literal("create"), Type.Literal("replace"), Type.Literal("delete")]),
        old_text: Type.Optional(Type.String({ maxLength: 120_000 })),
        text: Type.Optional(Type.String({ maxLength: 120_000 })),
      }),
      async execute(_id, args) {
        if (submitted || !editable.has(args.path)) throw new Error("The file is outside this write action.");
        const target = resolveFile(root, args.path);
        if (args.operation === "create") {
          if (fs.existsSync(target) || args.text === undefined) throw new Error("Expected a new file and its text.");
          replaceFile(target, args.text, 0o644);
        } else {
          const before = readText(target);
          if (args.operation === "delete") {
            fs.unlinkSync(target);
          } else {
            if (!args.old_text || args.text === undefined) throw new Error("Include old text and replacement text.");
            const first = before.indexOf(args.old_text);
            if (first < 0 || before.indexOf(args.old_text, first + 1) >= 0) {
              throw new Error("Old text must match exactly once.");
            }
            const after = before.slice(0, first) + args.text + before.slice(first + args.old_text.length);
            replaceFile(target, after, fs.statSync(target).mode & 0o777);
          }
        }
        return reply("Updated " + args.path);
      },
    });
  }

  pi.registerTool({
    name: "finish_step",
    label: "Submit step result",
    description: "Submit the single structured result. Encode the action-specific object as a JSON string.",
    parameters: Type.Object({ result: Type.String({ maxLength: 16000 }) }),
    async execute(_id, { result }) {
      if (submitted) throw new Error("A result already exists for this action.");
      const value = JSON.parse(result);
      if (!value || typeof value !== "object" || Array.isArray(value) || typeof value.status !== "string") {
        throw new Error("Expected an object with a status string.");
      }
      submitted = true;
      return { ...reply("Result recorded for the scheduler."), details: { token: context.token, value } };
    },
  });
}
