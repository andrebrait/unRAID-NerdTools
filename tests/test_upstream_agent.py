"""Offline tests for the bounded upstream recipe agent: real filesystem, local provider protocol fixture, no network."""
import copy
import hashlib
import http.server
import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upstream-agent.py"
SPEC = importlib.util.spec_from_file_location("upstream_agent", SCRIPT)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)

REAL_GETADDRINFO = socket.getaddrinfo
SECRET = "TOP-SECRET-VALUE"
TOOLS = {"read_package", "list_files", "read_file", "write_recipe", "remove_recipe", "write_package", "patch_package", "fetch_url", "finish"}


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def slackbuild(name, extra=""):
    return f"#!/bin/sh\necho {name}{extra}\n"


def pkg(name, version="1.0", flags=None, smoke=None):
    entry = {"name": name, "version": version, "recipe": f"build/recipes/{name}/{name}.SlackBuild",
             "recipe_sha256": sha(slackbuild(name)),
             "provenance": {"kind": "source", "url": f"https://example.org/{name}", "revision": "a" * 40},
             "inputs": [{"url": f"https://example.org/{name}-{version}.tar.gz", "file": f"{name}-{version}.tar.gz",
                         "sha256": sha(name + version)}],
             "dependencies": [], "build_dependencies": []}
    entry.update(flags or {})
    entry["smoke"] = smoke if smoke is not None else [[f"/opt/nerdtools/bin/{name}", "--version"]]
    return entry


def lock_text(entries, compact=()):
    chunks = ["  " + (json.dumps(e) if e["name"] in compact else json.dumps(e, indent=2).replace("\n", "\n  ")) for e in entries]
    return "[\n" + ",\n".join(chunks) + "\n]\n"


DELTA = pkg("delta", flags={"internal": True})  # used only by the batch's candidate: repairable
EPSILON = pkg("epsilon", flags={"internal": True})  # also used by a non-candidate: not writable
ALPHA = pkg("alpha", smoke=[["/opt/nerdtools/bin/alpha", "--version"], ["/usr/bin/test", "-x", "/opt/nerdtools/bin/alpha"]])
ALPHA["dependencies"] = ["delta", "epsilon"]
BETA = pkg("beta", flags={"internal": True}, smoke=[["/usr/bin/test", "-r", "/opt/nerdtools/lib64/libbeta.so"]])
GAMMA = pkg("gamma")
GAMMA["dependencies"] = ["epsilon"]
TOOLCHAIN = [{"name": "tc", "url": "https://example.org/tc.tar.gz", "file": "tc.tar.gz", "sha256": sha("tc")}]
GAMMA_LINE = "  " + json.dumps(GAMMA)  # hand-formatted entry: edits to other entries must not reformat it


def write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def make_repo(repo):
    write(repo / "build/locks/slackware.json", lock_text([ALPHA, GAMMA], compact=("gamma",)))
    write(repo / "build/locks/slackbuilds.json", lock_text([BETA, DELTA, EPSILON]))
    write(repo / "build/locks/toolchain.json", json.dumps(TOOLCHAIN, indent=2) + "\n")
    for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
        write(repo / f"build/recipes/{name}/{name}.SlackBuild", slackbuild(name), 0o755)
        write(repo / f"build/recipes/{name}/slack-desc", f"{name}: description\n")
    write(repo / "build/recipes/alpha/patches/fix.patch", "--- a\n+++ b\n")
    write(repo / "build/recipes/alpha/server.key", SECRET)
    write(repo / "build/recipes/alpha/api-token.txt", SECRET)
    write(repo / "build/README.md", "Build guide: use the private prefix.\n")
    write(repo / "build/native.py", "# trusted\n")
    write(repo / "docs/notes.md", "public notes\n")
    write(repo / "README.md", "readme\n")
    write(repo / "scripts/upstream-updates.py", "# orchestrator\n")
    write(repo / ".env", f"TOKEN={SECRET}\n")
    write(repo / ".git/config", f"[credential]\n\ttoken = {SECRET}\n")


def tree(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            out[rel] = ("dir",)
        else:
            out[rel] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
    return out


def candidate(name, ours, upstream):
    return {"name": name, "ours": ours, "upstream": upstream, "source": "Slackware-current", "url": "https://slackware.example/PACKAGES.TXT"}


def context(**extra):
    base = {"candidates": [candidate("alpha", "1.0", "2.0"), candidate("beta", "1.0", "1.5")],
            "feedback": "review feedback text", "guide": "build guide text", "attempt": 1, "previous_report": {"summary": "earlier attempt"}}
    base.update(extra)
    return base


NEW_ALPHA = slackbuild("alpha", " 2")


def alpha_package(version="2.0", **changes):
    entry = copy.deepcopy(ALPHA)
    entry["version"] = version
    entry["inputs"] = [{"url": f"https://example.org/alpha-{version}.tar.gz", "file": f"alpha-{version}.tar.gz", "sha256": sha("alpha" + version)}]
    del entry["recipe_sha256"]  # computed by the tool: the model cannot hash files
    entry.update(changes)
    return entry


WRITE_ALPHA = ("write_recipe", {"path": "build/recipes/alpha/alpha.SlackBuild", "content": NEW_ALPHA, "executable": True})
PACKAGE_ALPHA = ("write_package", {"name": "alpha", "package": alpha_package()})


def finish(alpha="accept", beta="reject"):
    return ("finish", {"summary": "done", "recipes": [{"name": "alpha", "decision": alpha, "reason": "alpha reason"},
                                                      {"name": "beta", "decision": beta, "reason": "beta reason"}]})


def newdep_package(**changes):
    entry = {"name": "libnew", "version": "3.1", "recipe": "build/recipes/libnew/libnew.SlackBuild",
             "provenance": {"kind": "source", "url": "https://example.org/libnew", "revision": "b" * 40},
             "inputs": [{"url": "https://example.org/libnew-3.1.tar.gz", "file": "libnew-3.1.tar.gz", "sha256": sha("libnew")}],
             "dependencies": [], "build_dependencies": [], "internal": True,
             "smoke": [["/usr/bin/test", "-r", "/opt/nerdtools/lib64/libnew.so"]]}
    entry.update(changes)
    return entry


def oa(*calls, status="completed", **extra):
    output = [{"type": "function_call", "call_id": f"call_{n}", "name": name, "arguments": json.dumps(args) if not isinstance(args, str) else args}
              for n, (name, args) in enumerate(calls)]
    return 200, {"id": "resp_1", "status": status, "output": output, **extra}


def an(*calls, stop="tool_use"):
    content = [{"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": args} for n, (name, args) in enumerate(calls)]
    return 200, {"id": "msg_1", "type": "message", "role": "assistant", "stop_reason": stop, "content": content}


class FakeProvider:
    """Scripted local HTTP server speaking just enough of the two provider protocols."""

    def __init__(self):
        self.queues = {"openai": [], "anthropic": []}
        self.requests = {"openai": [], "anthropic": []}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                name = self.path.strip("/")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests[name].append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, payload = owner.queues[name].pop(0) if owner.queues[name] else (418, {"error": "script exhausted"})
                if status == "hang":
                    time.sleep(2)
                    return
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, name):
        return f"http://127.0.0.1:{self.server.server_port}/{name}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "repo"
        make_repo(self.repo)
        write(self.root / "outside.txt", SECRET)

    def session(self, **extra):
        return agent.Session(self.repo, context(**extra))

    def read(self, rel):
        return (self.repo / rel).read_text()

    def lock(self, name="slackware"):
        return json.loads(self.read(f"build/locks/{name}.json"))

    def patch(self, name, value):
        patcher = mock.patch.object(agent, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)


class ProviderCase(Base):
    def setUp(self):
        super().setUp()
        self.provider = FakeProvider()
        self.addCleanup(self.provider.close)
        self.patch("OPENAI_URL", self.provider.url("openai"))
        self.patch("ANTHROPIC_URL", self.provider.url("anthropic"))
        self.patch("PROVIDER_TIMEOUT", 5)
        env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-openai-test", "ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def script(self, provider, *responses):
        self.provider.queues[provider].extend(responses)

    def run_agent(self, **extra):
        return agent.run_agent(self.repo, context(**extra))

    def sent(self, provider):
        return self.provider.requests[provider]


class SessionProtocolTests(ProviderCase):
    def test_openai_session_ports_one_candidate_rejects_the_other_and_sends_the_documented_protocol(self):
        beta_lock = self.read("build/locks/slackbuilds.json")
        beta_tree = tree(self.repo / "build/recipes/beta")
        self.script("openai", oa(("read_package", {"name": "alpha"})), oa(WRITE_ALPHA), oa(PACKAGE_ALPHA), oa(finish()))

        result = self.run_agent()

        self.assertEqual(result, {"summary": "done", "recipes": [
            {"name": "alpha", "decision": "accept", "reason": "alpha reason"},
            {"name": "beta", "decision": "reject", "reason": "beta reason"}]})
        recipe = self.repo / "build/recipes/alpha/alpha.SlackBuild"
        self.assertEqual(recipe.read_text(), NEW_ALPHA)
        self.assertTrue(recipe.stat().st_mode & 0o111)
        updated = {item["name"]: item for item in self.lock()}["alpha"]
        self.assertEqual(updated["version"], "2.0")
        self.assertEqual(updated["recipe_sha256"], sha(NEW_ALPHA))
        self.assertEqual(updated["smoke"], ALPHA["smoke"])
        self.assertEqual(updated["provenance"], ALPHA["provenance"])
        self.assertIn(GAMMA_LINE, self.read("build/locks/slackware.json"))  # untouched entry keeps its bytes
        self.assertEqual(self.read("build/locks/slackbuilds.json"), beta_lock)
        self.assertEqual(tree(self.repo / "build/recipes/beta"), beta_tree)

        requests = self.sent("openai")
        self.assertEqual(len(requests), 4)
        first = requests[0]
        self.assertEqual(first["headers"]["authorization"], "Bearer sk-openai-test")
        body = first["body"]
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["tool_choice"], "required")
        self.assertIs(body["parallel_tool_calls"], False)
        self.assertEqual(body["max_output_tokens"], agent.MAX_OUTPUT_TOKENS)
        self.assertEqual({tool["name"] for tool in body["tools"]}, TOOLS)
        self.assertTrue(all(tool["type"] == "function" for tool in body["tools"]))
        prompt = body["input"][0]["content"]
        for text in ("alpha", "beta", "2.0", "build guide text", "review feedback text", "earlier attempt"):
            self.assertIn(text, prompt)
        replay = requests[1]["body"]["input"]
        self.assertEqual(replay[1]["type"], "function_call")
        self.assertEqual(replay[2]["type"], "function_call_output")
        self.assertEqual(replay[2]["call_id"], "call_0")
        self.assertEqual(json.loads(replay[2]["output"])["lock"], "build/locks/slackware.json")
        self.assertEqual(self.sent("anthropic"), [])

    def test_models_come_from_environment_and_secrets_never_reach_tool_results(self):
        self.script("openai", oa(("read_file", {"path": ".env"})), oa(finish("reject", "reject")))
        with mock.patch.dict(os.environ, {"OPENAI_MODEL": "gpt-test"}):
            self.run_agent()
        requests = self.sent("openai")
        self.assertEqual(requests[0]["body"]["model"], "gpt-test")
        output = requests[1]["body"]["input"][-1]["output"]
        self.assertIn("error", json.loads(output))
        bodies = json.dumps([request["body"] for request in requests])
        self.assertNotIn(SECRET, bodies)
        self.assertNotIn("sk-openai-test", bodies)

    def test_anthropic_protocol_when_primary_key_is_missing(self):
        del os.environ["OPENAI_API_KEY"]
        self.script("anthropic", an(("read_package", {"name": "alpha"})), an(finish("reject", "reject")))

        result = self.run_agent()

        self.assertEqual([item["decision"] for item in result["recipes"]], ["reject", "reject"])
        self.assertEqual(self.sent("openai"), [])
        first, second = self.sent("anthropic")
        self.assertEqual(first["headers"]["x-api-key"], "sk-ant-test")
        self.assertEqual(first["headers"]["anthropic-version"], "2026-10-21")
        self.assertEqual(first["body"]["model"], "claude-haiku-5-5")
        self.assertEqual(first["body"]["output_config"], {"effort": "medium"})
        self.assertEqual(first["body"]["max_tokens"], agent.MAX_OUTPUT_TOKENS)
        self.assertEqual(first["body"]["tool_choice"], {"type": "any", "disable_parallel_tool_use": True})
        self.assertEqual({tool["name"] for tool in first["body"]["tools"]}, TOOLS)
        self.assertTrue(all("input_schema" in tool for tool in first["body"]["tools"]))
        messages = second["body"]["messages"]
        self.assertEqual(messages[1]["role"], "assistant")
        result_block = messages[2]["content"][0]
        self.assertEqual((messages[2]["role"], result_block["type"], result_block["tool_use_id"]), ("user", "tool_result", "toolu_0"))
        self.assertNotIn("is_error", result_block)

    def test_missing_both_keys_fails_without_any_request(self):
        for value in (None, "  "):
            with self.subTest(value=value):
                os.environ.pop("ANTHROPIC_API_KEY", None)
                os.environ.pop("OPENAI_API_KEY", None)
                if value:
                    os.environ["OPENAI_API_KEY"] = value
                with self.assertRaises(agent.AgentError):
                    self.run_agent()
        self.assertEqual(self.sent("openai") + self.sent("anthropic"), [])

    def test_unknown_candidates_are_rejected_before_any_provider_call(self):
        for candidates in ([], [candidate("nope", "1", "2")], [candidate("alpha", "1.0", "2.0")] * 2, [{"name": "alpha"}]):
            with self.subTest(candidates=candidates):
                with self.assertRaises(agent.AgentError):
                    self.run_agent(candidates=candidates)
        self.assertEqual(self.sent("openai"), [])

    def test_multi_candidate_decisions_follow_candidate_order_with_exact_fields(self):
        self.script("openai", oa(("finish", {"summary": "s", "recipes": [
            {"name": "beta", "decision": "reject", "reason": "b"}, {"name": "alpha", "decision": "reject", "reason": "a"}]})))
        result = self.run_agent()
        self.assertEqual([item["name"] for item in result["recipes"]], ["alpha", "beta"])
        self.assertEqual(set(result), {"summary", "recipes"})
        self.assertTrue(all(set(item) == {"name", "decision", "reason"} for item in result["recipes"]))


class FallbackTests(ProviderCase):
    def test_availability_failures_fall_back_to_anthropic(self):
        for status in (429, 500, 502, 503, 529):
            with self.subTest(status=status):
                self.provider.requests["anthropic"].clear()
                self.script("openai", (status, {"error": {"message": "busy"}}))
                self.script("anthropic", an(finish("reject", "reject")))
                result = self.run_agent()
                self.assertEqual(len(result["recipes"]), 2)
                self.assertEqual(len(self.sent("anthropic")), 1)

    def test_timeout_and_network_errors_fall_back(self):
        self.patch("PROVIDER_TIMEOUT", 0.3)
        self.script("openai", ("hang", None))
        self.script("anthropic", an(finish("reject", "reject")))
        self.run_agent()
        self.assertEqual(len(self.sent("anthropic")), 1)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.patch("OPENAI_URL", f"http://127.0.0.1:{port}/openai")
        self.script("anthropic", an(finish("reject", "reject")))
        self.run_agent()
        self.assertEqual(len(self.sent("anthropic")), 2)

    def test_client_errors_malformed_responses_and_bad_tools_never_fall_back(self):
        cases = {
            "bad request": (400, {"error": {"message": "bad"}}),
            "auth": (401, {"error": {"message": "key"}}),
            "forbidden": (403, {"error": {"message": "no"}}),
            "not found": (404, {}),
            "unprocessable": (422, {}),
            "redirect": (302, {}),
            "not json": (200, b"<html>"),
            "failed status": oa(status="failed"),
            "truncated": oa(("list_files", {"path": "build"}), status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
            "text only": (200, {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]}),
            "unknown tool": oa(("run_shell", {"command": "id"})),
            "bad arguments": oa(("list_files", "{not json")),
            "arguments not object": oa(("list_files", "[]")),
        }
        for label, response in cases.items():
            with self.subTest(label):
                self.script("openai", response)
                with self.assertRaises(agent.AgentError) as caught:
                    self.run_agent()
                self.assertNotIsInstance(caught.exception, agent.Transient)
        self.assertEqual(self.sent("anthropic"), [])

    def test_refusal_is_final_and_anthropic_cannot_override_it(self):
        refusal = (200, {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]})
        filtered = oa(status="incomplete", incomplete_details={"reason": "content_filter"})
        for response in (refusal, filtered):
            self.script("openai", response)
            self.script("anthropic", an(finish("accept", "accept")))
            with self.assertRaises(agent.Refused):
                self.run_agent()
        self.assertEqual(self.sent("anthropic"), [])
        self.assertEqual(tree(self.repo / "build"), tree_original(self))

    def test_fallback_provider_refusal_and_truncation_fail_closed(self):
        for stop in ("refusal", "max_tokens", "end_turn", "pause_turn"):
            with self.subTest(stop=stop):
                self.script("openai", (503, {}))
                self.script("anthropic", (200, {"stop_reason": stop, "content": []}))
                with self.assertRaises(agent.AgentError):
                    self.run_agent()
        self.assertEqual(self.provider.queues["anthropic"], [])

    def test_unavailable_fallback_or_missing_fallback_key_fails_closed_without_retries(self):
        self.script("openai", (503, {}))
        self.script("anthropic", (503, {}))
        with self.assertRaises(agent.AgentError):
            self.run_agent()
        self.assertEqual((len(self.sent("openai")), len(self.sent("anthropic"))), (1, 1))
        del os.environ["ANTHROPIC_API_KEY"]
        self.script("openai", (429, {}))
        with self.assertRaises(agent.AgentError):
            self.run_agent()

    def test_fallback_resets_conversation_with_completed_actions_and_preserves_filesystem(self):
        self.script("openai", oa(WRITE_ALPHA), (503, {}))
        self.script("anthropic", an(PACKAGE_ALPHA), an(finish()))

        result = self.run_agent()

        self.assertEqual(result["recipes"][0]["decision"], "accept")
        self.assertEqual((self.repo / "build/recipes/alpha/alpha.SlackBuild").read_text(), NEW_ALPHA)
        first, second = self.sent("anthropic")
        messages = first["body"]["messages"]
        self.assertEqual([m["role"] for m in messages], ["user"])  # fresh conversation, no provider-specific state
        prompt = messages[0]["content"]
        for text in ("alpha", "beta", "build guide text", "review feedback text", "write_recipe", "build/recipes/alpha/alpha.SlackBuild"):
            self.assertIn(text, prompt)
        self.assertNotIn("function_call", json.dumps(first["body"]))
        self.assertEqual(len(second["body"]["messages"]), 3)

    def test_fallback_finish_still_restores_candidates_the_primary_edited_before_failing(self):
        self.script("openai", oa(WRITE_ALPHA), oa(("write_recipe", {"path": "build/recipes/alpha/new.patch", "content": "x", "executable": False})), (500, {}))
        self.script("anthropic", an(finish("reject", "reject")))
        self.run_agent()
        # rejected candidates are restored by finish, even those edited before the provider switch
        self.assertEqual(tree(self.repo / "build"), tree_original(self))


def tree_original(case):
    fresh = case.root / "pristine"
    make_repo(fresh)
    return tree(fresh / "build")


class LimitTests(ProviderCase):
    def test_turn_limit_fails_closed_without_fallback(self):
        self.patch("MAX_TURNS", 3)
        self.script("openai", *[oa(("list_files", {"path": "build"}))] * 10)
        with self.assertRaises(agent.AgentError):
            self.run_agent()
        self.assertEqual(len(self.sent("openai")), 3)
        self.assertEqual(self.sent("anthropic"), [])

    def test_turn_budget_is_shared_with_the_fallback_provider(self):
        self.patch("MAX_TURNS", 2)
        self.script("openai", oa(("list_files", {"path": "build"})), (503, {}))
        self.script("anthropic", an(finish("reject", "reject")))
        with self.assertRaises(agent.AgentError):
            self.run_agent()
        self.assertEqual(self.sent("anthropic"), [])

    def test_every_tool_call_counts_even_inside_one_response(self):
        self.patch("MAX_CALLS", 2)
        self.script("openai", oa(*[("list_files", {"path": "build"})] * 3))
        with self.assertRaises(agent.AgentError):
            self.run_agent()
        self.assertEqual(self.sent("anthropic"), [])

    def test_total_tool_output_is_bounded(self):
        self.patch("MAX_TOTAL_RESULT", 50)
        self.script("openai", oa(("read_file", {"path": "build/README.md"})), oa(("read_file", {"path": "build/README.md"})))
        with self.assertRaises(agent.AgentError):
            self.run_agent()

    def test_finishing_stops_the_session_and_later_tools_are_rejected(self):
        session = self.session()
        session.call(*finish("reject", "reject"))
        self.assertTrue(session.done)
        for name, args in (("list_files", {"path": "build"}), ("remove_recipe", {"path": "build/recipes/alpha/slack-desc"}),
                           ("write_recipe", {"path": "build/recipes/alpha/x", "content": "x", "executable": False})):
            with self.assertRaises(agent.AgentError):
                session.call(name, args)
        self.assertTrue((self.repo / "build/recipes/alpha/slack-desc").exists())


class FinishTests(Base):
    def test_incomplete_or_malformed_decisions_are_tool_errors_and_do_not_finish(self):
        session = self.session()
        good = finish("reject", "reject")[1]
        bad = [
            {**good, "recipes": good["recipes"][:1]},
            {**good, "recipes": good["recipes"] + [good["recipes"][0]]},
            {**good, "recipes": good["recipes"][:1] + [{"name": "nope", "decision": "reject", "reason": "x"}]},
            {**good, "recipes": [dict(good["recipes"][0], decision="maybe"), good["recipes"][1]]},
            {**good, "recipes": [dict(good["recipes"][0], reason="  "), good["recipes"][1]]},
            {**good, "recipes": [dict(good["recipes"][0], extra=1), good["recipes"][1]]},
            {**good, "summary": ""},
            {"recipes": good["recipes"]},
        ]
        for args in bad:
            with self.subTest(args=args):
                with self.assertRaises(agent.ToolError):
                    session.call("finish", args)
                self.assertFalse(session.done)
        session.call("finish", good)
        self.assertTrue(session.done)

    def test_accepting_requires_the_candidates_upstream_version(self):
        session = self.session()
        with self.assertRaises(agent.ToolError):
            session.call(*finish("accept", "reject"))
        session.call(*WRITE_ALPHA)
        session.call(*PACKAGE_ALPHA)
        session.call("remove_recipe", {"path": "build/recipes/alpha/alpha.SlackBuild"})
        with self.assertRaises(agent.ToolError):  # lock still points at a recipe that no longer exists
            session.call(*finish("accept", "reject"))
        session.call(*WRITE_ALPHA)
        session.call(*finish("accept", "reject"))

    def test_accepted_dependencies_must_exist_and_runtime_dependencies_must_be_published(self):
        session = self.session()
        session.call(*WRITE_ALPHA)
        session.call("write_package", {"name": "alpha", "package": alpha_package(dependencies=["missing"])})
        with self.assertRaises(agent.ToolError):
            session.call(*finish("accept", "reject"))
        session.call("write_recipe", {"path": "build/recipes/libnew/libnew.SlackBuild", "content": slackbuild("libnew"), "executable": True})
        flags = newdep_package(catalog=False)
        del flags["internal"]
        session.call("write_package", {"name": "libnew", "package": flags, "lock": "slackbuilds"})
        session.call("write_package", {"name": "alpha", "package": alpha_package(dependencies=["libnew"])})
        with self.assertRaises(agent.ToolError):  # build-only package as a runtime dependency
            session.call(*finish("accept", "reject"))
        session.call("write_package", {"name": "alpha", "package": alpha_package(build_dependencies=["libnew"])})
        session.call(*finish("accept", "reject"))

    def test_rejected_candidates_are_restored_byte_for_byte(self):
        original = tree(self.repo / "build")
        session = self.session()
        session.call(*WRITE_ALPHA)
        session.call(*PACKAGE_ALPHA)
        session.call("write_recipe", {"path": "build/recipes/alpha/patches/extra.patch", "content": "x", "executable": False})
        session.call("remove_recipe", {"path": "build/recipes/alpha/slack-desc"})
        session.call("write_recipe", {"path": "build/recipes/beta/beta.SlackBuild", "content": "changed\n", "executable": False})
        session.call("write_package", {"name": "beta", "package": {**{k: v for k, v in BETA.items() if k != "recipe_sha256"}, "version": "1.5"}})
        self.assertNotEqual(tree(self.repo / "build"), original)

        session.call(*finish("reject", "reject"))

        self.assertEqual(tree(self.repo / "build"), original)

    def test_unused_new_packages_are_discarded_and_referenced_ones_kept(self):
        original = tree(self.repo / "build")
        for referenced in (False, True):
            with self.subTest(referenced=referenced):
                session = self.session()
                session.call("write_recipe", {"path": "build/recipes/libnew/libnew.SlackBuild", "content": slackbuild("libnew"), "executable": True})
                session.call("write_package", {"name": "libnew", "package": newdep_package(), "lock": "slackbuilds"})
                if referenced:
                    session.call(*WRITE_ALPHA)
                    session.call("write_package", {"name": "alpha", "package": alpha_package(dependencies=["libnew"])})
                session.call(*finish("accept" if referenced else "reject", "reject"))
                self.assertEqual((self.repo / "build/recipes/libnew").exists(), referenced)
                self.assertEqual(any(item["name"] == "libnew" for item in self.lock("slackbuilds")), referenced)
                if not referenced:
                    self.assertEqual(tree(self.repo / "build"), original)


class PackageToolTests(Base):
    def assertLocksUnchanged(self, call, *args):
        before = (self.read("build/locks/slackware.json"), self.read("build/locks/slackbuilds.json"))
        with self.assertRaises(agent.ToolError):
            call(*args)
        self.assertEqual((self.read("build/locks/slackware.json"), self.read("build/locks/slackbuilds.json")), before)

    def test_read_package_returns_the_individual_entry_and_lock_path(self):
        session = self.session()
        result = session.call("read_package", {"name": "beta"})
        self.assertEqual(result["lock"], "build/locks/slackbuilds.json")
        self.assertEqual(result["entry"]["name"], "beta")
        self.assertNotIn("inputs", result["entry"])
        self.assertEqual(result["inputs"], BETA["inputs"])
        self.assertEqual(result["input_count"], 1)
        self.assertIsNone(result["next_input_offset"])
        for name in ("nope", "tc", "../x"):
            with self.assertRaises(agent.ToolError):
                session.call("read_package", {"name": name})

    def test_existing_acceptance_metadata_is_immutable(self):
        session = self.session()
        session.call(*WRITE_ALPHA)
        for label, changes in {
            "recipe path": {"recipe": "build/recipes/alpha/other.SlackBuild"},
            "catalog": {"catalog": False},
            "internal": {"internal": True},
            "provided_by_unraid": {"provided_by_unraid": True},
            "other name": {"name": "beta"},
            "unknown field": {"privileged": True},
        }.items():
            with self.subTest(label):
                self.assertLocksUnchanged(session.call, "write_package", {"name": "alpha", "package": alpha_package(**changes)})
        beta = {k: v for k, v in BETA.items() if k not in ("recipe_sha256", "internal")}
        self.assertLocksUnchanged(session.call, "write_package", {"name": "beta", "package": {**beta, "version": "1.5"}})  # dropping internal

    def test_existing_source_cannot_become_upstream_binary_or_unpinned(self):
        session = self.session()
        session.call(*WRITE_ALPHA)
        bad_inputs = [
            [{"url": "http://example.org/a.tgz", "file": "a.tgz", "sha256": sha("a")}],
            [{"url": "https://u:p@example.org/a.tgz", "file": "a.tgz", "sha256": sha("a")}],
            [{"url": "https://example.org/a.tgz", "file": "a.tgz", "sha256": "ABC"}],
            [{"url": "https://example.org/a.tgz", "file": "../a.tgz", "sha256": sha("a")}],
            [{"url": "https://example.org/a.tgz", "file": "slack-desc", "sha256": sha("a")}],  # would overwrite a checked sidecar
            [{"url": "https://example.org/a.tgz", "file": "a.tgz", "sha256": sha("a")}] * 2,
            [],
        ]
        for inputs in bad_inputs:
            with self.subTest(inputs=inputs):
                self.assertLocksUnchanged(session.call, "write_package", {"name": "alpha", "package": alpha_package(inputs=inputs)})
        for provenance in ({"kind": "upstream-binary", "url": "https://example.org/x", "revision": "r"},
                           {"kind": "source", "url": "http://example.org/x", "revision": "r"},
                           {"kind": "source", "url": "https://example.org/x"}):
            with self.subTest(provenance=provenance):
                self.assertLocksUnchanged(session.call, "write_package", {"name": "alpha", "package": alpha_package(provenance=provenance)})

    def test_candidate_version_must_equal_upstream_and_non_candidates_are_not_writable(self):
        session = self.session()
        session.call(*WRITE_ALPHA)
        self.assertLocksUnchanged(session.call, "write_package", {"name": "alpha", "package": alpha_package(version="9.9")})
        gamma = {k: v for k, v in GAMMA.items() if k != "recipe_sha256"}
        self.assertLocksUnchanged(session.call, "write_package", {"name": "gamma", "package": gamma})
        self.assertLocksUnchanged(session.call, "write_recipe", {"path": "build/recipes/gamma/gamma.SlackBuild", "content": "x", "executable": True})

    def test_smoke_commands_are_preserved_and_may_be_appended(self):
        session = self.session()
        session.call(*WRITE_ALPHA)
        extra = ["/opt/nerdtools/bin/alpha", "--help"]
        for smoke in (ALPHA["smoke"][:1], ALPHA["smoke"][::-1], [], [["alpha"]] + ALPHA["smoke"][1:], "x", [[]], [[1]], ALPHA["smoke"] + [["a\0b"]]):
            with self.subTest(smoke=smoke):
                self.assertLocksUnchanged(session.call, "write_package", {"name": "alpha", "package": alpha_package(smoke=smoke)})
        session.call("write_package", {"name": "alpha", "package": alpha_package(smoke=ALPHA["smoke"] + [extra])})
        self.assertEqual(self.lock()[0]["smoke"], ALPHA["smoke"] + [extra])

    def test_recipe_edits_keep_the_lock_hash_current(self):
        session = self.session()
        written = session.call(*WRITE_ALPHA)
        self.assertEqual(written["sha256"], sha(NEW_ALPHA))  # returned so the model can pin it
        self.assertEqual(self.lock()[0]["recipe_sha256"], sha(NEW_ALPHA))
        self.assertEqual(self.lock()[0]["version"], "1.0")
        session.call("write_recipe", {"path": "build/recipes/alpha/alpha.SlackBuild", "content": "#!/bin/sh\nexit 0\n", "executable": True})
        self.assertEqual(self.lock()[0]["recipe_sha256"], sha("#!/bin/sh\nexit 0\n"))

    def test_new_dependency_package_rules(self):
        session = self.session()
        recipe = {"path": "build/recipes/libnew/libnew.SlackBuild", "content": slackbuild("libnew"), "executable": True}
        self.assertLocksUnchanged(session.call, "write_package", {"name": "libnew", "package": newdep_package(), "lock": "slackbuilds"})  # no recipe yet
        session.call("write_recipe", recipe)
        bad = {
            "no lock": ({"name": "libnew", "package": newdep_package()}),
            "toolchain lock": ({"name": "libnew", "package": newdep_package(), "lock": "toolchain"}),
            "no policy flag": ({"name": "libnew", "package": {k: v for k, v in newdep_package().items() if k != "internal"}, "lock": "slackbuilds"}),
            "catalog true": ({"name": "libnew", "package": newdep_package(catalog=True), "lock": "slackbuilds"}),
            "both flags": ({"name": "libnew", "package": newdep_package(catalog=False), "lock": "slackbuilds"}),
            "unraid provided": ({"name": "libnew", "package": newdep_package(provided_by_unraid=True), "lock": "slackbuilds"}),
            "binary": ({"name": "libnew", "package": newdep_package(provenance={"kind": "upstream-binary", "url": "https://e.org/x", "revision": "r"}), "lock": "slackbuilds"}),
            "recipe path": ({"name": "libnew", "package": newdep_package(recipe="build/recipes/libnew/other.SlackBuild"), "lock": "slackbuilds"}),
            "unsafe name": ({"name": "../libnew", "package": newdep_package(name="../libnew"), "lock": "slackbuilds"}),
        }
        for label, args in bad.items():
            with self.subTest(label):
                self.assertLocksUnchanged(session.call, "write_package", args)
        result = session.call("write_package", {"name": "libnew", "package": newdep_package(), "lock": "slackbuilds"})
        self.assertEqual(result["lock"], "build/locks/slackbuilds.json")
        added = self.lock("slackbuilds")[-1]
        self.assertEqual((added["name"], added["recipe_sha256"], added["internal"]), ("libnew", sha(slackbuild("libnew")), True))
        build_only = newdep_package(name="libbuild", recipe="build/recipes/libbuild/libbuild.SlackBuild", catalog=False)
        del build_only["internal"]
        session.call("write_recipe", {"path": "build/recipes/libbuild/libbuild.SlackBuild", "content": slackbuild("libbuild"), "executable": True})
        session.call("write_package", {"name": "libbuild", "package": build_only, "lock": "slackware"})
        self.assertEqual(self.lock()[-1]["catalog"], False)

    def test_new_packages_cannot_reuse_existing_lock_or_toolchain_names(self):
        session = self.session()
        for name in ("tc", "gamma"):
            with self.assertRaises(agent.ToolError):
                session.call("write_recipe", {"path": f"build/recipes/{name}/{name}.SlackBuild", "content": "x", "executable": True})
        self.assertEqual((self.repo / "build/recipes/gamma/gamma.SlackBuild").read_text(), slackbuild("gamma"))
        self.assertFalse((self.repo / "build/recipes/tc").exists())

    def test_dependencies_used_only_by_the_batch_are_repairable_but_shared_ones_are_not(self):
        original = tree(self.repo / "build")
        delta = {"path": "build/recipes/delta/delta.SlackBuild", "content": "changed\n", "executable": True}
        session = self.session()
        session.call("write_recipe", delta)
        with self.assertRaises(agent.ToolError):  # epsilon is also a dependency of the non-candidate gamma
            session.call("write_recipe", {**delta, "path": "build/recipes/epsilon/epsilon.SlackBuild"})
        plain = {k: v for k, v in DELTA.items() if k != "recipe_sha256"}
        session.call("write_package", {"name": "delta", "package": {**plain, "inputs": [{"url": "https://example.org/d.tgz", "file": "d.tgz", "sha256": sha("d")}]}})
        self.assertLocksUnchanged(session.call, "write_package", {"name": "delta", "package": {**plain, "version": "9.9"}})  # no unreviewed upstream bumps
        session.call(*finish("reject", "reject"))  # nothing accepted needs delta: back to the original
        self.assertEqual(tree(self.repo / "build"), original)

        session = self.session()
        session.call("write_recipe", delta)
        session.call(*WRITE_ALPHA)
        session.call(*PACKAGE_ALPHA)
        session.call(*finish("accept", "reject"))
        self.assertEqual((self.repo / "build/recipes/delta/delta.SlackBuild").read_text(), "changed\n")


class ConfinementTests(Base):
    def test_reads_cover_public_build_docs_and_page_by_offset(self):
        session = self.session()
        self.assertEqual(session.call("read_file", {"path": "docs/notes.md"})["text"], "public notes\n")
        self.assertEqual(session.call("read_file", {"path": "README.md"})["text"], "readme\n")
        self.patch("MAX_CHUNK", 5)
        first = session.call("read_file", {"path": "build/README.md"})
        self.assertEqual((first["text"], first["next_offset"]), ("Build", 5))
        second = session.call("read_file", {"path": "build/README.md", "offset": 5})
        self.assertEqual(second["text"], " guid")
        last = session.call("read_file", {"path": "build/README.md", "offset": 35})
        self.assertIsNone(last["next_offset"])

    def test_secret_non_public_and_escaping_paths_are_denied(self):
        session = self.session()
        os.symlink("../../outside.txt", self.repo / "build/leak")
        os.symlink("../.env", self.repo / "build/envlink")
        os.symlink("../.git/config", self.repo / "build/gitlink")
        os.symlink("/proc/self", self.repo / "build/procself")
        os.symlink("../scripts", self.repo / "build/scriptlink")
        paths = [".env", ".git/config", ".git", "build/../.env", "/etc/passwd", "/proc/self/environ", "scripts/upstream-updates.py",
                 "build/recipes/alpha/server.key", "build/recipes/alpha/api-token.txt", "build/recipes/alpha/..", "build/recipes/alpha/../../../.env",
                 "build/leak", "build/envlink", "build/gitlink", "build/procself/environ", "build/scriptlink/upstream-updates.py",
                 "build\\..\\.env", "build/\0", "", "build/missing", "build/recipes/alpha/patches", "../outside.txt"]
        for path in paths:
            for tool in ("read_file", "list_files"):
                with self.subTest(tool=tool, path=path):
                    try:
                        result = session.call(tool, {"path": path})
                    except agent.ToolError as error:
                        self.assertNotIn(SECRET, str(error))
                    else:  # only directories may list, only files may read
                        self.assertEqual((tool, path), ("list_files", "build/recipes/alpha/patches"))
                        self.assertNotIn(SECRET, json.dumps(result))

    def test_listings_hide_denied_entries_and_the_root_shows_only_public_roots(self):
        session = self.session()
        os.symlink("../../../.env", self.repo / "build/recipes/alpha/envlink")
        root = {entry["name"] for entry in session.call("list_files", {"path": "."})["entries"]}
        self.assertEqual(root, {"README.md", "build", "docs"})
        listing = {entry["name"] for entry in session.call("list_files", {"path": "build/recipes/alpha"})["entries"]}
        self.assertEqual(listing, {"alpha.SlackBuild", "slack-desc", "patches", "envlink"})
        self.assertNotIn(SECRET, json.dumps(session.call("list_files", {"path": "build/recipes/alpha"})))
        with self.assertRaises(agent.ToolError):
            session.call("read_file", {"path": "build/recipes/alpha/envlink"})

    def test_binary_files_are_not_returned(self):
        (self.repo / "build/blob.bin").write_bytes(b"\x00\x01\x02")
        with self.assertRaises(agent.ToolError):
            self.session().call("read_file", {"path": "build/blob.bin"})

    def test_tool_arguments_are_validated(self):
        session = self.session()
        for name, args in (("read_file", {}), ("read_file", {"path": 1}), ("read_file", {"path": "README.md", "offset": -1}),
                           ("read_file", {"path": "README.md", "offset": True}), ("read_file", {"path": "README.md", "extra": 1}),
                           ("write_recipe", {"path": "build/recipes/alpha/x", "content": "x"}),
                           ("write_recipe", {"path": "build/recipes/alpha/x", "content": "x", "executable": "yes"}),
                           ("fetch_url", {"url": "https://example.org/", "mode": "binary"})):
            with self.subTest(name=name, args=args):
                with self.assertRaises(agent.ToolError):
                    session.call(name, args)

    def test_recipe_writes_are_limited_to_batch_package_directories(self):
        session = self.session()
        outside_dir = self.root / "outside_dir"
        outside_dir.mkdir()
        os.symlink(outside_dir, self.repo / "build/recipes/alpha/linkdir")
        os.symlink("../../../../outside.txt", self.repo / "build/recipes/alpha/linkfile")
        paths = ["build/recipes/alpha/../beta/x", "build/recipes/gamma/x", "build/locks/slackware.json", "build/native.py", ".github/workflows/x.yml",
                 "/tmp/x", "build/recipes/alpha", "build/recipes", "build/recipes/alpha/.hidden", "build/recipes/alpha/server.key",
                 "build/recipes/alpha/has space", "build/recipes/alpha/linkdir/x", "build/recipes/alpha/linkfile", "build/recipes/alpha/slack-desc/x",
                 "build/recipes/../locks/x", "../outside.txt", "build/recipes/alpha/\0", "build\\recipes\\alpha\\x", "build/recipes/.git/x"]
        for path in paths:
            with self.subTest(path=path):
                with self.assertRaises(agent.ToolError):
                    session.call("write_recipe", {"path": path, "content": "pwned", "executable": True})
        self.assertEqual((self.root / "outside.txt").read_text(), SECRET)
        self.assertEqual(list(outside_dir.iterdir()), [])
        self.assertFalse((self.repo / "build/recipes/alpha/x").exists())

    def test_removal_is_confined_and_never_follows_symlinks(self):
        session = self.session()
        outside_dir = self.root / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "keep").write_text("keep")
        os.symlink(outside_dir, self.repo / "build/recipes/alpha/linkdir")
        os.symlink("../../../../outside.txt", self.repo / "build/recipes/alpha/linkfile")
        for path in ("build/recipes/alpha/linkdir/keep", "build/recipes/gamma/slack-desc", "build/locks/slackware.json", "build/README.md",
                     "build/recipes/alpha/patches", "build/recipes/alpha/../beta/slack-desc", "/etc/passwd", "build/recipes/alpha/server.key"):
            with self.subTest(path=path):
                with self.assertRaises(agent.ToolError):
                    session.call("remove_recipe", {"path": path})
        self.assertEqual((outside_dir / "keep").read_text(), "keep")
        session.call("remove_recipe", {"path": "build/recipes/alpha/linkfile"})  # the link itself, not its target
        self.assertFalse((self.repo / "build/recipes/alpha/linkfile").is_symlink())
        self.assertEqual((self.root / "outside.txt").read_text(), SECRET)

    def test_writes_create_nested_files_with_requested_mode_and_removal_prunes_empty_directories(self):
        session = self.session()
        path = "build/recipes/alpha/patches/deeper/new.patch"
        session.call("write_recipe", {"path": path, "content": "patch\n", "executable": False})
        written = self.repo / path
        self.assertEqual((written.read_text(), written.stat().st_mode & 0o777), ("patch\n", 0o644))
        session.call("write_recipe", {"path": path, "content": "patch2\n", "executable": True})
        self.assertEqual((written.read_text(), written.stat().st_mode & 0o777), ("patch2\n", 0o755))
        session.call("remove_recipe", {"path": path})
        self.assertFalse((self.repo / "build/recipes/alpha/patches/deeper").exists())
        self.assertTrue((self.repo / "build/recipes/alpha/patches/fix.patch").exists())
        self.assertEqual(sorted(p.name for p in (self.repo / "build/recipes/alpha").iterdir() if p.name.endswith(".tmp~")), [])

    def test_oversized_recipe_content_is_rejected(self):
        with self.assertRaises(agent.ToolError):
            self.session().call("write_recipe", {"path": "build/recipes/alpha/big", "content": "x" * (agent.MAX_WRITE + 1), "executable": False})


class FetchTests(Base):
    DNS = {"good.example": ["93.184.216.34"], "other.example": ["93.184.216.35"], "evil.example": ["10.0.0.5"],
           "mixed.example": ["93.184.216.34", "10.0.0.5"], "mapped.example": ["::ffff:10.0.0.5"],
           "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"]}

    def setUp(self):
        super().setUp()
        test = self
        test.DNS = dict(FetchTests.DNS)
        test.network = {}
        test.connections = []
        test.requests = []
        test.lookups = []

        class Response:
            def __init__(self, status, headers, body):
                self.status, self.headers, self.body, self.position = status, {k.lower(): v for k, v in headers.items()}, body, 0

            def getheader(self, name, default=None):
                return self.headers.get(name.lower(), default)

            def read(self, size=-1):
                chunk = self.body[self.position:] if size < 0 else self.body[self.position:self.position + size]
                self.position += len(chunk)
                return chunk

        class Connection:
            def __init__(self, host, address, timeout):
                test.connections.append((host, address))
                self.host = host

            def request(self, method, target, headers=None):
                test.requests.append((self.host, method, target, dict(headers or {})))

            def getresponse(self):
                return Response(*test.network.get((self.host, test.requests[-1][2]), (404, {}, b"")))

            def close(self):
                pass

        def resolver(host, port, *args, **kwargs):
            test.lookups.append(host)
            answer = test.DNS.get(host)
            if answer is None:
                raise socket.gaierror("unknown host")
            answer = answer() if callable(answer) else answer
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answer]

        self.patch("PinnedHTTPSConnection", Connection)
        patcher = mock.patch.object(agent.socket, "getaddrinfo", resolver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, url, mode="text"):
        return self.session().call("fetch_url", {"url": url, "mode": mode})

    def test_text_fetch_pins_the_validated_address_and_sends_no_credentials(self):
        self.network[("good.example", "/dir/file.txt?x=1")] = (200, {}, "recipe é".encode())
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret", "GITHUB_TOKEN": "ghs-secret"}):
            result = self.fetch("https://good.example/dir/file.txt?x=1#frag")
        self.assertEqual((result["text"], result["truncated"]), ("recipe é", False))
        self.assertEqual(self.connections, [("good.example", "93.184.216.34")])
        (host, method, target, headers), = self.requests
        self.assertEqual((method, target), ("GET", "/dir/file.txt?x=1"))
        self.assertTrue({key.lower() for key in headers} <= {"user-agent", "accept-encoding", "connection"})
        self.assertNotIn("secret", json.dumps(headers))
        self.network[("v6.example", "/")] = (200, {}, b"ok")
        self.assertEqual(self.fetch("https://v6.example/")["text"], "ok")

    def test_sha256_mode_hashes_the_archive_and_bounds_size(self):
        self.network[("good.example", "/a.tgz")] = (200, {}, b"archive-bytes")
        result = self.fetch("https://good.example/a.tgz", "sha256")
        self.assertEqual((result["sha256"], result["bytes"]), (sha(b"archive-bytes"), 13))
        self.patch("FETCH_HASH", 10)
        with self.assertRaises(agent.ToolError):
            self.fetch("https://good.example/a.tgz", "sha256")
        self.network[("good.example", "/big")] = (200, {"Content-Length": "999999"}, b"x")
        with self.assertRaises(agent.ToolError):
            self.fetch("https://good.example/big", "sha256")

    def test_text_mode_truncates_and_says_so(self):
        self.network[("good.example", "/long")] = (200, {}, b"0123456789")
        self.patch("FETCH_TEXT", 4)
        result = self.fetch("https://good.example/long")
        self.assertEqual((result["text"], result["truncated"]), ("0123", True))

    def test_failed_and_compressed_responses_are_errors(self):
        self.network[("good.example", "/missing")] = (404, {}, b"")
        self.network[("good.example", "/gz")] = (200, {"Content-Encoding": "gzip"}, b"x")
        for path in ("/missing", "/gz"):
            with self.assertRaises(agent.ToolError):
                self.fetch("https://good.example" + path, "sha256")

    def test_every_redirect_hop_is_revalidated(self):
        self.network[("good.example", "/start")] = (302, {"Location": "/final"}, b"")
        self.network[("good.example", "/final")] = (200, {}, b"landed")
        self.assertEqual(self.fetch("https://good.example/start")["text"], "landed")
        for location in ("https://evil.example/x", "http://good.example/x", "https://user:pw@good.example/x", "https://good.example:8443/x",
                         "https://169.254.169.254/latest/meta-data", "https://[::1]/", "file:///etc/passwd", "https://mixed.example/x"):
            with self.subTest(location=location):
                self.connections.clear()
                self.network[("good.example", "/start")] = (302, {"Location": location}, b"")
                with self.assertRaises(agent.ToolError):
                    self.fetch("https://good.example/start")
                self.assertEqual(self.connections, [("good.example", "93.184.216.34")])

    def test_redirect_loops_are_bounded(self):
        self.network[("good.example", "/loop")] = (301, {"Location": "https://good.example/loop"}, b"")
        with self.assertRaises(agent.ToolError):
            self.fetch("https://good.example/loop")
        self.assertEqual(len(self.connections), agent.FETCH_REDIRECTS + 1)

    def test_every_dns_answer_must_be_public_and_each_hop_resolves_once_and_pins(self):
        for host in ("evil.example", "mixed.example", "mapped.example", "unknown.example"):
            with self.subTest(host=host):
                with self.assertRaises(agent.ToolError):
                    self.fetch(f"https://{host}/x")
        self.assertEqual(self.connections, [])
        answers = iter([["93.184.216.34"], ["10.0.0.5"]])  # rebinding: second lookup of the same name is private
        self.DNS["rebind.example"] = lambda: next(answers)
        self.network[("rebind.example", "/a")] = (302, {"Location": "/b"}, b"")
        with self.assertRaises(agent.ToolError):
            self.fetch("https://rebind.example/a")
        self.assertEqual(self.connections, [("rebind.example", "93.184.216.34")])
        self.assertEqual(self.lookups.count("rebind.example"), 2)

    def test_non_https_credentials_ports_and_local_targets_are_rejected_without_connecting(self):
        for url in ("http://good.example/", "file:///etc/passwd", "ftp://good.example/", "https://user:pw@good.example/", "https://good.example:8443/",
                    "https://127.0.0.1/", "https://[::1]/", "https://10.1.2.3/", "https://169.254.169.254/", "https://0.0.0.0/", "https://[::ffff:127.0.0.1]/",
                    "https:///nohost", "//good.example/", "good.example/x", "https://good.example/ x", "https://good.example/é", "", "https://good.example/" + "a" * 3000):
            with self.subTest(url=url):
                with self.assertRaises(agent.ToolError):
                    self.fetch(url)
        self.assertEqual(self.connections, [])

    def test_real_resolver_rejects_localhost_and_numeric_loopback_forms(self):
        patcher = mock.patch.object(agent.socket, "getaddrinfo", REAL_GETADDRINFO)  # no network needed for these names
        patcher.start()
        self.addCleanup(patcher.stop)
        self.patch("PinnedHTTPSConnection", mock.Mock(side_effect=AssertionError("must not connect")))
        for url in ("https://localhost/", "https://2130706433/", "https://0x7f.1/", "https://127.1/"):
            with self.subTest(url=url):
                with self.assertRaises(agent.ToolError):
                    self.fetch(url)

    def test_public_address_classification(self):
        for address in ("93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"):
            self.assertTrue(agent.public_ip(address), address)
        for address in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255",
                        "192.0.2.1", "::1", "::", "fe80::1", "fc00::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::7f00:1",
                        "2002:7f00:1::", "2001:db8::1", "::7f00:1", "not-an-ip"):
            self.assertFalse(agent.public_ip(address), address)


if __name__ == "__main__":
    unittest.main()
